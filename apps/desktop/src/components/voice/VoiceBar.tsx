/**
 * The voice controls: waveform, push-to-talk, and the listening toggle.
 *
 * R2 requires a voice waveform, and the honest version of one is harder than
 * the decorative version: **every bar here is driven by the real microphone
 * level.** A waveform that animates on a timer looks better and tells the user
 * they are being recorded when they are not, which is the single worst thing a
 * microphone indicator can do.
 *
 * When the level is zero the bars sit flat. That is not a broken animation, it
 * is silence.
 */

import { useEffect, useRef } from 'react';

import { cx, StatusDot } from '@/components/ui/primitives';
import { useVoiceStore } from '@/state/voice';

/** Bars in the meter. Odd, so there is a centre to fall away from. */
const BARS = 21;

export function VoiceBar() {
  const available = useVoiceStore((s) => s.available);
  const status = useVoiceStore((s) => s.status);
  const continuous = useVoiceStore((s) => s.continuous);
  const calibrate = useVoiceStore((s) => s.calibrate);
  const calibrating = useVoiceStore((s) => s.calibrating);
  const threshold = useVoiceStore((s) => s.threshold);
  const lastWaitMs = useVoiceStore((s) => s.lastWaitMs);
  const transcript = useVoiceStore((s) => s.transcript);
  const error = useVoiceStore((s) => s.error);
  const speaking = useVoiceStore((s) => s.speaking);
  const speakReplies = useVoiceStore((s) => s.speakReplies);

  const pushToTalk = useVoiceStore((s) => s.pushToTalk);
  const toggleContinuous = useVoiceStore((s) => s.toggleContinuous);
  const setSpeakReplies = useVoiceStore((s) => s.setSpeakReplies);
  const stopSpeaking = useVoiceStore((s) => s.stopSpeaking);
  const voice = useVoiceStore((s) => s.voice);
  const loadVoiceInfo = useVoiceStore((s) => s.loadVoiceInfo);
  const openSettings = useVoiceStore((s) => s.openVoiceSettings);

  const listening = status === 'listening';

  useEffect(() => {
    void loadVoiceInfo();
  }, [loadVoiceInfo]);

  // ⌘⇧V is handled by the *global* shortcut in the Rust shell, bound once in
  // `connection.ts` — not by a `keydown` listener here.
  //
  // A webview listener only fires while MITTA has focus, and the moment you want
  // to talk to an assistant is the moment you are looking at something else. It
  // also unmounted with this component, so the key silently stopped working on
  // every surface except Chat.
  //
  // Both together would be worse than either: two paths calling `pushToTalk`
  // for one gesture restarts the recogniser mid-hold.

  if (!available) {
    // Said plainly rather than hidden. A missing button reads as a missing
    // feature; this reads as a feature that needs the desktop shell.
    return (
      <p className="px-4 pb-2 text-2xs text-fg-faint">
        Voice needs the desktop shell — run <span className="readout">make app</span>
      </p>
    );
  }

  return (
    <div className="flex items-center gap-3 px-4 pb-2">
      <button
        type="button"
        aria-label={listening ? 'Stop listening' : 'Hold to talk'}
        aria-pressed={listening}
        // Pointer events, not click: this is a hold control, and `onClick`
        // fires only after release, which would make the button do nothing
        // while it is held down.
        onPointerDown={() => void pushToTalk(true)}
        onPointerUp={() => void pushToTalk(false)}
        // Releasing outside the button must still stop the microphone.
        onPointerLeave={() => listening && !continuous && void pushToTalk(false)}
        className={cx(
          'flex size-8 shrink-0 items-center justify-center rounded-xs border transition-colors',
          listening
            ? 'border-accent bg-accent/15 text-accent'
            : 'border-border-subtle text-fg-muted hover:text-fg-secondary',
        )}
      >
        <MicIcon active={listening} />
      </button>

      <Waveform active={listening} />

      <div className="flex min-w-0 flex-1 items-center gap-2">
        {error !== null ? (
          <span className="truncate text-2xs text-danger">{error}</span>
        ) : transcript.length > 0 ? (
          <span className="truncate text-2xs text-fg-secondary">{transcript}</span>
        ) : (
          <span className="flex items-center gap-2 text-2xs text-fg-faint">
            <span>{continuous ? 'listening for “mitta”' : 'hold to talk · ⌘⇧V'}</span>
            {/* How long the last spoken request waited between the wake word
                matching and being sent — the latency MITTA owns, as opposed to
                the model's. Shown because three fixes went into this gap on
                reasoning alone and two were wrong. */}
            {lastWaitMs !== null && (
              <span
                className="readout"
                title="Wake word heard → request sent. Excludes the model's own time."
              >
                {(lastWaitMs / 1000).toFixed(1)}s to send
              </span>
            )}
          </span>
        )}
      </div>

      {speaking && (
        <button
          type="button"
          onClick={() => void stopSpeaking()}
          className="text-2xs text-accent hover:underline"
        >
          stop
        </button>
      )}

      {/* macOS ships only compact voices by default and mentions this nowhere
          the user would look, so an assistant that sounds tinny reads as a bug
          in MITTA rather than as a download they have not made. */}
      {voice?.canImprove === true && speakReplies && (
        <button
          type="button"
          onClick={() => void openSettings()}
          title={`Using ${voice.name} at compact quality. macOS has better voices as a free download.`}
          className="shrink-0 text-2xs text-warning hover:underline"
        >
          better voice
        </button>
      )}

      {/* Fits the speech gate to this room and this voice.
          Offered next to WAKE because that is the mode it affects: the gate is
          what decides whether the wake word is heard, and a gate set for a quiet
          room will miss it in a noisy one. */}
      <button
        type="button"
        onClick={() => void calibrate()}
        disabled={calibrating}
        title={
          threshold === null
            ? 'Measure the room for three seconds so MITTA knows what silence sounds like here'
            : `Gate set to ${threshold.toFixed(3)}. Run again if the room changed.`
        }
        className="shrink-0 text-2xs text-fg-muted hover:text-accent hover:underline disabled:opacity-50"
      >
        {calibrating ? 'measuring — stay quiet…' : 'calibrate'}
      </button>

      <Toggle
        label="SPEAK"
        on={speakReplies}
        onChange={() => setSpeakReplies(!speakReplies)}
        title={voice === null ? 'Read replies aloud' : `Read replies aloud — ${voice.name}`}
      />

      {/* Labelled WAKE, not ALWAYS.

          "Always" describes the microphone, which is the least useful half of
          what this does — the mode only ever acts on speech that followed the
          wake word, so everything else said near the machine is heard and
          discarded. A user reading "ALWAYS" reasonably concludes MITTA is
          acting on all of it, and either avoids a mode they wanted or trusts it
          for something it does not do.

          The microphone genuinely does stay open, so the live indicator stays:
          the label describes the behaviour and the dot describes the hardware
          (DEC-105). */}
      <Toggle
        label="WAKE"
        on={continuous}
        onChange={() => void toggleContinuous()}
        title={'Wake on “mitta” — the microphone stays open, and only speech after the wake word is acted on'}
        indicator={continuous}
      />
    </div>
  );
}

