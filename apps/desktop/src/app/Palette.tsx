import { useEffect, useRef, useState } from 'react';

import { cx, Kbd } from '@/components/ui/primitives';

/**
 * Command palette — the Raycast-shaped surface.
 *
 * This module is imported only by `palette.tsx`, which is a separate Vite
 * entry. It must not import from `@/app/MainWindow`, `@/state/store` slices it
 * does not need, or any feature module: R2 budgets this window at well under
 * 100 ms to first paint, and that budget is won or lost at bundle-composition
 * time, not by optimisation later.
 */

interface Command {
  id: string;
  label: string;
  hint?: string;
}

const COMMANDS: Command[] = [
  { id: 'chat.new', label: 'New conversation', hint: '⌘N' },
  { id: 'memory.search', label: 'Search memory' },
  { id: 'projects.open', label: 'Open project' },
  { id: 'tasks.active', label: 'Show active tasks' },
  { id: 'settings.open', label: 'Settings', hint: '⌘,' },
];

export function Palette() {
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const results = COMMANDS.filter((command) =>
    command.label.toLowerCase().includes(query.toLowerCase().trim()),
  );

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    setSelected(0);
  }, [query]);

  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setSelected((index) => Math.min(index + 1, results.length - 1));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setSelected((index) => Math.max(index - 1, 0));
    } else if (event.key === 'Escape') {
      event.preventDefault();
      // Hiding is the Rust shell's job (Channel B). Wired in Phase 4b.
      window.close();
    }
  }

  return (
    <div className="flex h-full items-start justify-center bg-transparent p-3">
      <div
        role="dialog"
        aria-label="Command palette"
        className="glass w-full overflow-hidden rounded-xl shadow-[--shadow-overlay]"
      >
        <input
          ref={inputRef}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Ask MITTA, or run a command…"
          aria-label="Command input"
          className={cx(
            'w-full bg-transparent px-4 py-3.5 text-lg text-fg-primary',
            'placeholder:text-fg-faint focus-visible:outline-none',
          )}
        />

        {results.length > 0 && (
          <ul className="scrollable max-h-80 border-t border-border-subtle p-1.5">
            {results.map((command, index) => (
              <li key={command.id}>
                <button
                  type="button"
                  onMouseEnter={() => setSelected(index)}
                  aria-selected={index === selected}
                  className={cx(
                    'flex w-full items-center justify-between rounded-md px-2.5 py-2 text-sm',
                    'transition-colors duration-[--duration-instant]',
                    index === selected
                      ? 'bg-surface-active text-fg-primary'
                      : 'text-fg-secondary hover:bg-surface-hover',
                  )}
                >
                  <span>{command.label}</span>
                  {command.hint !== undefined && <Kbd>{command.hint}</Kbd>}
                </button>
              </li>
            ))}
          </ul>
        )}

        {results.length === 0 && query.trim().length > 0 && (
          <div className="border-t border-border-subtle px-4 py-3 text-sm text-fg-muted">
            Press <Kbd>↵</Kbd> to send “{query.trim()}” to MITTA
          </div>
        )}
      </div>
    </div>
  );
}
