"""The planner — chains, and the ceilings that keep them from running away.

The interesting cases here are the ones where the model misbehaves: it repeats
a call, it never stops, it asks for a tool it was not offered. Those are not
edge cases in tool use, they are the ordinary failure modes, and each test below
is one of them.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from mitta.agent.orchestrator import TurnEvent
from mitta.agent.planner import AskFn, Plan, PlanEvent, Planner, strip_address
from mitta.conversations.models import ConversationDraft
from mitta.conversations.repository import ConversationRepository
from mitta.errors import ProviderUnavailableError
from mitta.llm.models import (
    Capabilities,
    ChatRequest,
    ChatResult,
    ModelDescriptor,
    Role,
    Usage,
)
from mitta.persistence.database import Database
from mitta.policy.approval import ApprovalAuthority
from mitta.policy.audit import AuditLog
from mitta.policy.engine import PolicyEngine
from mitta.policy.executor import Execution, ToolExecutor
from mitta.tools.base import Risk, ToolResult, ToolSpec
from mitta.tools.registry import ToolRegistry

MODEL = ModelDescriptor(id="m", provider="p", capabilities=Capabilities(tools=True))


def call(name: str, index: int = 1, **arguments: Any) -> dict[str, Any]:
    """One tool call in provider shape."""
    return {
        "id": f"call_{index}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class ScriptedGateway:
    """Returns a prepared answer per round, and records what it was asked.

    The last entry repeats if the planner asks more times than the script has
    answers — a script that ran out would otherwise fail as an IndexError and
    look like a planner bug.
    """

    def __init__(self, script: Sequence[list[dict[str, Any]] | Exception]) -> None:
        self._script = list(script)
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        step = self._script[min(len(self.requests) - 1, len(self._script) - 1)]
        if isinstance(step, Exception):
            raise step
        return ChatResult(
            text="",
            model=MODEL,
            usage=Usage(1, 1),
            latency_ms=1,
            tool_calls=step or None,
        )


class RecordingTool:
    """A tool that counts its runs, so a repeat is visible rather than inferred."""

    def __init__(self, name: str, *, risk: Risk = Risk.READ, output: str = "result") -> None:
        self._spec = ToolSpec(
            name=name,
            description=name,
            risk=risk,
            parameters={"type": "object", "properties": {"q": {"type": "string"}}},
        )
        self._output = output
        self.runs: list[dict[str, Any]] = []

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def run(self, params: dict[str, Any]) -> ToolResult:
        self.runs.append(params)
        return ToolResult(ok=True, content=self._output)


@pytest.fixture
def turn_id(migrated: Database) -> str:
    """A real turn row. `tool_invocations.turn_id` is a foreign key, so a made-up
    id fails at the insert rather than in the code under test."""
    repository = ConversationRepository(migrated)
    conversation = repository.create(ConversationDraft())
    return repository.begin_turn(conversation.id).id


@pytest.fixture
def policy(migrated: Database) -> PolicyEngine:
    return PolicyEngine(AuditLog(migrated), ApprovalAuthority(migrated))


@pytest.fixture
def executor(migrated: Database, policy: PolicyEngine) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(RecordingTool("web_search", output="Barcelona won 3-1"))
    registry.register(RecordingTool("write_note", risk=Risk.WRITE, output="wrote notes/x.md"))
    return ToolExecutor(registry, policy, migrated)


@pytest.fixture
def approve(executor: ToolExecutor, policy: PolicyEngine, turn_id: str) -> AskFn:
    """A user who says yes, through the real token path.

    Not a stub returning success: the token is minted and verified exactly as
    the WebSocket handler does it, so a chain that mishandles approval fails
    here rather than passing against a fake that cannot refuse.
    """

    async def ask(
        name: str, params: dict[str, Any], prompt: str | None
    ) -> AsyncIterator[tuple[TurnEvent | None, Execution | None]]:
        spec = executor._registry.get(name).spec
        token = policy.request_approval(spec, params, turn_id=turn_id)
        yield (
            None,
            await executor.execute(
                name,
                params,
                turn_id=turn_id,
                approval_id=token["id"],
                signature=token["signature"],
            ),
        )

    return ask


def tool(executor: ToolExecutor, name: str) -> RecordingTool:
    found = executor._registry.get(name)
    assert isinstance(found, RecordingTool)
    return found


async def drive(planner: Planner, turn_id: str, **kwargs: Any) -> tuple[list[PlanEvent], Plan]:
    """Run a chain to completion, splitting the events from the result."""
    events: list[PlanEvent] = []
    plan: Plan | None = None
    async for item in planner.run(turn_id=turn_id, ceiling=Risk.WRITE, **kwargs):
        if isinstance(item, Plan):
            plan = item
        else:
            events.append(item)
    assert plan is not None, "the planner must always end with a Plan"
    return events, plan


class TestChaining:
    async def test_a_two_step_chain_runs_both_tools_in_order(
        self, executor: ToolExecutor, turn_id: str, approve: AskFn
    ) -> None:
        # The case one round could not do: the second tool's argument comes from
        # the first tool's output.
        gateway = ScriptedGateway(
            [
                [call("web_search", 1, q="barcelona result")],
                [call("write_note", 2, q="Barcelona won 3-1")],
                [],
            ]
        )
        planner = Planner(gateway, executor)  # type: ignore[arg-type]

        _, plan = await drive(planner, turn_id, text="search the result and save it", ask=approve)

        assert [step.tool for step in plan.steps] == ["web_search", "write_note"]
        assert plan.stopped == "answered"
        assert len(tool(executor, "web_search").runs) == 1
        assert len(tool(executor, "write_note").runs) == 1

    async def test_the_transcript_pairs_every_result_with_its_call(
        self, executor: ToolExecutor, turn_id: str
    ) -> None:
        # Both providers reject a `tool` message whose assistant `tool_calls`
        # is missing, and a chain builds several of each.
        gateway = ScriptedGateway(
            [
                [call("web_search", 1, q="a")],
                [call("web_search", 2, q="b")],
                [],
            ]
        )
        planner = Planner(gateway, executor)  # type: ignore[arg-type]

        _, plan = await drive(planner, turn_id, text="search")

        roles = [m.role for m in plan.messages]
        assert roles == [Role.ASSISTANT, Role.TOOL, Role.ASSISTANT, Role.TOOL]
        ids = [m.tool_call_id for m in plan.messages if m.role is Role.TOOL]
        assert ids == ["call_1", "call_2"]

    async def test_a_turn_needing_nothing_ends_without_running_a_tool(
        self, executor: ToolExecutor, turn_id: str
    ) -> None:
        planner = Planner(ScriptedGateway([[]]), executor)  # type: ignore[arg-type]

        _, plan = await drive(planner, turn_id, text="explain how FAISS works")

        assert plan.steps == []
        assert plan.messages == []
        assert plan.stopped == "no_tool"

    async def test_the_planning_call_carries_the_results_forward(
        self, executor: ToolExecutor, turn_id: str
    ) -> None:
        # Without this the second round is choosing blind, which is the whole
        # reason a chain beats two independent selections.
        gateway = ScriptedGateway([[call("web_search", 1, q="a")], []])
        planner = Planner(gateway, executor)  # type: ignore[arg-type]

        await drive(planner, turn_id, text="search")

        second = gateway.requests[1]
        assert any(m.role is Role.TOOL and "Barcelona" in m.content for m in second.messages)


class TestCeilings:
    async def test_the_same_call_twice_runs_the_tool_once(
        self, executor: ToolExecutor, turn_id: str
    ) -> None:
        # The characteristic loop: the first search did not contain the answer,
        # so the model searches again for exactly the same thing.
        gateway = ScriptedGateway(
            [
                [call("web_search", 1, q="same")],
                [call("web_search", 2, q="same")],
                [],
            ]
        )
        planner = Planner(gateway, executor)  # type: ignore[arg-type]

        _, plan = await drive(planner, turn_id, text="search")

        assert len(plan.steps) == 2
        assert plan.steps[1].repeated is True
        assert len(tool(executor, "web_search").runs) == 1

    async def test_key_order_does_not_defeat_the_repeat_check(
        self, executor: ToolExecutor, turn_id: str
    ) -> None:
        # Argument order out of a model is not stable, and two calls differing
        # only in JSON key order are the same call.
        first = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "web_search", "arguments": '{"a": 1, "b": 2}'},
        }
        second = {
            "id": "call_2",
            "type": "function",
            "function": {"name": "web_search", "arguments": '{"b": 2, "a": 1}'},
        }
        planner = Planner(ScriptedGateway([[first], [second], []]), executor)  # type: ignore[arg-type]

        _, plan = await drive(planner, turn_id, text="search")

        assert plan.steps[1].repeated is True
        assert len(tool(executor, "web_search").runs) == 1

    async def test_a_model_that_never_stops_is_stopped(
        self, executor: ToolExecutor, turn_id: str
    ) -> None:
        # Every round asks for a tool, with different arguments so the repeat
        # check does not catch it. Only the round ceiling does.
        script = [[call("web_search", index, q=f"query {index}")] for index in range(1, 12)]
        planner = Planner(ScriptedGateway(script), executor, max_rounds=3)  # type: ignore[arg-type]

        _, plan = await drive(planner, turn_id, text="search")

        assert plan.stopped == "round_budget"
        assert len(plan.steps) == 3

    async def test_the_call_budget_trims_a_round_rather_than_overrunning_it(
        self, executor: ToolExecutor, turn_id: str
    ) -> None:
        gateway = ScriptedGateway(
            [[call("web_search", i, q=f"q{i}") for i in range(1, 6)], []],
        )
        planner = Planner(gateway, executor, max_calls=2)  # type: ignore[arg-type]

        _, plan = await drive(planner, turn_id, text="search")

        assert len(plan.steps) == 2
        assert plan.stopped == "call_budget"
        # Trimmed before running, not run and then reported as an overrun.
        assert len(tool(executor, "web_search").runs) == 2

    async def test_a_write_tool_is_not_offered_under_a_read_ceiling(
        self, executor: ToolExecutor, turn_id: str
    ) -> None:
        gateway = ScriptedGateway([[]])
        planner = Planner(gateway, executor)  # type: ignore[arg-type]

        async for _ in planner.run(text="search", turn_id=turn_id, ceiling=Risk.READ):
            pass

        offered = {t["function"]["name"] for t in (gateway.requests[0].tools or [])}
        assert offered == {"web_search"}


class TestFailure:
    async def test_a_provider_failure_keeps_what_was_already_gathered(
        self, executor: ToolExecutor, turn_id: str
    ) -> None:
        # The first search succeeded. Losing it because the second planning call
        # failed would waste a request that already left the machine.
        gateway = ScriptedGateway(
            [[call("web_search", 1, q="a")], ProviderUnavailableError("down")]
        )
        planner = Planner(gateway, executor)  # type: ignore[arg-type]

        _, plan = await drive(planner, turn_id, text="search")

        assert plan.stopped == "provider_error"
        assert len(plan.steps) == 1
        assert len(plan.messages) == 2

    async def test_a_denial_ends_the_chain(self, executor: ToolExecutor, turn_id: str) -> None:
        # Continuing would re-prompt for an action the user just refused.
        gateway = ScriptedGateway(
            [
                [call("write_note", 1, q="first")],
                [call("write_note", 2, q="second")],
                [],
            ]
        )
        planner = Planner(gateway, executor)  # type: ignore[arg-type]

        async def refuse(
            name: str, params: dict[str, Any], prompt: str | None
        ) -> AsyncIterator[tuple[TurnEvent | None, Execution | None]]:
            yield (TurnEvent("turn.tool_denied", {"tool": name, "reason": "user"}), None)
            yield (
                None,
                Execution(
                    invocation_id="",
                    tool_name=name,
                    params=params,
                    result=ToolResult.failure("Not permitted: user"),
                ),
            )

        _, plan = await drive(planner, turn_id, text="save a note", ask=refuse)

        assert plan.stopped == "denied"
        assert len(plan.steps) == 1
        assert tool(executor, "write_note").runs == []

    async def test_a_write_with_no_way_to_ask_fails_visibly(
        self, executor: ToolExecutor, turn_id: str
    ) -> None:
        # No broker, so the approval cannot be put to anyone. Reported as a
        # failed step the model can read, not skipped in silence.
        planner = Planner(ScriptedGateway([[call("write_note", 1, q="x")], []]), executor)  # type: ignore[arg-type]

        _, plan = await drive(planner, turn_id, text="save a note", ask=None)

        assert len(plan.steps) == 1
        assert plan.steps[0].ok is False
        assert tool(executor, "write_note").runs == []

    async def test_an_unknown_tool_is_reported_back_rather_than_raising(
        self, executor: ToolExecutor, turn_id: str
    ) -> None:
        # A model inventing a tool name is routine. The chain should survive it.
        gateway = ScriptedGateway([[call("teleport", 1, q="x")], []])
        planner = Planner(gateway, executor)  # type: ignore[arg-type]

        _, plan = await drive(planner, turn_id, text="search")

        assert plan.steps[0].ok is False
        assert "No such tool" in plan.steps[0].summary

    async def test_unparseable_arguments_become_an_empty_object(
        self, executor: ToolExecutor, turn_id: str
    ) -> None:
        broken = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "web_search", "arguments": "{not json"},
        }
        planner = Planner(ScriptedGateway([[broken], []]), executor)  # type: ignore[arg-type]

        _, plan = await drive(planner, turn_id, text="search")

        assert plan.steps[0].params == {}


class TestEvents:
    async def test_every_call_reports_a_start_and_a_finish(
        self, executor: ToolExecutor, turn_id: str
    ) -> None:
        gateway = ScriptedGateway(
            [[call("web_search", 1, q="a")], [call("web_search", 2, q="b")], []]
        )
        planner = Planner(gateway, executor)  # type: ignore[arg-type]

        events, _ = await drive(planner, turn_id, text="search")

        kinds = [event.type for event in events]
        assert kinds == [
            "turn.tool_started",
            "turn.tool_finished",
            "turn.tool_started",
            "turn.tool_finished",
        ]
        # The round is on the event, so a UI can show step 2 of a chain as a
        # second step rather than a repeat of the first.
        assert [e.data["round"] for e in events if e.type == "turn.tool_started"] == [1, 2]


class TestAddress:
    """Being called by name must not cost the user the feature.

    Groq answers `open youtube` with a correct `open_url` call and answers
    `hey mitta open YouTube` with a hard "Failed to call a function" — after
    which failover reaches a model that returns nothing, and the tool silently
    never fires. The wake word is "MITTA" (R7), so this is the normal phrasing.
    """

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("hey mitta open YouTube", "open YouTube"),
            ("mitta can u open apple music", "can u open apple music"),
            ("Mitta, search the web for barcelona", "search the web for barcelona"),
            ("ok mitta — save a note", "save a note"),
            ("MITTA: open spotify", "open spotify"),
            ("yo mitta whats the weather", "whats the weather"),
        ],
    )
    def test_a_leading_address_is_removed(self, given: str, expected: str) -> None:
        assert strip_address(given) == expected

    @pytest.mark.parametrize(
        "given",
        [
            "open youtube",
            # Not a vocative — the name is the subject of the question.
            "what does mitta mean",
            "tell me about mitta",
        ],
    )
    def test_everything_else_is_untouched(self, given: str) -> None:
        assert strip_address(given) == given

    def test_an_address_and_nothing_else_survives(self) -> None:
        # "mitta?" is a request to MITTA, not an empty request.
        assert strip_address("mitta?") == "mitta?"

    async def test_the_selection_call_sees_the_stripped_text(
        self, executor: ToolExecutor, turn_id: str
    ) -> None:
        gateway = ScriptedGateway([[]])
        planner = Planner(gateway, executor)  # type: ignore[arg-type]

        await drive(planner, turn_id, text="hey mitta open YouTube")

        sent = [m.content for m in gateway.requests[0].messages if m.role is Role.USER]
        assert sent == ["open YouTube"]
