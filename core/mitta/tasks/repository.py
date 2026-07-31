"""Plan, task, checkpoint and schedule persistence.

Five tables behind one repository, following `mitta.projects.repository`: they
are never useful apart. A task without its plan has no goal, a checkpoint
without its task has nothing to resume, and a schedule that fires produces both.

Two methods here are not ordinary CRUD.

`claim_due` is the scheduler's only way to pick up work, and it moves
`next_run_at` forward **inside the same transaction that selects the row**. A
select-then-update would let two ticks — or a tick that overlapped its
predecessor — both see the same due schedule and run it twice, which for an
action that writes a file means writing it twice.

`add_dependency` refuses an edge that would close a cycle, as a recursive CTE
over `task_dependencies` rather than an application-side walk. `DATABASE_DESIGN.md`
§7 requires that: a cycle discovered at execution time is a plan that hangs, and
the producer that emits one will be a language model.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from mitta.errors import NotFoundError, ValidationError
from mitta.ids import PLAN, SCHEDULE, TASK, prefixed
from mitta.persistence.database import Database
from mitta.tasks.cron import next_after
from mitta.tasks.models import (
    Checkpoint,
    Plan,
    PlanStatus,
    Schedule,
    ScheduleDraft,
    Task,
    TaskDraft,
    TaskStatus,
)
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

_PLAN_COLUMNS = """
    seq, id, goal, status, project_id, conversation_id, created_at, updated_at
"""

_TASK_COLUMNS = """
    seq, id, plan_id, parent_id, ordinal, title, description, tool_name, params,
    status, attempt, max_attempts, result, error, started_at, ended_at,
    created_at, updated_at
"""

_SCHEDULE_COLUMNS = """
    seq, id, name, cron, timezone, action, enabled, last_run_at, next_run_at, created_at
