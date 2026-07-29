/**
 * Memory explorer — the first surface where MITTA's memory is visible.
 *
 * Deliberately shows the machinery rather than hiding it: which index matched a
 * result, how many vectors are pending, whether semantic search is degraded.
 * R5 says anything the user cannot inspect they cannot trust, and a memory
 * system is precisely the component where that matters most.
 */

import { useEffect, useRef, useState } from 'react';

import { Button, cx, EmptyState, Kbd, Panel, StatusDot } from '@/components/ui/primitives';
import type { Memory, MemoryKind, MemoryStatus } from '@/lib/api/client';
import { useMemoryStore } from '@/state/memory';

const KINDS: { value: MemoryKind | 'all'; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'long_term', label: 'Facts' },
  { value: 'project', label: 'Project' },
  { value: 'episodic', label: 'Events' },
  { value: 'relationship', label: 'People' },
  { value: 'preference', label: 'Preferences' },
  { value: 'procedural', label: 'Workflows' },
];

const STATUSES: MemoryStatus[] = ['active', 'superseded', 'forgotten'];

export function MemorySurface() {
  const store = useMemoryStore();
  const { view, filters, memories, hits, stats, query, loading, error, semanticAvailable } = store;

  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (searchTimer.current !== null) clearTimeout(searchTimer.current);
    };
  }, []);

  function onQueryChange(next: string) {
    store.setQuery(next);
    if (searchTimer.current !== null) clearTimeout(searchTimer.current);
    // Debounced: search-as-you-type would otherwise fire a hybrid query and an
    // embedding pass on every keystroke.
    searchTimer.current = setTimeout(() => void store.search(next), 180);
  }

  const rows: { memory: Memory; vectorRank: number | null; keywordRank: number | null }[] =
    view === 'search'
      ? hits.map((hit) => ({
          memory: hit.memory,
          vectorRank: hit.vector_rank ?? null,
          keywordRank: hit.keyword_rank ?? null,
        }))
      : memories.map((memory) => ({ memory, vectorRank: null, keywordRank: null }));

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 space-y-3 border-b border-border-subtle p-4">
        <div className="flex items-center gap-2">
          <input
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder="Search memory…"
            aria-label="Search memory"
            className={cx(
              'flex-1 rounded-md border border-border-subtle bg-surface-input px-3 py-2',
              'text-sm text-fg-primary placeholder:text-fg-faint',
              'focus-visible:border-accent focus-visible:outline-none',
            )}
          />
          {view === 'search' && (
            <Button
              onClick={() => {
                store.setQuery('');
                void store.search('');
              }}
            >
              Clear
            </Button>
          )}
        </div>

        <CreateRow />

        <div className="flex flex-wrap items-center gap-1.5">
          {KINDS.map((kind) => (
            <FilterChip
              key={kind.value}
              active={filters.kind === kind.value}
              onClick={() => store.setFilters({ kind: kind.value })}
            >
              {kind.label}
            </FilterChip>
          ))}
          <span className="mx-1 h-4 w-px bg-border-subtle" />
          {STATUSES.map((status) => (
            <FilterChip
              key={status}
              active={filters.status === status}
              onClick={() => store.setFilters({ status })}
            >
              {status}
            </FilterChip>
          ))}
          <FilterChip
            active={filters.pinnedOnly}
            onClick={() => store.setFilters({ pinnedOnly: !filters.pinnedOnly })}
          >
            Pinned
          </FilterChip>
        </div>

        {view === 'search' && semanticAvailable === false && (
          <p className="text-2xs text-warning">
            Keyword only — nothing is indexed yet, so meaning-based matches are unavailable.
          </p>
        )}
        {error !== null && <p className="text-xs text-danger">{error}</p>}
      </div>

      <div className="scrollable min-h-0 flex-1 p-4">
        {rows.length === 0 ? (
          <EmptyState
            title={loading ? 'Loading…' : view === 'search' ? 'No matches' : 'No memories yet'}
            hint={
              view === 'search'
                ? 'Nothing scored above the relevance floor'
                : 'Add one above, or let MITTA learn as you talk'
            }
          />
        ) : (
          <ul className="space-y-2">
            {rows.map((row) => (
              <MemoryRow key={row.memory.id} {...row} />
            ))}
          </ul>
        )}
      </div>

      {stats !== null && <StatsBar />}
    </div>
  );
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cx(
        'rounded-full px-2.5 py-1 text-2xs capitalize transition-colors duration-[--duration-fast]',
        active
          ? 'bg-surface-active text-fg-primary'
          : 'text-fg-muted hover:bg-surface-hover hover:text-fg-secondary',
      )}
    >
      {children}
    </button>
  );
}

