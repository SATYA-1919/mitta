/**
 * Tasks — what MITTA did while you were not here.
 *
 * Two panes: the automations on the left, and the runs they produced on the
 * right. The split is the point. A schedule is a *promise* about the future, a
 * task is a *record* of the past, and the two need different things from the
 * UI — one has to be checkable before it happens, the other after.
 *
 * Three details are load-bearing rather than decorative:
 *
 * - A `tool` schedule is labelled as an authorisation, because that is what
 *   creating one is (DEC-122). The exact arguments are shown on the row rather
 *   than behind a disclosure: they are the binding, and a grant whose contents
 *   are one click away is one nobody checks.
 * - The next fire is shown on the user's own clock, from the server's
 *   `next_run_local`. Recomputing it here would put a second timezone
 *   implementation in the frontend for it to disagree with.
 * - When the sidecar is not ticking, the times are struck through and the
 *   surface says so. A list of automations that cannot fire, showing the times
 *   they would have fired at, is the worst version of this screen.
 */

import { useEffect, useState } from 'react';

import { HudPanel, HudRule } from '@/components/ui/hud';
import { Button, cx, EmptyState, StatusDot } from '@/components/ui/primitives';
import type { DotTone } from '@/components/ui/primitives';
import type { Plan, Schedule, Task, TaskStatus } from '@/lib/api/client';
import { POLL_MS, useTasksStore } from '@/state/tasks';

