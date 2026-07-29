"""LLM domain types.

The vocabulary every layer above the gateway speaks. Nothing here names a
vendor — `ARCHITECTURE.md` §6 requires that no component above the gateway can,
and a type that mentions Groq is how that requirement quietly stops holding.

The orchestrator asks for *capabilities* and a *task class*. Which company
answers is the gateway's business.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class TaskClass(StrEnum):
    """What a request is *for*.

    Routing is per task class rather than one global model because "free" and
    "best" pull in opposite directions (`ARCHITECTURE.md` §6). A planning step
    deserves the strongest model available; a personality rewrite runs on every
    reply and is felt as latency, so it wants the fastest.
    """

    PLANNING = "planning"
    CHAT = "chat"
    PERSONALITY = "personality"
    SUMMARISATION = "summarisation"
    TITLING = "titling"
    EXTRACTION = "extraction"


class Purpose(StrEnum):
    """Audit categories, matching the `llm_requests.purpose` CHECK constraint."""

    REASONING = "reasoning"
    PLANNING = "planning"
    PERSONALITY = "personality"
    SUMMARISATION = "summarisation"
    TITLING = "titling"
    EXTRACTION = "extraction"


TASK_PURPOSE: dict[TaskClass, Purpose] = {
    TaskClass.PLANNING: Purpose.PLANNING,
    TaskClass.CHAT: Purpose.REASONING,
    TaskClass.PERSONALITY: Purpose.PERSONALITY,
    TaskClass.SUMMARISATION: Purpose.SUMMARISATION,
    TaskClass.TITLING: Purpose.TITLING,
    TaskClass.EXTRACTION: Purpose.EXTRACTION,
}


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a model can do. The orchestrator selects on these, never on a name."""

    streaming: bool = True
    tools: bool = False
    vision: bool = False
    json_mode: bool = False
    context_window: int = 8_192
    max_output_tokens: int = 4_096

    def satisfies(self, required: Capabilities) -> bool:
        """Whether this model meets a requirement.

        Only the boolean capabilities and the context window are checked;
        `max_output_tokens` is a per-request cap rather than a filter.
        """
        if required.streaming and not self.streaming:
            return False
        if required.tools and not self.tools:
            return False
        if required.vision and not self.vision:
            return False
        if required.json_mode and not self.json_mode:
            return False
        return self.context_window >= required.context_window


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    """A model as the gateway sees it."""

    id: str
    provider: str
    capabilities: Capabilities
    # Per million tokens, USD. Zero means a free tier, not "unknown" — an
    # unknown cost would be indistinguishable from free at the point where the
    # router prefers the cheapest option.
    input_cost: float = 0.0
    output_cost: float = 0.0
    # Higher is better. Coarse on purpose: a finer scale would imply a precision
    # no benchmark supports across this many vendors.
    quality: int = 50

    def estimated_cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.input_cost + tokens_out * self.output_cost) / 1_000_000


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    name: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """The OpenAI chat-completions shape.

        Both confirmed providers speak it, which is the reason this normalises
        to it rather than to something bespoke. A provider that does not would
        translate in its own adapter.
        """
        payload: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.name is not None:
            payload["name"] = self.name
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        if self.tool_calls is not None:
            payload["tool_calls"] = self.tool_calls
        return payload


@dataclass(frozen=True, slots=True)
class ChatRequest:
    messages: list[ChatMessage]
    task: TaskClass = TaskClass.CHAT
    required: Capabilities = field(default_factory=Capabilities)
    temperature: float = 0.7
    max_tokens: int | None = None
    stop: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    stream: bool = True
    # Memory ids that fed this request's context. Recorded in `llm_requests` so
    # the user can inspect exactly what was sent (R5's enforcement clause) —
    # anything they cannot inspect, they cannot trust.
    memory_ids: list[str] = field(default_factory=list)

    def approximate_tokens(self) -> int:
        """Rough size, for context-window fitting.

        Four characters per token is the usual English approximation. Used only
        to reject a request that obviously will not fit; the real count comes
        back from the provider.
        """
        return sum(len(m.content) for m in self.messages) // 4


@dataclass(frozen=True, slots=True)
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass(frozen=True, slots=True)
class ChatChunk:
    """One streamed fragment."""

    text: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ChatResult:
    text: str
    model: ModelDescriptor
    usage: Usage
    latency_ms: int
    finish_reason: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    # Which provider was tried and failed before this one succeeded. Surfaced so
    # the UI can say who answered, rather than leaving the user guessing why a
    # reply felt different (ARCHITECTURE.md §6).
    failover_from: str | None = None

    @property
    def provider(self) -> str:
        return self.model.provider


type HealthState = Literal["healthy", "degraded", "unavailable"]
