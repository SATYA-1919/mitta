import { useEffect } from 'react';

import { Sidebar, SURFACES } from '@/components/layout/Sidebar';
import { StatusBar } from '@/components/layout/StatusBar';
import { EmptyState, Panel } from '@/components/ui/primitives';
import { displayText, useStore } from '@/state/store';

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

        <div className="scrollable min-h-0 flex-1">
          {surface === 'chat' ? <ChatSurface /> : <PendingSurface name={surface} />}
        </div>

        <StatusBar />
      </main>
    </div>
  );
}

function ChatSurface() {
  const turn = useStore((s) => s.activeTurn);
  const text = displayText(turn);

  if (turn === null) {
    return <EmptyState title="No active turn" hint="Press ⌘K to open the command palette" />;
  }

  return (
    <div className="mx-auto max-w-3xl p-6">
      <Panel className="p-4">
        <div className="mb-2 flex items-center gap-2 text-2xs text-fg-faint">
          <span className="font-mono">{turn.turnId}</span>
          {turn.register !== null && (
            <span className="rounded-xs bg-surface-input px-1.5 py-0.5">{turn.register}</span>
          )}
          {/* Visible so a long reply is explicable rather than surprising
              (DEC-033) — the register is why, and the user can see it. */}
        </div>
        <p className="selectable whitespace-pre-wrap text-sm leading-relaxed text-fg-primary">
          {text}
        </p>
        {turn.error !== null && <p className="mt-3 text-xs text-danger">{turn.error}</p>}
      </Panel>
    </div>
  );
}

function PendingSurface({ name }: { name: string }) {
  return <EmptyState title={`${name} is not built yet`} hint="Lands in a later phase" />;
}
