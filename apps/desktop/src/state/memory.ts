/**
 * Memory surface state.
 *
 * Kept separate from the main store slice on purpose: this is *server* state —
 * a cache of rows the sidecar owns — and conflating it with UI state is how a
 * stale copy ends up being treated as the truth. Every mutation here re-reads
 * from the server rather than patching the local list optimistically, because a
 * memory the user believes they pinned but did not is worse than a spinner.
 */

import { create } from 'zustand';

import {
  type ApiClient,
  ApiError,
  type Memory,
  type MemoryKind,
  type MemoryStats,
  type MemoryStatus,
  type SearchHit,
} from '@/lib/api/client';

export type MemoryView = 'browse' | 'search';

export interface MemoryFilters {
  kind: MemoryKind | 'all';
  status: MemoryStatus;
  pinnedOnly: boolean;
}

interface MemoryState {
  client: ApiClient | null;
  view: MemoryView;
  filters: MemoryFilters;

  memories: Memory[];
  total: number;
  hits: SearchHit[];
  /** Null when the last search was keyword-only or nothing was indexed. */
  semanticAvailable: boolean | null;
  stats: MemoryStats | null;

  query: string;
  selectedId: string | null;
  loading: boolean;
  error: string | null;

  attach: (client: ApiClient | null) => void;
  setView: (view: MemoryView) => void;
  setFilters: (patch: Partial<MemoryFilters>) => void;
  setQuery: (query: string) => void;
  select: (id: string | null) => void;

  refresh: () => Promise<void>;
  search: (query: string, options?: { recordAccess?: boolean }) => Promise<void>;
  create: (content: string, kind: MemoryKind, pinned: boolean) => Promise<void>;
  setPinned: (id: string, pinned: boolean) => Promise<void>;
  forget: (id: string) => Promise<void>;
  restore: (id: string) => Promise<void>;
  purge: (id: string) => Promise<void>;
  reindex: () => Promise<void>;
}

const DEFAULT_FILTERS: MemoryFilters = { kind: 'all', status: 'active', pinnedOnly: false };

function describe(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

export const useMemoryStore = create<MemoryState>((set, get) => ({
  client: null,
  view: 'browse',
  filters: DEFAULT_FILTERS,

  memories: [],
  total: 0,
  hits: [],
  semanticAvailable: null,
  stats: null,

  query: '',
  selectedId: null,
  loading: false,
  error: null,

  attach: (client) => {
    set({ client });
    if (client !== null) void get().refresh();
  },

  setView: (view) => set({ view }),

  setFilters: (patch) => {
    set({ filters: { ...get().filters, ...patch } });
    void get().refresh();
  },

  setQuery: (query) => set({ query }),
  select: (selectedId) => set({ selectedId }),

  refresh: async () => {
    const { client, filters } = get();
    if (client === null) return;

    set({ loading: true, error: null });
    try {
      const [list, stats] = await Promise.all([
        client.listMemories({
          ...(filters.kind === 'all' ? {} : { kind: filters.kind }),
          status: filters.status,
          pinnedOnly: filters.pinnedOnly,
          limit: 100,
        }),
        client.memoryStats(),
      ]);
      set({ memories: list.memories, total: list.total, stats, loading: false });
    } catch (error) {
      set({ error: describe(error), loading: false });
    }
  },

  search: async (query, options = {}) => {
    const { client } = get();
    if (client === null) return;

    const trimmed = query.trim();
    if (trimmed.length === 0) {
      set({ hits: [], semanticAvailable: null, view: 'browse' });
      return;
    }

    set({ loading: true, error: null, view: 'search' });
    try {
      const response = await client.searchMemories({
        query: trimmed,
        limit: 25,
        semantic: true,
        keyword: true,
        // Typing in the search box is not using a memory. Recording access here
        // would let idle browsing keep trivia alive at the expense of memories
        // that are genuinely consulted.
        record_access: options.recordAccess ?? false,
      });
      set({
        hits: response.hits,
        semanticAvailable: response.semantic_available,
        loading: false,
      });
    } catch (error) {
      set({ error: describe(error), loading: false, hits: [] });
    }
  },

  create: async (content, kind, pinned) => {
    const { client } = get();
    if (client === null) return;
    set({ error: null });
    try {
      await client.createMemory({ content, kind, pinned, importance: 0.5, confidence: 1.0 });
      await get().refresh();
    } catch (error) {
      set({ error: describe(error) });
    }
  },

  setPinned: async (id, pinned) => {
    await mutate(get, set, (client) => client.updateMemory(id, { pinned }));
  },

  forget: async (id) => {
    await mutate(get, set, (client) => client.forgetMemory(id));
  },

  restore: async (id) => {
    await mutate(get, set, (client) => client.restoreMemory(id));
  },

  purge: async (id) => {
    await mutate(get, set, (client) => client.purgeMemory(id));
    if (get().selectedId === id) set({ selectedId: null });
  },

  reindex: async () => {
    const { client } = get();
    if (client === null) return;
    set({ loading: true, error: null });
    try {
      const stats = await client.reindexMemories();
      set({ stats, loading: false });
      await get().refresh();
    } catch (error) {
      set({ error: describe(error), loading: false });
    }
  },
}));

/**
 * Run a mutation, then re-read. Never patches the local row optimistically:
 * the server applies rules the client does not know — a pinned memory refuses
 * to be forgotten, and a `forget` that silently no-ops would leave the UI
 * showing a state the database never entered.
 */
async function mutate(
  get: () => MemoryState,
  set: (patch: Partial<MemoryState>) => void,
  action: (client: ApiClient) => Promise<unknown>,
): Promise<void> {
  const { client } = get();
  if (client === null) return;
  set({ error: null });
  try {
    await action(client);
    await get().refresh();
    if (get().view === 'search') await get().search(get().query);
  } catch (error) {
    set({ error: describe(error) });
  }
}