"""

_CHECKPOINT_COLUMNS = "seq, task_id, label, state, created_at"


def _json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _optional_json(raw: str | None) -> dict[str, Any] | None:
    return None if raw is None else _json_object(raw)


def _row_to_plan(row: sqlite3.Row) -> Plan:
    return Plan(
        seq=row["seq"],
        id=row["id"],
        goal=row["goal"],
        status=PlanStatus(row["status"]),
        project_id=row["project_id"],
        conversation_id=row["conversation_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        seq=row["seq"],
        id=row["id"],
        plan_id=row["plan_id"],
        parent_id=row["parent_id"],
        ordinal=row["ordinal"],
        title=row["title"],
        description=row["description"],
        tool_name=row["tool_name"],
        params=_json_object(row["params"]),
        status=TaskStatus(row["status"]),
        attempt=row["attempt"],
        max_attempts=row["max_attempts"],
        result=_optional_json(row["result"]),
        error=_optional_json(row["error"]),
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_schedule(row: sqlite3.Row) -> Schedule:
    return Schedule(
        seq=row["seq"],
        id=row["id"],
        name=row["name"],
        cron=row["cron"],
        timezone=row["timezone"],
        action=_json_object(row["action"]),
        enabled=bool(row["enabled"]),
        last_run_at=row["last_run_at"],
        next_run_at=row["next_run_at"],
        created_at=row["created_at"],
    )


def _row_to_checkpoint(row: sqlite3.Row) -> Checkpoint:
    return Checkpoint(
        seq=row["seq"],
        task_id=row["task_id"],
        label=row["label"],
        state=_json_object(row["state"]),
        created_at=row["created_at"],
    )


class TaskRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- schedules ------------------------------------------------------------ #

    def create_schedule(self, draft: ScheduleDraft, *, now: int | None = None) -> Schedule:
        """Register a recurring automation.

        `next_run_at` is computed here and stored, rather than derived on every
        tick. The tick then costs one indexed lookup against
        `idx_schedules_due` instead of parsing every expression in the table,
        and — more usefully — the next fire time becomes something the user can
        be shown and can check before it happens.
        """
        ts = now if now is not None else int(time.time())
        schedule_id = prefixed(SCHEDULE)
        next_run = next_after(draft.cron, after=ts, timezone=draft.timezone)

        with self._db.write() as conn:
            conn.execute(
                """
                INSERT INTO schedules
                    (id, name, cron, timezone, action, enabled, next_run_at, created_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    schedule_id,
                    draft.name,
                    draft.cron,
                    draft.timezone,
                    json.dumps(draft.action, sort_keys=True),
                    int(draft.enabled),
                    next_run if draft.enabled else None,
                    ts,
                ),
            )
        log.info(
            "schedule.created",
            extra={
                "schedule_id": schedule_id,
                "cron": draft.cron,
                "timezone": draft.timezone,
                "kind": draft.action.get("kind"),
                "next_run_at": next_run,
            },
        )
        return self.get_schedule(schedule_id)

    def get_schedule(self, schedule_id: str) -> Schedule:
        with self._db.read() as conn:
            row = conn.execute(
                f"SELECT {_SCHEDULE_COLUMNS} FROM schedules WHERE id = ?",  # noqa: S608 - constant columns; values are bound
                (schedule_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("schedule", schedule_id)
        return _row_to_schedule(row)

    def list_schedules(self, *, enabled_only: bool = False) -> list[Schedule]:
        clause = "WHERE enabled = 1 " if enabled_only else ""
        sql = (
            f"SELECT {_SCHEDULE_COLUMNS} FROM schedules "  # noqa: S608 - constant columns and literal clauses
            f"{clause}"
            # Soonest first, so the list reads as "what happens next".
            "ORDER BY next_run_at IS NULL, next_run_at ASC, created_at ASC"
        )
        with self._db.read() as conn:
            rows = conn.execute(sql).fetchall()
        return [_row_to_schedule(row) for row in rows]

    def update_schedule(
        self,
        schedule_id: str,
        *,
        name: str | None = None,
        cron: str | None = None,
        timezone: str | None = None,
        enabled: bool | None = None,
        now: int | None = None,
    ) -> Schedule:
        """Patch semantics. The action is not patchable — see `ScheduleUpdate`.

        Any change to *when* recomputes `next_run_at` from the new expression.
        Leaving the old value would run the schedule once more on the old timing
        after the user had already changed it, which reads as the edit not
        having worked.
        """
        ts = now if now is not None else int(time.time())
        current = self.get_schedule(schedule_id)

        new_cron = cron if cron is not None else current.cron
        new_zone = timezone if timezone is not None else current.timezone
        new_enabled = enabled if enabled is not None else current.enabled
        timing_changed = new_cron != current.cron or new_zone != current.timezone

        next_run = current.next_run_at
        if not new_enabled:
            # A disabled schedule has no next run. Keeping the old timestamp
            # would make the UI promise a fire that cannot happen.
            next_run = None
        elif timing_changed or not current.enabled or next_run is None:
            next_run = next_after(new_cron, after=ts, timezone=new_zone)

        with self._db.write() as conn:
            conn.execute(
                """
                UPDATE schedules
                SET name = ?, cron = ?, timezone = ?, enabled = ?, next_run_at = ?
                WHERE id = ?
                """,
                (
                    name if name is not None else current.name,
                    new_cron,
                    new_zone,
                    int(new_enabled),
                    next_run,
                    schedule_id,
                ),
            )
        return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id: str) -> None:
        """Permanent, and the only way to withdraw a `tool` schedule's grant.

        Plans this schedule already produced survive it. They are the record of
        what ran, and deleting the automation must not delete the evidence.
        """
        with self._db.write() as conn:
            cur = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            if cur.rowcount == 0:
                raise NotFoundError("schedule", schedule_id)
        log.warning("schedule.deleted", extra={"schedule_id": schedule_id})

    def claim_due(self, *, now: int | None = None) -> list[Schedule]:
        """Take every schedule that is due, moving each one's clock forward.

        Selecting and advancing in one transaction is what makes a fire
        exactly-once. Two overlapping ticks would otherwise both read the same
        row as due — and the second one has no way to tell that the first is
        already running it.

        The returned rows carry the values *as claimed*: `last_run_at` is now,
        and `next_run_at` is the following occurrence. A run that fails does not
        rewind them. A retry that reruns an automation because its last attempt
        errored is how a half-completed action becomes a duplicated one.
        """
        ts = now if now is not None else int(time.time())
        claimed: list[Schedule] = []

        with self._db.write() as conn:
            rows = conn.execute(
                f"SELECT {_SCHEDULE_COLUMNS} FROM schedules "  # noqa: S608 - constant columns; values are bound
                "WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ? "
                "ORDER BY next_run_at ASC",
                (ts,),
            ).fetchall()

            for row in rows:
                schedule = _row_to_schedule(row)
                try:
                    following = next_after(schedule.cron, after=ts, timezone=schedule.timezone)
                except ValueError:
                    # The expression parsed when it was stored, so this is a row
                    # edited outside the API or a zone the OS dropped in an
                    # update. Disable rather than raise: one bad schedule must
                    # not stop the tick that carries every other one.
                    log.error(
                        "schedule.unschedulable",
                        extra={"schedule_id": schedule.id, "cron": schedule.cron},
                    )
                    conn.execute(
                        "UPDATE schedules SET enabled = 0, next_run_at = NULL WHERE id = ?",
                        (schedule.id,),
                    )
                    continue

                conn.execute(
                    "UPDATE schedules SET last_run_at = ?, next_run_at = ? WHERE id = ?",
                    (ts, following, schedule.id),
                )
                claimed.append(
                    Schedule(
                        seq=schedule.seq,
                        id=schedule.id,
                        name=schedule.name,
                        cron=schedule.cron,
                        timezone=schedule.timezone,
                        action=schedule.action,
                        enabled=schedule.enabled,
                        last_run_at=ts,
                        next_run_at=following,
                        created_at=schedule.created_at,
                    )
                )

        return claimed

    # -- plans ----------------------------------------------------------------- #

    def create_plan(
        self,
        goal: str,
        *,
        status: PlanStatus = PlanStatus.RUNNING,
        conversation_id: str | None = None,
        project_id: str | None = None,
        now: int | None = None,
    ) -> Plan:
        ts = now if now is not None else int(time.time())
        plan_id = prefixed(PLAN)
        with self._db.write() as conn:
            conn.execute(
                """
                INSERT INTO plans
                    (id, goal, status, project_id, conversation_id, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (plan_id, goal, status.value, project_id, conversation_id, ts, ts),
            )
        return self.get_plan(plan_id)

    def get_plan(self, plan_id: str) -> Plan:
        with self._db.read() as conn:
            row = conn.execute(
                f"SELECT {_PLAN_COLUMNS} FROM plans WHERE id = ?",  # noqa: S608 - constant columns; values are bound
                (plan_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("plan", plan_id)
        return _row_to_plan(row)

    def list_plans(self, *, limit: int = 50, offset: int = 0) -> list[Plan]:
        with self._db.read() as conn:
            rows = conn.execute(
                f"SELECT {_PLAN_COLUMNS} FROM plans "  # noqa: S608 - constant columns; values are bound
                "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [_row_to_plan(row) for row in rows]

    def set_plan_status(self, plan_id: str, status: PlanStatus, *, now: int | None = None) -> Plan:
        ts = now if now is not None else int(time.time())
        with self._db.write() as conn:
            cur = conn.execute(
                "UPDATE plans SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, ts, plan_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("plan", plan_id)
        return self.get_plan(plan_id)

    def attach_conversation(self, plan_id: str, conversation_id: str) -> None:
        """Record which thread a prompt run answered into.

        Set after the fact because the orchestrator creates the conversation:
        the plan exists first so that a run which fails before any model call
        still leaves a row explaining what was attempted.
        """
        with self._db.write() as conn:
            conn.execute(
                "UPDATE plans SET conversation_id = ? WHERE id = ?", (conversation_id, plan_id)
            )

    # -- tasks ----------------------------------------------------------------- #

    def add_task(
        self,
        plan_id: str,
        draft: TaskDraft,
        *,
        status: TaskStatus = TaskStatus.PENDING,
        depends_on: str | None = None,
        parent_id: str | None = None,
        now: int | None = None,
    ) -> Task:
        """Append a task to a plan. Ordinal is assigned, never passed in.

        Two producers appending to the same plan would otherwise have to agree
        on the next number, and the failure mode of getting that wrong is two
        tasks that sort arbitrarily against each other — in a list whose whole
        job is to say what happened in what order.
        """
        ts = now if now is not None else int(time.time())
        task_id = prefixed(TASK)

        with self._db.write() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 AS next FROM tasks WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            ordinal = int(row["next"])
            conn.execute(
                """
                INSERT INTO tasks
                    (id, plan_id, parent_id, ordinal, title, description, tool_name,
                     params, status, max_attempts, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task_id,
                    plan_id,
                    parent_id,
                    ordinal,
                    draft.title,
                    draft.description,
                    draft.tool_name,
                    json.dumps(draft.params, sort_keys=True),
                    status.value,
                    draft.max_attempts,
                    ts,
                    ts,
                ),
            )

        if depends_on is not None:
            self.add_dependency(task_id, depends_on)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Task:
        with self._db.read() as conn:
            row = conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?",  # noqa: S608 - constant columns; values are bound
                (task_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("task", task_id)
        return _row_to_task(row)

    def tasks_for(self, plan_id: str) -> list[Task]:
        with self._db.read() as conn:
            rows = conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE plan_id = ? ORDER BY ordinal",  # noqa: S608 - constant columns; values are bound
                (plan_id,),
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def recent_tasks(self, *, limit: int = 50, active_only: bool = False) -> list[Task]:
        """Active and recent tasks — `GET /v1/tasks`.

        "Active" is every non-terminal status, not just `running`. A task
        waiting on an approval is the one a user most needs to see, and it is
        not running by any definition.
        """
        clause = ""
        params: list[object] = []
        if active_only:
            placeholders = ",".join("?" * len(_ACTIVE_TASK_STATUSES))
            clause = f"WHERE status IN ({placeholders}) "
            params.extend(_ACTIVE_TASK_STATUSES)
        params.append(limit)

        sql = (
            f"SELECT {_TASK_COLUMNS} FROM tasks "  # noqa: S608 - constant columns and literal clauses; values are bound
            f"{clause}"
            "ORDER BY seq DESC LIMIT ?"
        )
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_task(row) for row in rows]

    def start_task(self, task_id: str, *, now: int | None = None) -> Task:
        """Mark a task running and count the attempt.

        The attempt counter increments here rather than on failure, so a task
        killed by the process dying has still spent one. A counter that only
        advanced on a clean failure would let a crash loop retry forever.
        """
        ts = now if now is not None else int(time.time())
        with self._db.write() as conn:
            cur = conn.execute(
                """
                UPDATE tasks
                SET status = ?, attempt = attempt + 1, started_at = ?, ended_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (TaskStatus.RUNNING.value, ts, ts, task_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("task", task_id)
        return self.get_task(task_id)

    def finish_task(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> Task:
        ts = now if now is not None else int(time.time())
        with self._db.write() as conn:
            cur = conn.execute(
                """
                UPDATE tasks
                SET status = ?, result = ?, error = ?, ended_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    json.dumps(result, sort_keys=True, default=str) if result is not None else None,
                    json.dumps(error, sort_keys=True, default=str) if error is not None else None,
                    ts,
                    ts,
                    task_id,
                ),
            )
            if cur.rowcount == 0:
                raise NotFoundError("task", task_id)
        return self.get_task(task_id)

    def set_task_status(self, task_id: str, status: TaskStatus, *, now: int | None = None) -> Task:
        ts = now if now is not None else int(time.time())
        with self._db.write() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, ts, task_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("task", task_id)
        return self.get_task(task_id)

    # -- dependencies ---------------------------------------------------------- #

    def add_dependency(self, task_id: str, depends_on: str) -> None:
        """Record that `task_id` cannot start until `depends_on` finishes.

        Rejects an edge that would close a cycle. The check runs inside the
        insert's transaction, because two edges added concurrently can each be
        acyclic against the graph they read and cyclic against the one they
        commit into.
        """
        if task_id == depends_on:
            raise ValidationError("a task cannot depend on itself")

        with self._db.write() as conn:
            if _reaches(conn, depends_on, task_id):
                raise ValidationError(f"{task_id} → {depends_on} would create a dependency cycle")
            conn.execute(
                "INSERT OR IGNORE INTO task_dependencies (task_id, depends_on) VALUES (?,?)",
                (task_id, depends_on),
            )

    def dependencies(self, plan_id: str) -> list[tuple[str, str]]:
        """Every edge in a plan, as `(task, depends_on)` pairs."""
        with self._db.read() as conn:
            rows = conn.execute(
                """
                SELECT d.task_id, d.depends_on
                FROM   task_dependencies d
                JOIN   tasks t ON t.id = d.task_id
                WHERE  t.plan_id = ?
                ORDER BY t.ordinal
                """,
                (plan_id,),
            ).fetchall()
        return [(row["task_id"], row["depends_on"]) for row in rows]

    # -- checkpoints ------------------------------------------------------------ #

    def checkpoint(
        self, task_id: str, label: str, state: dict[str, Any], *, now: int | None = None
    ) -> Checkpoint:
        ts = now if now is not None else int(time.time())
        with self._db.write() as conn:
            conn.execute(
                "INSERT INTO task_checkpoints (task_id, label, state, created_at) VALUES (?,?,?,?)",
                (task_id, label, json.dumps(state, sort_keys=True, default=str), ts),
            )
        latest = self.latest_checkpoint(task_id)
        if latest is None:  # pragma: no cover - inserted one statement ago
            raise NotFoundError("task_checkpoint", task_id)
        return latest

    def latest_checkpoint(self, task_id: str) -> Checkpoint | None:
        """What a resume starts from. Served by `idx_checkpoints_task`."""
        with self._db.read() as conn:
            row = conn.execute(
                f"SELECT {_CHECKPOINT_COLUMNS} FROM task_checkpoints "  # noqa: S608 - constant columns; values are bound
                "WHERE task_id = ? ORDER BY seq DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return None if row is None else _row_to_checkpoint(row)

    def checkpoints(self, task_id: str) -> list[Checkpoint]:
        with self._db.read() as conn:
            rows = conn.execute(
                f"SELECT {_CHECKPOINT_COLUMNS} FROM task_checkpoints "  # noqa: S608 - constant columns; values are bound
                "WHERE task_id = ? ORDER BY seq DESC",
                (task_id,),
            ).fetchall()
        return [_row_to_checkpoint(row) for row in rows]

    # -- startup reconciliation -------------------------------------------------- #

    def reconcile_orphaned_runs(self, *, now: int | None = None) -> int:
        """Close out plans and tasks the process died in the middle of.

        The same argument as `ConversationRepository.reconcile_orphaned_turns`:
        a plan left `running` by a crash shows in the UI as work in progress
        that no process is doing, and it never resolves because the thing that
        would have resolved it is gone. Returns how many plans were closed.
        """
        ts = now if now is not None else int(time.time())
        detail = json.dumps({"code": "runtime.interrupted", "message": "MITTA stopped mid-run."})

        with self._db.write() as conn:
            tasks = conn.execute(
                "UPDATE tasks SET status = ?, error = ?, ended_at = ?, updated_at = ? "
                "WHERE status IN (?,?)",
                (
                    TaskStatus.FAILED.value,
                    detail,
                    ts,
                    ts,
                    TaskStatus.RUNNING.value,
                    TaskStatus.READY.value,
                ),
            ).rowcount
            plans = conn.execute(
                "UPDATE plans SET status = ?, updated_at = ? WHERE status IN (?,?)",
                (PlanStatus.FAILED.value, ts, PlanStatus.RUNNING.value, PlanStatus.PAUSED.value),
            ).rowcount

        if plans or tasks:
            log.warning("tasks.reconciled", extra={"plans": plans, "tasks": tasks})
        return int(plans)


#: Non-terminal task statuses, in the order the CHECK constraint declares them.
_ACTIVE_TASK_STATUSES = (
    TaskStatus.PENDING.value,
    TaskStatus.READY.value,
    TaskStatus.RUNNING.value,
    TaskStatus.BLOCKED.value,
    TaskStatus.AWAITING_APPROVAL.value,
)


def _reaches(conn: sqlite3.Connection, start: str, target: str) -> bool:
    """Whether `target` is reachable from `start` by following dependencies.

    A recursive CTE rather than a Python walk, because the edges are rows and
    the question is a graph query — `DATABASE_DESIGN.md` §7 chose the separate
    table specifically so this would not be an application-side scan.
    """
    if start == target:
        return True
    row = conn.execute(
        """
        WITH RECURSIVE reachable(id) AS (
            SELECT depends_on FROM task_dependencies WHERE task_id = ?
            UNION
            SELECT d.depends_on FROM task_dependencies d
            JOIN reachable r ON d.task_id = r.id
        )
        SELECT 1 AS hit FROM reachable WHERE id = ? LIMIT 1
        """,
        (start, target),
    ).fetchone()
    return row is not None
