/**
 * Radial instruments — the arcs, rings and gauges the HUD is built from.
 *
 * All SVG, all driven by real values. The reference look is essentially one
 * idea repeated at different sizes: an incomplete ring with tick marks, a
 * number in the middle, and a label. Everything here is a variation on that.
 *
 * Nothing animates unless something is happening. A HUD where every ring spins
 * all the time is wallpaper — motion has to mean something or it stops being
 * information and starts being noise.
 */

import type { ReactNode } from 'react';

import { cx } from '@/components/ui/primitives';

const TAU = Math.PI * 2;

function polar(cx_: number, cy: number, r: number, angle: number): [number, number] {
  return [cx_ + r * Math.cos(angle), cy + r * Math.sin(angle)];
}

/** An arc path. Angles in turns (0–1), starting at 12 o'clock. */
function arcPath(cx_: number, cy: number, r: number, from: number, to: number): string {
  const start = from * TAU - TAU / 4;
  const end = to * TAU - TAU / 4;
  const [x1, y1] = polar(cx_, cy, r, start);
  const [x2, y2] = polar(cx_, cy, r, end);
  const large = to - from > 0.5 ? 1 : 0;
  return `M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2}`;
}

/**
 * A circular gauge: value arc, tick ring, centred readout.
 *
 * The gap at the bottom is deliberate. A closed ring reads as a pie chart and
 * invites comparing areas; an open one reads as a dial, which is what a
 * percentage actually is.
 */
export function RadialGauge({
  value,
  label,
  sublabel,
  size = 92,
  tone = 'accent',
}: {
  /** 0–1, clamped. */
  value: number;
  label: string;
  sublabel?: string;
  size?: number;
  tone?: 'accent' | 'warning' | 'danger';
}) {
  const clamped = Math.max(0, Math.min(1, value));
  const centre = size / 2;
  const radius = centre - 10;
  // Three-quarter sweep, opening at the bottom.
  const START = 0.625;
  const SWEEP = 0.75;

  const stroke =
    tone === 'danger'
      ? 'var(--color-danger)'
      : tone === 'warning'
        ? 'var(--color-warning)'
        : 'var(--color-accent)';

  return (
    <div className="flex flex-col items-center gap-0.5">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden>
        {/* Tick ring. Twenty-four marks reads as an instrument; four reads as a
            progress bar bent into a circle. */}
        {Array.from({ length: 24 }, (_, i) => {
          const angle = (START + (i / 24) * SWEEP) * TAU - TAU / 4;
          const [x1, y1] = polar(centre, centre, radius + 4, angle);
          const [x2, y2] = polar(centre, centre, radius + 7, angle);
          return (
            <line
              key={i}
              x1={x1}
              y1={y1}
              x2={x2}
              y2={y2}
              stroke="var(--color-accent)"
              strokeWidth="1"
              opacity={i / 24 <= clamped ? 0.75 : 0.18}
            />
          );
        })}

        <path
          d={arcPath(centre, centre, radius, START, START + SWEEP)}
          fill="none"
          stroke="var(--color-border-default)"
          strokeWidth="3"
          strokeLinecap="round"
        />
        <path
          d={arcPath(centre, centre, radius, START, START + SWEEP * Math.max(clamped, 0.001))}
          fill="none"
          stroke={stroke}
          strokeWidth="3"
          strokeLinecap="round"
          style={{ filter: `drop-shadow(0 0 4px ${stroke})` }}
        />

        <text
          x={centre}
          y={centre + 1}
          textAnchor="middle"
          className="readout fill-fg-primary"
          style={{ fontSize: size * 0.24, fontWeight: 500 }}
        >
          {Math.round(clamped * 100)}
        </text>
        <text
          x={centre}
          y={centre + size * 0.19}
          textAnchor="middle"
          className="fill-accent"
          style={{ fontSize: size * 0.11, letterSpacing: '0.16em' }}
        >
          {label}
        </text>
      </svg>
      {sublabel !== undefined && (
        <span className="readout text-2xs text-fg-faint">{sublabel}</span>
      )}
    </div>
  );
}

