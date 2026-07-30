"""Conversation CRUD and transcripts (API_DESIGN.md §3.4).

Read and manage only. Nothing here *sends* a message — that is a turn, it needs
the agent, and it arrives with it. Adding a write endpoint now would mean an
endpoint that persists a user message and then has nothing to answer it.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response, status

from mitta.api.auth import RequireToken
from mitta.api.schemas.conversations import (
    ClearHistoryRequest,
    ClearHistoryResponse,
    ConversationListResponse,
    ConversationResource,
    CreateConversationRequest,
    HistoryCountResponse,
    MessageListResponse,
    MessageResource,
    TurnResource,
    UpdateConversationRequest,
)
from mitta.conversations.models import ConversationDraft, ConversationStatus
from mitta.conversations.ranges import HistoryRange, cutoff_for
from mitta.conversations.repository import ConversationRepository
from mitta.errors import ValidationError

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])


def _repo(request: Request) -> ConversationRepository:
    repository: ConversationRepository = request.app.state.conversations
    return repository


@router.post(
    "",
    response_model=ConversationResource,
    status_code=status.HTTP_201_CREATED,
    summary="Start a conversation",
)
async def create(
    request: Request, body: CreateConversationRequest, _: RequireToken
) -> ConversationResource:
    draft = ConversationDraft(title=body.title, project_id=body.project_id)
    return ConversationResource.of(_repo(request).create(draft))


@router.get("", response_model=ConversationListResponse, summary="List conversations")
async def list_conversations(
    request: Request,
    _: RequireToken,
    conversation_status: ConversationStatus = Query(
        default=ConversationStatus.ACTIVE, alias="status"
    ),
    project_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ConversationListResponse:
    repository = _repo(request)
    conversations = repository.list_conversations(
        status=conversation_status, project_id=project_id, limit=limit, offset=offset
    )
    return ConversationListResponse(
        conversations=[ConversationResource.of(c) for c in conversations],
        total=repository.count(status=conversation_status),
    )


@router.get("/history/count", response_model=HistoryCountResponse, summary="Size of a range")
async def history_count(
    request: Request,
    _: RequireToken,
    period: HistoryRange = Query(alias="range"),
) -> HistoryCountResponse:
    """How many conversations clearing `range` would delete.

    Declared before `/{conversation_id}`: FastAPI matches in registration order,
    so a literal segment registered after the parameterised one is unreachable.
    """
    repository = _repo(request)
    since = cutoff_for(period)
    count = repository.count(status=None) if since is None else repository.count_since(since)
    return HistoryCountResponse(range=period, count=count, since=since)


@router.post("/history/clear", response_model=ClearHistoryResponse, summary="Clear a range")
async def clear_history(
    request: Request, body: ClearHistoryRequest, _: RequireToken
) -> ClearHistoryResponse:
    """Irreversible. Deletes whole conversations, cascading to their transcripts.

    Audited, and the entry records the resolved cutoff rather than the word the
    user pressed — "since 1785340800" is checkable later, "this month" is not.
    """
    if not body.confirm:
        raise ValidationError("clearing history requires confirm=true")

    repository = _repo(request)
    since = cutoff_for(body.range)
    deleted = repository.delete_all() if since is None else repository.delete_since(since)

    request.app.state.audit.record(
        actor="user",
        action="conversation.history_cleared",
        subject=body.range.value,
        verdict="allow",
        detail={"deleted": deleted, "since": since},
    )
    return ClearHistoryResponse(deleted=deleted, range=body.range, since=since)


@router.get("/{conversation_id}", response_model=ConversationResource, summary="Read one")
async def get(request: Request, conversation_id: str, _: RequireToken) -> ConversationResource:
    return ConversationResource.of(_repo(request).get(conversation_id))


@router.patch("/{conversation_id}", response_model=ConversationResource, summary="Rename or pin")
async def update(
    request: Request, conversation_id: str, body: UpdateConversationRequest, _: RequireToken
) -> ConversationResource:
    repository = _repo(request)
    conversation = repository.get(conversation_id)
    if body.title is not None:
        conversation = repository.rename(conversation_id, body.title)
    if body.pinned is not None:
        conversation = repository.set_pinned(conversation_id, body.pinned)
    return ConversationResource.of(conversation)


@router.get("/{conversation_id}/messages", response_model=MessageListResponse, summary="Transcript")
async def messages(
    request: Request,
    conversation_id: str,
    _: RequireToken,
    limit: int = Query(default=100, ge=1, le=500),
    before_seq: int | None = None,
) -> MessageListResponse:
    repository = _repo(request)
    repository.get(conversation_id)  # 404 rather than an empty list for a bad id
    history = repository.messages(conversation_id, limit=limit, before_seq=before_seq)
    return MessageListResponse(
        messages=[MessageResource.of(m) for m in history],
        conversation_id=conversation_id,
    )


@router.post("/{conversation_id}/archive", response_model=ConversationResource, summary="Archive")
async def archive(request: Request, conversation_id: str, _: RequireToken) -> ConversationResource:
    """Reversible. `DELETE` is the permanent one."""
    return ConversationResource.of(_repo(request).archive(conversation_id))


@router.post(
    "/{conversation_id}/unarchive", response_model=ConversationResource, summary="Unarchive"
)
async def unarchive(
    request: Request, conversation_id: str, _: RequireToken
) -> ConversationResource:
    return ConversationResource.of(_repo(request).unarchive(conversation_id))


@router.delete(
    "/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete permanently"
)
async def delete(request: Request, conversation_id: str, _: RequireToken) -> Response:
    """Irreversible, and cascades to every turn and message in the thread."""
    _repo(request).delete(conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{conversation_id}/turns/{turn_id}", response_model=TurnResource, summary="One turn")
async def turn(
    request: Request, conversation_id: str, turn_id: str, _: RequireToken
) -> TurnResource:
    return TurnResource.of(_repo(request).get_turn(turn_id))
