/**
 * Projects — scope, and the write boundary.
 *
 * Two panes: the projects themselves on the left, and the selected project's
 * registered paths and scoped memory on the right.
 *
 * The paths pane is the reason this surface is worth building. `project_paths`
 * is what the policy engine consults before a filesystem action, so this is a
 * permissions editor, and it is styled as one:
 *
 * - `writable` is a switch that is off by default and never inferred. Adding a
 *   path widens what MITTA can see; granting write is a second, separate act.
 * - An `excluded` path is shown in the danger tone, above the rest, because it
 *   is the only rule that refuses outright.
 * - Removing a path is two-step, like purging a memory (DEC-053). It is the one
 *   action here that silently *widens* what MITTA will ask about later.
 * - The checker at the bottom answers "what would MITTA do with this path"
 *   against the real engine query, not a re-implementation of it. R5: a boundary
 *   the user can only observe through a confirmation card at the moment of
 *   action is not one they can audit.
 */

import { useEffect, useState } from 'react';

import { HudPanel, HudRule } from '@/components/ui/hud';
import { Button, cx, EmptyState, Panel, StatusDot } from '@/components/ui/primitives';
import type { Containment, PathKind, Project, ProjectPath } from '@/lib/api/client';
import { useProjectsStore } from '@/state/projects';

export function ProjectsSurface() {
  const { projects, selectedId, includeArchived, loading, error } = useProjectsStore();
  const selected = projects.find((project) => project.id === selectedId) ?? null;

  return (
    <div className="flex h-full min-h-0">
      <section className="flex w-[280px] min-w-0 shrink-0 flex-col border-r border-border-subtle">
        <div className="shrink-0 space-y-2 border-b border-border-subtle p-3">
          <CreateRow />
          <label className="flex items-center gap-2 text-2xs text-fg-muted">
            <input
              type="checkbox"
              checked={includeArchived}
              onChange={(event) =>
                useProjectsStore.getState().setIncludeArchived(event.target.checked)
              }
              className="accent-[var(--color-accent)]"
            />
            Show archived
          </label>
        </div>

        <div className="scrollable min-h-0 flex-1 p-2">
          {projects.length === 0 ? (
            <EmptyState
              title={loading ? 'Loading…' : 'No projects'}
              hint="A project scopes memory, conversations and file access"
            />
          ) : (
            <ul className="space-y-1">
              {projects.map((project) => (
                <ProjectRow
                  key={project.id}
                  project={project}
                  active={project.id === selectedId}
                />
              ))}
            </ul>
          )}
        </div>

        {error !== null && (
          <p className="shrink-0 border-t border-border-subtle px-3 py-2 text-xs text-danger">
            {error}
          </p>
        )}
      </section>

      <div className="scrollable min-h-0 flex-1 space-y-3 p-4">
        {selected === null ? (
          <EmptyState
            title="No project selected"
            hint="Pick one to see its paths and scoped memory"
          />
        ) : (
          <>
            <PathsPane project={selected} />
            <ScopedMemoryPane />
          </>
        )}
        <BoundaryChecker />
      </div>
    </div>
  );
}

function CreateRow() {
  const create = useProjectsStore((s) => s.create);
  const [name, setName] = useState('');

  async function submit() {
    const trimmed = name.trim();
    if (trimmed.length === 0) return;
    setName('');
    await create(trimmed);
  }

  return (
    <div className="flex items-center gap-2">
      <input
        value={name}
        onChange={(event) => setName(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') void submit();
        }}
        placeholder="New project…"
        aria-label="New project name"
        className={cx(
          'min-w-0 flex-1 rounded-xs border border-border-subtle bg-surface-input px-2.5 py-1.5',
          'text-sm text-fg-primary placeholder:text-fg-faint',
          'focus-visible:border-accent focus-visible:outline-none',
        )}
      />
      <Button variant="primary" onClick={() => void submit()} disabled={name.trim() === ''}>
        Add
      </Button>
    </div>
  );
}

function ProjectRow({ project, active }: { project: Project; active: boolean }) {
  const store = useProjectsStore();
  const archived = project.status === 'archived';

  return (
    <li>
      <button
        type="button"
        onClick={() => void store.select(active ? null : project.id)}
        aria-current={active ? 'true' : undefined}
        className={cx(
          'group w-full rounded-xs px-2.5 py-2 text-left transition-colors duration-[--duration-fast]',
          active ? 'bg-surface-active' : 'hover:bg-surface-hover',
        )}
      >
        <span className="flex items-center gap-2">
          <span
            className={cx(
              'size-1.5 shrink-0 rounded-full',
              archived ? 'bg-fg-faint' : 'bg-accent',
            )}
          />
          <span
            className={cx(
              'min-w-0 flex-1 truncate text-sm',
              archived ? 'text-fg-muted' : 'text-fg-primary',
            )}
          >
            {project.name}
          </span>
          <span className="readout shrink-0 text-2xs text-fg-faint">{project.path_count}</span>
        </span>
        {archived && (
          // Not cosmetic. An archived project's paths stop granting access, so
          // the label has to say what it costs, not just that it is filed away.
          <span className="mt-1 block text-2xs text-warning">
            archived — path grants withdrawn
          </span>
        )}
      </button>
    </li>
  );
}

