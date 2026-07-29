"""The planner — tool chains, with a ceiling.

DEC-083 shipped one round of tools and said why: an unbounded loop is how an
agent spends someone's rate limit on a question it could not answer. That
reasoning still holds. What it got wrong was the conclusion — the answer to an
unbounded loop is a bounded one, not a single step.

One round cannot do the thing people actually ask for. "Search for the fixture
list and save it to notes" is two tools, and the second one needs the first
one's output. Under a single round the model either picks one tool and silently
drops half the request, or calls both in parallel with an invented argument for
the one that depended on the other. Both were observed.

**What bounds it.** Four rounds, six calls, and a repeat check. The repeat check
matters more than either number: the failure mode of a tool loop is almost never
a model doing six *different* useful things, it is a model calling the same
search twice because the first result did not contain the answer. That is
recognised here and answered from the earlier result rather than paid for again.

**A denial ends the chain.** If the user refuses a write, the planner stops
rather than continuing with the remaining steps. Continuing would re-prompt for
the same action a person has already declined, and a permission dialog that
reappears after you say no is one people learn to click through.

The planner never answers. It gathers, and hands the transcript back for the
turn to answer from — so everything a tool returned is visible to the model that
writes the reply, and to the user in the events along the way.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Final

from mitta.errors import ProviderError
from mitta.llm.gateway import LLMGateway
from mitta.llm.models import Capabilities, ChatMessage, ChatRequest, Role, TaskClass
from mitta.policy.executor import Execution, ToolExecutor
from mitta.telemetry.logging import get_logger
from mitta.tools.base import Risk

log = get_logger(__name__)

#: How many times the model may be asked "what next?". Each round is one cheap
#: model call plus whatever tools it names.
MAX_ROUNDS = 4

#: Total tool executions across the whole chain. Lower than `MAX_ROUNDS` times
#: the calls a round can contain, deliberately — a model that emits four calls
#: per round for four rounds has stopped planning and started flailing.
MAX_CALLS = 6

PLANNER_PROMPT = """Decide whether a tool is needed for this request.

If the user names an action a tool performs — search, open, save, write down —
call that tool. An explicit instruction is not a question to be answered.

Otherwise prefer calling nothing: most requests are answerable from what you
already know, and that is the correct outcome.

Match the request to the tool that does exactly that thing. Do not substitute a
tool that is merely adjacent — opening an editor is not writing a file, and
searching the web is not remembering something the user told you.

If you call a tool, pass the user's actual values. Do not invent a filename, a
query or an application the user did not name.

The user may address you as MITTA. That is your name, not a tool and not an
argument. Ignore it and act on the rest of the sentence.

A request can need more than one tool, in order. After a tool returns, you will
be asked again: call the next tool if the request is not yet satisfied, using
the values the previous result gave you. When every part of the request has been
carried out, stop calling tools and reply with a single word. Do not repeat a
call you have already made — you already have its result."""

#: Appended for the rounds after the first, where the risk is the opposite one:
#: a model that has just been rewarded for calling a tool will call another.
CONTINUE_NOTE = """The results so far are above.

A result that succeeded means that part of the request is done. Do not do it
again, and do not retry it with a different spelling of the same target —
"Music" after "Apple Music" already opened is the same action twice.

If the request is now fully carried out, stop: reply with a single word and
call nothing. Only call another tool if a part of the request genuinely remains
undone."""


#: A leading address to MITTA: "hey mitta,", "mitta ", "ok mitta —".
#:
#: The wake word is "MITTA" (R7), so being addressed by name is the normal
#: phrasing rather than an edge case, and the selection call does not need the
#: vocative to choose a tool. Only that call sees the stripped text; the
#: answering model still gets the sentence as the user wrote it.
#:
#: **This was originally added for the wrong reason, and the note is kept so
#: the next person does not repeat the inference.** `hey mitta open YouTube`
#: failed tool selection while `open youtube` succeeded, twice in a row, which
#: looked conclusive. It was coincidence: Groq's own error body showed
#: `failed_generation=<function=open_url{"url": "youtube"}</function>` for both
#: phrasings on five of six attempts. The model was choosing correctly every
#: time and Groq was rejecting its own model's legacy output format. The real
#: fix is `_recover_tool_call` in the provider; this only tidies the prompt.
_ADDRESS: Final = re.compile(
    r"^\s*(?:hey|hi|hello|ok|okay|yo|so)?[\s,]*mitta\b[\s,:—-]*",
    re.IGNORECASE,
)


def strip_address(text: str) -> str:
    """Remove a leading "hey mitta" so the request stands on its own.

    Returns the original when stripping would leave no words behind — "mitta?"
    is a request to MITTA, not a lone question mark.
    """
    stripped = _ADDRESS.sub("", text, count=1).strip()
    return stripped if any(char.isalnum() for char in stripped) else text


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One executed call, for the record and for the UI."""

    tool: str
    params: dict[str, Any]
    ok: bool
    summary: str
    invocation_id: str
    repeated: bool = False