export function TasksSurface() {
  const { schedules, tasks, plans, schedulerRunning, activeOnly, error } = useTasksStore();

  // Polls only while mounted. A scheduled run arrives with no socket frame
  // behind it, so something has to ask — but only while someone is looking.
  useEffect(() => {
    void useTasksStore.getState().refresh();
    const timer = setInterval(() => void useTasksStore.getState().refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="flex h-full min-h-0">
      <section className="flex w-[360px] min-w-0 shrink-0 flex-col border-r border-border-subtle">
        <div className="shrink-0 border-b border-border-subtle p-3">
          <NewSchedule />
        </div>

        <div className="scrollable min-h-0 flex-1 space-y-2 p-3">
          {!schedulerRunning && schedules.length > 0 && (
            <p className="border border-warning/40 bg-warning/5 px-2 py-1.5 text-2xs text-warning">
              The scheduler is not running. Nothing below will fire.
            </p>
          )}

          {schedules.length === 0 ? (
            <EmptyState
              title="No automations"
              hint="A schedule runs a question, or one exact tool call, on a timetable"
            />
          ) : (
            schedules.map((schedule) => (
              <ScheduleRow key={schedule.id} schedule={schedule} ticking={schedulerRunning} />
            ))
          )}
        </div>

        {error !== null && (
          <p className="shrink-0 border-t border-border-subtle px-3 py-2 text-xs text-danger">
            {error}
          </p>
        )}
      </section>

      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex shrink-0 items-center gap-3 border-b border-border-subtle px-4 py-2">
          <span className="label">RUNS</span>
          <span className="h-px flex-1 bg-border-subtle" />
          <label className="flex items-center gap-2 text-2xs text-fg-muted">
            <input
              type="checkbox"
              checked={activeOnly}
              onChange={(event) => useTasksStore.getState().setActiveOnly(event.target.checked)}
              className="accent-[var(--color-accent)]"
            />
            Unfinished only
          </label>
        </div>

        <div className="scrollable min-h-0 flex-1 space-y-2 p-4">
          {tasks.length === 0 ? (
            <EmptyState
              title="Nothing has run yet"
              hint="Runs appear here with every step MITTA took"
            />
          ) : (
            tasks.map((task) => (
              <TaskRow
                key={task.id}
                task={task}
                plan={plans.find((item) => item.id === task.plan_id) ?? null}
              />
            ))
          )}
        </div>
      </div>
    </div>
  );
}

// -- schedules ---------------------------------------------------------------

function ScheduleRow({ schedule, ticking }: { schedule: Schedule; ticking: boolean }) {
  const runningId = useTasksStore((s) => s.runningId);
  const [confirming, setConfirming] = useState(false);
  const action = schedule.action as { kind?: string; tool?: string; params?: unknown };
  const isTool = action.kind === 'tool';

  return (
    <HudPanel
      label={schedule.name}
      // The kind is stated in the frame rather than inferred from the body.
      // "Authorised call" is the whole difference between a schedule that asks
      // MITTA something and one that acts on the user's behalf unprompted.
      right={
        <span className={cx('readout text-2xs', isTool ? 'text-warning' : 'text-fg-faint')}>
          {isTool ? 'AUTHORISED CALL' : 'QUESTION'}
        </span>
      }
      className={cx(!schedule.enabled && 'opacity-55')}
    >
      <div className="space-y-2">
        <p className="readout text-2xs text-fg-secondary">
          {isTool ? (
            <>
              <span className="text-accent">{action.tool}</span>
              {'('}
              {renderParams(action.params)}
              {')'}
            </>
          ) : (
            <span className="text-fg-primary">{String((action as { text?: string }).text)}</span>
          )}
        </p>

        {isTool && (
          // Said plainly, once, on the row. This is the only place in MITTA
          // where an action runs without a card in front of it, and the
          // sentence that explains why belongs beside the thing it explains.
          <p className="text-2xs leading-relaxed text-fg-faint">
            Runs with exactly these arguments and no further prompt. Anything else is refused.
          </p>
        )}

        <HudRule />

        <div className="flex items-center justify-between gap-2">
          <span className="readout text-2xs text-fg-muted">{schedule.summary}</span>
          <span
            className={cx(
              'readout text-2xs',
              schedule.enabled && ticking ? 'text-fg-secondary' : 'text-fg-faint line-through',
            )}
          >
            {schedule.next_run_local ?? 'disabled'}
          </span>
        </div>

        <div className="flex items-center gap-1.5">
          <label className="mr-auto flex items-center gap-1.5 text-2xs text-fg-muted">
            <input
              type="checkbox"
              checked={schedule.enabled}
              onChange={(event) =>
                void useTasksStore.getState().setEnabled(schedule.id, event.target.checked)
              }
              className="accent-[var(--color-accent)]"
            />
            Enabled
          </label>

          <Button
            onClick={() => void useTasksStore.getState().runNow(schedule.id)}
            disabled={runningId === schedule.id}
          >
            {runningId === schedule.id ? 'Running…' : 'Run now'}
          </Button>

          {/* Two-step, like removing a project path: deleting a schedule is
              how its authorisation is withdrawn, and an accidental click
              silently stops something the user is relying on. */}
          {confirming ? (
            <Button
              variant="danger"
              onClick={() => {
                setConfirming(false);
                void useTasksStore.getState().remove(schedule.id);
              }}
            >
              Confirm
            </Button>
          ) : (
            <Button onClick={() => setConfirming(true)}>Delete</Button>
          )}
        </div>
      </div>
    </HudPanel>
  );
}

const PRESETS: { label: string; cron: string }[] = [
  { label: 'Daily 08:00', cron: '0 8 * * *' },
  { label: 'Weekdays 09:30', cron: '30 9 * * 1-5' },
  { label: 'Hourly', cron: '0 * * * *' },
];

function NewSchedule() {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [cron, setCron] = useState('0 8 * * *');
  const [text, setText] = useState('');

  // The machine's own zone, so "08:00" means what the user means by it without
  // them picking their own timezone out of a list of six hundred.
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

  if (!open) {
    return (
      <Button variant="primary" className="w-full" onClick={() => setOpen(true)}>
        New automation
      </Button>
    );
  }

  return (
    <form
      className="space-y-2"
      onSubmit={(event) => {
        event.preventDefault();
        void useTasksStore
          .getState()
          .create({
            name: name.trim(),
            cron: cron.trim(),
            timezone,
            action: { kind: 'prompt', text: text.trim() },
            enabled: true,
          })
          .then((created) => {
            if (!created) return;
            setOpen(false);
            setName('');
            setText('');
          });
      }}
    >
      <input
        value={name}
        onChange={(event) => setName(event.target.value)}
        placeholder="Name"
        aria-label="Automation name"
        className="w-full border border-border-subtle bg-surface-sunken px-2 py-1.5 text-xs text-fg-primary placeholder:text-fg-faint focus-visible:outline-accent"
      />
      <input
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder="Ask MITTA something, e.g. what happened overnight"
        aria-label="Question to ask"
        className="w-full border border-border-subtle bg-surface-sunken px-2 py-1.5 text-xs text-fg-primary placeholder:text-fg-faint focus-visible:outline-accent"
      />
      <div className="flex gap-1.5">
        {PRESETS.map((preset) => (
          <button
            key={preset.cron}
            type="button"
            onClick={() => setCron(preset.cron)}
            className={cx(
              'border px-1.5 py-1 text-2xs transition-colors',
              cron === preset.cron
                ? 'border-accent text-accent'
                : 'border-border-subtle text-fg-muted hover:text-fg-secondary',
            )}
          >
            {preset.label}
          </button>
        ))}
      </div>
      <input
        value={cron}
        onChange={(event) => setCron(event.target.value)}
        aria-label="Cron expression"
        className="readout w-full border border-border-subtle bg-surface-sunken px-2 py-1.5 text-2xs text-fg-secondary focus-visible:outline-accent"
      />
      {/* The zone is shown, never asked for. A schedule stored in the wrong
          timezone fires an hour out and looks like a bug in the clock. */}
      <p className="text-2xs text-fg-faint">Fires on {timezone}</p>

      <div className="flex gap-1.5">
        <Button type="submit" variant="primary" disabled={name.trim() === '' || text.trim() === ''}>
          Create
        </Button>
        <Button type="button" onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>

      {/* A prompt automation is the only kind this form makes, and saying why
          beats a disabled radio button nobody can explain. */}
      <p className="text-2xs leading-relaxed text-fg-faint">
        Scheduled questions can search and read. They cannot write, open or close anything —
        there is nobody there to approve it.
      </p>
    </form>
  );
}

// -- runs --------------------------------------------------------------------

const TASK_TONE: Record<TaskStatus, DotTone> = {
  pending: 'idle',
  ready: 'idle',
  running: 'ok',
  blocked: 'warn',
  awaiting_approval: 'warn',
  completed: 'ok',
  failed: 'error',
  skipped: 'idle',
};

function TaskRow({ task, plan }: { task: Task; plan: Plan | null }) {
  const runningId = useTasksStore((s) => s.runningId);
  const finished = ['completed', 'failed', 'skipped'].includes(task.status);
  const error = task.error as { message?: string } | null;

  return (
    <article className="border border-border-subtle bg-surface-raised/40 px-3 py-2">
      <div className="flex items-center gap-2">
        <StatusDot tone={TASK_TONE[task.status]} pulse={task.status === 'running'} />
        <span className="truncate text-xs text-fg-primary">{task.title}</span>
        {task.tool_name !== null && (
          <span className="readout shrink-0 text-2xs text-accent">tool</span>
        )}
        <span className="ml-auto readout shrink-0 text-2xs text-fg-muted">{task.status}</span>
      </div>

      {plan !== null && (
        <p className="mt-1 truncate text-2xs text-fg-faint">
          {plan.goal}
          {task.attempt > 1 && ` · attempt ${task.attempt} of ${task.max_attempts}`}
        </p>
      )}

      {error?.message != null && <p className="mt-1 text-2xs text-danger">{error.message}</p>}

      <div className="mt-1.5 flex items-center gap-1.5">
        {task.resumable && (
          <Button
            onClick={() => void useTasksStore.getState().resume(task.id)}
            disabled={runningId === task.id}
          >
            {runningId === task.id ? 'Retrying…' : 'Retry'}
          </Button>
        )}
        {!finished && (
          <Button variant="danger" onClick={() => void useTasksStore.getState().cancel(task.id)}>
            Cancel
          </Button>
        )}
      </div>
    </article>
  );
}

function renderParams(params: unknown): string {
  if (typeof params !== 'object' || params === null) return '';
  return Object.entries(params as Record<string, unknown>)
    .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
    .join(', ');
}