function PathsPane({ project }: { project: Project }) {
  const { paths } = useProjectsStore();
  const store = useProjectsStore();
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  // Exclusions first: they are the only rule that refuses outright, and burying
  // one among a dozen roots is how a user forgets it is there.
  const ordered = [...paths].sort((a, b) => {
    if (a.kind === 'excluded' && b.kind !== 'excluded') return -1;
    if (b.kind === 'excluded' && a.kind !== 'excluded') return 1;
    return a.path.localeCompare(b.path);
  });

  return (
    <HudPanel
      label={`PATHS · ${project.name}`}
      right={
        <div className="flex items-center gap-1">
          <Button
            onClick={() =>
              void store.setArchived(project.id, project.status !== 'archived')
            }
          >
            {project.status === 'archived' ? 'Unarchive' : 'Archive'}
          </Button>
          {confirmingDelete ? (
            <>
              <Button
                variant="danger"
                onClick={() => {
                  void store.remove(project.id);
                  setConfirmingDelete(false);
                }}
              >
                Delete forever
              </Button>
              <Button onClick={() => setConfirmingDelete(false)}>Cancel</Button>
            </>
          ) : (
            <Button variant="danger" onClick={() => setConfirmingDelete(true)}>
              Delete
            </Button>
          )}
        </div>
      }
    >
      <p className="mb-3 text-2xs leading-relaxed text-fg-muted">
        MITTA resolves every filesystem action against these. Outside all of them it asks;
        inside an excluded one it refuses. Write access is granted per path and off by default.
      </p>

      <AddPathRow />

      {ordered.length === 0 ? (
        <p className="mt-3 text-xs text-fg-faint">
          No paths registered — MITTA will ask before touching anything on disk.
        </p>
      ) : (
        <ul className="mt-3 space-y-1.5">
          {ordered.map((entry) => (
            <PathRow key={entry.path} entry={entry} />
          ))}
        </ul>
      )}
    </HudPanel>
  );
}

const KINDS: PathKind[] = ['root', 'repo', 'docs', 'excluded'];

function AddPathRow() {
  const addPath = useProjectsStore((s) => s.addPath);
  const [path, setPath] = useState('');
  const [kind, setKind] = useState<PathKind>('root');
  const [writable, setWritable] = useState(false);

  const excluded = kind === 'excluded';

  async function submit() {
    const trimmed = path.trim();
    if (trimmed.length === 0) return;
    setPath('');
    setWritable(false);
    await addPath(trimmed, kind, excluded ? false : writable);
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <input
        value={path}
        onChange={(event) => setPath(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') void submit();
        }}
        placeholder="/Users/you/work/project — or ~/work/project"
        aria-label="Path to register"
        className={cx(
          'min-w-0 flex-1 rounded-xs border border-border-subtle bg-surface-input px-2.5 py-1.5',
          'font-mono text-xs text-fg-primary placeholder:text-fg-faint',
          'focus-visible:border-accent focus-visible:outline-none',
        )}
      />
      <select
        value={kind}
        onChange={(event) => setKind(event.target.value as PathKind)}
        aria-label="Path kind"
        className="rounded-xs border border-border-subtle bg-surface-input px-2 py-1.5 text-xs text-fg-secondary"
      >
        {KINDS.map((value) => (
          <option key={value} value={value}>
            {value}
          </option>
        ))}
      </select>
      {/* Hidden for an exclusion rather than disabled: "writable exclusion" is
          not a state the boundary has, and offering it greyed out invites the
          question of what it would mean. */}
      {!excluded && (
        <label className="flex items-center gap-1.5 text-2xs text-fg-muted">
          <input
            type="checkbox"
            checked={writable}
            onChange={(event) => setWritable(event.target.checked)}
            className="accent-[var(--color-accent)]"
          />
          writable
        </label>
      )}
      <Button variant="primary" onClick={() => void submit()} disabled={path.trim() === ''}>
        Register
      </Button>
    </div>
  );
}

