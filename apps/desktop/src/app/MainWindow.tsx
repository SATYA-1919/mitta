import { useEffect } from 'react';

import { ChatSurface } from '@/components/chat/ChatSurface';
import { Sidebar, SURFACES } from '@/components/layout/Sidebar';
import { StatusBar } from '@/components/layout/StatusBar';
import { HistorySurface } from '@/components/history/HistorySurface';
import { MemorySurface } from '@/components/memory/MemorySurface';
import { MonitorSurface } from '@/components/monitor/MonitorSurface';
import { SettingsSurface } from '@/components/settings/SettingsSurface';
import { ActivityRing } from '@/components/ui/hud';
import { EmptyState } from '@/components/ui/primitives';
import { useStore } from '@/state/store';

/**
 * Persistent main window — the Cursor/Linear-shaped surface.
 *
 * Feature panes land in their own phases. What exists here is the shell: the
 * chrome, the routing between surfaces, and the live turn view, which is the
 * only one whose behaviour the transport layer can already drive.
 */
export function MainWindow() {
  const surface = useStore((s) => s.surface);
  const setSurface = useStore((s) => s.setSurface);
  const toggleSidebar = useStore((s) => s.toggleSidebar);
  const busy = useStore((s) => s.activeTurn?.status === 'running');

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (!event.metaKey || event.altKey || event.ctrlKey) return;

      if (event.key === 'b') {
        event.preventDefault();
        toggleSidebar();
        return;
      }
      const match = SURFACES.find((item) => item.hint === event.key);
      if (match !== undefined) {
        event.preventDefault();
        setSurface(match.id);
      }
    }
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [setSurface, toggleSidebar]);

  return (
    <div className="flex h-full w-full overflow-hidden bg-surface-base">
      <Sidebar />
      <main className="flex min-w-0 flex-1 flex-col">
        <header className="drag-region flex h-[--spacing-titlebar] shrink-0 items-center gap-3 border-b border-border-subtle px-4">
          <ActivityRing active={busy} size={14} label={busy ? 'working' : 'idle'} />
          <h1 className="label !text-[0.7rem] !text-fg-secondary">{surface}</h1>
          {/* A hairline running to the edge. Cheap, and it does more to make a
              header read as an instrument panel than any amount of chrome. */}
          <span className="h-px flex-1 bg-gradient-to-r from-accent/40 via-border-default to-transparent" />
        </header>

        <div className="min-h-0 flex-1">
          {surface === 'chat' && <ChatSurface />}
          {surface === 'memory' && <MemorySurface />}
          {surface === 'history' && <HistorySurface />}
          {surface === 'monitor' && <MonitorSurface />}
          {surface === 'settings' && <SettingsSurface />}
          {(surface === 'projects' || surface === 'tasks' || surface === 'plugins') && (
            <PendingSurface name={surface} />
          )}
        </div>

        <StatusBar />
      </main>
    </div>
  );
}


function PendingSurface({ name }: { name: string }) {
  return <EmptyState title={`${name} is not built yet`} hint="Lands in a later phase" />;
}
