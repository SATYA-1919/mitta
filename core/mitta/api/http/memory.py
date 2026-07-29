"""Memory CRUD, search and maintenance (API_DESIGN.md §3.2).

Every route reaches the engine through `MemoryService`, never through a
repository or the vector store directly. Bypassing the service is how a write
lands without waking the indexer, leaving a memory that exists but is not
searchable — a failure with no error and no log line.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response, status

from mitta.api.auth import RequireToken
from mitta.api.schemas.memory import (
    CreateMemoryRequest,
    MemoryListResponse,
    MemoryResource,
    MemoryStatsResponse,
    SearchHitResource,
    SearchRequest,
    SearchResponse,
    SweepResponse,
    UpdateMemoryRequest,
)
from mitta.memory.models import (
    MemoryDraft,
    MemoryKind,
    MemoryPatch,
    MemoryStatus,
    SourceKind,
)
from mitta.memory.service import MemoryService

router = APIRouter(prefix="/v1/memory", tags=["memory"])


def _service(request: Request) -> MemoryService:
    service: MemoryService = request.app.state.memory
    return service


@router.post(
    "",
    response_model=MemoryResource,
    status_code=status.HTTP_201_CREATED,
    summary="Store a memory",
)
async def create_memory(
    request: Request, body: CreateMemoryRequest, _: RequireToken
) -> MemoryResource:
    """Returns 201 even when the content already existed.

    `remember` is idempotent on content hash, so a duplicate returns the
    original rather than erroring. A 409 would be technically defensible and
    practically hostile: the caller asked for the fact to be remembered, and it
    is remembered.
    """
    draft = MemoryDraft(
        kind=body.kind,
        content=body.content,
        summary=body.summary,
        attributes=body.attributes,
        project_id=body.project_id,
        importance=body.importance,
        confidence=body.confidence,
        pinned=body.pinned,
        expires_at=body.expires_at,
        # Server-assigned. See CreateMemoryRequest for why the client cannot set it.
        source_kind=SourceKind.USER,
    )
    return MemoryResource.of(_service(request).remember(draft))


@router.get("", response_model=MemoryListResponse, summary="Browse memories")
async def list_memories(
    request: Request,
    _: RequireToken,
    kind: MemoryKind | None = None,
    project_id: str | None = None,
    memory_status: MemoryStatus = Query(default=MemoryStatus.ACTIVE, alias="status"),
    pinned_only: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> MemoryListResponse:
    service = _service(request)
    memories = service.list_memories(
        kind=kind,
        project_id=project_id,
        status=memory_status,
        pinned_only=pinned_only,
        limit=limit,
        offset=offset,
    )
    return MemoryListResponse(
        memories=[MemoryResource.of(memory) for memory in memories],
        total=service.count(status=memory_status),
        limit=limit,
        offset=offset,
    )


@router.post("/search", response_model=SearchResponse, summary="Hybrid search")
async def search_memories(request: Request, body: SearchRequest, _: RequireToken) -> SearchResponse:
    """POST rather than GET.

    The query is conversational text — it belongs in a body, not in a URL that
    lands in history and access logs. R5 is about what leaves the machine, but
    the same instinct applies to what is written down locally.
    """
    service = _service(request)
    results = service.recall(
        body.query,
        limit=body.limit,
        project_id=body.project_id,
        semantic=body.semantic,
        keyword=body.keyword,
        touch=body.record_access,
    )
    index = service.index_status()
    return SearchResponse(
        query=body.query,
        hits=[SearchHitResource.of(result) for result in results],
        semantic_available=body.semantic and index.vector_count > 0,
    )


def _stats(request: Request) -> MemoryStatsResponse:
    service = _service(request)
    index = service.index_status()
    embedder = request.app.state.embedder
    descriptor = embedder.descriptor
    # `is_available` only exists on the downloadable provider. For the stand-in
    # the honest answer is that no weights were ever needed, so False here means
    # "the real model is not on disk" rather than "nothing works".
    downloaded = embedder.is_available() if hasattr(embedder, "is_available") else False
    return MemoryStatsResponse(
        active=service.count(),
        total=service.count(status=None),
        index_name=index.index_name,
        model_id=index.model_id,
        dim=index.dim,
        vectors_indexed=index.vector_count,
        pending_embeddings=service.pending_count(),
        index_consistent=index.consistent,
        embedding_degraded=descriptor.degraded,
        embedding_model_id=descriptor.id,
        embedding_model_downloaded=downloaded,
    )


@router.get("/stats", response_model=MemoryStatsResponse, summary="Engine health")
async def memory_stats(request: Request, _: RequireToken) -> MemoryStatsResponse:
    """Reports what is true, including when it is unflattering.

    `pending_embeddings` above zero and `embedding_degraded` true are both
    normal states the user is entitled to see, rather than a spinner that
    implies work is happening when it cannot.
    """
    return _stats(request)


@router.get("/{memory_id}", response_model=MemoryResource, summary="Read one memory")
async def get_memory(request: Request, memory_id: str, _: RequireToken) -> MemoryResource:
    return MemoryResource.of(_service(request).get(memory_id))


@router.patch("/{memory_id}", response_model=MemoryResource, summary="Edit a memory")
async def update_memory(
    request: Request, memory_id: str, body: UpdateMemoryRequest, _: RequireToken
) -> MemoryResource:
    patch = MemoryPatch.model_validate(body.model_dump(exclude_unset=True))
    return MemoryResource.of(_service(request).update(memory_id, patch))


@router.post(
    "/{memory_id}/correct", response_model=MemoryResource, summary="Supersede with a correction"
)
async def correct_memory(
    request: Request, memory_id: str, body: CreateMemoryRequest, _: RequireToken
) -> MemoryResource:
    """The original is kept and linked, never overwritten.

    "I moved to Bangalore" does not make "I lived in Hyderabad" false — it makes
    it historical, and only one of those is answerable if the row is gone.
    """
    draft = MemoryDraft(
        kind=body.kind,
        content=body.content,
        summary=body.summary,
        attributes=body.attributes,
        project_id=body.project_id,
        importance=body.importance,
        confidence=body.confidence,
        pinned=body.pinned,
        expires_at=body.expires_at,
        source_kind=SourceKind.USER,
    )
    return MemoryResource.of(_service(request).correct(memory_id, draft))


@router.post("/{memory_id}/forget", response_model=MemoryResource, summary="Demote to forgotten")
async def forget_memory(request: Request, memory_id: str, _: RequireToken) -> MemoryResource:
    """Reversible. The row survives; only its visibility changes."""
    return MemoryResource.of(_service(request).forget(memory_id))


@router.post("/{memory_id}/restore", response_model=MemoryResource, summary="Undo a forget")
async def restore_memory(request: Request, memory_id: str, _: RequireToken) -> MemoryResource:
    return MemoryResource.of(_service(request).restore(memory_id))


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete permanently")
async def purge_memory(request: Request, memory_id: str, _: RequireToken) -> Response:
    """Irreversible, and the only endpoint that destroys data.

    Deliberately a separate verb from `forget`: a UI that maps "delete" onto
    this without a confirmation is doing something the user cannot undo, and the
    HTTP method should make that obvious to whoever wires it up.
    """
    _service(request).purge(memory_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/maintenance/sweep", response_model=SweepResponse, summary="Run a decay sweep")
async def sweep(request: Request, _: RequireToken) -> SweepResponse:
    report = _service(request).sweep()
    return SweepResponse(examined=report.examined, expired=report.expired, decayed=report.decayed)


@router.post("/maintenance/reindex", response_model=MemoryStatsResponse, summary="Rebuild vectors")
async def reindex(request: Request, _: RequireToken) -> MemoryStatsResponse:
    """Drops every vector and re-embeds from SQLite.

    A failure here propagates rather than being caught: a reindex that cannot
    run because the model is absent must say so, not report success over an
    index it silently left empty.
    """
    _service(request).reindex()
    return _stats(request)
