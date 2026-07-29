/**
 * The main window, rebuilt as a HUD.
 *
 * No sidebar. Navigation is a top rail, and the space that freed goes to
 * instrument clusters flanking the content — which is what the reference
 * layout actually is: a rail across the top, readouts down the sides, and a
 * core in the middle.
 *
 * Every readout here is driven by a real value. A HUD full of invented
 * telemetry looks right in a screenshot and teaches the user, on the first
 * surface they see, that the numbers are decoration.
 */

import { useEffect } from 'react';

import { ChatSurface } from '@/components/chat/ChatSurface';
import { HistorySurface } from '@/components/history/HistorySurface';
import { MemorySurface } from '@/components/memory/MemorySurface';
import { MonitorSurface } from '@/components/monitor/MonitorSurface';
import { ProjectsSurface } from '@/components/projects/ProjectsSurface';
import { SettingsSurface } from '@/components/settings/SettingsSurface';
import { HudRule } from '@/components/ui/hud';
import { cx, EmptyState, StatusDot } from '@/components/ui/primitives';
import { CoreRing, RadialGauge } from '@/components/ui/radial';
import { useMemoryStore } from '@/state/memory';
import { type Surface, useStore } from '@/state/store';

const SURFACES: { id: Surface; label: string; key: string }[] = [
  { id: 'chat', label: 'CHAT', key: '1' },
  { id: 'memory', label: 'MEMORY', key: '2' },
  { id: 'history', label: 'HISTORY', key: '3' },
  { id: 'monitor', label: 'MONITOR', key: '4' },
  { id: 'settings', label: 'SETTINGS', key: '5' },
  { id: 'projects', label: 'PROJECTS', key: '6' },
  { id: 'tasks', label: 'TASKS', key: '7' },
  { id: 'plugins', label: 'PLUGINS', key: '8' },
];

export function MainWindow() {
  const surface = useStore((s) => s.surface);
  const setSurface = useStore((s) => s.setSurface);
  const busy = useStore((s) => s.activeTurn?.status === 'running');

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (!event.metaKey || event.altKey || event.ctrlKey) return;
      const match = SURFACES.find((item) => item.key === event.key);
      if (match !== undefined) {
        event.preventDefault();
        setSurface(match.id);
      }
    }
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [setSurface]);

  return (
    <div className="grid-surface flex h-full w-full flex-col overflow-hidden bg-surface-base">
      <TopRail surface={surface} onSelect={setSurface} busy={busy} />

      <div className="flex min-h-0 flex-1">
        <LeftCluster busy={busy} />

        <main className="min-w-0 flex-1">
          {surface === 'chat' && <ChatSurface />}
          {surface === 'memory' && <MemorySurface />}
          {surface === 'history' && <HistorySurface />}
          {surface === 'monitor' && <MonitorSurface />}
          {surface === 'settings' && <SettingsSurface />}
          {surface === 'projects' && <ProjectsSurface />}
          {(surface === 'tasks' || surface === 'plugins') && (
            <EmptyState title={`${surface} is not built yet`} hint="Lands in a later phase" />
          )}
        </main>

        <RightCluster />
      </div>
    </div>
  );
}

function TopRail({
  surface,
  onSelect,
  busy,
}: {
  surface: Surface;
  onSelect: (surface: Surface) => void;
  busy: boolean;
}) {
  const connection = useStore((s) => s.connection);

  return (
    // The left padding clears the macOS traffic lights. `titleBarStyle: Overlay`
    // draws them over the content, and a nav item underneath them is a nav item
    // that cannot be clicked.
    <header className="drag-region flex shrink-0 items-center gap-4 border-b border-accent/25 bg-surface-sunken/80 px-4 py-2 pl-[86px]">
      <div className="flex items-center gap-2">
        <span
          className={cx(
            'size-1.5 rounded-full bg-accent',
            busy && 'shadow-[0_0_8px_var(--color-accent)]',
          )}
        />
        <span className="readout text-xs tracking-[0.34em] text-accent">MITTA</span>
      </div>

      <span className="h-4 w-px bg-accent/30" />

      <nav className="no-drag flex items-center gap-1">
        {SURFACES.map((item) => {
          const active = surface === item.id;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => onSelect(item.id)}
              aria-current={active ? 'page' : undefined}
              className={cx(
                'relative px-2.5 py-1 text-[0.65rem] tracking-[0.18em] transition-colors',
                // A lit underline marks the active tab. On a rail an underline
                // reads as a channel selector; a filled block reads as a button.
                'after:absolute after:inset-x-1.5 after:-bottom-[9px] after:h-px after:transition-colors',
                active
                  ? 'text-accent after:bg-accent after:shadow-[0_0_6px_var(--color-accent)]'
                  : 'text-fg-muted after:bg-transparent hover:text-fg-secondary',
              )}
            >
              {item.label}
            </button>
          );
        })}
      </nav>

      <span className="h-px flex-1 bg-gradient-to-r from-accent/40 via-border-subtle to-transparent" />

      <span className="flex items-center gap-1.5">
        <StatusDot
          tone={connection === 'open' ? 'ok' : 'error'}
          pulse={connection === 'connecting'}
        />
        <span className="label !text-[0.58rem]">{connection}</span>
      </span>
    </header>
  );
}