function PathRow({ entry }: { entry: ProjectPath }) {
  const store = useProjectsStore();
  const [confirming, setConfirming] = useState(false);
  const excluded = entry.kind === 'excluded';

  return (
    <li>
      <Panel
        className={cx(
          'group flex items-center gap-2 p-2',
          excluded && 'border-danger/40 bg-danger/[0.04]',
        )}
      >
        <StatusDot tone={excluded ? 'error' : entry.writable ? 'warn' : 'ok'} />
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-fg-primary">
          {entry.path}
        </span>
        <span
          className={cx(
            'shrink-0 rounded-xs px-1.5 py-0.5 text-2xs',
            excluded ? 'bg-danger/15 text-danger' : 'bg-surface-input text-fg-muted',
          )}
        >
          {entry.kind}
        </span>

        {!excluded && (
          <label className="flex shrink-0 items-center gap-1.5 text-2xs text-fg-muted">
            <input
              type="checkbox"
              checked={entry.writable}
              onChange={(event) =>
                void store.setPathWritable(entry, event.target.checked)
              }
              aria-label={`Allow writes to ${entry.path}`}
              className="accent-[var(--color-accent)]"
            />
            write
          </label>
        )}

        <span className="flex shrink-0 items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
          {/* Two-step. Removing a rule is the one action here that widens what
              MITTA will do without asking about it again. */}
          {confirming ? (
            <>
              <Button
                variant="danger"
                onClick={() => {
                  void store.removePath(entry.path);
                  setConfirming(false);
                }}
              >
                Remove
              </Button>
              <Button onClick={() => setConfirming(false)}>Cancel</Button>
            </>
          ) : (
            <Button onClick={() => setConfirming(true)}>Deregister</Button>
          )}
        </span>
      </Panel>
    </li>
  );
}

function ScopedMemoryPane() {
  const memories = useProjectsStore((s) => s.memories);

  return (
    <HudPanel label={`SCOPED MEMORY · ${memories.length}`}>
      {memories.length === 0 ? (
        <p className="text-xs text-fg-faint">
          Nothing scoped to this project yet. Project memories are the facts MITTA should recall
          here and nowhere else.
        </p>
      ) : (
        <ul className="space-y-1.5">
          {memories.map((memory) => (
            <li key={memory.id} className="flex items-start gap-2 text-xs">
              <span className="mt-1 size-1 shrink-0 rounded-full bg-accent/60" />
              <span className="selectable min-w-0 flex-1 leading-relaxed text-fg-secondary">
                {memory.content}
              </span>
              <span className="readout shrink-0 text-2xs text-fg-faint">{memory.kind}</span>
            </li>
          ))}
        </ul>
      )}
    </HudPanel>
  );
}

const CONTAINMENT_COPY: Record<Containment, { tone: string; dot: 'ok' | 'warn' | 'error' }> = {
  writable: { tone: 'text-success', dot: 'ok' },
  read_only: { tone: 'text-fg-secondary', dot: 'ok' },
  outside: { tone: 'text-warning', dot: 'warn' },
  excluded: { tone: 'text-danger', dot: 'error' },
};

function BoundaryChecker() {
  const probe = useProjectsStore((s) => s.probe);
  const probePath = useProjectsStore((s) => s.probePath);
  const clearProbe = useProjectsStore((s) => s.clearProbe);
  const [value, setValue] = useState('');

  useEffect(() => {
    if (value.trim().length === 0) {
      clearProbe();
      return;
    }
    // Debounced. Each probe is a `stat` walk on the server to resolve symlinks,
    // and firing one per keystroke would do that for every prefix of the path.
    const timer = setTimeout(() => void probePath(value), 200);
    return () => clearTimeout(timer);
  }, [value, probePath, clearProbe]);

  const verdict = probe === null ? null : CONTAINMENT_COPY[probe.containment];

  return (
    <HudPanel label="BOUNDARY CHECK">
      <p className="mb-2 text-2xs leading-relaxed text-fg-muted">
        Ask what MITTA would do with a path, before anything asks for it. This runs the same
        query the policy engine runs.
      </p>
      <input
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="/path/to/check"
        aria-label="Path to check against the boundary"
        className={cx(
          'w-full rounded-xs border border-border-subtle bg-surface-input px-2.5 py-1.5',
          'font-mono text-xs text-fg-primary placeholder:text-fg-faint',
          'focus-visible:border-accent focus-visible:outline-none',
        )}
      />

      {probe !== null && verdict !== null && (
        <>
          <HudRule className="my-2.5" />
          <div className="space-y-1.5">
            <p className="flex items-center gap-2 text-xs">
              <StatusDot tone={verdict.dot} />
              <span className={cx('readout uppercase', verdict.tone)}>{probe.containment}</span>
              {/* Three states, not two. An exclusion is refused outright, and
                  calling that "MITTA would ask" offers a choice the engine does
                  not honour (DEC-114). */}
              <span className={cx(probe.refused ? 'text-danger' : 'text-fg-muted')}>
                {probe.refused
                  ? '· MITTA would refuse'
                  : probe.needs_confirmation
                    ? '· MITTA would ask'
                    : '· MITTA would proceed'}
              </span>
            </p>
            {/* The server's own sentence, not one reconstructed here. The engine
                and the UI cannot disagree about a permission if only one of them
                writes the explanation. */}
            <p className="text-2xs leading-relaxed text-fg-secondary">{probe.explanation}</p>
            {probe.path !== value.trim() && (
              <p className="font-mono text-2xs text-fg-faint">resolved to {probe.path}</p>
            )}
          </div>
        </>
      )}
    </HudPanel>
  );
}
