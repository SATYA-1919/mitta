"""Tasks, plans, schedules and the loop that fires them.

The claims this phase makes are all about what happens with nobody watching, so
that is what these test: that a due schedule fires once rather than twice, that
a scheduled write runs under an authorisation bound to the arguments the user
wrote, that a destructive tool cannot be scheduled at all, and that a prompt run
is never offered a tool it would need permission for.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from mitta.agent.orchestrator import Orchestrator, TurnEvent
from mitta.agent.planner import Plan as PlannerPlan
from mitta.conversations.models import ConversationDraft, InputKind
from mitta.conversations.repository import ConversationRepository
from mitta.errors import ConflictError, ValidationError
from mitta.persistence.database import Database
from mitta.policy.audit import AuditLog
from mitta.policy.broker import ApprovalBroker
from mitta.policy.engine import PolicyEngine
from mitta.policy.executor import ToolExecutor
from mitta.projects.models import PathKind, ProjectDraft, ProjectPathDraft
from mitta.projects.repository import ProjectRepository
from mitta.tasks.models import PlanStatus, ScheduleDraft, TaskDraft, TaskStatus
from mitta.tasks.repository import TaskRepository
from mitta.tasks.runner import TaskRunner
from mitta.tasks.scheduler import Scheduler
from mitta.tools.base import Risk, ToolResult, ToolSpec
from mitta.tools.registry import ToolRegistry

pytestmark = pytest.mark.asyncio

#: A fixed instant, so "next run" assertions do not drift with the clock.
#: 2026-06-01 07:00 UTC, a Monday.
NOW = 1_780_297_200


class FakeTool:
    """Records what it was run with, so an unauthorised call is visible."""

    def __init__(
        self,
        name: str,
        *,
        risk: Risk = Risk.READ,
        path_params: tuple[str, ...] = (),
        ok: bool = True,
    ) -> None:
        self._spec = ToolSpec(
            name=name,
            description=name,
            risk=risk,
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            path_params=path_params,
        )
        self._ok = ok
        self.runs: list[dict[str, Any]] = []

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def run(self, params: dict[str, Any]) -> ToolResult:
        self.runs.append(params)
        return ToolResult(ok=self._ok, content="done" if self._ok else "no")


def schedule_draft(**overrides: Any) -> ScheduleDraft:
    body: dict[str, Any] = {
        "name": "Morning briefing",
        "cron": "0 8 * * *",
        "timezone": "Europe/London",
        "action": {"kind": "prompt", "text": "what happened overnight"},
    }
    body.update(overrides)
    return ScheduleDraft(**body)


def tool_action(tool: str, **params: Any) -> dict[str, Any]:
    return {"kind": "tool", "tool": tool, "params": params}


# ── Schedules ──────────────────────────────────────────────────────────────


class TestSchedules:
    async def test_creating_one_computes_when_it_next_fires(
        self, task_repository: TaskRepository
    ) -> None:
        schedule = task_repository.create_schedule(schedule_draft(), now=NOW)
        assert schedule.next_run_at is not None
        assert schedule.next_run_at > NOW
        assert schedule.enabled is True

    async def test_a_disabled_schedule_has_no_next_run(
        self, task_repository: TaskRepository
    ) -> None:
        """A time beside a schedule that cannot fire is a promise nothing keeps."""
        schedule = task_repository.create_schedule(schedule_draft(enabled=False), now=NOW)
        assert schedule.next_run_at is None

    async def test_claiming_advances_the_clock_so_a_fire_cannot_repeat(
        self, task_repository: TaskRepository
    ) -> None:
        """The property that makes a fire exactly-once.

        Two ticks in the same second — or one overlapping its predecessor —
        would otherwise both read the row as due, and for a schedule that writes
        a file that means writing it twice.
        """
        task_repository.create_schedule(schedule_draft(cron="* * * * *"), now=NOW)

        first = task_repository.claim_due(now=NOW + 120)
        second = task_repository.claim_due(now=NOW + 120)

        assert len(first) == 1
        assert second == []
        assert first[0].next_run_at is not None
        assert first[0].next_run_at > NOW + 120

    async def test_a_failed_run_does_not_rewind_the_clock(
        self, task_repository: TaskRepository
    ) -> None:
        """Claiming is what advances the timetable, not completing.

        A retry that reran an automation because its last attempt errored is how
        a half-completed action becomes a duplicated one.
        """
        task_repository.create_schedule(schedule_draft(cron="* * * * *"), now=NOW)
        claimed = task_repository.claim_due(now=NOW + 120)[0]
        stored = task_repository.get_schedule(claimed.id)
        assert stored.next_run_at == claimed.next_run_at
        assert stored.last_run_at == NOW + 120

    async def test_a_row_that_can_no_longer_be_scheduled_is_disabled_not_raised(
        self, task_repository: TaskRepository, migrated: Database
    ) -> None:
        """One bad schedule must not stop the tick that carries every other one."""
        schedule = task_repository.create_schedule(schedule_draft(cron="* * * * *"), now=NOW)
        other = task_repository.create_schedule(
            schedule_draft(name="fine", cron="* * * * *"), now=NOW
        )
        # Edited outside the API — a hand-repaired database, or a zone the OS
        # dropped in an update.
        with migrated.write() as conn:
            conn.execute("UPDATE schedules SET cron = ? WHERE id = ?", ("nonsense", schedule.id))

        claimed = task_repository.claim_due(now=NOW + 120)

        assert [item.id for item in claimed] == [other.id]
        assert task_repository.get_schedule(schedule.id).enabled is False

    async def test_retiming_recomputes_the_next_fire(
        self, task_repository: TaskRepository
    ) -> None:
        schedule = task_repository.create_schedule(schedule_draft(), now=NOW)
        moved = task_repository.update_schedule(schedule.id, cron="0 20 * * *", now=NOW)
        assert moved.next_run_at != schedule.next_run_at

    async def test_disabling_clears_the_next_fire_and_enabling_restores_it(
        self, task_repository: TaskRepository
    ) -> None:
        schedule = task_repository.create_schedule(schedule_draft(), now=NOW)
        off = task_repository.update_schedule(schedule.id, enabled=False, now=NOW)
        assert off.next_run_at is None

        on = task_repository.update_schedule(schedule.id, enabled=True, now=NOW)
        assert on.next_run_at == schedule.next_run_at

    async def test_deleting_keeps_the_runs_it_already_produced(
        self, task_repository: TaskRepository
    ) -> None:
        """The automation is the grant; the plans are the evidence of what it did."""
        schedule = task_repository.create_schedule(schedule_draft(), now=NOW)
        plan = task_repository.create_plan(schedule.name, now=NOW)
        task_repository.delete_schedule(schedule.id)
        assert task_repository.get_plan(plan.id).goal == schedule.name

    async def test_an_unparseable_expression_is_refused_at_the_draft(self) -> None:
        with pytest.raises(Exception, match=r"cron|expected 5"):
            schedule_draft(cron="every morning")

    async def test_an_unknown_action_kind_is_refused(self) -> None:
        with pytest.raises(Exception, match="unknown action kind"):
            schedule_draft(action={"kind": "shell", "cmd": "rm -rf /"})


# ── Plans, tasks and the graph ─────────────────────────────────────────────


class TestPlansAndTasks:
    async def test_ordinals_are_assigned_in_insertion_order(
        self, task_repository: TaskRepository
    ) -> None:
        plan = task_repository.create_plan("goal", now=NOW)
        first = task_repository.add_task(plan.id, TaskDraft(title="one"), now=NOW)
        second = task_repository.add_task(plan.id, TaskDraft(title="two"), now=NOW)
        assert [first.ordinal, second.ordinal] == [0, 1]

    async def test_a_dependency_cycle_is_refused(self, task_repository: TaskRepository) -> None:
        """A cycle found at execution time is a plan that hangs, and the
        producer that will eventually emit one is a language model."""
        plan = task_repository.create_plan("goal", now=NOW)
        a = task_repository.add_task(plan.id, TaskDraft(title="a"), now=NOW)
        b = task_repository.add_task(plan.id, TaskDraft(title="b"), now=NOW)
        c = task_repository.add_task(plan.id, TaskDraft(title="c"), now=NOW)

        task_repository.add_dependency(b.id, a.id)
        task_repository.add_dependency(c.id, b.id)

        with pytest.raises(ValidationError, match="cycle"):
            task_repository.add_dependency(a.id, c.id)

    async def test_a_task_cannot_depend_on_itself(self, task_repository: TaskRepository) -> None:
        plan = task_repository.create_plan("goal", now=NOW)
        task = task_repository.add_task(plan.id, TaskDraft(title="a"), now=NOW)
        with pytest.raises(ValidationError):
            task_repository.add_dependency(task.id, task.id)

    async def test_starting_a_task_spends_an_attempt(
        self, task_repository: TaskRepository
    ) -> None:
        """Counted on start, not on failure, so a crash loop cannot retry forever."""
        plan = task_repository.create_plan("goal", now=NOW)
        task = task_repository.add_task(plan.id, TaskDraft(title="a"), now=NOW)
        assert task_repository.start_task(task.id, now=NOW).attempt == 1

    async def test_active_means_every_unfinished_status(
        self, task_repository: TaskRepository
    ) -> None:
        """A task waiting on an approval is the one a user most needs to see,
        and it is not running by any definition."""
        plan = task_repository.create_plan("goal", now=NOW)
        waiting = task_repository.add_task(
            plan.id, TaskDraft(title="waiting"), status=TaskStatus.AWAITING_APPROVAL, now=NOW
        )
        done = task_repository.add_task(plan.id, TaskDraft(title="done"), now=NOW)
        task_repository.finish_task(done.id, TaskStatus.COMPLETED, now=NOW)

        active = task_repository.recent_tasks(active_only=True)
        assert [task.id for task in active] == [waiting.id]

    async def test_a_crash_leaves_no_plan_running(self, task_repository: TaskRepository) -> None:
        """The same defect as a turn left running: work in progress that no
        process is doing, and nothing will ever resolve it."""
        plan = task_repository.create_plan("goal", status=PlanStatus.RUNNING, now=NOW)
        task = task_repository.add_task(plan.id, TaskDraft(title="a"), now=NOW)
        task_repository.start_task(task.id, now=NOW)

        assert task_repository.reconcile_orphaned_runs(now=NOW) == 1
        assert task_repository.get_plan(plan.id).status is PlanStatus.FAILED
        assert task_repository.get_task(task.id).status is TaskStatus.FAILED

    async def test_checkpoints_come_back_newest_first(
        self, task_repository: TaskRepository
    ) -> None:
        plan = task_repository.create_plan("goal", now=NOW)
        task = task_repository.add_task(plan.id, TaskDraft(title="a"), now=NOW)
        task_repository.checkpoint(task.id, "one", {"step": 1}, now=NOW)
        task_repository.checkpoint(task.id, "two", {"step": 2}, now=NOW)

        latest = task_repository.latest_checkpoint(task.id)
        assert latest is not None and latest.state == {"step": 2}


# ── Running a tool action ──────────────────────────────────────────────────


class TestToolRuns:
    async def test_a_read_tool_runs_with_no_approval_at_all(
        self,
        task_repository: TaskRepository,
        tool_registry: ToolRegistry,
        task_runner: TaskRunner,
    ) -> None:
        tool = FakeTool("web_search")
        tool_registry.register(tool)
        schedule = task_repository.create_schedule(
            schedule_draft(action=tool_action("web_search", path="q")), now=NOW
        )

        outcome = await task_runner.run_now(schedule)

        assert outcome.plan.status is PlanStatus.COMPLETED
        assert tool.runs == [{"path": "q"}]

    async def test_a_write_runs_under_a_token_bound_to_the_stored_arguments(
        self,
        task_repository: TaskRepository,
        tool_registry: ToolRegistry,
        task_runner: TaskRunner,
        migrated: Database,
    ) -> None:
        """DEC-122: authoring the schedule is the authorisation.

        The token is minted from the row, and the executor re-derives the hash
        from the arguments it is about to use — so the call that runs is the
        call that was authored, or nothing runs.
        """
        tool = FakeTool("write_note", risk=Risk.WRITE)
        tool_registry.register(tool)
        schedule = task_repository.create_schedule(
            schedule_draft(action=tool_action("write_note", path="week.md")), now=NOW
        )

        outcome = await task_runner.run_now(schedule)

        assert outcome.plan.status is PlanStatus.COMPLETED
        assert tool.runs == [{"path": "week.md"}]
        with migrated.read() as conn:
            token = conn.execute(
                "SELECT tool_name, consumed_at FROM approval_tokens ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        # Minted and burned in the same run. A token left unconsumed would be a
        # live authorisation sitting behind the user.
        assert token["tool_name"] == "write_note"
        assert token["consumed_at"] is not None

    async def test_the_invocation_is_attributable_to_its_task(
        self,
        task_repository: TaskRepository,
        tool_registry: ToolRegistry,
        task_runner: TaskRunner,
        migrated: Database,
    ) -> None:
        """"What did MITTA do while I was away" is answered by `task_id`."""
        tool_registry.register(FakeTool("write_note", risk=Risk.WRITE))
        schedule = task_repository.create_schedule(
            schedule_draft(action=tool_action("write_note", path="week.md")), now=NOW
        )

        outcome = await task_runner.run_now(schedule)
        tasks = task_repository.tasks_for(outcome.plan.id)

        with migrated.read() as conn:
            row = conn.execute(
                "SELECT task_id, turn_id FROM tool_invocations ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        assert row["task_id"] == tasks[0].id
        assert row["turn_id"] is None

    async def test_a_destructive_tool_cannot_be_scheduled(
        self,
        task_repository: TaskRepository,
        tool_registry: ToolRegistry,
        task_runner: TaskRunner,
    ) -> None:
        """Re-checked at every fire, not only at creation: a tool's risk tier can
        change between versions, and a grant must not survive it getting worse."""
        tool = FakeTool("delete_everything", risk=Risk.DESTRUCTIVE)
        tool_registry.register(tool)
        schedule = task_repository.create_schedule(
            schedule_draft(action=tool_action("delete_everything")), now=NOW
        )

        outcome = await task_runner.run_now(schedule)
        task = task_repository.tasks_for(outcome.plan.id)[0]

        assert outcome.plan.status is PlanStatus.FAILED
        assert task.error is not None
        assert task.error["code"] == "policy.unattended_refused"
        assert tool.runs == []

    async def test_an_exclusion_added_later_beats_the_schedule(
        self,
        task_repository: TaskRepository,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        policy_engine: PolicyEngine,
        migrated_audit: AuditLog,
        projects: ProjectRepository,
        tmp_path: Path,
    ) -> None:
        """The later instruction is the one that counts (DEC-114).

        A standing authorisation is not a way around an exclusion — the refusal
        is checked before the token, so excluding a path after authoring the
        schedule stops it.
        """
        secret = tmp_path / "secrets.md"
        project = projects.create(ProjectDraft(name="work"))
        projects.add_path(
            project.id, ProjectPathDraft(path=str(tmp_path), kind=PathKind.ROOT, writable=True)
        )
        projects.add_path(
            project.id, ProjectPathDraft(path=str(secret), kind=PathKind.EXCLUDED)
        )

        tool = FakeTool("write_note", risk=Risk.WRITE, path_params=("path",))
        tool_registry.register(tool)
        runner = TaskRunner(
            task_repository, tool_registry, tool_executor, policy_engine, migrated_audit
        )
        schedule = task_repository.create_schedule(
            schedule_draft(action=tool_action("write_note", path=str(secret))), now=NOW
        )

        outcome = await runner.run_now(schedule)
        task = task_repository.tasks_for(outcome.plan.id)[0]

        assert outcome.plan.status is PlanStatus.FAILED
        assert task.error is not None and task.error["code"] == "policy.refused"
        assert tool.runs == []

    async def test_a_tool_that_no_longer_exists_fails_the_plan_honestly(
        self, task_repository: TaskRepository, task_runner: TaskRunner
    ) -> None:
        schedule = task_repository.create_schedule(
            schedule_draft(action=tool_action("gone")), now=NOW
        )
        outcome = await task_runner.run_now(schedule)
        task = task_repository.tasks_for(outcome.plan.id)[0]
        assert outcome.plan.status is PlanStatus.FAILED
        assert task.error is not None and task.error["code"] == "tool.unknown"

    async def test_the_fire_is_audited_with_the_schedule_as_its_source(
        self,
        task_repository: TaskRepository,
        tool_registry: ToolRegistry,
        task_runner: TaskRunner,
        migrated_audit: AuditLog,
    ) -> None:
        """Without `granted_by`, the log says the user approved a write at 03:00 —
        true only in the sense that they wrote the schedule."""
        tool_registry.register(FakeTool("write_note", risk=Risk.WRITE))
        schedule = task_repository.create_schedule(
            schedule_draft(action=tool_action("write_note", path="week.md")), now=NOW
        )

        await task_runner.run_now(schedule)

        actions = {entry.action for entry in migrated_audit.recent(limit=50)}
        assert "schedule.fired" in actions
        assert "schedule.authorisation_used" in actions


# ── Running a prompt action ────────────────────────────────────────────────


class ScriptedOrchestrator:
    """Emits a prepared event stream, and records how it was called."""

    def __init__(self, events: list[TurnEvent]) -> None:
        self._events = events
        self.unattended: bool | None = None
        self.input_kind: InputKind | None = None

    def run(
        self,
        *,
        text: str,
        conversation_id: str | None = None,
        input_kind: InputKind = InputKind.TEXT,
        unattended: bool = False,
    ) -> AsyncIterator[TurnEvent]:
        self.unattended = unattended
        self.input_kind = input_kind

        async def stream() -> AsyncIterator[TurnEvent]:
            for event in self._events:
                yield event

        return stream()


class TestPromptRuns:
    def _runner(
        self,
        task_repository: TaskRepository,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        policy_engine: PolicyEngine,
        audit: AuditLog,
        orchestrator: Any,
    ) -> TaskRunner:
        return TaskRunner(
            task_repository,
            tool_registry,
            tool_executor,
            policy_engine,
            audit,
            orchestrator=orchestrator,
        )

    async def test_the_run_is_marked_unattended_and_scheduled(
        self,
        task_repository: TaskRepository,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        policy_engine: PolicyEngine,
        migrated_audit: AuditLog,
        conversations: ConversationRepository,
    ) -> None:
        conversation = conversations.create(ConversationDraft())
        orchestrator = ScriptedOrchestrator(
            [
                TurnEvent("turn.accepted", {"conversation_id": conversation.id}),
                TurnEvent("turn.message", {"message_id": "msg_1", "content": "all quiet"}),
            ]
        )
        runner = self._runner(
            task_repository,
            tool_registry,
            tool_executor,
            policy_engine,
            migrated_audit,
            orchestrator,
        )
        schedule = task_repository.create_schedule(schedule_draft(), now=NOW)

        outcome = await runner.run_now(schedule)

        assert orchestrator.unattended is True
        assert orchestrator.input_kind is InputKind.SCHEDULED
        assert outcome.plan.status is PlanStatus.COMPLETED
        # The answer is read in the thread it was written into, so the plan has
        # to point at it.
        assert task_repository.get_plan(outcome.plan.id).conversation_id == conversation.id

    async def test_every_tool_call_becomes_a_task_in_order(
        self,
        task_repository: TaskRepository,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        policy_engine: PolicyEngine,
        migrated_audit: AuditLog,
        conversations: ConversationRepository,
    ) -> None:
        conversation = conversations.create(ConversationDraft())
        orchestrator = ScriptedOrchestrator(
            [
                TurnEvent("turn.accepted", {"conversation_id": conversation.id}),
                TurnEvent("turn.tool_started", {"tool": "web_search", "params": {"q": "news"}}),
                TurnEvent(
                    "turn.tool_finished",
                    {"tool": "web_search", "ok": True, "summary": "…", "invocation_id": "inv_1"},
                ),
                TurnEvent("turn.tool_started", {"tool": "open_url", "params": {"url": "x"}}),
                TurnEvent(
                    "turn.tool_finished",
                    {"tool": "open_url", "ok": True, "summary": "…", "invocation_id": "inv_2"},
                ),
                TurnEvent("turn.message", {"message_id": "msg_1", "content": "here you go"}),
            ]
        )
        runner = self._runner(
            task_repository,
            tool_registry,
            tool_executor,
            policy_engine,
            migrated_audit,
            orchestrator,
        )
        schedule = task_repository.create_schedule(schedule_draft(), now=NOW)

        outcome = await runner.run_now(schedule)
        tasks = task_repository.tasks_for(outcome.plan.id)

        assert [task.tool_name for task in tasks] == [None, "web_search", "open_url"]
        assert all(task.status is TaskStatus.COMPLETED for task in tasks)
        # A linear chain: each step could only have been chosen after seeing the
        # previous one's result, and the edges say so.
        assert task_repository.dependencies(outcome.plan.id) == [(tasks[2].id, tasks[1].id)]
        # Children of the turn task, which is what `parent_id` is for.
        assert tasks[1].parent_id == tasks[0].id

    async def test_a_failed_turn_fails_the_plan_with_the_reason(
        self,
        task_repository: TaskRepository,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        policy_engine: PolicyEngine,
        migrated_audit: AuditLog,
    ) -> None:
        orchestrator = ScriptedOrchestrator(
            [TurnEvent("turn.error", {"code": "provider.unavailable", "message": "no key"})]
        )
        runner = self._runner(
            task_repository,
            tool_registry,
            tool_executor,
            policy_engine,
            migrated_audit,
            orchestrator,
        )
        schedule = task_repository.create_schedule(schedule_draft(), now=NOW)

        outcome = await runner.run_now(schedule)
        task = task_repository.tasks_for(outcome.plan.id)[0]

        assert outcome.plan.status is PlanStatus.FAILED
        assert task.error is not None and task.error["code"] == "provider.unavailable"

    async def test_without_reasoning_the_plan_says_so_rather_than_hanging(
        self, task_repository: TaskRepository, task_runner: TaskRunner
    ) -> None:
        schedule = task_repository.create_schedule(schedule_draft(), now=NOW)
        outcome = await task_runner.run_now(schedule)
        task = task_repository.tasks_for(outcome.plan.id)[0]
        assert outcome.plan.status is PlanStatus.FAILED
        assert task.error is not None and task.error["code"] == "agent.unavailable"


class TestUnattendedCeiling:
    """The orchestrator half of DEC-123."""

    async def test_an_unattended_turn_is_offered_read_tools_only(
        self,
        conversations: ConversationRepository,
        memory_service: Any,
        gateway: Any,
        policy_engine: PolicyEngine,
    ) -> None:
        """A capability never offered cannot be requested — and an approval card
        nobody is there to see would hang the run until the broker timed out.

        The orchestrator here is wired *with* a broker, so it is capable of
        offering `WRITE` and does so on an ordinary turn. The unattended run is
        what takes that away, which is the assertion that matters: a test built
        on an orchestrator that could not ask anyway would pass without the
        behaviour existing.
        """
        seen: list[tuple[Risk, bool]] = []

        class SpyPlanner:
            async def run(
                self, *, text: str, turn_id: str, ceiling: Risk, ask: Any = None
            ) -> AsyncIterator[PlannerPlan]:
                seen.append((ceiling, ask is not None))
                yield PlannerPlan()

        orchestrator = Orchestrator(
            conversations,
            memory_service,
            gateway,
            broker=ApprovalBroker(),
            policy=policy_engine,
            planner=SpyPlanner(),  # type: ignore[arg-type]
        )

        async for _ in orchestrator.run(text="search for the news"):
            pass
        async for _ in orchestrator.run(text="search for the news", unattended=True):
            pass

        assert seen == [(Risk.WRITE, True), (Risk.READ, False)]


# ── Cancel and resume ──────────────────────────────────────────────────────


class TestCancelAndResume:
    async def test_cancelling_skips_everything_unfinished(
        self, task_repository: TaskRepository, task_runner: TaskRunner
    ) -> None:
        """A plan is a sequence whose later steps were chosen for the earlier
        ones, so stopping one and continuing would carry out a request the user
        has just interrupted."""
        plan = task_repository.create_plan("goal", status=PlanStatus.RUNNING, now=NOW)
        done = task_repository.add_task(plan.id, TaskDraft(title="done"), now=NOW)
        task_repository.finish_task(done.id, TaskStatus.COMPLETED, now=NOW)
        pending = task_repository.add_task(plan.id, TaskDraft(title="pending"), now=NOW)

        cancelled = task_runner.cancel(pending.id)

        assert cancelled.status is PlanStatus.CANCELLED
        assert task_repository.get_task(pending.id).status is TaskStatus.SKIPPED
        # Untouched: it really did happen.
        assert task_repository.get_task(done.id).status is TaskStatus.COMPLETED

    async def test_resuming_reruns_the_failed_step_and_leaves_the_rest(
        self,
        task_repository: TaskRepository,
        tool_registry: ToolRegistry,
        task_runner: TaskRunner,
    ) -> None:
        """A re-run of a step that already wrote a file is not a resume, it is a
        second write — so the completed step keeps its status and its result."""
        tool = FakeTool("write_note", risk=Risk.WRITE)
        tool_registry.register(tool)

        plan = task_repository.create_plan("goal", status=PlanStatus.FAILED, now=NOW)
        earlier = task_repository.add_task(plan.id, TaskDraft(title="search"), now=NOW)
        task_repository.finish_task(earlier.id, TaskStatus.COMPLETED, result={"n": 1}, now=NOW)
        failed = task_repository.add_task(
            plan.id,
            TaskDraft(title="write_note", tool_name="write_note", params={"path": "week.md"}),
            now=NOW,
        )
        task_repository.start_task(failed.id, now=NOW)
        task_repository.finish_task(failed.id, TaskStatus.FAILED, error={"code": "x"}, now=NOW)

        outcome = await task_runner.resume(failed.id)

        assert outcome.plan.status is PlanStatus.COMPLETED
        assert tool.runs == [{"path": "week.md"}]
        assert task_repository.get_task(earlier.id).result == {"n": 1}
        assert task_repository.get_task(failed.id).attempt == 2

    async def test_resuming_something_that_did_not_fail_is_a_conflict(
        self, task_repository: TaskRepository, task_runner: TaskRunner
    ) -> None:
        plan = task_repository.create_plan("goal", now=NOW)
        task = task_repository.add_task(plan.id, TaskDraft(title="a"), now=NOW)
        task_repository.finish_task(task.id, TaskStatus.COMPLETED, now=NOW)

        with pytest.raises(ConflictError, match="nothing to resume"):
            await task_runner.resume(task.id)

    async def test_attempts_run_out(
        self, task_repository: TaskRepository, task_runner: TaskRunner
    ) -> None:
        plan = task_repository.create_plan("goal", now=NOW)
        task = task_repository.add_task(
            plan.id, TaskDraft(title="a", max_attempts=1, tool_name="gone"), now=NOW
        )
        task_repository.start_task(task.id, now=NOW)
        task_repository.finish_task(task.id, TaskStatus.FAILED, error={"code": "x"}, now=NOW)

        with pytest.raises(ConflictError, match="already been attempted"):
            await task_runner.resume(task.id)


# ── The tick ───────────────────────────────────────────────────────────────


class TestScheduler:
    async def test_a_tick_launches_what_is_due(
        self,
        task_repository: TaskRepository,
        tool_registry: ToolRegistry,
        task_runner: TaskRunner,
    ) -> None:
        tool_registry.register(FakeTool("web_search"))
        task_repository.create_schedule(
            schedule_draft(cron="* * * * *", action=tool_action("web_search")), now=NOW
        )
        scheduler = Scheduler(task_repository, task_runner)

        started = scheduler.tick(now=NOW + 120)
        # The run itself is a task; give the loop a turn to finish it.
        await asyncio.sleep(0)
        await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()])

        assert started == 1
        assert task_repository.list_plans()[0].status is PlanStatus.COMPLETED

    async def test_a_schedule_never_overlaps_itself(
        self,
        task_repository: TaskRepository,
        tool_registry: ToolRegistry,
        task_runner: TaskRunner,
    ) -> None:
        """A run still in flight when the next occurrence comes round means the
        interval is shorter than the work. Starting a second copy is how one
        slow briefing becomes six competing for the same rate limit."""
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowTool(FakeTool):
            async def run(self, params: dict[str, Any]) -> ToolResult:
                started.set()
                await release.wait()
                return await super().run(params)

        tool_registry.register(SlowTool("web_search"))
        task_repository.create_schedule(
            schedule_draft(cron="* * * * *", action=tool_action("web_search")), now=NOW
        )
        scheduler = Scheduler(task_repository, task_runner)

        assert scheduler.tick(now=NOW + 120) == 1
        await started.wait()
        assert scheduler.tick(now=NOW + 300) == 0

        release.set()
        await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()])

    async def test_the_loop_survives_a_tick_that_raises(
        self, task_repository: TaskRepository, task_runner: TaskRunner
    ) -> None:
        """Everything after a raising tick would silently never run again, and
        the symptom looks nothing like the cause."""
        calls: list[int] = []

        class Exploding(TaskRepository):
            def claim_due(self, *, now: int | None = None) -> list[Any]:
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("boom")
                return []

        scheduler = Scheduler(Exploding(task_repository._db), task_runner, tick_seconds=0.01)
        scheduler.start()
        await asyncio.sleep(0.05)
        await scheduler.stop()

        assert len(calls) > 1
        assert scheduler.running is False

    async def test_maintenance_runs_from_the_loop_that_is_already_awake(
        self, task_repository: TaskRepository, task_runner: TaskRunner
    ) -> None:
        """Expired, unanswered approval tokens were previously purged by nothing."""
        purges: list[int] = []
        scheduler = Scheduler(
            task_repository,
            task_runner,
            tick_seconds=0.01,
            on_maintenance=lambda: (purges.append(1), 1)[1],
        )
        scheduler.start()
        await asyncio.sleep(0.05)
        await scheduler.stop()

        assert len(purges) == 1  # hourly, so exactly once in a burst of ticks
