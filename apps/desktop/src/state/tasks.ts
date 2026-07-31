/**
 * Tasks and schedules surface state.
 *
 * Server state, kept out of the main store slice for the reason `state/projects.ts`
 * gives: a cached copy treated as the truth is how a UI reports a permission the
 * sidecar does not hold. That applies here too — a `tool` schedule is a standing
 * authorisation, so a list showing one as enabled after the toggle failed is a
 * false statement about what MITTA will do at 08:00.
 *
 * **This surface polls, and that is a deliberate limitation rather than an
 * oversight.** A scheduled run starts with no socket frame behind it — the
 * WebSocket carries turns, and a turn is something a user began. Rather than
 * inventing a second push channel for one surface, the list refreshes on an
 * interval while it is open and stops when it is not. The cost is up to
 * `POLL_MS` of staleness on a running task; the alternative was a server-push
 * design that nothing else needs yet.
 */

import { create } from 'zustand';

import {
  type ApiClient,
  ApiError,
  type CreateScheduleRequest,
  type Plan,
  type Schedule,
  type Task,
} from '@/lib/api/client';

/** Slow enough to be free, fast enough that a finished run does not look stuck. */
export const POLL_MS = 4_000;

interface TasksState {
  client: ApiClient | null;

  schedules: Schedule[];
  /** False when the sidecar is not ticking — the times are then a promise
   *  nothing is keeping, and the surface says so rather than showing them. */
  schedulerRunning: boolean;

  tasks: Task[];
  plans: Plan[];
  activeOnly: boolean;

  loading: boolean;
  /** Set while a run started from this surface is in flight, so the button can
   *  say what it is doing instead of appearing to have done nothing. */
  runningId: string | null;
  error: string | null;

  attach: (client: ApiClient | null) => void;
  setActiveOnly: (activeOnly: boolean) => void;
  refresh: () => Promise<void>;

  create: (body: CreateScheduleRequest) => Promise<boolean>;
  setEnabled: (id: string, enabled: boolean) => Promise<void>;
  remove: (id: string) => Promise<void>;
  runNow: (id: string) => Promise<void>;

  cancel: (taskId: string) => Promise<void>;
  resume: (taskId: string) => Promise<void>;
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

export const useTasksStore = create<TasksState>((set, get) => ({
  client: null,

  schedules: [],
  schedulerRunning: false,
  tasks: [],
  plans: [],
  activeOnly: false,

  loading: false,
  runningId: null,
  error: null,

  attach: (client) => {
    set({ client });
    if (client !== null) void get().refresh();
  },

  setActiveOnly: (activeOnly) => {
    set({ activeOnly });
    void get().refresh();
  },

  refresh: async () => {
    const { client, activeOnly } = get();
    if (client === null) return;

    set({ loading: true });
    try {
      const [schedules, tasks] = await Promise.all([
        client.listSchedules(),
        client.listTasks(activeOnly),
      ]);
      set({
        schedules: schedules.schedules,
        schedulerRunning: schedules.scheduler_running,
        tasks: tasks.tasks,
        plans: tasks.plans,
        loading: false,
        error: null,
      });
    } catch (error) {
      set({ error: describe(error), loading: false });
    }
  },

  create: async (body) => {
    const { client } = get();
    if (client === null) return false;
    set({ error: null });
    try {
      await client.createSchedule(body);
      await get().refresh();
      return true;
    } catch (error) {
      // Returned rather than thrown so the form can stay open with what the
      // user typed. A 422 here is usually a cron typo, and clearing the field
      // they need to fix is the least helpful possible response.
      set({ error: describe(error) });
      return false;
    }
  },

  setEnabled: async (id, enabled) => {
    const { client } = get();
    if (client === null) return;
    try {
      await client.updateSchedule(id, { enabled });
      // Re-read rather than patching locally: the server recomputes the next
      // fire, and a row showing a time the server has cleared is the exact
      // false statement this store exists to avoid.
      await get().refresh();
    } catch (error) {
      set({ error: describe(error) });
    }
  },

  remove: async (id) => {
    const { client } = get();
    if (client === null) return;
    try {
      await client.deleteSchedule(id);
      await get().refresh();
    } catch (error) {
      set({ error: describe(error) });
    }
  },

  runNow: async (id) => {
    const { client } = get();
    if (client === null) return;
    set({ runningId: id, error: null });
    try {
      await client.runSchedule(id);
      await get().refresh();
    } catch (error) {
      set({ error: describe(error) });
    } finally {
      set({ runningId: null });
    }
  },

  cancel: async (taskId) => {
    const { client } = get();
    if (client === null) return;
    try {
      await client.cancelTask(taskId);
      await get().refresh();
    } catch (error) {
      set({ error: describe(error) });
    }
  },

  resume: async (taskId) => {
    const { client } = get();
    if (client === null) return;
    set({ runningId: taskId, error: null });
    try {
      await client.resumeTask(taskId);
      await get().refresh();
    } catch (error) {
      set({ error: describe(error) });
    } finally {
      set({ runningId: null });
    }
  },
}));
