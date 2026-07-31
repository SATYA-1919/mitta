"""Plan, task and schedule models.

The split follows `mitta.projects.models`: frozen dataclasses for rows that came
out of the database, Pydantic for anything that came from outside the process.

**`ScheduleAction` is the security-sensitive type here**, and it is why this
module has more validation than a CRUD phase would normally need. A `tool`
action carries the exact call a scheduled run will make, and DEC-122 turns
authoring one into a standing authorisation for that call — so the arguments are
frozen at creation, the risk tier is checked at creation, and neither can be
widened later by anything except the user rewriting the schedule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mitta.tasks.cron import parse as parse_cron
from mitta.tasks.cron import resolve_timezone


class PlanStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


#: Statuses a plan can no longer move out of. Anything else is either running or
#: waiting to, which is what `GET /v1/tasks?active=true` means by active.
TERMINAL_PLAN_STATUSES: frozenset[PlanStatus] = frozenset(
    {PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED}
)

TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED}
)


@dataclass(frozen=True, slots=True)
class Plan:
    """One run. The goal is what the user asked for, in their words."""

    seq: int
    id: str
    goal: str
    status: PlanStatus
    project_id: str | None
    conversation_id: str | None
    created_at: int
    updated_at: int

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL_PLAN_STATUSES


@dataclass(frozen=True, slots=True)
class Task:
    seq: int
    id: str
    plan_id: str
    parent_id: str | None
    ordinal: int
    title: str
    description: str | None
    tool_name: str | None
    params: dict[str, Any]
    status: TaskStatus
    attempt: int
    max_attempts: int
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    started_at: int | None
    ended_at: int | None
    created_at: int
    updated_at: int

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Opaque state written after a task completes.

    `state` belongs to whatever wrote it; nothing here interprets it. It exists
    so a resumed plan knows what has already happened — for a task that has
    already written a file, re-running from the top is not a resume, it is a
    second write.
    """

    seq: int
    task_id: str
    label: str
    state: dict[str, Any]
    created_at: int


@dataclass(frozen=True, slots=True)
class Schedule:
    seq: int
    id: str
    name: str
    cron: str
    timezone: str
    action: dict[str, Any]
    enabled: bool
    last_run_at: int | None
    next_run_at: int | None
    created_at: int

    def typed_action(self) -> ScheduleAction:
        """Re-validate the stored action on the way out.

        Deliberately not cached at write time. The row is the authority for what
        a run will do, and a scheduler that trusted an in-memory copy would keep
        executing an action the user had already edited or repaired by hand.
        """
        return parse_action(self.action)


class PromptAction(BaseModel):
    """Run a sentence through the agent, as if the user had typed it.

    Tool selection is the planner's, so the run can search and read — and cannot
    write. `TaskRunner` caps an unattended prompt at `Risk.READ` (DEC-123),
    which is enforced there rather than described here.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["prompt"] = "prompt"
    text: str = Field(min_length=1, max_length=2000)
    #: Continue an existing thread instead of starting one. A daily briefing
    #: reads better as one conversation than as thirty single-message ones.
    conversation_id: str | None = None


class ToolAction(BaseModel):
    """Run one named tool with exactly these arguments.

    The arguments are the authorisation (DEC-122). They are stored verbatim and
    hashed at fire time; a call that does not hash to what is stored here does
    not run.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["tool"] = "tool"
    tool: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("params")
    @classmethod
    def _json_shaped(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Reject arguments that would not survive the round trip to SQLite.

        The stored JSON is what the parameter hash is computed over at every
        later fire. A value that serialises differently than it was submitted —
        a set, a tuple, a datetime — would hash differently on the way back out
        and refuse a call the user authored, which reads as MITTA ignoring a
        schedule for no reason.
        """
        try:
            json.dumps(value, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"params must be plain JSON: {exc}") from exc
        return value


ScheduleAction = PromptAction | ToolAction


def parse_action(raw: dict[str, Any]) -> ScheduleAction:
    """Validate a stored or submitted action.

    An unknown `kind` is an error rather than a default. Defaulting would mean a
    typo in the field that decides whether a run may touch the disk resolves to
    *something*, and the something it resolves to is the more capable branch.
    """
    kind = raw.get("kind")
    if kind == "prompt":
        return PromptAction.model_validate(raw)
    if kind == "tool":
        return ToolAction.model_validate(raw)
    raise ValueError(f"unknown action kind: {kind!r}")


class ScheduleDraft(BaseModel):
    """A schedule as submitted.

    Validated here rather than in the repository because two of these fields can
    only be checked by trying: a cron expression that does not parse and a
    timezone the machine does not know are both accepted by any type that only
    says `str`, and both surface as a schedule that silently never fires.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    #: Five fields, `minute hour day-of-month month day-of-week`.
    cron: str = Field(min_length=1, max_length=200)
    #: An IANA zone name. `UTC` is the schema default and almost never what
    #: someone means by "eight in the morning".
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    action: dict[str, Any]
    enabled: bool = True

    @field_validator("cron")
    @classmethod
    def _parseable(cls, value: str) -> str:
        parse_cron(value)  # raises ValueError, which the API renders as a 422
        return value

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        resolve_timezone(value)
        return value

    @field_validator("action")
    @classmethod
    def _valid_action(cls, value: dict[str, Any]) -> dict[str, Any]:
        parse_action(value)
        return value

    def typed_action(self) -> ScheduleAction:
        return parse_action(self.action)


class ScheduleUpdate(BaseModel):
    """Patch semantics: `None` means "leave alone".

    `action` is deliberately absent. Editing the arguments of a `tool` schedule
    is editing a standing authorisation, and doing it through a patch would let
    a one-field request widen what a scheduled run may do without ever
    re-stating the whole call. Changing what a schedule *does* means deleting it
    and creating the replacement, which is one deliberate act rather than two
    halves of one.
    """

    model_config = ConfigDict(extra="forbid")

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


@dataclass(frozen=True, slots=True)
class TaskDraft:
    """A task about to be recorded.

    Internal — there is no endpoint that creates one. Plans are produced by the
    scheduler and by nothing else this phase (`API_DESIGN.md` §3.5 specifies no
    `POST /v1/tasks` and no `POST /v1/plans`, which is the same statement from
    the other side).
    """

    title: str
    tool_name: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    description: str | None = None
    max_attempts: int = 3