function CreateRow() {
  const create = useMemoryStore((s) => s.create);
  const [content, setContent] = useState('');
  const [kind, setKind] = useState<MemoryKind>('long_term');
  const [pinned, setPinned] = useState(false);

  async function submit() {
    const trimmed = content.trim();
    if (trimmed.length === 0) return;
    await create(trimmed, kind, pinned);
    setContent('');
    setPinned(false);
  }

  return (
    <div className="flex items-center gap-2">
      <input
        value={content}
        onChange={(event) => setContent(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') void submit();
        }}
        placeholder="Remember something…"
        aria-label="New memory"
        className={cx(
          'flex-1 rounded-md border border-border-subtle bg-surface-input px-3 py-1.5',
          'text-sm text-fg-primary placeholder:text-fg-faint',
          'focus-visible:border-accent focus-visible:outline-none',
        )}
      />
      <select
        value={kind}
        onChange={(event) => setKind(event.target.value as MemoryKind)}
        aria-label="Memory kind"
        className="rounded-md border border-border-subtle bg-surface-input px-2 py-1.5 text-xs text-fg-secondary"
      >
        {KINDS.filter((k) => k.value !== 'all').map((k) => (
          <option key={k.value} value={k.value}>
            {k.label}
          </option>
        ))}
      </select>
      <FilterChip active={pinned} onClick={() => setPinned(!pinned)}>
        Pin
      </FilterChip>
      <Button variant="primary" onClick={() => void submit()} disabled={content.trim() === ''}>
        Add
      </Button>
    </div>
  );
}

function MemoryRow({
  memory,
  vectorRank,
  keywordRank,
}: {
  memory: Memory;
  vectorRank: number | null;
  keywordRank: number | null;
}) {
  const store = useMemoryStore();
  const [confirmingPurge, setConfirmingPurge] = useState(false);

  return (
    <li>
      <Panel className="group p-3">
        <div className="flex items-start gap-3">
          <div className="min-w-0 flex-1">
            <p className="selectable text-sm leading-relaxed text-fg-primary">{memory.content}</p>
            <div className="mt-1.5 flex flex-wrap items-center gap-2 text-2xs text-fg-faint">
              <span className="rounded-xs bg-surface-input px-1.5 py-0.5">{memory.kind}</span>
              {memory.pinned && <span className="text-accent">pinned</span>}
              {memory.status !== 'active' && <span className="text-warning">{memory.status}</span>}
              <span>importance {memory.importance.toFixed(2)}</span>
              {memory.access_count > 0 && <span>recalled {memory.access_count}×</span>}
              {/* Which index matched. Retrieval stays inspectable rather than
                  being a black box the user has to take on faith. */}
              {vectorRank !== null && <span className="text-success">meaning #{vectorRank}</span>}
              {keywordRank !== null && <span className="text-accent">keyword #{keywordRank}</span>}
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
            <Button onClick={() => void store.setPinned(memory.id, !memory.pinned)}>
              {memory.pinned ? 'Unpin' : 'Pin'}
            </Button>
            {memory.status === 'forgotten' ? (
              <Button onClick={() => void store.restore(memory.id)}>Restore</Button>
            ) : (
              <Button onClick={() => void store.forget(memory.id)}>Forget</Button>
            )}
            {/* Two-step, because `purge` is the one irreversible operation in the
                whole engine (DEC-053). A single-click delete on a list row is
                how someone loses a memory they meant to keep. */}
            {confirmingPurge ? (
              <>
                <Button
                  variant="danger"
                  onClick={() => {
                    void store.purge(memory.id);
                    setConfirmingPurge(false);
                  }}
                >
                  Delete forever
                </Button>
                <Button onClick={() => setConfirmingPurge(false)}>Cancel</Button>
              </>
            ) : (
              <Button variant="danger" onClick={() => setConfirmingPurge(true)}>
                Delete
              </Button>
            )}
          </div>
        </div>
      </Panel>
    </li>
  );
}

function StatsBar() {
  const stats = useMemoryStore((s) => s.stats);
  const reindex = useMemoryStore((s) => s.reindex);
  const loading = useMemoryStore((s) => s.loading);
  if (stats === null) return null;

  return (
    <footer className="flex shrink-0 flex-wrap items-center gap-3 border-t border-border-subtle px-4 py-2 text-2xs text-fg-muted">
      <span>{stats.active} active</span>
      <span className="font-mono">
        {stats.vectors_indexed} indexed
        {stats.pending_embeddings > 0 && ` · ${stats.pending_embeddings} pending`}
      </span>

      {/* The engine reports degradation honestly rather than showing a spinner
          that implies work is happening when it cannot (DEC-052). */}
      {stats.embedding_degraded ? (
        <span className="flex items-center gap-1.5 text-warning">
          <StatusDot tone="warn" />
          fallback embeddings — run <Kbd>make download-model</Kbd> for semantic recall
        </span>
      ) : (
        <span className="flex items-center gap-1.5">
          <StatusDot tone="ok" />
          <span className="font-mono">{stats.embedding_model_id}</span>
        </span>
      )}

      {!stats.index_consistent && (
        <span className="flex items-center gap-1.5 text-danger">
          <StatusDot tone="error" />
          index out of sync
        </span>
      )}

      <div className="flex-1" />
      <Button onClick={() => void reindex()} disabled={loading}>
        Rebuild index
      </Button>
    </footer>
  );
}
