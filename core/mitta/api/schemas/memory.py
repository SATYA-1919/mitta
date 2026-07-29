"""Memory API schemas.

Wire shapes, deliberately distinct from the domain models in `mitta.memory`.
Exposing `Memory` directly would tie the HTTP contract — and the generated
TypeScript — to the storage layout, so a column rename would become a frontend
change. `seq` in particular never crosses the boundary: it is a FAISS index key,
and a client that learns it will eventually be tempted to use it.
"""

from __future__ import annotations

from pydantic import Field

from mitta.api.schemas.common import Schema
from mitta.memory.models import Memory, MemoryKind, MemoryStatus, SourceKind
from mitta.memory.retrieval import RetrievalResult


class MemoryResource(Schema):
    """A memory as the UI sees it."""

    id: str
    kind: MemoryKind
    project_id: str | None
    content: str
    summary: str | None
    attributes: dict[str, object]
    importance: float
    confidence: float
    status: MemoryStatus
    superseded_by: str | None
    source_kind: SourceKind
    pinned: bool
    access_count: int
    last_accessed_at: int | None
    expires_at: int | None
    created_at: int
    updated_at: int

    @classmethod
    def of(cls, memory: Memory) -> MemoryResource:
        return cls(
            id=memory.id,
            kind=memory.kind,
            project_id=memory.project_id,
            content=memory.content,
            summary=memory.summary,
            attributes=memory.attributes,
            importance=memory.importance,
            confidence=memory.confidence,
            status=memory.status,
            superseded_by=memory.superseded_by,
            source_kind=memory.source_kind,
            pinned=memory.pinned,
            access_count=memory.access_count,
            last_accessed_at=memory.last_accessed_at,
            expires_at=memory.expires_at,
            created_at=memory.created_at,
            updated_at=memory.updated_at,
        )


class CreateMemoryRequest(Schema):
    kind: MemoryKind = MemoryKind.LONG_TERM
    content: str = Field(min_length=1, max_length=32_000)
    summary: str | None = Field(default=None, max_length=2_000)
    attributes: dict[str, object] = Field(default_factory=dict)
    project_id: str | None = None
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    pinned: bool = False
    expires_at: int | None = None
    # Absent from the request on purpose: `source_kind` is set by the server to
    # `user` for anything arriving over HTTP. A client that could claim
    # `consolidation` could launder a fabricated memory as machine-derived.


class UpdateMemoryRequest(Schema):
    content: str | None = Field(default=None, min_length=1, max_length=32_000)
    summary: str | None = Field(default=None, max_length=2_000)
    attributes: dict[str, object] | None = None
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    pinned: bool | None = None
    expires_at: int | None = None


class SearchRequest(Schema):
    query: str = Field(min_length=1, max_length=2_000)
    limit: int = Field(default=10, ge=1, le=100)
    project_id: str | None = None
    semantic: bool = True
    keyword: bool = True
    # Search-as-you-type must not inflate access counts, so the client decides
    # whether a query is a real use or a preview.
    record_access: bool = True


class SearchHitResource(Schema):
    memory: MemoryResource
    score: float
    # Which index found it. Surfaced so retrieval stays inspectable rather than
    # being a black box the user has to take on faith (R5).
    vector_rank: int | None
    keyword_rank: int | None
    matched_both: bool

    @classmethod
    def of(cls, result: RetrievalResult) -> SearchHitResource:
        return cls(
            memory=MemoryResource.of(result.memory),
            score=result.score,
            vector_rank=result.vector_rank,
            keyword_rank=result.keyword_rank,
            matched_both=result.matched_both,
        )


class SearchResponse(Schema):
    query: str
    hits: list[SearchHitResource]
    # False when the semantic leg could not run — the index is empty or the
    # model is absent. The UI says so rather than presenting keyword-only
    # results as if they were the full search.
    semantic_available: bool


class MemoryListResponse(Schema):
    memories: list[MemoryResource]
    total: int
    limit: int
    offset: int


class MemoryStatsResponse(Schema):
    active: int
    total: int
    index_name: str
    model_id: str
    dim: int
    vectors_indexed: int
    pending_embeddings: int
    index_consistent: bool
    # True when running on the stand-in provider: memories are still indexed and
    # searchable, but recall is by token overlap rather than meaning. Surfaced
    # because a status readout that hides it would have the user believing
    # semantic search works when it does not.
    embedding_degraded: bool
    embedding_model_id: str
    embedding_model_downloaded: bool


class SweepResponse(Schema):
    examined: int
    expired: int
    decayed: int
