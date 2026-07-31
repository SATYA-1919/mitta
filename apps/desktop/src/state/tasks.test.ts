import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiClient, type ApiClientOptions, type Schedule, type Task } from '@/lib/api/client';

import { useTasksStore } from './tasks';

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

const SCHEDULE: Schedule = {
  id: 'sch_1',
  name: 'Morning briefing',
  cron: '0 8 * * *',
  timezone: 'Europe/London',
  action: { kind: 'prompt', text: 'what happened overnight' },
  enabled: true,
  last_run_at: null,
  next_run_at: 1_780_300_800,
  created_at: 0,
  summary: 'daily at 08:00',
  next_run_local: '2026-06-02T08:00+01:00',
};

const TASK: Task = {
  id: 'tsk_1',
  plan_id: 'pln_1',
  parent_id: null,
  ordinal: 0,
  title: 'web_search',
  description: null,
  tool_name: 'web_search',
  params: {},
  status: 'failed',
  attempt: 1,
  max_attempts: 3,
  result: null,
  error: { code: 'tool.failed', message: 'no network' },
  started_at: 0,
  ended_at: 1,
  created_at: 0,
  updated_at: 1,
  resumable: true,
};

const PLAN = {
  id: 'pln_1',
  goal: 'Morning briefing',
  status: 'failed',
  project_id: null,
  conversation_id: null,
  created_at: 0,
  updated_at: 1,
};

function route(url: string): { status?: number; body: unknown } {
  if (url.includes('/v1/schedules')) {
    return { body: { schedules: [SCHEDULE], total: 1, scheduler_running: true } };
  }
  if (url.includes('/v1/tasks')) {
    return { body: { tasks: [TASK], plans: [PLAN], total: 1 } };
  }
  return { body: {} };
}

const initial = useTasksStore.getState();

beforeEach(() => {
  useTasksStore.setState(initial, true);
});

describe('attach', () => {
  it('loads schedules and runs as soon as a client arrives', async () => {
    const { client } = clientWith(route);

    useTasksStore.getState().attach(client);

    await vi.waitFor(() => expect(useTasksStore.getState().schedules).toHaveLength(1));
    expect(useTasksStore.getState().tasks).toHaveLength(1);
    expect(useTasksStore.getState().schedulerRunning).toBe(true);
  });

  it('does nothing without a client rather than throwing', async () => {
    await expect(useTasksStore.getState().refresh()).resolves.toBeUndefined();
    await expect(useTasksStore.getState().cancel('tsk_1')).resolves.toBeUndefined();
    await expect(useTasksStore.getState().runNow('sch_1')).resolves.toBeUndefined();
  });
});

describe('scheduler status', () => {
  it('is carried from the server rather than assumed', async () => {
    /* A list of automations that cannot fire has to say so — the times beside
       them are otherwise a promise nothing is keeping. */
    const { client } = clientWith((url) =>
      url.includes('/v1/schedules')
        ? { body: { schedules: [SCHEDULE], total: 1, scheduler_running: false } }
        : route(url),
    );

    useTasksStore.getState().attach(client);

    await vi.waitFor(() => expect(useTasksStore.getState().schedules).toHaveLength(1));
    expect(useTasksStore.getState().schedulerRunning).toBe(false);
  });
});

describe('creating', () => {
  it('re-reads instead of appending what it sent', async () => {
    /* The server computes `next_run_at` and the local rendering of it. A row
       built from the request body would show neither. */
    const { client, calls } = clientWith(route);
    useTasksStore.setState({ client });

    const created = await useTasksStore.getState().create({
      name: 'Briefing',
      cron: '0 8 * * *',
      timezone: 'Europe/London',
      action: { kind: 'prompt', text: 'news' },
      enabled: true,
    });

    expect(created).toBe(true);
    expect(calls.some((call) => call.method === 'POST' && call.url.endsWith('/v1/schedules'))).toBe(
      true,
    );
    expect(useTasksStore.getState().schedules[0]?.next_run_local).toBe('2026-06-02T08:00+01:00');
  });

  it('reports a rejected expression without clearing the form', async () => {
    /* A 422 here is usually a cron typo, and returning `false` is what lets the
       form stay open on the field the user has to fix. */
    const { client } = clientWith((url) =>
      url.includes('/v1/schedules')
        ? {
            status: 422,
            body: {
              error: {
                code: 'validation.failed',
                message: 'expected 5 cron fields',
                retryable: false,
                details: {},
                request_id: null,
              },
            },
          }
        : route(url),
    );
    useTasksStore.setState({ client });

    const created = await useTasksStore.getState().create({
      name: 'Briefing',
      cron: 'every morning',
      timezone: 'UTC',
      action: { kind: 'prompt', text: 'news' },
      enabled: true,
    });

    expect(created).toBe(false);
    expect(useTasksStore.getState().error).toContain('cron');
  });
});

describe('toggling', () => {
  it('re-reads rather than patching the row locally', async () => {
    /* Disabling clears the next fire server-side. A locally patched row would
       keep showing a time the server has already withdrawn. */
    const { client, calls } = clientWith((url) => {
      if (url.includes('/v1/schedules/sch_1')) return { body: SCHEDULE };
      return route(url);
    });
    useTasksStore.setState({ client });

    await useTasksStore.getState().setEnabled('sch_1', false);

    expect(calls.filter((call) => call.method === 'PATCH')).toHaveLength(1);
    expect(calls.filter((call) => call.method === 'GET' && call.url.includes('/v1/schedules')))
      .not.toHaveLength(0);
  });
});

describe('runs', () => {
  it('marks which one is in flight so the button can say so', async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });

    const { client } = clientWith(route);
    const runSchedule = vi.spyOn(client, 'runSchedule').mockImplementation(async () => {
      await gate;
      return { plan: PLAN, tasks: [] } as never;
    });
    useTasksStore.setState({ client });

    const pending = useTasksStore.getState().runNow('sch_1');
    await vi.waitFor(() => expect(useTasksStore.getState().runningId).toBe('sch_1'));

    release();
    await pending;

    expect(useTasksStore.getState().runningId).toBeNull();
    expect(runSchedule).toHaveBeenCalledWith('sch_1');
  });

  it('clears the in-flight marker when a retry fails', async () => {
    const { client } = clientWith((url) =>
      url.includes('/resume')
        ? {
            status: 409,
            body: {
              error: {
                code: 'conflict.state',
                message: 'nothing to resume',
                retryable: false,
                details: {},
                request_id: null,
              },
            },
          }
        : route(url),
    );
    useTasksStore.setState({ client });

    await useTasksStore.getState().resume('tsk_1');

    expect(useTasksStore.getState().runningId).toBeNull();
    expect(useTasksStore.getState().error).toBe('nothing to resume');
  });
});
