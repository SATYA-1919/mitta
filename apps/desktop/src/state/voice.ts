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

/** Silence after the wake word before the request is treated as complete.
 *
 *  Apple's recogniser does not tell us when someone has stopped talking in a
 *  continuous session — it keeps the same task open — so the end of an
 *  utterance is inferred from the transcript going quiet. */
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

  apply: (update: VoiceUpdate) => void;
  setSpeakReplies: (on: boolean) => void;
  requestPermission: () => Promise<void>;
  pushToTalk: (down: boolean) => Promise<void>;
  toggleContinuous: () => Promise<void>;
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
    get().onUtterance?.(trimmed);
    set({ transcript: '' });
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
    onUtterance: null,

    bind: (onUtterance) => set({ onUtterance }),

    apply: (update) => {
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
      if (update.triggered && update.transcript.trim().length > 0) {
        clearSettle();
        settleTimer = setTimeout(() => deliver(update.transcript), UTTERANCE_SETTLE_MS);
      }
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
      try {
        if (down) {
          set({ error: null, transcript: '' });
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
        } else {
          set({ error: null });
          await startListening(true);
          set({ continuous: true });
        }
      } catch (error) {
        set({ status: 'failed', error: describe(error), continuous: false, level: 0 });
      }
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
