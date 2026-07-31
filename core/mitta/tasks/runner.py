"""Running an automation when nobody is watching.

Every property that makes MITTA safe to use interactively was built around a
person being present: a card on screen, a single-use token bound to the
parameters they read, a chain that stops the moment they say no. An unattended
run has none of that, so this module states what it substitutes for each.

**A `prompt` run is capped at `Risk.READ` (DEC-123).** The planner is offered
only the tools that observe, so a scheduled sentence can search, fetch and
summarise, and cannot write, open or close anything. A capability never offered
cannot be requested, which is cheaper than refusing it afterwards — and it means
a model that has been prompt-injected by a web page it fetched at 3am has
nothing to reach for.

**A `tool` run carries an authorisation the user wrote themselves (DEC-122).**
The arguments are frozen in the schedule and the token minted at fire time is
bound to their hash, so the call that runs is the call that was authored or
nothing runs. Destructive tools are refused outright: there is no version of
"unattended" that should include deleting things.

Everything either kind does is recorded as `tasks` rows and, for tool calls, in
`tool_invocations` against the task. An action taken while the user was asleep
has to be inspectable afterwards or it is indistinguishable from one MITTA did
not take.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Final

from mitta.agent.orchestrator import Orchestrator
from mitta.conversations.models import InputKind
from mitta.errors import ConflictError, NotFoundError, PolicyError
from mitta.policy.audit import AuditLog
from mitta.policy.engine import PolicyEngine
from mitta.policy.executor import ToolExecutor
from mitta.tasks.models import (
    Plan,
    PlanStatus,
    PromptAction,
    Schedule,
    Task,
    TaskDraft,
    TaskStatus,
    ToolAction,
)
from mitta.tasks.repository import TaskRepository
from mitta.telemetry.logging import get_logger
from mitta.tools.base import Risk, ToolSpec
from mitta.tools.registry import ToolRegistry

log = get_logger(__name__)

#: The most a scheduled call may do. Compared by tier rather than by equality,
#: so that a tier added above `DESTRUCTIVE` later is excluded by default — the
#: direction a mistake here has to fail in.
MAX_UNATTENDED_RISK = Risk.WRITE

_RISK_ORDER: Final[dict[Risk, int]] = {Risk.READ: 0, Risk.WRITE: 1, Risk.DESTRUCTIVE: 2}

#: How long a single run may take before it is abandoned. A scheduled run has no
#: one waiting on it, which is exactly why it needs a ceiling: a hung provider
#: call would otherwise hold a plan `running` until the process restarts, and
#: the next fire would find its predecessor still going.
RUN_TIMEOUT_SECONDS = 300


class UnattendedRefusalError(PolicyError):
    """A tool that may not run with nobody watching. Carries the reason verbatim.

    The message is shown to the user, so it says what was refused and why rather
    than "not permitted" — a schedule that silently does nothing is the failure
    this whole surface exists to make visible.
    """

    code = "policy.unattended_refused"


def authorised_unattended(spec: ToolSpec) -> None:
    """Whether a tool is eligible for a scheduled run at all.

    Checked when the schedule is created *and* again at every fire. The second
    check is the one that matters: a tool's risk tier can change between
    versions, and a grant written against the old tier must not survive the
    tool becoming more dangerous than the user agreed to.
    """
    if _RISK_ORDER[spec.risk] > _RISK_ORDER[MAX_UNATTENDED_RISK]:
        raise UnattendedRefusalError(
            f"{spec.name} deletes or overwrites, so it cannot be scheduled. "
            f"Destructive actions are only ever run with you watching."
        )


@dataclass(frozen=True, slots=True)
class RunOutcome:
    plan: Plan
    #: Terminal task statuses in the order they finished, for the log line.
    steps: tuple[str, ...] = ()


class TaskRunner:
    """Executes a schedule's action as a plan, and records what happened."""

    def __init__(
        self,
        repository: TaskRepository,
        registry: ToolRegistry,
        executor: ToolExecutor,
        policy: PolicyEngine,
        audit: AuditLog,
        *,
        orchestrator: Orchestrator | None = None,
        timeout_seconds: int = RUN_TIMEOUT_SECONDS,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._executor = executor
        self._policy = policy
        self._audit = audit
        self._orchestrator = orchestrator
        self._timeout = timeout_seconds
        #: Plans currently executing, so `cancel` has something to interrupt.
        self._running: dict[str, asyncio.Task[RunOutcome]] = {}

    # -- entry points ----------------------------------------------------------- #

    def launch(self, schedule: Schedule) -> asyncio.Task[RunOutcome]:
        """Start a run in the background and return its task.

        The scheduler does not await this. A tick that waited for a five-minute
        briefing to finish would be five minutes late noticing everything else,
        and a slow schedule would delay the ones behind it.
        """
        plan = self._repository.create_plan(schedule.name, status=PlanStatus.RUNNING)
        runner = asyncio.create_task(self._guarded(plan, schedule))
        # Held so the task is not garbage-collected mid-flight, which asyncio
        # permits and which would abandon a run silently — and so `cancel` has
        # something to interrupt.
        self._running[plan.id] = runner
        runner.add_done_callback(lambda _: self._running.pop(plan.id, None))
        return runner

    async def run_now(self, schedule: Schedule) -> RunOutcome:
        """Run a schedule immediately and wait for it — `POST /v1/schedules/{id}/run`.

        The same path as a real fire, deliberately. A "test this schedule"
        button that took a different route would only prove that the button
        works.
        """
        return await self.launch(schedule)

    async def resume(self, task_id: str) -> RunOutcome:
        """Re-run a failed task, keeping everything that already succeeded.

        This is a resume in the sense `DATABASE_DESIGN.md` §7 means it: the
        tasks before this one stay `completed` and are not run again, because a
        re-run of a step that already wrote a file is not a resume, it is a
        second write.

        **For a prompt run it is a re-ask, and the difference is worth stating.**
        A prompt plan's steps were chosen by a model rather than written down in
        advance, so there is no partial sequence to continue from — asking again
        is the only honest meaning available, and the model may choose
        differently the second time.
        """
        task = self._repository.get_task(task_id)
        plan = self._repository.get_plan(task.plan_id)

        # `ConflictError`, not a refusal: nothing here is about permission. The
        # request is well-formed and asks for something the current state does
        # not permit, which is a 409 and reads as one in the UI.
        if task.status is not TaskStatus.FAILED:
            raise ConflictError(
                f"{task.title} is {task.status.value}, not failed — there is nothing to resume."
            )
        if task.attempt >= task.max_attempts:
            raise ConflictError(f"{task.title} has already been attempted {task.attempt} times.")

        self._repository.set_plan_status(plan.id, PlanStatus.RUNNING)
        started = self._repository.start_task(task_id)

        if started.tool_name is None:
            # The turn task of a prompt plan. Its children were the tool calls
            # the last attempt made; they are left alone as the record of that
            # attempt, and the new one appends its own.
            outcome = await self._run_prompt(plan, PromptAction(text=plan.goal), turn_task=started)
        else:
            outcome = await self._run_tool(
                plan, ToolAction(tool=started.tool_name, params=started.params), task=started
            )
        return outcome

    def cancel(self, task_id: str) -> Plan:
        """Stop the plan this task belongs to.

        Cancels at the asyncio level rather than setting a flag a loop checks.
        A run spends almost all of its time inside a provider call or a tool, so
        a cooperative flag would be read minutes after the user pressed the
        button — and "cancel" that takes four minutes is a button people press
        twice.
        """
        task = self._repository.get_task(task_id)
        runner = self._running.get(task.plan_id)
        if runner is not None:
            runner.cancel()

        for pending in self._repository.tasks_for(task.plan_id):
            if not pending.finished:
                self._repository.finish_task(
                    pending.id,
                    TaskStatus.SKIPPED,
                    error={"code": "run.cancelled", "message": "Cancelled."},
                )
        plan = self._repository.set_plan_status(task.plan_id, PlanStatus.CANCELLED)
        log.info("plan.cancelled", extra={"plan_id": plan.id, "task_id": task_id})
        return plan

    # -- execution -------------------------------------------------------------- #

    async def _guarded(self, plan: Plan, schedule: Schedule) -> RunOutcome:
        """One run, with every exit closing the plan row.

        A plan left `running` is the same defect as a turn left `running`
        (DEC-069): the surface shows work in progress that no process is doing,
        and nothing will ever resolve it because the thing that would have is
        gone.
        """
        started = time.monotonic()
        try:
            action = schedule.typed_action()
        except ValueError as exc:
            return self._fail_plan(plan, "schedule.invalid_action", str(exc))

        self._audit.record(
            actor="system",
            action="schedule.fired",
            subject=schedule.id,
            detail={"plan_id": plan.id, "name": schedule.name, "kind": action.kind},
        )

        try:
            async with asyncio.timeout(self._timeout):
                if isinstance(action, ToolAction):
                    outcome = await self._run_tool(plan, action)
                else:
                    outcome = await self._run_prompt(plan, action)
        except asyncio.CancelledError:
            # Cancellation already wrote the terminal statuses in `cancel`;
            # re-raising keeps the asyncio contract intact.
            raise
        except TimeoutError:
            return self._fail_plan(
                plan, "run.timeout", f"The run passed {self._timeout}s and was stopped."
            )
        except Exception as exc:  # pragma: no cover - defence in depth
            log.exception("plan.unexpected_failure", extra={"plan_id": plan.id})
            return self._fail_plan(plan, "internal.error", str(exc))

        log.info(
            "plan.finished",
            extra={
                "plan_id": plan.id,
                "schedule_id": schedule.id,
                "status": outcome.plan.status.value,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "steps": len(outcome.steps),
            },
        )
        return outcome

    async def _run_tool(
        self, plan: Plan, action: ToolAction, *, task: Task | None = None
    ) -> RunOutcome:
        """Execute one pre-authorised call.

        The authorisation is minted here, from the arguments stored on the
        schedule — never from anything a model produced, because nothing a model
        produced is involved. The executor re-derives the hash from the
        arguments it is about to use, so the two have to agree.
        """
        if task is None:
            task = self._repository.add_task(
                plan.id,
                TaskDraft(
                    title=f"{action.tool}",
                    tool_name=action.tool,
                    params=action.params,
                    description="Scheduled tool call",
                ),
            )
            task = self._repository.start_task(task.id)

        try:
            tool = self._registry.get(action.tool)
        except NotFoundError:
            return self._fail_task(
                plan, task, "tool.unknown", f"There is no tool called {action.tool}."
            )

        spec = tool.spec
        try:
            authorised_unattended(spec)
        except UnattendedRefusalError as exc:
            return self._fail_task(plan, task, exc.code, exc.message)

        decision = self._policy.evaluate(spec, action.params)
        if decision.refused:
            # A standing refusal outranks the schedule. The user excluded that
            # path after authoring the automation, and the later instruction is
            # the one that counts (DEC-114).
            return self._fail_task(
                plan,
                task,
                "policy.refused",
                decision.prompt or f"{action.tool} is not permitted: {decision.reason}",
            )

        approval_id: str | None = None
        signature: str | None = None
        if decision.needs_confirmation:
            token = self._policy.request_approval(spec, action.params)
            approval_id = str(token["id"])
            signature = str(token["signature"])
            self._audit.record(
                actor="user",
                action="schedule.authorisation_used",
                subject=spec.name,
                verdict="allow",
                detail={
                    "plan_id": plan.id,
                    "task_id": task.id,
                    "approval_id": approval_id,
                    # Names the source of the authority. Without it the audit
                    # log says the user approved a write at 03:00, which is true
                    # only in the sense that they wrote the schedule.
                    "granted_by": "schedule",
                },
            )

        execution = await self._executor.execute(
            action.tool,
            action.params,
            task_id=task.id,
            approval_id=approval_id,
            signature=signature,
        )

        if not execution.result.ok:
            return self._fail_task(plan, task, "tool.failed", execution.result.content)

        self._repository.finish_task(
            task.id,
            TaskStatus.COMPLETED,
            result={
                "content": execution.result.content[:1000],
                "invocation_id": execution.invocation_id,
            },
        )
        self._repository.checkpoint(
            task.id,
            action.tool,
            {"invocation_id": execution.invocation_id, "ok": True},
        )
        return RunOutcome(
            self._repository.set_plan_status(plan.id, PlanStatus.COMPLETED),
            (TaskStatus.COMPLETED.value,),
        )

    async def _run_prompt(
        self, plan: Plan, action: PromptAction, *, turn_task: Task | None = None
    ) -> RunOutcome:
        """Run a sentence through the agent with nobody there to answer it."""
        if self._orchestrator is None:
            return self._fail_plan(
                plan, "agent.unavailable", "Reasoning is not available in this process."
            )

        if turn_task is None:
            turn_task = self._repository.add_task(
                plan.id,
                TaskDraft(title=action.text, description="Scheduled prompt"),
            )
            turn_task = self._repository.start_task(turn_task.id)

        steps: list[str] = []
        current: Task | None = None
        previous_step: str | None = None
        failure: dict[str, Any] | None = None
        message_id: str | None = None
        answer_chars = 0

        async for event in self._orchestrator.run(
            text=action.text,
            conversation_id=action.conversation_id,
            input_kind=InputKind.SCHEDULED,
            unattended=True,
        ):
            if event.type == "turn.accepted":
                conversation_id = str(event.data.get("conversation_id") or "")
                if conversation_id:
                    self._repository.attach_conversation(plan.id, conversation_id)

            elif event.type == "turn.tool_started":
                current = self._repository.add_task(
                    plan.id,
                    TaskDraft(
                        title=str(event.data.get("tool") or "tool"),
                        tool_name=str(event.data.get("tool") or ""),
                        params=_params_of(event.data),
                        description="Chosen by the planner",
                    ),
                    parent_id=turn_task.id,
                    # A linear chain: each step could only have been chosen
                    # after seeing the previous one's result, and the edges say
                    # so. A producer that emits genuine fan-out would write a
                    # different shape into the same table.
                    depends_on=previous_step,
                )
                current = self._repository.start_task(current.id)

            elif event.type == "turn.tool_finished" and current is not None:
                ok = bool(event.data.get("ok"))
                repeated = bool(event.data.get("repeated"))
                self._repository.finish_task(
                    current.id,
                    TaskStatus.SKIPPED
                    if repeated
                    else (TaskStatus.COMPLETED if ok else TaskStatus.FAILED),
                    result={
                        "summary": str(event.data.get("summary") or "")[:1000],
                        "invocation_id": event.data.get("invocation_id"),
                        "repeated": repeated,
                    },
                )
                # Written against the turn task, not the step: it is the record
                # of how far the run got, and the step it describes is already
                # closed.
                self._repository.checkpoint(
                    turn_task.id,
                    str(event.data.get("tool") or "tool"),
                    {"step": len(steps) + 1, "ok": ok, "repeated": repeated},
                )
                steps.append(current.id)
                previous_step = current.id
                current = None

            elif event.type == "turn.message":
                message_id = str(event.data.get("message_id") or "") or None
                answer_chars = len(str(event.data.get("content") or ""))

            elif event.type == "turn.error":
                failure = {
                    "code": str(event.data.get("code") or "turn.failed"),
                    "message": str(event.data.get("message") or "The run failed."),
                }

        if failure is not None:
            return self._fail_task(plan, turn_task, failure["code"], failure["message"])

        if message_id is None:
            # No error and no answer. Rare, and worth failing rather than
            # recording a successful run that produced nothing to read.
            return self._fail_task(plan, turn_task, "turn.empty", "The run produced no answer.")

        self._repository.finish_task(
            turn_task.id,
            TaskStatus.COMPLETED,
            result={"message_id": message_id, "chars": answer_chars, "tool_calls": len(steps)},
        )
        return RunOutcome(
            self._repository.set_plan_status(plan.id, PlanStatus.COMPLETED),
            tuple(steps),
        )

    # -- failure paths ----------------------------------------------------------- #

    def _fail_task(self, plan: Plan, task: Task, code: str, message: str) -> RunOutcome:
        self._repository.finish_task(
            task.id, TaskStatus.FAILED, error={"code": code, "message": message}
        )
        log.warning(
            "task.failed",
            extra={"plan_id": plan.id, "task_id": task.id, "code": code},
        )
        return RunOutcome(self._repository.set_plan_status(plan.id, PlanStatus.FAILED), ())

    def _fail_plan(self, plan: Plan, code: str, message: str) -> RunOutcome:
        """Fail a plan that never got as far as having a task.

        A row with a goal, a failure and no steps is still the answer to "did my
        schedule run this morning?", which is the question the surface exists
        for.
        """
        task = self._repository.add_task(plan.id, TaskDraft(title=plan.goal))
        self._repository.finish_task(
            task.id, TaskStatus.FAILED, error={"code": code, "message": message}
        )
        log.warning("plan.failed", extra={"plan_id": plan.id, "code": code})
        return RunOutcome(self._repository.set_plan_status(plan.id, PlanStatus.FAILED), ())


def _params_of(data: dict[str, object]) -> dict[str, Any]:
    """The `params` field of a tool event, or an empty dict.

    Event payloads are `dict[str, object]` by contract, so what arrives here is
    whatever the planner put in — and a task row recording `params` as a string
    because a malformed event carried one is worse than a task with no
    arguments, which at least reads as unknown rather than as a call nobody
    made with those values.
    """
    params = data.get("params")
    return dict(params) if isinstance(params, dict) else {}
