"""Conversation, turn and message models.

Three levels, and the distinction between them is load-bearing:

* A **conversation** is a thread the user sees and returns to.
* A **turn** is one request-to-response cycle. It owns the plan, the tool calls
  and the token accounting, and it is the unit that can be cancelled.
* A **message** is one entry in the transcript.

Collapsing turns into messages would work until the first multi-step request,
at which point "cancel this" has nothing to address and "what did that cost"
has nothing to sum. The turn exists so both questions have an answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class TurnStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AWAITING_APPROVAL = "awaiting_approval"


class InputKind(StrEnum):
    TEXT = "text"
    VOICE = "voice"
    PALETTE = "palette"
    SCHEDULED = "scheduled"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class Register(StrEnum):
    """DEC-033. Derived per turn, never configured."""

    PLAYFUL = "playful"
    SERIOUS = "serious"


# Records are frozen dataclasses rather than Pydantic models.
#
# They are built from trusted database rows and never validate untrusted input,
# so Pydantic bought nothing — the same reasoning as `mitta.llm.models`. It also
# actively hurt: a field named `register` shadows `ABCMeta.register` on
# `BaseModel`, and renaming it would put a second name on a concept the schema,
# the database column and DEC-033 all agree to call "register".


@dataclass(frozen=True, slots=True)
class Conversation:
    seq: int
    id: str
    title: str | None
    project_id: str | None
    status: ConversationStatus
    pinned: bool
    forked_from: str | None
    summary: str | None
    message_count: int
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class Turn:
    seq: int
    id: str
    conversation_id: str
    project_id: str | None
    status: TurnStatus
    input_kind: InputKind
    register: Register | None
    plan_id: str | None
    tokens_in: int
    tokens_out: int
    tool_call_count: int
    error: dict[str, object] | None
    started_at: int
    ended_at: int | None

    @property
    def duration_ms(self) -> int | None:
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at) * 1000


@dataclass(frozen=True, slots=True)
class Message:
    seq: int
    id: str
    conversation_id: str
    turn_id: str | None
    role: MessageRole
    content: str
    # The pre-personality text. Kept so DEC-008's central claim — that the style
    # pass changes expression and never meaning — is auditable rather than
    # merely asserted. Null when no rewrite happened.
    content_raw: str | None
    tool_calls: list[dict[str, object]] | None
    tool_call_id: str | None
    input_kind: InputKind | None
    model_id: str | None
    provider: str | None
    register: Register | None
    token_input: int | None
    token_output: int | None
    latency_ms: int | None
    styled: bool
    error: dict[str, object] | None
    created_at: int

    @property
    def was_rewritten(self) -> bool:
        """Whether the personality layer actually changed anything.

        `styled` records that the pass ran; this records that it did something.
        A no-op rewrite must not make the UI swap text it already displayed
        (DEC-046).
        """
        return self.styled and self.content_raw is not None and self.content_raw != self.content


class ConversationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    project_id: str | None = None
    forked_from: str | None = None


@dataclass(frozen=True, slots=True)
class MessageDraft:
    """A message as submitted. Identity and timestamps are the store's."""

    role: MessageRole
    content: str
    content_raw: str | None = None
    turn_id: str | None = None
    tool_calls: list[dict[str, object]] | None = None
    tool_call_id: str | None = None
    input_kind: InputKind | None = None
    model_id: str | None = None
    provider: str | None = None
    register: Register | None = None
    token_input: int | None = None
    token_output: int | None = None
    latency_ms: int | None = None
    styled: bool = False
    error: dict[str, object] | None = None