/**
 * The meter.
 *
 * Drawn straight to DOM heights from a ref rather than through React state:
 * the level arrives ten times a second, and re-rendering twenty-one elements
 * on every sample to animate a decoration is exactly the jank R2 forbids.
 */
function Waveform({ active }: { active: boolean }) {
  const bars = useRef<(HTMLSpanElement | null)[]>([]);

  useEffect(
    () =>
      useVoiceStore.subscribe((state) => {
        const level = state.status === 'listening' ? state.level : 0;
        for (let index = 0; index < BARS; index += 1) {
          const bar = bars.current[index];
          if (bar == null) continue;
          // Taller in the middle, tapering out — the shape a level meter has,
          // scaled by one real number.
          const distance = Math.abs(index - (BARS - 1) / 2) / ((BARS - 1) / 2);
          const shaped = level * (1 - distance * 0.75);
          bar.style.height = `${Math.max(2, shaped * 22)}px`;
        }
      }),
    [],
  );

  return (
    <div
      className="flex h-6 shrink-0 items-center gap-[2px]"
      aria-hidden
      data-active={active}
    >
      {Array.from({ length: BARS }, (_, index) => (
        <span
          key={index}
          ref={(element) => {
            bars.current[index] = element;
          }}
          className={cx(
            'w-[2px] rounded-full transition-[height] duration-75',
            active ? 'bg-accent' : 'bg-border-default',
          )}
          style={{ height: '2px' }}
        />
      ))}
    </div>
  );
}

function Toggle({
  label,
  on,
  onChange,
  title,
  indicator = false,
}: {
  label: string;
  on: boolean;
  onChange: () => void;
  title: string;
  indicator?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onChange}
      title={title}
      aria-pressed={on}
      className={cx(
        'flex shrink-0 items-center gap-1 px-1.5 py-0.5 text-[0.58rem] tracking-[0.18em] transition-colors',
        on ? 'text-accent' : 'text-fg-faint hover:text-fg-muted',
      )}
    >
      {indicator && <StatusDot tone={on ? 'warn' : 'idle'} pulse={on} />}
      {label}
    </button>
  );
}

function MicIcon({ active }: { active: boolean }) {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <rect x="9" y="2" width="6" height="12" rx="3" fill={active ? 'currentColor' : 'none'} />
      <path d="M5 11a7 7 0 0 0 14 0" strokeLinecap="round" />
      <path d="M12 18v3" strokeLinecap="round" />
    </svg>
  );
}
