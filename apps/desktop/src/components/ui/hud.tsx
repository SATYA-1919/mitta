/**
 * HUD chrome.
 *
 * The JARVIS-inspired layer (DEC-090, R2 as amended): clipped panel corners,
 * cyan hairlines, an activity ring, segmented meters, connector rules.
 *
 * One rule runs through all of it — **glow goes on chrome, never behind text.**
 * A border, a ring, a tick mark can carry a drop-shadow and look right. Body
 * text with a halo behind it photographs well and is tiring to read within
 * about a minute, and this is a window that stays open for hours.
 *
 * There is no background image. The reference had one; a photographic backdrop
 * under a dense readout costs contrast everywhere and gains nothing that the
 * frame does not already say.
 */

import type { ReactNode } from 'react';

import { cx } from '@/components/ui/primitives';

export function HudPanel({
  label,
  right,
  children,
  className,
  active = false,
}: {
  label?: string;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  active?: boolean;
}) {
  return (
    <section
      className={cx(
        // The frame carries the glow. `box-shadow` on a 1px cyan line is the
        // whole effect; the same shadow on the panel body would wash the
        // content sitting on it.
        'hud-frame hud-ticks relative overflow-hidden bg-surface-raised/40',
        active && 'border-accent/70 shadow-[0_0_22px_-6px_var(--color-accent)]',
        className,
      )}
    >
      {(label !== undefined || right !== undefined) && (
        <header className="flex items-center gap-2 border-b border-accent/20 bg-accent/[0.04] px-3 py-1.5 pl-4">
          {label !== undefined && <span className="label">{label}</span>}
          {/* Connector rule. A label that runs into a hairline reads as a
              schematic; the same label alone reads as a heading. */}
          <span className="h-px flex-1 bg-gradient-to-r from-accent/50 to-transparent" />
          {right}
        </header>
      )}
      <div className="p-3 pl-4">{children}</div>
    </section>
  );
}

/**
 * Activity ring.
 *
 * Rotates only while `active`. A ring that spins permanently is decoration and
 * stops carrying information — the whole point is that motion means MITTA is
 * doing something.
 */
export function ActivityRing({
  active = false,
  size = 18,
  label,
}: {
  active?: boolean;
  size?: number;
  label?: string;
}) {
  const radius = size / 2 - 2;
  const circumference = 2 * Math.PI * radius;

  return (
    <span className="inline-flex items-center gap-1.5" title={label}>
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        aria-hidden
        className={cx('shrink-0', active && 'animate-[spin_2.4s_linear_infinite]')}
      >
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth="1"
          className="text-border-default"
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          // A 25% arc: enough to read as motion, short enough that the gap is
          // obvious when it stops.
          strokeDasharray={`${circumference * 0.25} ${circumference}`}
          className={active ? 'text-accent' : 'text-fg-faint'}
        />
        <circle cx={size / 2} cy={size / 2} r={1.5} className="fill-accent" />
      </svg>
    </span>
  );
}

/**
 * Segmented meter.
 *
 * Discrete cells rather than a smooth bar. A continuous fill invites reading a
 * precise value off a 40-pixel strip, which it cannot support; ten cells say
 * "about six tenths" and are honest about the resolution.
 */
export function Meter({
  value,
  label,
  segments = 10,
  tone = 'accent',
}: {
  /** 0–1. Clamped, because a metric that overshoots should not overflow the row. */
  value: number;
  label: string;
  segments?: number;
  tone?: 'accent' | 'warning' | 'danger';
}) {
  const filled = Math.round(Math.max(0, Math.min(1, value)) * segments);
  const colour =
    tone === 'danger' ? 'bg-danger' : tone === 'warning' ? 'bg-warning' : 'bg-accent';

  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="label !text-[0.6rem]">{label}</span>
      <span className="flex gap-px" aria-label={`${label} ${Math.round(value * 100)}%`}>
        {Array.from({ length: segments }, (_, index) => (
          <span
            key={index}
            className={cx(
              'h-2.5 w-1',
              index < filled ? colour : 'bg-border-subtle',
              // Only the leading cell glows, so the meter has an edge rather
              // than a wash.
              index === filled - 1 && 'shadow-[0_0_4px_var(--color-accent)]',
            )}
          />
        ))}
      </span>
      <span className="readout text-2xs text-fg-muted">
        {Math.round(Math.max(0, Math.min(1, value)) * 100)}%
      </span>
    </span>
  );
}

/** A horizontal rule with a tick, for separating regions of a dense layout. */
export function HudRule({ className }: { className?: string }) {
  return (
    <div className={cx('flex items-center gap-1.5', className)} aria-hidden>
      <span className="size-1 rotate-45 border border-accent/60" />
      <span className="h-px flex-1 bg-gradient-to-r from-accent/25 via-border-subtle to-transparent" />
    </div>
  );
}
