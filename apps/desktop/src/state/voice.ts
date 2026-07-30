/**
 * Voice state (R7, DEC-105).
 *
 * Separate from the main store for the same reason memory is: this one is fed
 * by a Rust event at up to 20 Hz, and a level meter ticking ten times a second
 * must not re-render the chat transcript.
 *
 * Two activation paths, both landing in the same place:
 *
 * - **Push-to-talk.** Hold the button or ⌘⇧V. The transcript fills the composer
 *   as you speak and sends when you let go.
 * - **Continuous.** Opt-in. The wake word is matched in Rust; the webview acts
 *   on the request that followed it.
 *
 * Neither path invents anything. `level` comes from the audio tap and goes to
 * zero the moment the microphone closes, because the waveform is the only
 * honest signal a user has that they are being recorded.
 */

import { create } from 'zustand';

import {
  calibrateVoice,
  getVoiceInfo,
  isVoiceAvailable,
  openVoiceSettings,
  requestVoicePermission,
  setVoice as setVoiceNative,
  speak as speakNative,
  startListening,
  stopListening,
  stopSpeaking as stopSpeakingNative,
  type VoiceInfo,
  type VoiceUpdate,
} from '@/lib/ipc/tauri';

export type VoiceStatus = 'unavailable' | 'idle' | 'listening' | 'failed';

/** Where the wake-mode preference lives across launches.
 *
 *  In the webview rather than the sidecar's `preferences` table, because the
 *  microphone belongs to the shell: the sidecar cannot open it, and a setting
 *  stored where the component that acts on it cannot read it synchronously at
 *  startup is a setting that gets applied a beat late every launch. */
const WAKE_PREFERENCE_KEY = 'mitta.voice.wake';

function storedWakePreference(): boolean {
  try {
    return globalThis.localStorage?.getItem(WAKE_PREFERENCE_KEY) === 'on';
  } catch {
    // Storage can throw outright in a restricted webview. A preference that
    // cannot be read is not a reason to fail to start.
    return false;
  }
}

function rememberWakePreference(on: boolean): void {
  try {
    globalThis.localStorage?.setItem(WAKE_PREFERENCE_KEY, on ? 'on' : 'off');
  } catch {
    // Not worth surfacing. The toggle still works for this session.
  }
}

/** Fallback silence window before a request is treated as complete.
 *
 *  Only reached when the recogniser never reports `isFinal`, which happens in a
 *  continuous session because it keeps one task open across utterances. When
 *  `isFinal` does arrive the request goes immediately and this timer is never
 *  used — see `apply`. */
const UTTERANCE_SETTLE_MS = 1_200;

export interface VoiceSlice {
  available: boolean;
  status: VoiceStatus;
  /** Live transcript, partial while speaking. */
  transcript: string;
  level: number;
  speaking: boolean;
  continuous: boolean;
  error: string | null;
  /** Read replies aloud. Off by default: an assistant that starts talking
   *  because you typed something is a surprise, not a feature. */
  speakReplies: boolean;
  /** Which system voice is being used, and whether a better one exists. */
  voice: VoiceInfo | null;
  /** True while the room is being measured. */
  calibrating: boolean;
  /** The speech gate, once calibrated. Null means the built-in default. */
  threshold: number | null;
  /** Milliseconds between the wake word matching and the request being sent.
   *
   *  This is the latency MITTA owns, as distinct from the model's. Null until a
   *  spoken request has completed. */
  lastWaitMs: number | null;

  apply: (update: VoiceUpdate) => void;
  setSpeakReplies: (on: boolean) => void;
  requestPermission: () => Promise<void>;
  pushToTalk: (down: boolean) => Promise<void>;
  toggleContinuous: () => Promise<void>;
  /** Measure the room and fit the speech gate to it. */
  calibrate: (seconds?: number) => Promise<void>;
  /** Re-enable wake mode if it was on last time. Called once, at connect. */
  restoreWakePreference: () => Promise<void>;
  speak: (text: string) => Promise<void>;
  stopSpeaking: () => Promise<void>;
  loadVoiceInfo: () => Promise<void>;
  chooseVoice: (identifier: string | null) => Promise<void>;
  openVoiceSettings: () => Promise<void>;
  reset: () => void;

