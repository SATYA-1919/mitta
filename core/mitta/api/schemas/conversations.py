"""Conversation wire schemas."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from mitta.api.schemas.common import Schema
from mitta.conversations.models import (
    Conversation,
    ConversationStatus,
    InputKind,
    Message,
    MessageRole,
    Register,
    Turn,
    TurnStatus,
)


class ConversationResource(Schema):
    id: str
    title: str | None
    project_id: str | None
    status: ConversationStatus
    pinned: bool
    summary: str | None
    message_count: int
    created_at: int
    updated_at: int

    @classmethod
    def of(cls, conversation: Conversation) -> ConversationResource:
        return cls(
            id=conversation.id,
            title=conversation.title,
            project_id=conversation.project_id,
            status=conversation.status,
            pinned=conversation.pinned,
            summary=conversation.summary,
            message_count=conversation.message_count,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )


class MessageResource(Schema):
    # `register` shadows `ABCMeta.register` on BaseModel, and Pydantic then
    # takes that bound method as the field's default — which breaks OpenAPI
    # generation, and therefore the generated TypeScript. The alias keeps the
    # wire name the one DEC-033 and the database column both use.
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    id: str
    conversation_id: str
    turn_id: str | None
    role: MessageRole
    content: str
    # The pre-personality text, so DEC-008's claim that the style pass changes
    # expression and never meaning is auditable in the UI rather than asserted.
    content_raw: str | None
    model_id: str | None
    provider: str | None
    style_register: Register | None = Field(alias="register")
    styled: bool
    latency_ms: int | None
    created_at: int

    @classmethod
    def of(cls, message: Message) -> MessageResource:
        return cls(
            id=message.id,
            conversation_id=message.conversation_id,
            turn_id=message.turn_id,
            role=message.role,
            content=message.content,
            content_raw=message.content_raw,
            model_id=message.model_id,
            provider=message.provider,
            style_register=message.register,
            styled=message.styled,
            latency_ms=message.latency_ms,
            created_at=message.created_at,
        )


class TurnResource(Schema):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    id: str
    conversation_id: str
    status: TurnStatus
    input_kind: InputKind
    style_register: Register | None = Field(alias="register")
    tokens_in: int
    tokens_out: int
    tool_call_count: int
    error: dict[str, object] | None
    started_at: int
    ended_at: int | None

    @classmethod
    def of(cls, turn: Turn) -> TurnResource:
        return cls(
            id=turn.id,
            conversation_id=turn.conversation_id,
            status=turn.status,
            input_kind=turn.input_kind,
            style_register=turn.register,
            tokens_in=turn.tokens_in,
            tokens_out=turn.tokens_out,
            tool_call_count=turn.tool_call_count,
            error=turn.error,
            started_at=turn.started_at,
            ended_at=turn.ended_at,
        )


class CreateConversationRequest(Schema):
    title: str | None = Field(default=None, max_length=200)
    project_id: str | None = None


class UpdateConversationRequest(Schema):
    title: str | None = Field(default=None, max_length=200)
    pinned: bool | None = None


class ConversationListResponse(Schema):
    conversations: list[ConversationResource]
    total: int


class MessageListResponse(Schema):
    messages: list[MessageResource]
    conversation_id: str
