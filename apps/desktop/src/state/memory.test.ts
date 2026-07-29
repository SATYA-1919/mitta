import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiClient, type ApiClientOptions } from '@/lib/api/client';

import { useMemoryStore } from './memory';

/** Builds a client backed by a scripted fetch, so no network is involved. */
function clientWith(
  handler: (url: string, init: RequestInit) => { status?: number; body: unknown },
): { client: ApiClient; calls: { url: string; method: string; body: unknown }[] } {
  const calls: { url: string; method: string; body: unknown }[] = [];

  const fetchImpl = ((input: string, init: RequestInit = {}) => {
    calls.push({
      url: input,
      method: init.method ?? 'GET',
      body: typeof init.body === 'string' ? JSON.parse(init.body) : undefined,
    });
    const { status = 200, body } = handler(input, init);
    return Promise.resolve(
      new Response(body === null ? '' : JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  }) as unknown as NonNullable<ApiClientOptions['fetch']>;

  return {
    client: new ApiClient({ baseUrl: 'http://127.0.0.1:1', token: 't', fetch: fetchImpl }),
    calls,
  };
}

const MEMORY = {
  id: 'mem_1',
  kind: 'long_term',
  project_id: null,
  content: 'Satya prefers dark mode',
  summary: null,
  attributes: {},
  importance: 0.5,
  confidence: 1,
  status: 'active',
  superseded_by: null,
  source_kind: 'user',
  pinned: false,
  access_count: 0,
  last_accessed_at: null,
  expires_at: null,
  created_at: 0,
  updated_at: 0,
};

const STATS = {
  active: 1,
  total: 1,
  index_name: 'memories',
  model_id: 'm',
  dim: 384,
  vectors_indexed: 1,
  pending_embeddings: 0,
  index_consistent: true,
  embedding_degraded: false,
  embedding_model_id: 'BAAI/bge-small-en-v1.5',
  embedding_model_downloaded: true,
};

function route(url: string): { status?: number; body: unknown } {
  if (url.includes('/stats')) return { body: STATS };
  if (url.includes('/search')) {
    return {
      body: {
        query: 'q',
        hits: [{ memory: MEMORY, score: 0.5, vector_rank: 1, keyword_rank: null, matched_both: false }],
        semantic_available: true,
      },
    };
  }
  if (url.endsWith('/v1/memory') || url.includes('/v1/memory?')) {
    return { body: { memories: [MEMORY], total: 1, limit: 100, offset: 0 } };
  }
  return { body: MEMORY };
}

const initial = useMemoryStore.getState();

beforeEach(() => {
  useMemoryStore.setState(initial, true);
});

describe('attach', () => {
  it('loads memories and stats as soon as a client arrives', async () => {
    const { client } = clientWith(route);

    useMemoryStore.getState().attach(client);
    await vi.waitFor(() => expect(useMemoryStore.getState().memories).toHaveLength(1));

    expect(useMemoryStore.getState().stats?.index_consistent).toBe(true);
  });

  it('detaching stops it holding a dead client', () => {
    const { client } = clientWith(route);
    useMemoryStore.getState().attach(client);
    useMemoryStore.getState().attach(null);

    expect(useMemoryStore.getState().client).toBeNull();
  });

  it('does nothing without a client rather than throwing', async () => {
    await expect(useMemoryStore.getState().refresh()).resolves.toBeUndefined();
    await expect(useMemoryStore.getState().search('x')).resolves.toBeUndefined();
  });
});

describe('search', () => {
  it('does not record access for search-as-you-type', async () => {
    // Idle browsing must not keep trivia alive at the expense of memories that
    // are genuinely consulted.
    const { client, calls } = clientWith(route);
    useMemoryStore.setState({ client });

    await useMemoryStore.getState().search('dark mode');

    const search = calls.find((call) => call.url.includes('/search'));
    expect((search?.body as { record_access: boolean }).record_access).toBe(false);
  });

  it('records access when explicitly asked', async () => {
    const { client, calls } = clientWith(route);
    useMemoryStore.setState({ client });

    await useMemoryStore.getState().search('dark mode', { recordAccess: true });

    const search = calls.find((call) => call.url.includes('/search'));
    expect((search?.body as { record_access: boolean }).record_access).toBe(true);
  });

  it('an empty query returns to browsing rather than searching for nothing', async () => {
    const { client, calls } = clientWith(route);
    useMemoryStore.setState({ client, view: 'search', hits: [] });

    await useMemoryStore.getState().search('   ');

    expect(useMemoryStore.getState().view).toBe('browse');
    expect(calls.some((call) => call.url.includes('/search'))).toBe(false);
  });

  it('surfaces the server admitting semantic search is unavailable', async () => {
    const { client } = clientWith((url) =>
      url.includes('/search')
        ? { body: { query: 'q', hits: [], semantic_available: false } }
        : route(url),
    );
    useMemoryStore.setState({ client });

    await useMemoryStore.getState().search('anything');

    expect(useMemoryStore.getState().semanticAvailable).toBe(false);
  });
});

describe('mutations', () => {
  it('re-reads from the server instead of patching locally', async () => {
    // The server applies rules the client does not know — a pinned memory
    // refuses to be forgotten — so a local patch can show a state the database
    // never entered.
    const { client, calls } = clientWith(route);
    useMemoryStore.setState({ client });

    await useMemoryStore.getState().forget('mem_1');

    expect(calls.map((c) => c.url).filter((u) => u.includes('/forget'))).toHaveLength(1);
    expect(calls.some((c) => c.method === 'GET' && c.url.includes('/v1/memory?'))).toBe(true);
  });

  it('reports a failed mutation instead of silently dropping it', async () => {
    const { client } = clientWith((url) =>
      url.includes('/forget')
        ? {
            status: 404,
            body: {
              error: {
                code: 'not_found.memory',
                message: 'memory not found',
                retryable: false,
                details: {},
                request_id: null,
              },
            },
          }
        : route(url),
    );
    useMemoryStore.setState({ client });

    await useMemoryStore.getState().forget('mem_gone');

    expect(useMemoryStore.getState().error).toBe('memory not found');
  });

  it('clears the selection when the selected memory is purged', async () => {
    const { client } = clientWith((url) => (url.includes('mem_1') ? { body: null } : route(url)));
    useMemoryStore.setState({ client, selectedId: 'mem_1' });

    await useMemoryStore.getState().purge('mem_1');

    expect(useMemoryStore.getState().selectedId).toBeNull();
  });
});

describe('filters', () => {
  it('sends the active filters to the server', async () => {
    const { client, calls } = clientWith(route);
    useMemoryStore.setState({ client });

    useMemoryStore.getState().setFilters({ kind: 'preference', pinnedOnly: true });
    await vi.waitFor(() =>
      expect(calls.some((call) => call.url.includes('kind=preference'))).toBe(true),
    );

    const listed = calls.find((call) => call.url.includes('kind=preference'));
    expect(listed?.url).toContain('pinned_only=true');
  });

  it('omits the kind parameter entirely when browsing everything', async () => {
    const { client, calls } = clientWith(route);
    useMemoryStore.setState({ client });

    await useMemoryStore.getState().refresh();

    const listed = calls.find((call) => call.url.includes('/v1/memory?'));
    expect(listed?.url).not.toContain('kind=');
  });
});
