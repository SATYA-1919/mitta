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

  // ⌘⇧V, held. `keydown` repeats while a key is down, so the guard is what
  // stops the recogniser being restarted thirty times a second.
  const held = useRef(false);
  useEffect(() => {
    if (!available) return;

    function down(event: KeyboardEvent) {
      if (!event.metaKey || !event.shiftKey || event.code !== 'KeyV') return;
      event.preventDefault();
      if (held.current) return;
      held.current = true;
      void pushToTalk(true);
    }
    function up(event: KeyboardEvent) {
      if (!held.current) return;
      // Also fires when a modifier is released first, which is the common way
      // out of a chord — otherwise the microphone stays open after the hold.
      if (event.code === 'KeyV' || event.key === 'Meta' || event.key === 'Shift') {
        held.current = false;
        void pushToTalk(false);
      }
    }
    // Releasing while the window is unfocused never produces a `keyup`.
    function blur() {
      if (!held.current) return;
      held.current = false;
      void pushToTalk(false);
    }

    window.addEventListener('keydown', down);
    window.addEventListener('keyup', up);
    window.addEventListener('blur', blur);
    return () => {
      window.removeEventListener('keydown', down);
      window.removeEventListener('keyup', up);
      window.removeEventListener('blur', blur);
    };
  }, [available, pushToTalk]);

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
          <span className="text-2xs text-fg-faint">
            {continuous ? 'listening for “mitta”' : 'hold to talk · ⌘⇧V'}
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

      <Toggle
        label="SPEAK"
        on={speakReplies}
        onChange={() => setSpeakReplies(!speakReplies)}
        title={voice === null ? 'Read replies aloud' : `Read replies aloud — ${voice.name}`}
      />

      {/* The microphone stays open the whole time this is on, so it gets a
          live indicator rather than only a label (DEC-105). */}
      <Toggle
        label="ALWAYS"
        on={continuous}
        onChange={() => void toggleContinuous()}
        title="Listen continuously for the wake word — keeps the microphone open"
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
