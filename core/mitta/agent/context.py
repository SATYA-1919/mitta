"""Context assembly — the single budgeted chokepoint (R5).

Everything sent to a model passes through here, and what was sent is recorded.
R5's enforcement clause is that anything the user cannot inspect, they cannot
trust; a memory system that quietly decides what leaves the machine is exactly
the component where that matters.

Two rules the budget enforces:

**Only what the current request needs.** Not the memory database, not the whole
transcript — the retrieved working set for this turn and nothing more.

**Recent messages before recalled memories, when they compete.** A model given
stale context and fresh context weights the wrong one surprisingly often, and
the user's last sentence is the least surprising thing to keep.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from mitta.conversations.models import Message, MessageRole
from mitta.llm.models import ChatMessage, Role
from mitta.memory.retrieval import RetrievalResult
from mitta.tools.base import ToolSpec

# Characters per token, English. Rough by design — the true count comes back
# from the provider, and this only has to be good enough to avoid overflow.
CHARS_PER_TOKEN = 4

#: Fraction of the window reserved for the model's reply. Filling the context to
#: the brim produces a truncated answer, which is a worse failure than dropping
#: an old message: the user sees a sentence stop mid-word.
OUTPUT_RESERVE = 0.35

SYSTEM_PROMPT = """You are MITTA, a desktop assistant running on the user's own Mac.

You have access to what the user has told you before. Memories retrieved for \
this request appear below; use them when relevant and ignore them when not. \
Never claim to remember something that is not there.

Be direct. Answer the question that was asked. If you do not know, say so.

Never claim to have done something unless a tool result above shows it \
succeeded. Describing an action you did not take is worse than refusing, \
because the user will believe it.

When a tool result above shows an action succeeded, say plainly that you did \
it — "opening it now", "done, it's open". Do not ask whether the user wants \
it done; it already is. Do not follow it with a question unless they asked one."""

#: Appended when the runtime knows which tools are wired.
#:
#: Added because MITTA denied a capability it had used a minute earlier. Asked
#: to open YouTube it said "I cannot open YouTube yet", and asked why, it
#: explained it was "a text-based assistant with no ability to open external
#: applications" — immediately after opening Apple Music. The model was never
#: told what it could do, so it fell back on what an assistant usually cannot.
#:
#: A capability list is not a licence to claim success: the rule above still
#: stands, and the tool result is still the only evidence that anything ran.
CAPABILITY_PREAMBLE = """What you can actually do on this Mac, right now:

{capabilities}

These are real and they work. Never tell the user you are text-only, that you \
cannot open things, or that you have no way to act — you have the tools listed \
above.

But a capability is not an action. If no tool result appears above, nothing ran \
this turn: do not say the action is done and do not say it is happening. Say \
you can do it and that it did not go through this time.

If a request genuinely needs something not on that list, name the specific \
thing you are missing instead of describing yourself as incapable in general."""


@dataclass(frozen=True, slots=True)
class AssembledContext:
    """What will be sent, plus the record of why."""

    messages: list[ChatMessage]
    memory_ids: list[str] = field(default_factory=list)
    #: Memories retrieved but dropped for budget. Recorded so "why didn't it
    #: remember X" has an answer that is not a shrug.
    dropped_memory_ids: list[str] = field(default_factory=list)
    dropped_message_count: int = 0
    estimated_tokens: int = 0

    @property
    def was_truncated(self) -> bool:
        return self.dropped_message_count > 0 or bool(self.dropped_memory_ids)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


_ROLE_MAP: dict[MessageRole, Role] = {
    MessageRole.USER: Role.USER,
    MessageRole.ASSISTANT: Role.ASSISTANT,
    MessageRole.SYSTEM: Role.SYSTEM,
    MessageRole.TOOL: Role.TOOL,
}


def capability_lines(specs: Iterable[ToolSpec]) -> str:
    """One line per tool, for the capability preamble.

    The tool's own description, trimmed to its first sentence. The full text is
    written to help a model *choose* between tools and includes the boundaries
    ("NOT for launching an application"), which is noise in a list whose job is
    to tell the model what it has.
    """
    lines: list[str] = []
    for spec in sorted(specs, key=lambda s: s.name):
        summary = spec.description.split(". ")[0].rstrip(".")
        lines.append(f"- {spec.name}: {summary}")
    return "\n".join(lines)


def assemble(
    *,
    user_input: str,
    history: list[Message],
    memories: list[RetrievalResult],
    context_window: int,
    system_prompt: str = SYSTEM_PROMPT,
    capabilities: str = "",
) -> AssembledContext:
    """Build the message list for one turn, within budget.

    Drop order is deliberate and is the whole design:

    1. The system prompt and the user's current input are never dropped. Without
       either there is no request.
    2. Memories go first, lowest-ranked first. They are supporting evidence, and
       the retriever already ordered them by usefulness.
    3. Only then history, oldest first.

    Dropping history before memories would be the intuitive choice and is wrong:
    a conversation missing its middle reads as the assistant losing the thread,
    which users notice immediately, while a missing low-ranked memory is
    invisible.
    """
    budget = int(context_window * (1 - OUTPUT_RESERVE))

    if capabilities:
        system_prompt = (
            f"{system_prompt}\n\n{CAPABILITY_PREAMBLE.format(capabilities=capabilities)}"
        )

    fixed = estimate_tokens(system_prompt) + estimate_tokens(user_input)
    remaining = budget - fixed

    kept_memories: list[RetrievalResult] = []
    dropped_memories: list[str] = []
    for result in memories:
        cost = estimate_tokens(result.memory.context_text)
        if cost <= remaining:
            kept_memories.append(result)
            remaining -= cost
        else:
            dropped_memories.append(result.memory.id)

    kept_history: list[Message] = []
    dropped_history = 0
    for message in reversed(history):  # newest first, so the tail survives
        cost = estimate_tokens(message.content)
        if cost <= remaining:
            kept_history.append(message)
            remaining -= cost
        else:
            dropped_history += 1
    kept_history.reverse()

    prompt = system_prompt
    if kept_memories:
        recalled = "\n".join(f"- {r.memory.context_text}" for r in kept_memories)
        prompt = f"{system_prompt}\n\nWhat you remember about the user:\n{recalled}"

    messages = [ChatMessage(Role.SYSTEM, prompt)]
    messages.extend(
        ChatMessage(_ROLE_MAP[message.role], message.content)
        for message in kept_history
        # Tool messages need their call id to be valid, and tool calling is not
        # wired yet. Including them would produce a malformed request.
        if message.role is not MessageRole.TOOL
    )
    messages.append(ChatMessage(Role.USER, user_input))

    return AssembledContext(
        messages=messages,
        memory_ids=[r.memory.id for r in kept_memories],
        dropped_memory_ids=dropped_memories,
        dropped_message_count=dropped_history,
        estimated_tokens=sum(estimate_tokens(m.content) for m in messages),
    )