@dataclass(slots=True)
class Plan:
    """What the chain produced.

    `messages` is the transcript in provider shape — each assistant `tool_calls`
    message followed by its `tool` results — ready to append to the answering
    request. The ordering is load-bearing: a `tool` message whose matching
    assistant call is missing is rejected by both providers.
    """

    messages: list[ChatMessage] = field(default_factory=list)
    steps: list[PlanStep] = field(default_factory=list)
    #: Why the chain ended. Recorded because "MITTA stopped after six calls" and
    #: "MITTA decided it was finished" look identical from the reply alone.
    stopped: str = "no_tool"


#: Opens an approval with the user and returns the execution once they answer.
#: Supplied by the orchestrator, which owns the broker and the event stream.
AskFn = Callable[[str, dict[str, Any], str | None], AsyncIterator[tuple[Any, Execution | None]]]


@dataclass(frozen=True, slots=True)
class PlanEvent:
    """Mirrors `TurnEvent`. Kept separate so the planner does not import it —
    `agent.orchestrator` imports the planner, and the reverse would be a cycle."""

    type: str
    data: dict[str, object]


class Planner:
    def __init__(
        self,
        gateway: LLMGateway,
        tools: ToolExecutor,
        *,
        max_rounds: int = MAX_ROUNDS,
        max_calls: int = MAX_CALLS,
    ) -> None:
        self._gateway = gateway
        self._tools = tools
        self._max_rounds = max_rounds
        self._max_calls = max_calls

    async def run(
        self,
        *,
        text: str,
        turn_id: str,
        ceiling: Risk,
        ask: AskFn | None = None,
    ) -> AsyncIterator[PlanEvent | Plan]:
        """Drive the chain, yielding events as they happen.

        The final item is always a `Plan`. Events and the result share one
        stream because the caller needs to forward the events *while* they
        happen — a tool that takes four seconds should say so before it
        finishes, not after the whole chain does.
        """
        plan = Plan()

        schema = _schema_for(self._tools, ceiling)
        if not schema:
            yield plan
            return

        # The planning conversation is deliberately separate from the turn's
        # context: the model is choosing, not answering, and the user's memories
        # do not help it choose. Keeping them out also keeps R5's budget honest —
        # this call sends the request and the tool results, nothing else.
        transcript: list[ChatMessage] = [
            ChatMessage(Role.SYSTEM, PLANNER_PROMPT),
            ChatMessage(Role.USER, strip_address(text)),
        ]
        seen: dict[str, PlanStep] = {}

        for round_index in range(self._max_rounds):
            if len(plan.steps) >= self._max_calls:
                plan.stopped = "call_budget"
                break

            try:
                decision = await self._gateway.complete(
                    ChatRequest(
                        messages=transcript,
                        task=TaskClass.PLANNING,
                        required=Capabilities(tools=True),
                        tools=schema,
                        stream=False,
                        max_tokens=512,
                    )
                )
            except ProviderError:
                # Tools are an enhancement. A failure here leaves the turn to
                # answer from memory alone, which is what it would have done
                # anyway — but anything already gathered still counts.
                log.warning(
                    "plan.selection_failed",
                    extra={"turn_id": turn_id, "round": round_index},
                )
                plan.stopped = "provider_error"
                break

            calls = decision.tool_calls or []
            if not calls:
                plan.stopped = "answered" if plan.steps else "no_tool"
                break

            # Trim to what the budget can still afford rather than running them
            # all and reporting the overrun afterwards.
            room = self._max_calls - len(plan.steps)
            if len(calls) > room:
                calls = calls[:room]

            transcript.append(ChatMessage(Role.ASSISTANT, "", tool_calls=calls))
            plan.messages.append(ChatMessage(Role.ASSISTANT, "", tool_calls=calls))

            denied = False
            for call in calls:
                name, arguments = _decode(call)

                yield PlanEvent(
                    "turn.tool_started",
                    {"tool": name, "params": arguments, "round": round_index + 1},
                )

                key = _fingerprint(name, arguments)
                earlier = seen.get(key)
                if earlier is not None:
                    # Answering from the earlier result rather than paying for
                    # it again. Said plainly, because a model given the same
                    # output twice with no explanation tends to try a third time.
                    content = (
                        f"You already called {name} with these arguments. "
                        f"The result was:\n\n{earlier.summary}"
                    )
                    step = PlanStep(
                        tool=name,
                        params=arguments,
                        ok=earlier.ok,
                        summary=earlier.summary,
                        invocation_id=earlier.invocation_id,
                        repeated=True,
                    )
                    plan.steps.append(step)
                    yield PlanEvent(
                        "turn.tool_finished",
                        {
                            "tool": name,
                            "ok": step.ok,
                            "invocation_id": step.invocation_id,
                            "summary": step.summary[:200],
                            "repeated": True,
                        },
                    )
                    _append_result(transcript, plan, call, content)
                    continue

                execution = await self._tools.execute(name, arguments, turn_id=turn_id)

                if execution.awaiting_approval:
                    if ask is None:
                        # Cannot ask, so cannot proceed. Reported as a failed
                        # step rather than silently skipped.
                        execution = Execution(
                            invocation_id=execution.invocation_id,
                            tool_name=name,
                            params=arguments,
                            result=execution.result,
                        )
                    else:
                        async for event, resolved in ask(name, arguments, execution.prompt):
                            if event is not None:
                                yield PlanEvent(event.type, event.data)
                                if event.type == "turn.tool_denied":
                                    denied = True
                            if resolved is not None:
                                execution = resolved

                step = PlanStep(
                    tool=name,
                    params=arguments,
                    ok=execution.result.ok,
                    summary=execution.result.content,
                    invocation_id=execution.invocation_id,
                )
                plan.steps.append(step)
                if not step.repeated:
                    seen[key] = step

                yield PlanEvent(
                    "turn.tool_finished",
                    {
                        "tool": name,
                        "ok": step.ok,
                        "invocation_id": step.invocation_id,
                        # Surfaced so the user sees what MITTA did on their
                        # behalf, not just that it did something (DEC-081).
                        "summary": step.summary[:200],
                        "repeated": False,
                    },
                )

                _append_result(transcript, plan, call, execution.result.content)

            if denied:
                plan.stopped = "denied"
                break

            if len(plan.steps) >= self._max_calls:
                plan.stopped = "call_budget"
                break

            transcript.append(ChatMessage(Role.SYSTEM, CONTINUE_NOTE))
        else:
            plan.stopped = "round_budget"

        if plan.steps:
            log.info(
                "plan.complete",
                extra={
                    "turn_id": turn_id,
                    "calls": len(plan.steps),
                    "repeats": sum(1 for s in plan.steps if s.repeated),
                    "stopped": plan.stopped,
                },
            )

        yield plan


def _schema_for(tools: ToolExecutor, ceiling: Risk) -> list[dict[str, Any]]:
    order = {Risk.READ: 0, Risk.WRITE: 1, Risk.DESTRUCTIVE: 2}
    return [spec.to_wire() for spec in tools.specs() if order[spec.risk] <= order[ceiling]]


def _decode(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    function = call.get("function") or {}
    name = str(function.get("name") or "")
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except json.JSONDecodeError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return name, arguments


def _fingerprint(name: str, params: dict[str, Any]) -> str:
    """Identity of a call, for the repeat check.

    Sorted keys, because argument order out of a model is not stable and two
    calls that differ only in JSON key order are the same call.
    """
    return f"{name}:{json.dumps(params, sort_keys=True, default=str)}"


def _append_result(
    transcript: list[ChatMessage], plan: Plan, call: dict[str, Any], content: str
) -> None:
    message = ChatMessage(Role.TOOL, content, tool_call_id=str(call.get("id") or ""))
    transcript.append(message)
    plan.messages.append(message)
