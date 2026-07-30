"""Turn orchestration — the loop that makes MITTA answer.

One turn: persist the user's message, recall relevant memory, assemble a
budgeted context, stream a reply, persist it, and record what happened.

Streaming is the interface, not an optimisation. A desktop assistant that shows
nothing for four seconds and then a paragraph feels broken even when it is
faster than one that streams — the perceived latency is time-to-first-token, not
time-to-complete.

The turn is also the unit of accounting and of cancellation. Both need somewhere
to attach, which is why `turns` is a table rather than a field on a message
(DEC-069).
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final

from mitta.agent.context import AssembledContext, assemble, capability_lines
from mitta.agent.extraction import MemoryExtractor
from mitta.agent.planner import Plan, Planner
from mitta.conversations.models import (
    ConversationDraft,
    InputKind,
    MessageDraft,
    MessageRole,
    Register,
    TurnStatus,
)
from mitta.conversations.repository import ConversationRepository
from mitta.errors import MittaError, ProviderError
from mitta.llm.gateway import LLMGateway
from mitta.llm.models import (
    ChatMessage,
    ChatRequest,
    ModelDescriptor,
    TaskClass,
)
from mitta.memory.service import MemoryService
from mitta.personality.rewriter import PersonalityLayer
from mitta.policy.broker import ApprovalBroker
from mitta.policy.engine import PolicyEngine
from mitta.policy.executor import Execution, ToolExecutor
from mitta.telemetry.logging import get_logger
from mitta.tools.base import Risk, ToolResult

log = get_logger(__name__)

#: How many past messages to consider. The context assembler trims further to
#: fit; this only bounds the query.
HISTORY_LIMIT = 30

#: How many memories to retrieve. Deliberately small — the retrieval floor
#: (DEC-051) already discards weak matches, and a large working set is both a
#: context cost and, under R5, more of the user's life sent to a third party
#: than the request needed.
MEMORY_LIMIT = 6

#: Conservative default until model capabilities are consulted per request.
DEFAULT_CONTEXT_WINDOW = 32_000

#: Words that suggest an action rather than a question. Deliberately generous —
#: a false positive costs one cheap model call, a false negative means the tool
#: silently never fires and the user concludes the feature does not work.
_TOOL_HINTS: Final = re.compile(
    r"(?i)\b(search|look ?up|google|find out|latest|news|current|today|"
    r"open|launch|start|run|"
    # Closing an application is an action like opening one, and its absence here
    # made `close_app` unreachable: the gate runs before tool selection, so a
    # request it does not recognise never reaches the model that would have
    # chosen the tool. The tool existed, was registered, was tested — and "close
    # spotify" did nothing, which reads exactly like the tool being broken.
    r"close|quit|exit|shut ?down|"
    r"save|write|note|record|jot|store this|"
    r"who won|what happened|when is|when did|price of|weather)\b"
)


def _default_planner(gateway: LLMGateway, tools: ToolExecutor | None) -> Planner | None:
    return None if tools is None else Planner(gateway, tools)


def wants_tools(text: str) -> bool:
    """Whether this request is worth a tool-selection round-trip.

    Cheap and deterministic. The alternative — asking the model on every turn —
    costs a call on the many turns that need nothing, and reliably produces
    spurious tool calls, because a model shown a hammer will find a nail.
    """
    return bool(_TOOL_HINTS.search(text))


@dataclass(frozen=True, slots=True)
class TurnEvent:
    """One thing that happened during a turn, for the caller to forward."""

    type: str
    data: dict[str, object]


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    turn_id: str
    conversation_id: str
    message_id: str | None
    text: str
    provider: str | None
    model_id: str | None
    failed: bool


class Orchestrator:
    def __init__(
        self,
        conversations: ConversationRepository,
        memory: MemoryService,
        gateway: LLMGateway,
        extractor: MemoryExtractor | None = None,
        personality: PersonalityLayer | None = None,
        tools: ToolExecutor | None = None,
        broker: ApprovalBroker | None = None,
        policy: PolicyEngine | None = None,
        planner: Planner | None = None,
    ) -> None:
        self._conversations = conversations
        self._memory = memory
        self._gateway = gateway
        self._extractor = extractor
        self._personality = personality
        self._tools = tools
        self._broker = broker
        self._policy = policy
        # Built here when tools exist and no planner was injected, so a caller
        # that wires an executor gets chaining without having to know the
        # planner exists. Tests inject one with tighter budgets.
        self._planner = planner if planner is not None else _default_planner(gateway, tools)

    async def run(
        self,
        *,
        text: str,
        conversation_id: str | None = None,
        input_kind: InputKind = InputKind.TEXT,
    ) -> AsyncIterator[TurnEvent]:
        """Run one turn, yielding events as they happen.

        Every exit path — success, provider failure, unexpected error — closes
        the turn row. A turn left `running` shows in the UI as a thinking
        indicator that never resolves (DEC-069), so the `finally` is not
        defensive tidiness; it is the thing that stops that state existing.
        """
        conversation = (
            self._conversations.get(conversation_id)
            if conversation_id is not None
            else self._conversations.create(ConversationDraft())
        )
        turn = self._conversations.begin_turn(conversation.id, input_kind=input_kind)

        yield TurnEvent(
            "turn.accepted",
            {"turn_id": turn.id, "conversation_id": conversation.id},
        )

        self._conversations.add_message(
            conversation.id,
            MessageDraft(
                role=MessageRole.USER,
                content=text,
                turn_id=turn.id,
                input_kind=input_kind,
            ),
        )

        started = time.monotonic()
        collected: list[str] = []
        provider: str | None = None
        model_id: str | None = None
        failure: MittaError | None = None

        # Per-phase durations, logged at the end of the turn.
        #
        # A single `latency_ms` says a turn was slow and nothing about which of
        # the four sequential model round trips it spent that in. "It's slow" is
        # not actionable; "the rewrite is 1.4s of a 2.1s turn" is. Durations
        # only — no content, so this stays inside R5.
        timings: dict[str, int] = {}

        def mark(phase: str, since: float) -> None:
            timings[phase] = int((time.monotonic() - since) * 1000)

        try:
            yield TurnEvent("turn.thinking", {"phase": "recalling"})
            phase_started = time.monotonic()
            context = self._build_context(text, conversation.id)
            mark("recall", phase_started)

            log.info(
                "turn.context_assembled",
                extra={
                    "turn_id": turn.id,
                    "memories": len(context.memory_ids),
                    "dropped_memories": len(context.dropped_memory_ids),
                    "dropped_messages": context.dropped_message_count,
                    "estimated_tokens": context.estimated_tokens,
                },
            )

            yield TurnEvent(
                "turn.context",
                {
                    # Surfaced so the user can see exactly what was sent on their
                    # behalf. R5 is unenforceable if this is invisible.
                    "memory_ids": context.memory_ids,
                    "dropped_memory_ids": context.dropped_memory_ids,
                    "estimated_tokens": context.estimated_tokens,
                },
            )

            tool_messages: list[ChatMessage] = []
            phase_started = time.monotonic()
            async for event, extra in self._run_tools(text, turn.id):
                if event is not None:
                    yield event
                tool_messages.extend(extra)
            mark("tools", phase_started)

            yield TurnEvent("turn.thinking", {"phase": "reasoning"})
            phase_started = time.monotonic()
            request = ChatRequest(
                messages=[*context.messages, *tool_messages],
                task=TaskClass.CHAT,
                memory_ids=context.memory_ids,
                stream=True,
            )

            answered_by: list[ModelDescriptor] = []
            first_token: float | None = None
            async for chunk in self._gateway.stream(request, on_selected=answered_by.append):
                if chunk.text:
                    if first_token is None:
                        first_token = time.monotonic()
                        # Time to first token, measured from the top of the turn.
                        # This is the number the user actually feels: the point
                        # something appears rather than the point it is finished.
                        timings["first_token"] = int((first_token - started) * 1000)
                    collected.append(chunk.text)
                    yield TurnEvent("turn.delta", {"text": chunk.text})
            mark("reasoning", phase_started)

            if answered_by:
                provider = answered_by[0].provider
                model_id = answered_by[0].id

        except ProviderError as exc:
            failure = exc
            yield TurnEvent(
                "turn.error",
                {"code": exc.code, "message": exc.message, "retryable": exc.retryable},
            )
        except Exception as exc:
            log.exception("turn.unexpected_failure", extra={"turn_id": turn.id})
            failure = MittaError(str(exc))
            yield TurnEvent(
                "turn.error",
                {"code": "internal.error", "message": "Something went wrong.", "retryable": False},
            )

        raw_answer = "".join(collected)
        answer = raw_answer
        register: Register | None = None
        styled = False

        if self._personality is not None and raw_answer and failure is None:
            yield TurnEvent("turn.thinking", {"phase": "styling"})
            phase_started = time.monotonic()
            style = await self._personality.apply(raw_answer, user_text=text)
            mark("styling", phase_started)
            answer = style.text
            register = Register(style.register.value)
            # `styled` records that the pass ran and changed something. A no-op
            # rewrite must not make the UI swap text it already displayed
            # (DEC-046), so the two are not the same flag.
            styled = style.changed

        latency_ms = int((time.monotonic() - started) * 1000)
        message_id: str | None = None

        if answer:
            message = self._conversations.add_message(
                conversation.id,
                MessageDraft(
                    role=MessageRole.ASSISTANT,
                    content=answer,
                    turn_id=turn.id,
                    provider=provider,
                    model_id=model_id,
                    latency_ms=latency_ms,
                    register=register,
                    # The pre-personality text, kept only when a rewrite actually
                    # happened. This is what makes DEC-008's claim — that the
                    # style pass changes expression and never meaning — auditable
                    # rather than asserted.
                    content_raw=raw_answer if styled else None,
                    styled=styled,
                ),
            )
            message_id = message.id
            yield TurnEvent(
                "turn.message",
                {
                    "message_id": message.id,
                    "content": answer,
                    "styled": styled,
                    "register": register.value if register else None,
                    "provider": provider,
                    "model_id": model_id,
                },
            )

        self._conversations.end_turn(
            turn.id,
            status=TurnStatus.FAILED if failure is not None else TurnStatus.COMPLETED,
            register=register,
            error=(
                {"code": failure.code, "message": failure.message} if failure is not None else None
            ),
        )

        # Learn from the exchange — after the reply, never before. Extraction is
        # a second model call, and making the user wait for MITTA to take notes
        # would trade the thing they asked for against a thing they did not.
        learned: list[str] = []
        if self._extractor is not None and failure is None and answer:
            result = await self._extractor.extract(self._conversations.turn_messages(turn.id))
            learned = result.stored
            if learned:
                yield TurnEvent("memory.learned", {"memory_ids": learned, "count": len(learned)})

        log.info(
            "turn.timings",
            extra={
                "turn_id": turn.id,
                "total_ms": latency_ms,
                # Every phase that ran, in the order the turn spends them. The
                # sum is deliberately comparable to `total_ms`: a gap between
                # them is time going somewhere none of these names covers, which
                # is exactly what you want to be able to notice.
                **{f"{phase}_ms": value for phase, value in timings.items()},
            },
        )

        yield TurnEvent(
            "turn.done",
            {
                "turn_id": turn.id,
                "conversation_id": conversation.id,
                "message_id": message_id,
                "latency_ms": latency_ms,
                "failed": failure is not None,
                "learned": len(learned),
                # Surfaced, not just logged. The Monitor surface exists to make
                # claims checkable, and "MITTA is slow" is a claim.
                "timings": timings,
            },
        )

    async def _run_tools(
        self, text: str, turn_id: str
    ) -> AsyncIterator[tuple[TurnEvent | None, list[ChatMessage]]]:
        """Let the model call tools, in a chain, before it answers.

        The chain itself is the planner's; this decides whether to start one and
        forwards what it reports.

        `WRITE` tools are offered only when there is a way to ask permission.
        Showing a model a capability that cannot complete produces confident
        promises MITTA cannot keep, and a capability never offered cannot be
        requested — which is cheaper than refusing the call afterwards.

        The pass is skipped entirely when nothing in the request suggests a
        tool. Most turns need none, and asking the model "do you want a tool?"
        on every one of them costs a round-trip *and* invites a spurious call:
        a model shown a hammer will find a nail.
        """
        if self._planner is None or not wants_tools(text):
            return

        def ask(
            name: str, params: dict[str, Any], prompt: str | None
        ) -> AsyncIterator[tuple[TurnEvent | None, Execution | None]]:
            return self._ask_then_run(name, params, prompt, turn_id)

        ceiling = Risk.WRITE if self._can_ask() else Risk.READ
        async for item in self._planner.run(
            text=text,
            turn_id=turn_id,
            ceiling=ceiling,
            ask=ask if self._can_ask() else None,
        ):
            if isinstance(item, Plan):
                # The transcript arrives whole, at the end. Emitting the
                # messages step by step would let a half-built chain — an
                # assistant `tool_calls` with no matching results yet — reach
                # the answering request, which both providers reject.
                if item.steps:
                    yield (
                        TurnEvent(
                            "turn.plan",
                            {
                                "calls": len(item.steps),
                                "stopped": item.stopped,
                                "tools": [step.tool for step in item.steps],
                            },
                        ),
                        [],
                    )
                yield (None, item.messages)
            else:
                yield (TurnEvent(item.type, item.data), [])

    def _can_ask(self) -> bool:
        return self._broker is not None and self._policy is not None

    async def _ask_then_run(
        self, name: str, params: dict[str, Any], prompt: str | None, turn_id: str
    ) -> AsyncIterator[tuple[TurnEvent | None, Execution | None]]:
        """Put the decision in front of the user and wait for it.

        The turn stops here. Nothing has happened yet — the executor recorded a
        `pending` invocation and returned without running the tool.
        """
        assert self._broker is not None and self._policy is not None
        assert self._tools is not None

        request = self._broker.open(
            tool_name=name,
            params=params,
            prompt=prompt or f"Allow {name}?",
            turn_id=turn_id,
        )
        yield (
            TurnEvent(
                "turn.approval_required",
                {
                    "request_id": request.id,
                    "tool": name,
                    # The concrete arguments, not a summary. The user is
                    # approving these exact values, and the token binds to them.
                    "params": params,
                    "prompt": request.prompt,
                },
            ),
            None,
        )

        outcome = await self._broker.wait(request.id)
        if not outcome.approved or outcome.token is None:
            yield (
                TurnEvent("turn.tool_denied", {"tool": name, "reason": outcome.reason}),
                None,
            )
            yield (
                None,
                Execution(
                    invocation_id="",
                    tool_name=name,
                    params=params,
                    result=ToolResult.failure(f"Not permitted: {outcome.reason}"),
                ),
            )
            return

        # Re-authorised from scratch with the token. The executor re-derives the
        # parameter hash from the arguments about to be used, so an approval for
        # different values fails here rather than being trusted.
        yield (
            None,
            await self._tools.execute(
                name,
                params,
                turn_id=turn_id,
                approval_id=outcome.token["id"],
                signature=outcome.token["signature"],
            ),
        )

    def _build_context(self, text: str, conversation_id: str) -> AssembledContext:
        # `touch=False`: a memory used to answer has genuinely been used, but
        # recording that here would inflate access counts for memories the model
        # may have ignored entirely. Access is recorded when a memory demonstrably
        # contributed, which needs attribution the current pipeline cannot do.
        memories = self._memory.recall(text, limit=MEMORY_LIMIT, touch=False)
        history = self._conversations.recent_context(conversation_id, limit=HISTORY_LIMIT)

        return assemble(
            user_input=text,
            # The user's message was already persisted, so drop it from history
            # or the model sees it twice.
            history=[m for m in history if m.content != text or m.role is not MessageRole.USER],
            memories=memories,
            context_window=DEFAULT_CONTEXT_WINDOW,
            # What MITTA can actually do. Without this the answering model
            # guesses, and it guesses that an assistant cannot open anything.
            capabilities=capability_lines(self._tools.specs()) if self._tools is not None else "",
        )
