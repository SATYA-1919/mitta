import { cx } from '@/components/ui/primitives';
import type { Surface } from '@/state/store';
import { useStore } from '@/state/store';

/**
 * Navigation is data, not markup. A `<nav>` full of hand-written `<button>`s
 * drifts the moment a surface is added — the keyboard shortcut, the label and
 * the ordering end up defined in three different places.
 */
const SURFACES: { id: Surface; label: string; hint: string }[] = [
  { id: 'chat', label: 'Chat', hint: '1' },
  { id: 'projects', label: 'Projects', hint: '2' },
  { id: 'memory', label: 'Memory', hint: '3' },
  { id: 'tasks', label: 'Tasks', hint: '4' },
  { id: 'plugins', label: 'Plugins', hint: '5' },
  { id: 'monitor', label: 'Monitor', hint: '6' },
  { id: 'history', label: 'History', hint: '7' },
  { id: 'settings', label: 'Settings', hint: ',' },
];

export function Sidebar() {
  const surface = useStore((s) => s.surface);
  const collapsed = useStore((s) => s.sidebarCollapsed);
  const setSurface = useStore((s) => s.setSurface);

  return (
    <nav
      aria-label="Primary"
      className={cx(
        'flex h-full shrink-0 flex-col border-r border-border-subtle bg-surface-sunken',
        'transition-[width] duration-[--duration-normal] ease-[--ease-out]',
        collapsed ? 'w-[52px]' : 'w-[--spacing-sidebar]',
      )}
    >
      <div className="drag-region flex h-[--spacing-titlebar] items-center px-4">
        {!collapsed && (
          <span className="font-mono text-xs tracking-[0.18em] text-fg-muted">MITTA</span>
        )}
      </div>

      <ul className="scrollable flex-1 space-y-0.5 px-2 py-2">
        {SURFACES.map((item) => {
          const active = surface === item.id;
          return (
            <li key={item.id}>
              <button
                type="button"
                aria-current={active ? 'page' : undefined}
                onClick={() => setSurface(item.id)}
                className={cx(
                  'group flex w-full items-center justify-between rounded-md px-2.5 py-1.5',
                  'text-sm transition-colors duration-[--duration-fast]',
                  active
                    ? 'bg-surface-active text-fg-primary'
                    : 'text-fg-muted hover:bg-surface-hover hover:text-fg-secondary',
                )}
              >
                <span className={cx('truncate', collapsed && 'sr-only')}>{item.label}</span>
                {!collapsed && (
                  <span className="font-mono text-2xs text-fg-faint opacity-0 transition-opacity group-hover:opacity-100">
                    {item.hint}
                  </span>
                )}
                {collapsed && <span aria-hidden>{item.label.charAt(0)}</span>}
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

export { SURFACES };
