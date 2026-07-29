import { ActivityRing, Meter } from '@/components/ui/hud';
import { type DotTone, Kbd, StatusDot } from '@/components/ui/primitives';
import type { ConnectionState } from '@/lib/transport/socket';
import { useStore } from '@/state/store';

const CONNECTION_TONE: Record<ConnectionState, DotTone> = {
  idle: 'idle',
  connecting: 'warn',
  open: 'ok',
  reconnecting: 'warn',
  closed: 'error',
};

const CONNECTION_LABEL: Record<ConnectionState, string> = {
  idle: 'Not connected',
  connecting: 'Connecting',
  open: 'Connected',
  reconnecting: 'Reconnecting',
  closed: 'Disconnected',
};

/**
 * The status bar is where the product's honesty lives. Connection state,
 * schema version and GPU availability are all shown as they actually are —
 * `ARCHITECTURE.md` §13 commits to showing GPU as unavailable rather than
 * fabricating a number, and this is where that promise is kept.
 */
export function StatusBar() {
  const connection = useStore((s) => s.connection);
  const detail = useStore((s) => s.connectionDetail);
  const metrics = useStore((s) => s.metrics);
  const activeTurn = useStore((s) => s.activeTurn);

  return (
    <footer
      className={cxRow()}
      aria-label="Status"
    >
      <div className="flex items-center gap-1.5" title={detail ?? undefined}>
        <StatusDot
          tone={CONNECTION_TONE[connection]}
          pulse={connection === 'connecting' || connection === 'reconnecting'}
        />
        <span>{CONNECTION_LABEL[connection]}</span>
      </div>

      {activeTurn?.status === 'running' && (
        <div className="flex items-center gap-1.5 text-accent">
          <ActivityRing active size={14} />
          <span className="label !text-accent">{activeTurn.phase ?? 'working'}</span>
        </div>
      )}

      <div className="flex-1" />

      {metrics !== null && (
        <>
          <Meter
            label="CPU"
            value={metrics.cpuPercent / 100}
            tone={metrics.cpuPercent > 85 ? 'danger' : metrics.cpuPercent > 60 ? 'warning' : 'accent'}
          />
          <Meter
            label="MEM"
            value={
              metrics.memoryTotalBytes > 0
                ? metrics.memoryUsedBytes / metrics.memoryTotalBytes
                : 0
            }
          />
          {/* No unprivileged GPU API on Apple Silicon (ARCHITECTURE.md §13).
              Shown as absent rather than as a fabricated number. */}
          <span className="readout text-fg-faint">
            GPU {metrics.gpuPercent === null ? '—' : `${metrics.gpuPercent.toFixed(0)}%`}
          </span>
          {metrics.batteryPercent !== null && (
            <span className="readout">
              BAT {metrics.batteryPercent.toFixed(0)}%{metrics.batteryCharging === true ? '⚡' : ''}
            </span>
          )}
        </>
      )}

      <div className="flex items-center gap-1">
        <Kbd>⌘</Kbd>
        <Kbd>K</Kbd>
      </div>
    </footer>
  );
}

function cxRow(): string {
  return [
    'flex h-[--spacing-statusbar] shrink-0 items-center gap-4 px-3',
    'border-t border-border-subtle bg-surface-sunken',
    'text-2xs text-fg-muted',
  ].join(' ');
}
