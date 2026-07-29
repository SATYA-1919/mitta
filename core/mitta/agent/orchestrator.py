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

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from mitta.agent.context import AssembledContext, assemble
from mitta.agent.extraction import MemoryExtractor
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
    Capabilities,
    ChatMessage,
    ChatRequest,
    ModelDescriptor,
    Role,
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
    ) -> None:
        self._conversations = conversations
        self._memory = memory
        self._gateway = gateway
        self._extractor = extractor
        self._personality = personality
        self._tools = tools
        self._broker = broker
        self._policy = policy

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

        try:
            yield TurnEvent("turn.thinking", {"phase": "recalling"})
            context = self._build_context(text, conversation.id)

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
            if self._tools is not None:
                async for event, extra in self._run_tools(context, text, turn.id):
                    if event is not None:
                        yield event
                    tool_messages.extend(extra)

            yield TurnEvent("turn.thinking", {"phase": "reasoning"})
            request = ChatRequest(
                messages=[*context.messages, *tool_messages],
                task=TaskClass.CHAT,
                memory_ids=context.memory_ids,
                stream=True,
            )

            answered_by: list[ModelDescriptor] = []
            async for chunk in self._gateway.stream(request, on_selected=answered_by.append):
                if chunk.text:
                    collected.append(chunk.text)
                    yield TurnEvent("turn.delta", {"text": chunk.text})

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
            style = await self._personality.apply(raw_answer, user_text=text)
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

        yield TurnEvent(
            "turn.done",
            {
                "turn_id": turn.id,
                "conversation_id": conversation.id,
                "message_id": message_id,
                "latency_ms": latency_ms,
                "failed": failure is not None,
                "learned": len(learned),
            },
        )

    async def _run_tools(
        self, context: AssembledContext, text: str, turn_id: str
    ) -> AsyncIterator[tuple[TurnEvent | None, list[ChatMessage]]]:
        """Let the model call tools before it answers.

        A single round, not a loop. Multi-step tool chains need the planner, and
        an unbounded loop here is how an agent spends someone's rate limit on a
        question it could not answer. One round covers the case that actually
        matters — "search for X, then tell me" — and the ceiling is visible
        rather than emergent.

        Only `READ` tools are offered. Anything that writes needs the approval
        round-trip through the UI, which does not exist yet; showing the model a
        capability that cannot complete would produce confident promises MITTA
        cannot keep.
        """
        assert self._tools is not None
        schema = [spec.to_wire() for spec in self._tools.specs() if spec.risk is Risk.READ]
        if not schema:
            return

        try:
            decision = await self._gateway.complete(
                ChatRequest(
                    messages=context.messages,
                    task=TaskClass.CHAT,
                    required=Capabilities(tools=True),
                    tools=schema,
                    stream=False,
                    max_tokens=512,
                )
            )
        except ProviderError:
            # Tools are an enhancement. A failure here leaves the turn to answer
            # from memory alone, which is what it would have done anyway.
            log.warning("turn.tool_selection_failed", extra={"turn_id": turn_id})
            return

        calls = decision.tool_calls or []
        if not calls:
            return

        # The assistant's own tool_calls message has to precede the results, or
        # the provider rejects the `tool` messages as unmatched.
        collected: list[ChatMessage] = [ChatMessage(Role.ASSISTANT, "", tool_calls=calls)]
        for call in calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}

            yield (
                TurnEvent("turn.tool_started", {"tool": name, "params": arguments}),
                [],
            )

            execution = await self._tools.execute(name, arguments, turn_id=turn_id)

            if execution.awaiting_approval:
                async for event, resolved in self._ask_then_run(
                    name, arguments, execution.prompt, turn_id
                ):
                    if event is not None:
                        yield (event, [])
                    if resolved is not None:
                        execution = resolved

            yield (
                TurnEvent(
                    "turn.tool_finished",
                    {
                        "tool": name,
                        "ok": execution.result.ok,
                        "invocation_id": execution.invocation_id,
                        # Surfaced so the user sees what MITTA did on their
                        # behalf, not just that it did something (DEC-081).
                        "summary": execution.result.content[:200],
                    },
                ),
                [],
            )

            collected.append(
                ChatMessage(
                    Role.TOOL,
                    execution.result.content,
                    tool_call_id=str(call.get("id") or ""),
                )
            )

        yield (None, collected)

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
        )