  /** Set by the app: what to do with a completed utterance. Injected rather
   *  than imported so this store never reaches into the chat store, which
   *  would make the two impossible to test apart. */
  onUtterance: ((text: string) => void) | null;
  bind: (handler: ((text: string) => void) | null) => void;
}

export const useVoiceStore = create<VoiceSlice>((set, get) => {
  let settleTimer: ReturnType<typeof setTimeout> | null = null;

  /** Whether push-to-talk is currently held. See `pushToTalk`. */
  let pressed = false;

  /**
   * When the wake word was first matched for the utterance now in flight.
   *
   * The gap between this and delivery is the part of a spoken request's latency
   * that belongs to *us* rather than to the model — silence detection, the poll
   * interval, the settle fallback. Three separate fixes have gone into that gap
   * on the strength of reasoning about it, and two of them were wrong, so it is
   * now measured and shown rather than argued about.
   */
  let triggeredAt: number | null = null;

  /** Level observers, for calibration. Empty except while measuring. */
  const levelObservers = new Set<(level: number) => void>();

  function subscribeLevel(observer: (level: number) => void): () => void {
    levelObservers.add(observer);
    return () => void levelObservers.delete(observer);
  }

  function clearSettle() {
    if (settleTimer !== null) {
      clearTimeout(settleTimer);
      settleTimer = null;
    }
  }

  /** Hand a finished utterance to whoever is bound, once. */
  function deliver(text: string) {
    clearSettle();
    const trimmed = text.trim();
    if (trimmed.length === 0) return;
    const waited = triggeredAt === null ? null : Math.round(performance.now() - triggeredAt);
    triggeredAt = null;
    get().onUtterance?.(trimmed);
    set({ transcript: '', lastWaitMs: waited });
  }

  return {
    available: isVoiceAvailable(),
    status: isVoiceAvailable() ? 'idle' : 'unavailable',
    transcript: '',
    level: 0,
    speaking: false,
    continuous: false,
    error: null,
    speakReplies: false,
    voice: null,
    calibrating: false,
    threshold: null,
    lastWaitMs: null,
    onUtterance: null,

    bind: (onUtterance) => set({ onUtterance }),

    apply: (update) => {
      // Fed to calibration before anything else, and only while listening: a
      // level published after the microphone closed is a stale reading, and
      // measuring the room from it would set the gate from silence that is not
      // the room's silence.
      if (levelObservers.size > 0 && update.state === 'listening') {
        for (const observer of levelObservers) observer(update.level);
      }

      set({
        status: update.state,
        transcript: update.transcript,
        // Forced to zero when not listening. Rust already does this; the
        // webview repeats it because a stale level is the one value here that
        // would misrepresent whether the microphone is open.
        level: update.state === 'listening' ? update.level : 0,
        speaking: update.speaking,
        continuous: update.continuous,
        error: update.error,
      });

      if (!update.continuous) return;

      // Continuous mode only. Push-to-talk sends on release, which is an
      // explicit end-of-utterance and needs no guessing.
      if (!update.triggered || update.transcript.trim().length === 0) return;

      // First trigger of this utterance starts the clock. Reset in `deliver`,
      // so a long request is measured from when MITTA first heard its name
      // rather than from the last partial transcript.
      triggeredAt ??= performance.now();

      // When the recogniser has decided the utterance is over, act on it
      // immediately instead of waiting out the settle timer.
      //
      // This was the whole felt latency of a spoken request. The backend answers
      // a tool turn in under 900 ms, and every voice request was having a flat
      // 1.2 s of silence added in front of that — for a signal Apple had already
      // published and this store was ignoring. The timer stays as the fallback,
      // because a continuous session often never finalises: the recogniser keeps
      // one task open and `isFinal` may simply never arrive.
      clearSettle();
      if (update.isFinal) {
        deliver(update.transcript);
        return;
      }
      settleTimer = setTimeout(() => deliver(update.transcript), UTTERANCE_SETTLE_MS);
    },

    setSpeakReplies: (speakReplies) => {
      if (!speakReplies) void stopSpeakingNative().catch(() => {});
      set({ speakReplies });
    },

    requestPermission: async () => {
      if (!get().available) return;
      await requestVoicePermission();
    },

    pushToTalk: async (down) => {
      if (!get().available) return;
      // A repeat press is ignored; a release never is.
      //
      // Tracked on our own flag rather than on `status`, because `status` only
      // becomes `listening` once the Rust poll loop publishes an update — up to
      // 50 ms after the press. Gating the *release* on `status` would mean a
      // quick tap released before that first update never called
      // `stopListening`, and the microphone stayed open with no way to close it.
      if (down && pressed) return;
      if (!down && !pressed) return;
      pressed = down;
      try {
        if (down) {
          set({ error: null, transcript: '' });
          // Permission first, and awaited. On the very first press macOS has not
          // asked yet, and starting the engine before authorisation returns is
          // how the first hold silently records nothing.
          await requestVoicePermission();
          await startListening(false);
        } else {
          await stopListening();
          // The transcript that exists at release is the whole utterance.
          deliver(get().transcript);
          set({ status: 'idle', level: 0 });
        }
      } catch (error) {
        set({ status: 'failed', error: describe(error), level: 0 });
      }
    },

    toggleContinuous: async () => {
      if (!get().available) return;
      const on = get().continuous;
      try {
        if (on) {
          clearSettle();
          await stopListening();
          set({ continuous: false, status: 'idle', level: 0, transcript: '' });
          rememberWakePreference(false);
        } else {
          set({ error: null });
          await startListening(true);
          set({ continuous: true });
          rememberWakePreference(true);
        }
      } catch (error) {
        set({ status: 'failed', error: describe(error), continuous: false, level: 0 });
        // Not remembered on failure. Persisting an intent that did not take
        // would reopen the microphone-that-never-opened on every launch.
      }
    },

    calibrate: async (seconds = 3) => {
      // Measure the room, then set the gate above it.
      //
      // Sampling happens here rather than in Swift because the level already
      // arrives in `apply` twenty times a second — a second measurement path
      // would be a second definition of "how loud is it", and they would drift.
      //
      // The microphone has to be open to measure, so wake mode is started for
      // the duration if it was not already on, and put back afterwards.
      if (!get().available || get().calibrating) return;

      const wasListening = get().continuous;
      set({ calibrating: true, error: null });
      const samples: number[] = [];
      const unsubscribe = subscribeLevel((level) => samples.push(level));

      try {
        if (!wasListening) await startListening(true);
        await new Promise((resolve) => setTimeout(resolve, seconds * 1000));

        if (samples.length === 0) {
          set({ error: 'Heard nothing at all — is the microphone connected?' });
          return;
        }

        // A high percentile, not the maximum: one cough or door slam during the
        // quiet window would otherwise set a threshold so high the wake word
        // could never trip it again.
        const sorted = [...samples].sort((a, b) => a - b);
        const ambient = sorted[Math.floor(sorted.length * 0.9)] ?? 0;
        set({ threshold: await calibrateVoice(ambient) });
      } catch (error) {
        set({ error: describe(error) });
      } finally {
        unsubscribe();
        if (!wasListening) {
          await stopListening().catch(() => {});
          set({ continuous: false, status: 'idle', level: 0 });
        }
        set({ calibrating: false });
      }
    },

    restoreWakePreference: async () => {
      // Called once when the shell connects. Only ever turns wake mode *on*:
      // this restores a choice the user made, and a stored "off" is simply the
      // default, not an instruction to close a microphone nothing opened.
      if (!get().available || get().continuous) return;
      if (!storedWakePreference()) return;
      await get().toggleContinuous();
    },

    speak: async (text) => {
      if (!get().available || text.trim().length === 0) return;
      await speakNative(text).catch(() => {});
    },

    stopSpeaking: async () => {
      await stopSpeakingNative().catch(() => {});
    },

    loadVoiceInfo: async () => {
      if (!get().available) return;
      try {
        set({ voice: await getVoiceInfo() });
      } catch {
        // Not worth surfacing: the voice picker is a refinement, and the
        // synthesiser still works with whatever the system picked.
        set({ voice: null });
      }
    },

    chooseVoice: async (identifier) => {
      await setVoiceNative(identifier).catch(() => {});
      await get().loadVoiceInfo();
    },

    openVoiceSettings: async () => {
      await openVoiceSettings().catch(() => {});
    },

    reset: () => {
      clearSettle();
      set({ transcript: '', level: 0, error: null });
    },
  };
});

function describe(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === 'object' && error !== null) {
    const shaped = error as { message?: unknown };
    if (typeof shaped.message === 'string') return shaped.message;
  }
  return String(error);
}