/**
 * System instruments.
 *
 * Hidden below `xl`. A gauge squeezed to nothing is worse than an absent one —
 * it still costs the space and stops being readable.
 */
function LeftCluster({ busy }: { busy: boolean }) {
  const metrics = useStore((s) => s.metrics);
  const turn = useStore((s) => s.activeTurn);

  return (
    <aside className="hidden w-[172px] shrink-0 flex-col items-center gap-5 border-r border-accent/15 py-6 xl:flex">
      <CoreRing active={busy} size={140}>
        <span className="readout text-[0.65rem] tracking-[0.2em] text-accent">
          {busy ? 'ACTIVE' : 'IDLE'}
        </span>
        {turn !== null && (
          <span className="readout mt-0.5 text-[0.55rem] text-fg-faint">
            {turn.phase ?? turn.status}
          </span>
        )}
      </CoreRing>

      <HudRule className="w-full px-4" />

      {metrics === null ? (
        // Metrics come from Rust over IPC (DEC-003), so a browser session has
        // none. Saying so beats a zeroed dial, which looks like a reading.
        <p className="px-4 text-center text-[0.6rem] leading-relaxed text-fg-faint">
          System telemetry needs the desktop shell
        </p>
      ) : (
        <>
          <RadialGauge
            value={metrics.cpuPercent / 100}
            label="CPU"
            size={104}
            tone={
              metrics.cpuPercent > 85 ? 'danger' : metrics.cpuPercent > 60 ? 'warning' : 'accent'
            }
          />
          <RadialGauge
            value={
              metrics.memoryTotalBytes > 0
                ? metrics.memoryUsedBytes / metrics.memoryTotalBytes
                : 0
            }
            label="MEM"
            size={104}
            sublabel={`${(metrics.memoryUsedBytes / 1024 ** 3).toFixed(1)} GB`}
          />
        </>
      )}
    </aside>
  );
}

/** Memory and turn instruments. */
function RightCluster() {
  const stats = useMemoryStore((s) => s.stats);
  const turn = useStore((s) => s.activeTurn);
  const ready = useStore((s) => s.ready);

  const indexed = stats?.vectors_indexed ?? 0;
  const pending = stats?.pending_embeddings ?? 0;
  const total = indexed + pending;
  // Nothing indexed is not "100% indexed". A full ring over zero vectors is
  // exactly the fabricated reading the rest of this file refuses to show.
  const coverage = total > 0 ? indexed / total : 0;

  return (
    <aside className="hidden w-[172px] shrink-0 flex-col items-center gap-5 border-l border-accent/15 py-6 xl:flex">
      <RadialGauge
        value={coverage}
        label="INDEX"
        size={104}
        sublabel={stats === null ? 'no data' : `${indexed} vectors`}
        tone={stats?.index_consistent === false ? 'warning' : 'accent'}
      />

      <HudRule className="w-full px-4" />

      <dl className="w-full space-y-1.5 px-4 text-[0.6rem]">
        <Readout label="MEM" value={stats === null ? '—' : String(stats.active)} />
        <Readout
          label="RECALL"
          value={stats === null ? '—' : stats.embedding_degraded ? 'WORD' : 'SEM'}
          warn={stats?.embedding_degraded === true}
        />
        <Readout label="CORE" value={ready ? 'READY' : 'WAIT'} warn={!ready} />
        <Readout label="CTX" value={turn === null ? '—' : String(turn.memoryIds.length)} />
        <Readout label="TOOL" value={turn === null ? '—' : String(turn.tools.length)} />
      </dl>

      {turn?.modelId != null && (
        <>
          <HudRule className="w-full px-4" />
          <p className="readout px-3 text-center text-[0.55rem] leading-relaxed text-fg-faint">
            {turn.modelId}
          </p>
        </>
      )}
    </aside>
  );
}

function Readout({
  label,
  value,
  warn = false,
}: {
  label: string;
  value: string;
  warn?: boolean;
}) {
  return (
    <div className="flex items-center gap-2">
      <dt className="label !text-[0.55rem]">{label}</dt>
      <span className="h-px flex-1 bg-border-subtle" />
      <dd className={cx('readout', warn ? 'text-warning' : 'text-fg-secondary')}>{value}</dd>
    </div>
  );
}
