import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiClient, type ApiClientOptions, type ProjectPath } from '@/lib/api/client';

import { useProjectsStore } from './projects';

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

const PROJECT = {
  id: 'prj_1',
  name: 'MITTA',
  description: null,
  color: null,
  status: 'active',
  settings: {},
  path_count: 1,
  created_at: 0,
  updated_at: 0,
};

const PATH: ProjectPath = {
  project_id: 'prj_1',
  path: '/work/mitta',
  kind: 'root',
  writable: false,
  created_at: 0,
};

function route(url: string): { status?: number; body: unknown } {
  if (url.includes('/resolve-path')) {
    if (url.includes('.env')) {
      return {
        body: {
          path: '/work/mitta/.env',
          containment: 'excluded',
          matched_path: '/work/mitta/.env',
          project_id: 'prj_1',
          needs_confirmation: false,
          refused: true,
          explanation: '/work/mitta/.env is excluded.',
        },
      };
    }
    return {
      body: {
        path: '/work/mitta/x',
        containment: 'writable',
        matched_path: '/work/mitta',
        project_id: 'prj_1',
        needs_confirmation: false,
        refused: false,
        explanation: '/work/mitta/x is inside a writable project path.',
      },
    };
  }
  if (url.includes('/paths')) return { body: { paths: [PATH], project_id: 'prj_1' } };
  if (url.includes('/memory')) {
    return { body: { memories: [], total: 0, limit: 50, offset: 0 } };
  }
  if (url.includes('/v1/projects?')) return { body: { projects: [PROJECT], total: 1 } };
  return { body: PROJECT };
}

const initial = useProjectsStore.getState();

beforeEach(() => {
  useProjectsStore.setState(initial, true);
});

describe('attach', () => {
  it('loads projects as soon as a client arrives', async () => {
    const { client } = clientWith(route);

    useProjectsStore.getState().attach(client);
    await vi.waitFor(() => expect(useProjectsStore.getState().projects).toHaveLength(1));
  });

  it('does nothing without a client rather than throwing', async () => {
    await expect(useProjectsStore.getState().refresh()).resolves.toBeUndefined();
    await expect(useProjectsStore.getState().addPath('/x', 'root', true)).resolves.toBeUndefined();
    await expect(useProjectsStore.getState().probePath('/x')).resolves.toBeUndefined();
  });
});

describe('listing', () => {
  it('asks for active projects by default, not everything', async () => {
    const { client, calls } = clientWith(route);
    useProjectsStore.setState({ client });

    await useProjectsStore.getState().refresh();

    expect(calls.at(0)?.url).toContain('status=active');
  });

  it('uses the `all` literal for archived, not an empty parameter', async () => {
    // An empty `?status=` is a 422 on the server, not "no filter", so the third
    // state has to be named.
    const { client, calls } = clientWith(route);
    useProjectsStore.setState({ client, includeArchived: true });

    await useProjectsStore.getState().refresh();

    expect(calls.at(0)?.url).toContain('status=all');
  });
});

describe('paths', () => {
  it('re-reads from the server instead of appending the submitted path', async () => {
    // The server canonicalises, and re-adding an existing path updates it in
    // place. Appending locally would show the submitted spelling and a duplicate.
    const { client, calls } = clientWith(route);
    useProjectsStore.setState({ client, selectedId: 'prj_1' });

    await useProjectsStore.getState().addPath('~/work/mitta/..', 'root', true);

    expect(calls.some((call) => call.method === 'POST' && call.url.includes('/paths'))).toBe(true);
    expect(calls.some((call) => call.method === 'GET' && call.url.includes('/paths'))).toBe(true);
    expect(useProjectsStore.getState().paths).toEqual([PATH]);
  });

  it('flipping a write grant upserts rather than deleting first', async () => {
    // Delete-then-add would leave a window in which no rule covers the path at
    // all, and the boundary would read it as unknown.
    const { client, calls } = clientWith(route);
    useProjectsStore.setState({ client, selectedId: 'prj_1' });

    await useProjectsStore.getState().setPathWritable(PATH, true);

    expect(calls.some((call) => call.method === 'DELETE')).toBe(false);
    const post = calls.find((call) => call.method === 'POST');
    expect((post?.body as { writable: boolean }).writable).toBe(true);
  });

  it('sends the path as a query parameter when deregistering', async () => {
    const { client, calls } = clientWith(route);
    useProjectsStore.setState({ client, selectedId: 'prj_1' });

    await useProjectsStore.getState().removePath('/work/mitta');

    const deleted = calls.find((call) => call.method === 'DELETE');
    expect(deleted?.url).toContain('path=%2Fwork%2Fmitta');
  });

  it('reports a rejected grant instead of showing it as applied', async () => {
    const { client } = clientWith((url, init) =>
      init.method === 'POST' && url.includes('/paths')
        ? {
            status: 404,
            body: {
              error: {
                code: 'not_found.project',
                message: 'project not found',
                retryable: false,
                details: {},
                request_id: null,
              },
            },
          }
        : route(url),
    );
    useProjectsStore.setState({ client, selectedId: 'prj_gone' });

    await useProjectsStore.getState().addPath('/work', 'root', true);

    expect(useProjectsStore.getState().error).toBe('project not found');
    expect(useProjectsStore.getState().paths).toEqual([]);
  });
});

describe('selection', () => {
  it('drops a response for a project that is no longer selected', async () => {
    // Clicking faster than the requests return would otherwise put one
    // project's paths under another's name — a misstatement about a boundary.
    const { client } = clientWith(route);
    useProjectsStore.setState({ client });

    const pending = useProjectsStore.getState().select('prj_1');
    useProjectsStore.setState({ selectedId: 'prj_2' });
    await pending;

    expect(useProjectsStore.getState().paths).toEqual([]);
  });

  it('clears paths and memories when deselecting', async () => {
    const { client } = clientWith(route);
    useProjectsStore.setState({ client, selectedId: 'prj_1', paths: [PATH] });

    await useProjectsStore.getState().select(null);

    expect(useProjectsStore.getState().paths).toEqual([]);
  });
});

describe('boundary check', () => {
  it('reports the verdict and the server’s own explanation', async () => {
    const { client } = clientWith(route);
    useProjectsStore.setState({ client });

    await useProjectsStore.getState().probePath('/work/mitta/x');

    const probe = useProjectsStore.getState().probe;
    expect(probe?.containment).toBe('writable');
    expect(probe?.needs_confirmation).toBe(false);
    // Not reconstructed client-side: the engine and the UI cannot disagree
    // about a permission if only one of them writes the sentence.
    expect(probe?.explanation).toContain('writable project path');
  });

  it('an exclusion comes back refused, not confirmable', async () => {
    // The surface renders three states off these two flags. Both true, or both
    // false for an exclusion, is a UI offering a choice the engine will not
    // honour.
    const { client } = clientWith(route);
    useProjectsStore.setState({ client });

    await useProjectsStore.getState().probePath('/work/mitta/.env');

    const probe = useProjectsStore.getState().probe;
    expect(probe?.refused).toBe(true);
    expect(probe?.needs_confirmation).toBe(false);
  });

  it('an empty path clears the probe rather than querying for nothing', async () => {
    const { client, calls } = clientWith(route);
    useProjectsStore.setState({ client });

    await useProjectsStore.getState().probePath('   ');

    expect(useProjectsStore.getState().probe).toBeNull();
    expect(calls).toHaveLength(0);
  });
});
