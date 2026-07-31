"""Task, plan and schedule wire schemas.

`ScheduleResource` carries three fields the database does not store, and each
one exists because the alternative is the UI computing it: `next_run_local` (the
next fire in the schedule's own zone, which is the clock the user set it by),
`summary` (a short rendering of the cron), and `unattended_risk` (the tier the
scheduled call sits in). The last is the important one — it is what lets the
surface say "this schedule can write" without the frontend holding its own copy
of the risk table and eventually disagreeing with the policy engine.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from mitta.api.schemas.common import Schema
from mitta.tasks.cron import describe as describe_cron
from mitta.tasks.cron import parse as parse_cron
from mitta.tasks.cron import resolve_timezone
from mitta.tasks.models import (
    Checkpoint,
    Plan,
    PlanStatus,
    Schedule,
    Task,
    TaskStatus,
    parse_action,
)


class TaskResource(Schema):
    id: str
    plan_id: str
    parent_id: str | None
    ordinal: int
    title: str
    description: str | None
    tool_name: str | None
    params: dict[str, object]
    status: TaskStatus
    attempt: int
    max_attempts: int
    result: dict[str, object] | None
    error: dict[str, object] | None
    started_at: int | None
    ended_at: int | None
    created_at: int
    updated_at: int
    #: Whether `POST /{id}/resume` would be accepted. Derived here so the button
    #: and the endpoint cannot disagree about when a retry is still available.
    resumable: bool

    @classmethod
    def of(cls, task: Task) -> TaskResource:
        return cls(
            id=task.id,
            plan_id=task.plan_id,
            parent_id=task.parent_id,
            ordinal=task.ordinal,
            title=task.title,
            description=task.description,
            tool_name=task.tool_name,
            params=task.params,
            status=task.status,
            attempt=task.attempt,
            max_attempts=task.max_attempts,
            result=task.result,
            error=task.error,
            started_at=task.started_at,
            ended_at=task.ended_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
            resumable=task.status is TaskStatus.FAILED and task.attempt < task.max_attempts,
        )


class CheckpointResource(Schema):
    """What a resumed run would start from."""

    task_id: str
    label: str
    state: dict[str, object]
    created_at: int

    @classmethod
    def of(cls, checkpoint: Checkpoint) -> CheckpointResource:
        return cls(
            task_id=checkpoint.task_id,
            label=checkpoint.label,
            state=checkpoint.state,
            created_at=checkpoint.created_at,
        )


class PlanResource(Schema):
    id: str
    goal: str
    status: PlanStatus
    project_id: str | None
    #: Set for a prompt run — the thread its answer was written into, which is
    #: where the user actually reads the result.
    conversation_id: str | None
    created_at: int
    updated_at: int

    @classmethod
    def of(cls, plan: Plan) -> PlanResource:
        return cls(
            id=plan.id,
            goal=plan.goal,
            status=plan.status,
            project_id=plan.project_id,
            conversation_id=plan.conversation_id,
            created_at=plan.created_at,
            updated_at=plan.updated_at,
        )


class DependencyEdge(Schema):
    task_id: str
    depends_on: str


class PlanDetailResponse(Schema):
    """The full graph — `GET /v1/plans/{id}`.

    Edges travel separately from tasks rather than as a list on each node. They
    are rows in their own table for the reason `DATABASE_DESIGN.md` §7 gives,
    and flattening them into the nodes on the wire would invite a client to
    treat them as a property of a task rather than a relation between two.
    """

    plan: PlanResource
    tasks: list[TaskResource]
    edges: list[DependencyEdge]


class TaskDetailResponse(Schema):
    task: TaskResource
    plan: PlanResource
    checkpoints: list[CheckpointResource]


class TaskListResponse(Schema):
    tasks: list[TaskResource]
    #: Plans referenced by those tasks, so the list can show what each step was
    #: for without a request per row.
    plans: list[PlanResource]
    total: int


class ScheduleResource(Schema):
    id: str
    name: str
    cron: str
    timezone: str
    action: dict[str, object]
    enabled: bool
    last_run_at: int | None
    next_run_at: int | None
    created_at: int
    #: A short rendering of the expression — "daily at 08:00". Never a full
    #: translation: past the simple cases those read worse than the cron, and a
    #: wrong one is a false statement about when something will happen.
    summary: str
    #: The next fire as an ISO timestamp in the schedule's own zone. The user
    #: set 08:00 on their clock; showing them a UTC epoch makes them do the
    #: conversion that the stored timezone exists to avoid.
    next_run_local: str | None

    @classmethod
    def of(cls, schedule: Schedule) -> ScheduleResource:
        local: str | None = None
        if schedule.next_run_at is not None:
            zone = resolve_timezone(schedule.timezone)
            local = datetime.fromtimestamp(schedule.next_run_at, zone).isoformat(timespec="minutes")
        return cls(
            id=schedule.id,
            name=schedule.name,
            cron=schedule.cron,
            timezone=schedule.timezone,
            action=schedule.action,
            enabled=schedule.enabled,
            last_run_at=schedule.last_run_at,
            next_run_at=schedule.next_run_at,
            created_at=schedule.created_at,
            summary=describe_cron(schedule.cron),
            next_run_local=local,
        )


class ScheduleListResponse(Schema):
    schedules: list[ScheduleResource]
    total: int
    #: False when the process is not ticking — a browser session against a
    #: sidecar started without a scheduler, or a shutdown in progress. A list of
    #: automations that cannot fire has to say so; the times beside them would
    #: otherwise be a promise nothing is keeping.
    scheduler_running: bool


class CreateScheduleRequest(Schema):
    """A new automation.

    There is deliberately no tool that constructs this request. Authoring a
    `tool` schedule is what authorises its call at every later fire (DEC-122),
    so the ability to write one is the ability to grant a standing permission —
    and a model that could do that would have found the way around the approval
    model rather than through it.
    """

    name: str = Field(min_length=1, max_length=120)
    cron: str = Field(
        min_length=1,
        max_length=200,
        description="Five fields — minute hour day-of-month month day-of-week — or @daily.",
    )
    timezone: str = Field(
        default="UTC",
        min_length=1,
        max_length=64,
        description="IANA zone. The schedule fires on this clock, across DST.",
    )
    action: dict[str, object] = Field(
        description='{"kind":"prompt","text":…} or {"kind":"tool","tool":…,"params":…}'
    )
    enabled: bool = True

    # Validated on the wire schema rather than only in the domain draft, so that
    # a bad expression comes back as a 422 naming the field. Constructing the
    # domain type inside the handler instead would raise a Pydantic error the
    # API has no handler for, and the user would get a 500 for a typo.
    @field_validator("cron")
    @classmethod
    def _parseable(cls, value: str) -> str:
        parse_cron(value)
        return value

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        resolve_timezone(value)
        return value

    @field_validator("action")
    @classmethod
    def _valid_action(cls, value: dict[str, object]) -> dict[str, object]:
        parse_action(dict(value))
        return value


class UpdateScheduleRequest(Schema):
    """Name, timing and enablement only.

    The action is not patchable. Editing the arguments of a `tool` schedule is
    editing a standing authorisation, and a patch would let one field widen what
    a run may do without re-stating the whole call.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    cron: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None

    @field_validator("cron")
    @classmethod
    def _parseable(cls, value: str | None) -> str | None:
        if value is not None:
            parse_cron(value)
        return value

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        if value is not None:
            resolve_timezone(value)
        return value


class RunResponse(Schema):
    """What a manual run or a resume produced."""

    plan: PlanResource
    tasks: list[TaskResource]
