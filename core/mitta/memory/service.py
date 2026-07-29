"""Memory service — the one entry point everything above uses.

The agent, the API and consolidation all talk to this. None of them assembles a
repository, a vector store and a retriever themselves, because doing so is how
"write a memory" ends up meaning three slightly different things in three
places — one of which forgets to wake the indexer, and its memories are
silently unsearchable.

Deliberately not a god object: it holds no logic of its own beyond composition
and the few policies that genuinely span components (dedupe on write, touch on
read, the decay sweep). Everything else is delegated.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from mitta.config.settings import MemorySettings
from mitta.memory.indexer import Indexer
from mitta.memory.models import Memory, MemoryDraft, MemoryKind, MemoryPatch, MemoryStatus
from mitta.memory.normalise import content_hash
from mitta.memory.repository import MemoryRepository
from mitta.memory.retention import RetentionPolicy, retention_score, should_forget
from mitta.memory.retrieval import HybridRetriever, RetrievalQuery, RetrievalResult
from mitta.memory.vectors.store import IndexStatus, VectorStore
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SweepReport:
    """What a decay sweep did. Returned so it can be surfaced, not just logged.

    A system that quietly demotes a user's memories on a schedule owes them a
    record of it.
    """

    examined: int
    expired: int
    decayed: int

    @property
    def total_forgotten(self) -> int:
        return self.expired + self.decayed


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepository,
        store: VectorStore,
        indexer: Indexer,
        *,
        settings: MemorySettings | None = None,
    ) -> None:
        self._repository = repository
        self._store = store
        self._indexer = indexer
        self._retriever = HybridRetriever(repository, store)

        config = settings or MemorySettings()
        self._policy = RetentionPolicy(
            decay_lambda=config.decay_lambda,
            forget_threshold=config.forget_threshold,
        )

    # -- write --------------------------------------------------------------- #

    def remember(self, draft: MemoryDraft, *, now: int | None = None) -> Memory:
        """Store a memory, or return the existing identical one.

        Exact-duplicate detection is by content hash, which catches the common
        case — the same fact stated the same way twice — for the cost of one
        indexed lookup. Near-duplicate detection needs a vector and therefore a
        model, so it belongs to consolidation, which runs in the background and
        can afford to wait for one.
        """
        ts = now if now is not None else int(time.time())
        existing = self._repository.find_by_hash(content_hash(draft.content))
        if existing is not None and existing.kind is draft.kind:
            log.debug("memory.duplicate_ignored", extra={"memory_id": existing.id})
            return existing

        memory = self._repository.add(draft, now=ts)
        self._indexer.notify()
        return memory

    def update(self, memory_id: str, patch: MemoryPatch, *, now: int | None = None) -> Memory:
        memory = self._repository.update(memory_id, patch, now=now)
        self._indexer.notify()
        return memory

    def correct(self, memory_id: str, draft: MemoryDraft, *, now: int | None = None) -> Memory:
        memory = self._repository.supersede(memory_id, draft, now=now)
        self._indexer.notify()
        return memory

    def forget(self, memory_id: str, *, now: int | None = None) -> Memory:
        memory = self._repository.forget(memory_id, now=now)
        # Wake the indexer so the vector leaves the index promptly. A forgotten
        # memory still answering searches is the one staleness a user notices
        # immediately, and would rightly read as the system ignoring them.
        self._indexer.notify()
        return memory

    def restore(self, memory_id: str, *, now: int | None = None) -> Memory:
        memory = self._repository.restore(memory_id, now=now)
        self._indexer.notify()
        return memory

    def purge(self, memory_id: str) -> None:
        memory = self._repository.get(memory_id)
        self._repository.purge(memory_id)
        self._store.remove([memory.seq])

    # -- read ---------------------------------------------------------------- #

    def get(self, memory_id: str) -> Memory:
        return self._repository.get(memory_id)

    def list_memories(
        self,
        *,
        kind: MemoryKind | None = None,
        project_id: str | None = None,
        status: MemoryStatus = MemoryStatus.ACTIVE,
        pinned_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Memory]:
        return self._repository.list_memories(
            kind=kind,
            project_id=project_id,
            status=status,
            pinned_only=pinned_only,
            limit=limit,
            offset=offset,
        )

    def recall(
        self,
        query: str,
        *,
        limit: int = 10,
        project_id: str | None = None,
        semantic: bool = True,
        keyword: bool = True,
        touch: bool = True,
        now: int | None = None,
    ) -> list[RetrievalResult]:
        """Hybrid search. Records access unless `touch` is disabled.

        `touch` exists because not every read is a use. Rendering the memory
        explorer or previewing search-as-you-type should not inflate access
        counts — that would let idle browsing keep trivia alive at the expense
        of memories that are actually consulted.
        """
        ts = now if now is not None else int(time.time())
        results = self._retriever.search(
            RetrievalQuery(
                text=query,
                limit=limit,
                project_id=project_id,
                semantic=semantic,
                keyword=keyword,
            ),
            now=ts,
        )
        if touch and results:
            self._repository.touch([result.memory.seq for result in results], now=ts)
        return results

    def count(self, *, status: MemoryStatus | None = MemoryStatus.ACTIVE) -> int:
        return self._repository.count(status=status)

    def index_status(self) -> IndexStatus:
        return self._store.status()

    def pending_count(self, *, cap: int = 1_000) -> int:
        """How many memories are waiting for a vector.

        Capped rather than a full `COUNT(*)`: the number is for a status
        readout, and "1000+" is as useful as an exact figure while costing a
        bounded query instead of a scan of the whole table.
        """
        model_id = self._store.provider.descriptor.id
        return len(self._repository.find_stale_embeddings(model_id=model_id, limit=cap))

    # -- maintenance ---------------------------------------------------------- #

    def sweep(self, *, now: int | None = None, batch: int = 500) -> SweepReport:
        """Expire TTLs and demote decayed memories.

        Both paths only ever set `status = 'forgotten'`. Nothing here deletes,
        and pinned memories are exempt from both.
        """
        ts = now if now is not None else int(time.time())

        expired_seqs = self._repository.expired(now=ts, limit=batch)
        expired = self._repository.forget_seqs(expired_seqs, now=ts)

        candidates = self._repository.list_memories(status=MemoryStatus.ACTIVE, limit=batch)
        decayed_seqs = [
            memory.seq
            for memory in candidates
            if should_forget(
                score=retention_score(
                    importance=memory.importance,
                    last_accessed_at=memory.last_accessed_at,
                    created_at=memory.created_at,
                    access_count=memory.access_count,
                    now=ts,
                    policy=self._policy,
                ),
                pinned=memory.pinned,
                policy=self._policy,
            )
        ]
        decayed = self._repository.forget_seqs(decayed_seqs, now=ts)

        if expired or decayed:
            self._indexer.notify()
            log.info("memory.sweep", extra={"expired": expired, "decayed": decayed})

        return SweepReport(examined=len(candidates), expired=expired, decayed=decayed)

    def score_of(self, memory: Memory, *, now: int | None = None) -> float:
        """Retention score for one memory. Exposed so the UI can explain itself.

        A user shown "this will be forgotten soon" deserves to see why, and a
        number they can inspect is the difference between a policy and a whim.
        """
        ts = now if now is not None else int(time.time())
        return retention_score(
            importance=memory.importance,
            last_accessed_at=memory.last_accessed_at,
            created_at=memory.created_at,
            access_count=memory.access_count,
            now=ts,
            policy=self._policy,
        )

    def reindex(self) -> int:
        """Drop and rebuild the vector index. Returns vectors written."""
        self._store.clear_tracking()
        log.info("memory.reindex_started")
        return self._indexer.drain()

    def touch(self, seqs: Sequence[int], *, now: int | None = None) -> None:
        self._repository.touch(seqs, now=now)

    @property
    def policy(self) -> RetentionPolicy:
        return self._policy
