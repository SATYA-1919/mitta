/**
 * Monitor — what MITTA is doing to the machine right now.
 *
 * Reports what it can measure and says so when it cannot. GPU has no
 * unprivileged API on Apple Silicon (ARCHITECTURE.md §13), so it shows as
 * unavailable rather than as a plausible number — a fabricated metric is worse
 * than a missing one, because it will be believed.
 */

import { ActivityRing, HudPanel, Meter } from '@/components/ui/hud';
import { StatusDot } from '@/components/ui/primitives';
import { useMemoryStore } from '@/state/memory';
import { useStore } from '@/state/store';

function gb(bytes: number): string {
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

export function MonitorSurface() {
  const metrics = useStore((s) => s.metrics);
  const connection = useStore((s) => s.connection);
  const components = useStore((s) => s.components);
  const schemaVersion = useStore((s) => s.schemaVersion);
  const activeTurn = useStore((s) => s.activeTurn);
  const stats = useMemoryStore((s) => s.stats);

  return (
    <div className="scrollable grid-surface h-full">
      <div className="mx-auto grid max-w-3xl gap-4 p-6 sm:grid-cols-2">
        <HudPanel label="system" active={metrics !== null}>
          {metrics === null ? (
            // Metrics come from Rust over IPC (DEC-003), so a browser session
            // genuinely has none. Said, not spun.
            <p className="text-xs text-fg-muted">
              Not available — metrics come from the desktop shell.
            </p>
          ) : (
            <div className="space-y-3">
              <Meter
                label="CPU"
                value={metrics.cpuPercent / 100}
                segments={16}
                tone={
                  metrics.cpuPercent > 85
                    ? 'danger'
                    : metrics.cpuPercent > 60
                      ? 'warning'
                      : 'accent'
                }
              />
              <Meter
                label="MEM"
                value={
                  metrics.memoryTotalBytes > 0
                    ? metrics.memoryUsedBytes / metrics.memoryTotalBytes
                    : 0
                }
                segments={16}
              />
              <div className="readout text-2xs text-fg-faint">
                {gb(metrics.memoryUsedBytes)} / {gb(metrics.memoryTotalBytes)}
              </div>
              <div className="readout text-2xs text-fg-faint">
                GPU — no unprivileged API on Apple Silicon
              </div>
            </div>
          )}
        </HudPanel>

        <HudPanel label="turn" active={activeTurn?.status === 'running'}>
          {activeTurn === null ? (
            <p className="text-xs text-fg-muted">Idle.</p>
          ) : (
            <div className="space-y-1.5 text-xs">
              <div className="flex items-center gap-2">
                <ActivityRing active={activeTurn.status === 'running'} size={14} />
                <span className="label !text-accent">{activeTurn.phase ?? activeTurn.status}</span>
              </div>
              <div className="readout text-2xs text-fg-muted">
                MEM {activeTurn.memoryIds.length} · TOOLS {activeTurn.tools.length}
              </div>
              {activeTurn.modelId !== null && (
                <div className="readout text-2xs text-fg-faint">{activeTurn.modelId}</div>
              )}
            </div>
          )}
        </HudPanel>

        <HudPanel label="backend">
          <div className="space-y-1.5 text-xs">
            <div className="flex items-center gap-2">
              <StatusDot tone={connection === 'open' ? 'ok' : 'error'} />
              <span className="readout text-fg-primary">{connection}</span>
            </div>
            <div className="readout text-2xs text-fg-faint">schema v{schemaVersion}</div>
            {components.map((component) => (
              <div key={component.name} className="flex items-center gap-2">
                <StatusDot tone={component.state === 'ok' ? 'ok' : 'warn'} />
                <span className="readout text-2xs text-fg-muted">{component.name}</span>
              </div>
            ))}
          </div>
        </HudPanel>

        <HudPanel label="memory index">
          {stats === null ? (
            <p className="text-xs text-fg-muted">—</p>
          ) : (
            <div className="space-y-1.5">
              <div className="readout text-2xs text-fg-muted">
                {stats.active} active · {stats.vectors_indexed} vectors
              </div>
              {stats.pending_embeddings > 0 && (
                <Meter
                  label="IDX"
                  value={
                    stats.vectors_indexed /
                    Math.max(1, stats.vectors_indexed + stats.pending_embeddings)
                  }
                  segments={12}
                />
              )}
              <div className="flex items-center gap-2">
                <StatusDot tone={stats.embedding_degraded ? 'warn' : 'ok'} />
                <span className="readout text-2xs text-fg-faint">{stats.embedding_model_id}</span>
              </div>
            </div>
          )}
        </HudPanel>
      </div>
    </div>
  );
}