/**
 * The core: concentric rings that spin only while MITTA is working.
 *
 * Three rings at different speeds and directions. One ring reads as a spinner;
 * three reads as a mechanism, which is the whole difference between "loading"
 * and "thinking".
 */
export function CoreRing({
  active = false,
  size = 200,
  children,
}: {
  active?: boolean;
  size?: number;
  children?: ReactNode;
}) {
  const c = size / 2;

  return (
    <div className="relative" style={{ width: size, height: size }}>
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        aria-hidden
        className="absolute inset-0"
      >
        {/* Outer: segmented, slow, clockwise. */}
        <g
          className={cx(active && 'animate-[spin_18s_linear_infinite]')}
          style={{ transformOrigin: 'center' }}
        >
          {Array.from({ length: 12 }, (_, i) => (
            <path
              key={i}
              d={arcPath(c, c, c - 4, i / 12 + 0.012, (i + 1) / 12 - 0.012)}
              fill="none"
              stroke="var(--color-accent)"
              strokeWidth="1.5"
              opacity={0.28 + (i % 3) * 0.12}
            />
          ))}
        </g>

        {/* Middle: one long arc, faster, anticlockwise. */}
        <g
          className={cx(active && 'animate-[spin_7s_linear_infinite_reverse]')}
          style={{ transformOrigin: 'center' }}
        >
          <path
            d={arcPath(c, c, c - 20, 0.05, 0.42)}
            fill="none"
            stroke="var(--color-accent)"
            strokeWidth="2"
            strokeLinecap="round"
            opacity="0.8"
            style={{ filter: 'drop-shadow(0 0 6px var(--color-accent))' }}
          />
          <path
            d={arcPath(c, c, c - 20, 0.55, 0.78)}
            fill="none"
            stroke="var(--color-accent)"
            strokeWidth="2"
            strokeLinecap="round"
            opacity="0.45"
          />
        </g>

        {/* Inner tick ring, static — a fixed reference the moving rings read
            against. Without it the motion has nothing to be relative to. */}
        {Array.from({ length: 36 }, (_, i) => {
          const angle = (i / 36) * TAU;
          const [x1, y1] = polar(c, c, c - 34, angle);
          const [x2, y2] = polar(c, c, c - 30, angle);
          return (
            <line
              key={i}
              x1={x1}
              y1={y1}
              x2={x2}
              y2={y2}
              stroke="var(--color-accent)"
              strokeWidth="1"
              opacity={i % 3 === 0 ? 0.5 : 0.2}
            />
          );
        })}

        <circle
          cx={c}
          cy={c}
          r={c - 46}
          fill="none"
          stroke="var(--color-accent)"
          strokeWidth="1"
          opacity="0.35"
        />
        <circle
          cx={c}
          cy={c}
          r={c - 54}
          className="fill-accent"
          opacity={active ? 0.14 : 0.06}
          style={{ filter: 'blur(6px)' }}
        />
      </svg>

      <div className="absolute inset-0 flex flex-col items-center justify-center text-center">
        {children}
      </div>
    </div>
  );
}

/** A sparkline. Real samples only — a decorative waveform is a fake reading. */
export function Sparkline({
  values,
  width = 120,
  height = 24,
}: {
  values: number[];
  width?: number;
  height?: number;
}) {
  if (values.length < 2) {
    return <div style={{ width, height }} className="opacity-30" />;
  }
  const max = Math.max(...values, 1);
  const step = width / (values.length - 1);
  const points = values
    .map((v, i) => `${i * step},${height - (v / max) * height}`)
    .join(' ');

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden>
      <polyline
        points={points}
        fill="none"
        stroke="var(--color-accent)"
        strokeWidth="1"
        opacity="0.8"
      />
    </svg>
  );
}
