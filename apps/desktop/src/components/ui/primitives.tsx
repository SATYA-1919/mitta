/**
 * Design-system primitives.
 *
 * Only genuinely cross-feature components live here. Anything used by one
 * feature belongs in that feature's folder — a shared component with one caller
 * is indirection with no payoff.
 */

import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from 'react';

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ');
}

// -- Button -----------------------------------------------------------------

type ButtonVariant = 'primary' | 'ghost' | 'danger';

const BUTTON_VARIANTS: Record<ButtonVariant, string> = {
  // Dark text on cyan: at this lightness the accent is bright enough that white
  // text on it fails contrast, and light-on-bright is what produces the halo
  // the token comment warns about.
  primary: 'bg-accent text-fg-inverted hover:bg-accent-hover font-medium',
  ghost: 'bg-transparent text-fg-secondary hover:bg-surface-hover hover:text-fg-primary',
  danger: 'bg-danger-surface text-danger hover:bg-danger hover:text-fg-primary',
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  /** React 19 passes `ref` as an ordinary prop; no forwardRef needed. */
  ref?: React.Ref<HTMLButtonElement>;
}

export function Button({ variant = 'ghost', className, ref, ...props }: ButtonProps) {
  return (
    <button
      ref={ref}
      type="button"
      className={cx(
        'inline-flex items-center gap-2 rounded-xs px-3 py-1.5 text-sm font-medium',
        'transition-colors duration-[--duration-fast]',
        'disabled:pointer-events-none disabled:opacity-40',
        BUTTON_VARIANTS[variant],
        className,
      )}
      {...props}
    />
  );
}

// -- Panel ------------------------------------------------------------------

export function Panel({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cx(
        // Square, bracketed, hairline. A rounded filled card reads as a
        // consumer surface; this reads as a readout.
        'bracket rounded-xs border border-border-subtle bg-surface-raised',
        className,
      )}
      {...props}
    />
  );
}

// -- StatusDot --------------------------------------------------------------

export type DotTone = 'ok' | 'warn' | 'error' | 'idle';

const DOT_TONES: Record<DotTone, string> = {
  ok: 'bg-success',
  warn: 'bg-warning',
  error: 'bg-danger',
  idle: 'bg-fg-faint',
};

export function StatusDot({ tone, pulse = false }: { tone: DotTone; pulse?: boolean }) {
  return (
    <span
      aria-hidden
      className={cx(
        'inline-block size-1.5 shrink-0 rounded-full',
        DOT_TONES[tone],
        pulse && 'animate-pulse',
      )}
    />
  );
}

// -- Kbd --------------------------------------------------------------------

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd
      className={cx(
        'inline-flex min-w-5 items-center justify-center rounded-xs px-1.5 py-0.5',
        'border border-border-subtle bg-surface-input',
        'font-mono text-2xs text-fg-muted',
      )}
    >
      {children}
    </kbd>
  );
}

// -- EmptyState -------------------------------------------------------------

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-1.5 px-8 text-center">
      <p className="text-sm text-fg-secondary">{title}</p>
      {hint !== undefined && <p className="text-xs text-fg-faint">{hint}</p>}
    </div>
  );
}
