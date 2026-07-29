/**
 * Projects surface state.
 *
 * Server state, kept out of the main store slice for the same reason
 * `state/memory.ts` is — a cached copy treated as the truth is how a UI reports
 * a permission the sidecar does not actually hold.
 *
 * That risk is sharper here than it is for memory. Every mutation re-reads from
 * the server rather than patching locally, because the rows in `paths` are a
 * security boundary: a list showing a path as writable when the write grant
 * failed is not a stale list, it is a false statement about what MITTA is
 * allowed to do.
 */

import { create } from 'zustand';

import {
  type ApiClient,
  ApiError,
  type Memory,
  type PathKind,
  type PathResolution,
  type Project,
  type ProjectPath,
} from '@/lib/api/client';

interface ProjectsState {
  client: ApiClient | null;

  projects: Project[];
  includeArchived: boolean;

  selectedId: string | null;
  /** Paths and memories for the selected project only. Loading every project's
   *  paths up front would be a request per row for data nothing displays. */
  paths: ProjectPath[];
  memories: Memory[];

  /** The last `resolve-path` answer, for the boundary checker. */
  probe: PathResolution | null;

  loading: boolean;
  error: string | null;

  attach: (client: ApiClient | null) => void;
  setIncludeArchived: (includeArchived: boolean) => void;
  select: (id: string | null) => Promise<void>;

  refresh: () => Promise<void>;
  create: (name: string) => Promise<void>;
  setArchived: (id: string, archived: boolean) => Promise<void>;
  remove: (id: string) => Promise<void>;

  addPath: (path: string, kind: PathKind, writable: boolean) => Promise<void>;
  removePath: (path: string) => Promise<void>;
  setPathWritable: (path: ProjectPath, writable: boolean) => Promise<void>;

  probePath: (path: string) => Promise<void>;
  clearProbe: () => void;
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

export const useProjectsStore = create<ProjectsState>((set, get) => ({
  client: null,

  projects: [],
  includeArchived: false,

  selectedId: null,
  paths: [],
  memories: [],
  probe: null,

  loading: false,
  error: null,

  attach: (client) => {
    set({ client });
    if (client !== null) void get().refresh();
  },

  setIncludeArchived: (includeArchived) => {
    set({ includeArchived });
    void get().refresh();
  },

  select: async (id) => {
    set({ selectedId: id, paths: [], memories: [] });
    if (id === null) return;

    const { client } = get();
    if (client === null) return;

    set({ loading: true, error: null });
    try {
      const [paths, memories] = await Promise.all([
        client.projectPaths(id),
        client.projectMemories(id),
      ]);
      // Guard against a stale response. Clicking through the list faster than
      // the requests return would otherwise land project A's paths under
      // project B's name — and here that is a misstatement about a boundary.
      if (get().selectedId !== id) return;
      set({ paths: paths.paths, memories: memories.memories, loading: false });
    } catch (error) {
      if (get().selectedId !== id) return;
      set({ error: describe(error), loading: false });
    }
  },

  refresh: async () => {
    const { client, includeArchived } = get();
    if (client === null) return;

    set({ loading: true, error: null });
    try {
      const list = await client.listProjects(includeArchived);
      set({ projects: list.projects, loading: false });
    } catch (error) {
      set({ error: describe(error), loading: false });
    }
  },

  create: async (name) => {
    const { client } = get();
    if (client === null) return;
    set({ loading: true, error: null });
    try {
      const project = await client.createProject({ name, description: null, color: null });
      await get().refresh();
      await get().select(project.id);
    } catch (error) {
      set({ error: describe(error), loading: false });
    }
  },

  setArchived: async (id, archived) => {
    const { client } = get();
    if (client === null) return;
    try {
      await client.updateProject(id, { status: archived ? 'archived' : 'active' });
      await get().refresh();
    } catch (error) {
      set({ error: describe(error) });
    }
  },

  remove: async (id) => {
    const { client } = get();
    if (client === null) return;
    try {
      await client.deleteProject(id);
      if (get().selectedId === id) await get().select(null);
      await get().refresh();
    } catch (error) {
      set({ error: describe(error) });
    }
  },

  addPath: async (path, kind, writable) => {
    const { client, selectedId } = get();
    if (client === null || selectedId === null) return;
    set({ error: null });
    try {
      await client.addProjectPath(selectedId, { path, kind, writable });
      // Re-read rather than appending the response. The server canonicalises the
      // path, and re-adding an existing one updates it in place — appending
      // would show the submitted spelling and a duplicate row.
      await get().select(selectedId);
      await get().refresh();
    } catch (error) {
      set({ error: describe(error) });
    }
  },

  removePath: async (path) => {
    const { client, selectedId } = get();
    if (client === null || selectedId === null) return;
    try {
      await client.removeProjectPath(selectedId, path);
      await get().select(selectedId);
      await get().refresh();
    } catch (error) {
      set({ error: describe(error) });
    }
  },

  setPathWritable: async (path, writable) => {
    // `POST` upserts on (project, path), so flipping the grant is the same call
    // as adding it. Delete-then-add would leave a window with no rule at all.
    await get().addPath(path.path, path.kind, writable);
  },

  probePath: async (path) => {
    const { client } = get();
    if (client === null) return;
    const trimmed = path.trim();
    if (trimmed.length === 0) {
      set({ probe: null });
      return;
    }
    try {
      set({ probe: await client.resolvePath(trimmed) });
    } catch (error) {
      set({ probe: null, error: describe(error) });
    }
  },

  clearProbe: () => set({ probe: null }),
}));
