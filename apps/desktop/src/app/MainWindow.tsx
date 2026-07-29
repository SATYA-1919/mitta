import { useEffect } from 'react';

import { ChatSurface } from '@/components/chat/ChatSurface';
import { Sidebar, SURFACES } from '@/components/layout/Sidebar';
import { StatusBar } from '@/components/layout/StatusBar';
import { MemorySurface } from '@/components/memory/MemorySurface';
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
        <header className="drag-region flex h-[--spacing-titlebar] shrink-0 items-center border-b border-border-subtle px-4">
          <h1 className="text-sm font-medium capitalize text-fg-secondary">{surface}</h1>
        </header>

        <div className="min-h-0 flex-1">
          {surface === 'chat' && <ChatSurface />}
          {surface === 'memory' && <MemorySurface />}
          {surface !== 'chat' && surface !== 'memory' && <PendingSurface name={surface} />}
        </div>

        <StatusBar />
      </main>
    </div>
  );
}


function PendingSurface({ name }: { name: string }) {
  return <EmptyState title={`${name} is not built yet`} hint="Lands in a later phase" />;
}
