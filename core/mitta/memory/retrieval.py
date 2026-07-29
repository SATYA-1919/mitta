"""Hybrid retrieval — vector search fused with keyword search.

Two indexes, because they fail in opposite directions and the failures are not
subtle.

Embeddings are bad at exactly the queries people most expect to work: exact
identifiers, file paths, error codes, rare proper nouns. Asking for `MITTA-1481`
and getting "the ticket about authentication" is a worse answer than no answer,
and no amount of model quality fixes it — 384 floats cannot preserve a literal
string. FTS5 handles those perfectly and is useless for "that thing I said about
deployment being flaky", which the embedding handles.

Fusion is **Reciprocal Rank Fusion**, not a weighted sum of scores:

    score(d) = sum over indexes of 1 / (k + rank(d))       k = 60

FAISS inner-product scores live in [-1, 1]; BM25 scores are unbounded negatives
whose scale moves with corpus size and term rarity. Any fixed weighting between
them is a magic number that is wrong for a different corpus, and it silently
becomes wrong as the corpus grows. RRF reads only *ranks*, so it needs no
calibration and cannot drift.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from mitta.memory.models import Memory
from mitta.memory.repository import KeywordHit, MemoryRepository
from mitta.memory.vectors.index import SearchHit
from mitta.memory.vectors.store import VectorStore
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

RRF_K = 60
SECONDS_PER_DAY = 86_400

# Half-life of ~30 days for the recency term. Distinct from the retention
# half-life (~46 days): retention asks "is this still worth keeping", ranking
# asks "is this likely what they mean right now", and recency matters more to
# the second question.
_RECENCY_LAMBDA = math.log(2) / 30.0


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    memory: Memory
    score: float
    vector_rank: int | None
    keyword_rank: int | None

    @property
    def matched_both(self) -> bool:
        return self.vector_rank is not None and self.keyword_rank is not None


@dataclass(slots=True)
class _Fused:
    seq: int
    score: float = 0.0
    vector_rank: int | None = None
    keyword_rank: int | None = None


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    text: str
    limit: int = 10
    project_id: str | None = None
    # Over-fetch from each index before fusing. A document ranked 3rd by vectors
    # and 40th by keywords should still win on combined evidence, and it cannot
    # if the keyword list was truncated at 10.
    candidate_multiplier: int = 4
    semantic: bool = True
    keyword: bool = True
    rerank: bool = True
    weights: dict[str, float] = field(default_factory=dict)


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[int]],
    *,
    k: int = RRF_K,
    weights: Sequence[float] | None = None,
) -> dict[int, float]:
    """Fuse ranked id lists into one score map.

    `k` damps the top of each list. With `k = 60`, the gap between ranks 1 and 2
    is small relative to the gap between appearing and not appearing — which is
    the intended behaviour, because agreement between two independent retrievers
    is stronger evidence than one retriever's confidence in its ordering.
    """
    if weights is not None and len(weights) != len(ranked_lists):
        raise ValueError("weights must align with ranked_lists")

    scores: dict[int, float] = {}
    for position, ids in enumerate(ranked_lists):
        weight = 1.0 if weights is None else weights[position]
        for rank, identifier in enumerate(ids, start=1):
            scores[identifier] = scores.get(identifier, 0.0) + weight / (k + rank)
    return scores


class HybridRetriever:
    def __init__(self, repository: MemoryRepository, store: VectorStore) -> None:
        self._repository = repository
        self._store = store

    def search(self, query: RetrievalQuery, *, now: int) -> list[RetrievalResult]:
        candidates = max(query.limit * query.candidate_multiplier, query.limit)

        vector_hits: list[SearchHit] = []
        if query.semantic:
            vector_hits = self._store.search_text(query.text, limit=candidates)
            if query.project_id is not None:
                vector_hits = self._filter_to_project(vector_hits, query.project_id)

        keyword_hits: list[KeywordHit] = []
        if query.keyword:
            keyword_hits = self._repository.search_keyword(
                query.text, limit=candidates, project_id=query.project_id
            )

        fused = self._fuse(vector_hits, keyword_hits, query.weights)
        if not fused:
            return []

        ordered = sorted(fused.values(), key=lambda item: item.score, reverse=True)
        # Hydrate more than needed when re-ranking, since re-ranking reorders and
        # a memory outside the top `limit` by fusion may enter it afterwards.
        hydrate = ordered[: candidates if query.rerank else query.limit]
        memories = {
            memory.seq: memory
            for memory in self._repository.get_many([item.seq for item in hydrate])
        }

        results = [
            RetrievalResult(
                memory=memories[item.seq],
                score=item.score,
                vector_rank=item.vector_rank,
                keyword_rank=item.keyword_rank,
            )
            for item in hydrate
            if item.seq in memories
        ]

        if query.rerank:
            results = rerank(results, now=now)
        return results[: query.limit]

    def _filter_to_project(self, hits: Sequence[SearchHit], project_id: str) -> list[SearchHit]:
        """Restrict vector hits to one project.

        Done after search rather than inside it because FAISS has no notion of
        metadata. Over-fetching and filtering is the standard answer at this
        corpus size; an ID-partitioned index per project would be faster and is
        not worth its complexity until projects number in the hundreds.
        """
        if not hits:
            return []
        seqs = [hit.seq for hit in hits]
        allowed = {
            memory.seq
            for memory in self._repository.get_many(seqs)
            if memory.project_id == project_id
        }
        return [hit for hit in hits if hit.seq in allowed]

    @staticmethod
    def _fuse(
        vector_hits: Sequence[SearchHit],
        keyword_hits: Sequence[KeywordHit],
        weights: dict[str, float],
    ) -> dict[int, _Fused]:
        vector_weight = weights.get("semantic", 1.0)
        keyword_weight = weights.get("keyword", 1.0)

        fused: dict[int, _Fused] = {}
        for rank, hit in enumerate(vector_hits, start=1):
            entry = fused.setdefault(hit.seq, _Fused(seq=hit.seq))
            entry.vector_rank = rank
            entry.score += vector_weight / (RRF_K + rank)
        for rank, keyword_hit in enumerate(keyword_hits, start=1):
            entry = fused.setdefault(keyword_hit.seq, _Fused(seq=keyword_hit.seq))
            entry.keyword_rank = rank
            entry.score += keyword_weight / (RRF_K + rank)
        return fused


def rerank(results: Sequence[RetrievalResult], *, now: int) -> list[RetrievalResult]:
    """Re-order by relevance x recency x importance.

    Relevance alone surfaces a two-year-old note over yesterday's correction on
    the same topic, which is the single most annoying failure mode a memory
    system has. The multiplicative form means a memory must clear a bar on every
    axis — a highly relevant but trivial and ancient note loses to a relevant,
    important, recent one, and nothing wins on one dimension alone.
    """
    scored: list[tuple[float, RetrievalResult]] = []
    for result in results:
        memory = result.memory
        reference = memory.last_accessed_at or memory.created_at
        age_days = max(0.0, (now - reference) / SECONDS_PER_DAY)
        recency = math.exp(-_RECENCY_LAMBDA * age_days)

        # Floors keep a factor from zeroing the product. Importance 0.0 means
        # "unimportant", not "never retrievable", and a memory nobody has touched
        # in five years should rank low rather than be unreachable.
        importance = 0.25 + 0.75 * memory.importance
        pin_bonus = 1.25 if memory.pinned else 1.0

        scored.append((result.score * (0.3 + 0.7 * recency) * importance * pin_bonus, result))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        RetrievalResult(
            memory=result.memory,
            score=score,
            vector_rank=result.vector_rank,
            keyword_rank=result.keyword_rank,
        )
        for score, result in scored
    ]
