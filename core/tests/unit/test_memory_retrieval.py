"""Hybrid retrieval, fusion and the service facade."""

from __future__ import annotations

import pytest

from mitta.memory.indexer import Indexer
from mitta.memory.models import MemoryDraft, MemoryKind, MemoryPatch, MemoryStatus
from mitta.memory.repository import MemoryRepository
from mitta.memory.retrieval import RRF_K, RetrievalResult, reciprocal_rank_fusion, rerank
from mitta.memory.service import MemoryService

DAY = 86_400
NOW = 1_800_000_000


def draft(content: str, **kwargs: object) -> MemoryDraft:
    payload: dict[str, object] = {"kind": MemoryKind.LONG_TERM, "content": content}
    payload.update(kwargs)
    return MemoryDraft.model_validate(payload)


class TestFusion:
    def test_agreement_between_indexes_beats_one_strong_rank(self) -> None:
        # The property RRF is chosen for: two independent retrievers agreeing is
        # stronger evidence than one retriever's confidence in its own ordering.
        scores = reciprocal_rank_fusion([[10, 20], [20, 30]])

        assert scores[20] > scores[10]
        assert scores[20] > scores[30]

    def test_uses_ranks_not_scores(self) -> None:
        # Which is why no calibration between FAISS and BM25 scales is needed.
        assert reciprocal_rank_fusion([[1]])[1] == pytest.approx(1 / (RRF_K + 1))

    def test_weights_must_align_with_the_lists(self) -> None:
        with pytest.raises(ValueError, match="align"):
            reciprocal_rank_fusion([[1], [2]], weights=[1.0])

    def test_empty_input_fuses_to_nothing(self) -> None:
        assert reciprocal_rank_fusion([]) == {}


class TestRerank:
    def _result(self, memory_service: MemoryService, content: str, **kw: object) -> RetrievalResult:
        memory = memory_service.remember(draft(content, **kw), now=NOW)
        return RetrievalResult(memory=memory, score=1.0, vector_rank=1, keyword_rank=1)

    def test_recent_beats_ancient_at_equal_relevance(
        self, memory_service: MemoryService, repository: MemoryRepository
    ) -> None:
        # The single most annoying memory failure: yesterday's correction losing
        # to a two-year-old note on the same topic.
        old = repository.add(draft("lives in Hyderabad"), now=NOW - 700 * DAY)
        new = repository.add(draft("lives in Bangalore"), now=NOW - DAY)

        ordered = rerank(
            [
                RetrievalResult(memory=old, score=1.0, vector_rank=1, keyword_rank=1),
                RetrievalResult(memory=new, score=1.0, vector_rank=2, keyword_rank=2),
            ],
            now=NOW,
        )

        assert ordered[0].memory.id == new.id

    def test_importance_breaks_ties(self, repository: MemoryRepository) -> None:
        trivial = repository.add(draft("ordered coffee", importance=0.1), now=NOW)
        vital = repository.add(draft("allergic to penicillin", importance=1.0), now=NOW)

        ordered = rerank(
            [
                RetrievalResult(memory=trivial, score=1.0, vector_rank=1, keyword_rank=1),
                RetrievalResult(memory=vital, score=1.0, vector_rank=1, keyword_rank=1),
            ],
            now=NOW,
        )

        assert ordered[0].memory.id == vital.id

    def test_zero_importance_still_ranks_rather_than_vanishing(
        self, repository: MemoryRepository
    ) -> None:
        # "Unimportant" must not mean "unreachable" — the floors in the
        # multiplicative score exist so no single factor can zero a result.
        memory = repository.add(draft("barely matters", importance=0.0), now=NOW)

        ordered = rerank(
            [RetrievalResult(memory=memory, score=1.0, vector_rank=1, keyword_rank=1)], now=NOW
        )

        assert ordered[0].score > 0

    def test_pinned_memories_are_favoured(self, repository: MemoryRepository) -> None:
        ordinary = repository.add(draft("a note"), now=NOW)
        pinned = repository.add(draft("a pinned note", pinned=True), now=NOW)

        ordered = rerank(
            [
                RetrievalResult(memory=ordinary, score=1.0, vector_rank=1, keyword_rank=1),
                RetrievalResult(memory=pinned, score=1.0, vector_rank=1, keyword_rank=1),
            ],
            now=NOW,
        )

        assert ordered[0].memory.id == pinned.id


class TestRecall:
    def test_finds_a_memory_by_meaning_and_by_keyword(
        self, memory_service: MemoryService, indexer: Indexer
    ) -> None:
        memory_service.remember(draft("the auth flow uses PKCE"), now=NOW)
        memory_service.remember(draft("mochi is my cat"), now=NOW)
        indexer.drain()

        results = memory_service.recall("auth flow PKCE", now=NOW)

        assert results
        assert results[0].memory.content == "the auth flow uses PKCE"

    def test_exact_identifier_is_found_even_without_a_vector(
        self, memory_service: MemoryService
    ) -> None:
        # Deliberately not indexed: the keyword leg must carry this alone, which
        # is the failure mode embeddings cannot cover.
        memory_service.remember(draft("ticket MITTA-1481 is the auth rewrite"), now=NOW)

        results = memory_service.recall("MITTA-1481", semantic=False, now=NOW)

        assert len(results) == 1

    def test_keyword_only_and_semantic_only_can_each_be_disabled(
        self, memory_service: MemoryService, indexer: Indexer
    ) -> None:
        memory_service.remember(draft("kubernetes cluster autoscaling"), now=NOW)
        indexer.drain()

        assert memory_service.recall("kubernetes", keyword=False, now=NOW)
        assert memory_service.recall("kubernetes", semantic=False, now=NOW)

    def test_recall_marks_results_as_accessed(
        self, memory_service: MemoryService, indexer: Indexer
    ) -> None:
        memory = memory_service.remember(draft("something memorable"), now=NOW)
        indexer.drain()

        memory_service.recall("memorable", now=NOW)

        assert memory_service.get(memory.id).access_count == 1

    def test_browsing_does_not_inflate_access_counts(
        self, memory_service: MemoryService, indexer: Indexer
    ) -> None:
        # Otherwise idle browsing keeps trivia alive at the expense of memories
        # that are actually consulted.
        memory = memory_service.remember(draft("something memorable"), now=NOW)
        indexer.drain()

        memory_service.recall("memorable", touch=False, now=NOW)

        assert memory_service.get(memory.id).access_count == 0

    def test_forgotten_memories_do_not_surface(
        self, memory_service: MemoryService, indexer: Indexer
    ) -> None:
        memory = memory_service.remember(draft("a private thought"), now=NOW)
        indexer.drain()
        memory_service.forget(memory.id, now=NOW)
        indexer.drain()  # the vector must leave the index too

        assert memory_service.recall("private thought", now=NOW) == []

    def test_project_scope_is_respected_across_both_legs(
        self,
        memory_service: MemoryService,
        indexer: Indexer,
        migrated,  # type: ignore[no-untyped-def]
    ) -> None:
        with migrated.write() as conn:
            conn.executemany(
                "INSERT INTO projects (id, name, status, created_at, updated_at) "
                "VALUES (?,?, 'active', 0, 0)",
                [("prj_a", "A"), ("prj_b", "B")],
            )
        memory_service.remember(
            MemoryDraft(
                kind=MemoryKind.PROJECT, content="uses pnpm for packages", project_id="prj_a"
            ),
            now=NOW,
        )
        memory_service.remember(
            MemoryDraft(
                kind=MemoryKind.PROJECT, content="uses pnpm for packages too", project_id="prj_b"
            ),
            now=NOW,
        )
        indexer.drain()

        results = memory_service.recall("pnpm packages", project_id="prj_a", now=NOW)

        assert all(r.memory.project_id == "prj_a" for r in results)
        assert len(results) == 1

    def test_recall_on_an_empty_store_returns_nothing(self, memory_service: MemoryService) -> None:
        assert memory_service.recall("anything at all", now=NOW) == []


class TestService:
    def test_identical_content_is_not_stored_twice(self, memory_service: MemoryService) -> None:
        first = memory_service.remember(draft("Satya prefers dark mode"), now=NOW)
        second = memory_service.remember(draft("Satya prefers dark mode\n"), now=NOW)

        assert first.id == second.id
        assert memory_service.count() == 1

    def test_the_same_text_under_a_different_kind_is_a_different_memory(
        self, memory_service: MemoryService
    ) -> None:
        memory_service.remember(draft("prefers tabs"), now=NOW)
        memory_service.remember(
            MemoryDraft(kind=MemoryKind.PREFERENCE, content="prefers tabs"), now=NOW
        )

        assert memory_service.count() == 2

    def test_correction_supersedes_and_only_the_new_fact_is_recalled(
        self, memory_service: MemoryService, indexer: Indexer
    ) -> None:
        original = memory_service.remember(draft("lives in Hyderabad"), now=NOW)
        replacement = memory_service.correct(original.id, draft("lives in Bangalore"), now=NOW)
        indexer.drain()

        # Shares tokens with both rows on purpose: the superseded one must be
        # excluded by status, not by failing to match. ("where do I live" would
        # pass for the wrong reason — the double has no synonymy.)
        results = memory_service.recall("lives in", now=NOW)

        assert [r.memory.id for r in results] == [replacement.id]
        # The superseded row survives, so "where did I used to live" is answerable.
        assert memory_service.get(original.id).status is MemoryStatus.SUPERSEDED

    def test_editing_content_makes_the_vector_stale_then_fresh(
        self, memory_service: MemoryService, indexer: Indexer
    ) -> None:
        memory = memory_service.remember(draft("mentions kubernetes"), now=NOW)
        indexer.drain()

        memory_service.update(memory.id, MemoryPatch(content="mentions nomad"), now=NOW)
        assert indexer.run_once().embedded == 1  # detected as stale
        assert indexer.run_once().embedded == 0  # and now settled

    def test_purge_removes_the_vector_as_well_as_the_row(
        self, memory_service: MemoryService, indexer: Indexer
    ) -> None:
        memory = memory_service.remember(draft("delete me entirely"), now=NOW)
        indexer.drain()

        memory_service.purge(memory.id)

        assert memory_service.index_status().vector_count == 0
        assert memory_service.recall("delete me entirely", now=NOW) == []

    def test_reindex_rebuilds_every_vector(
        self, memory_service: MemoryService, indexer: Indexer
    ) -> None:
        for i in range(5):
            memory_service.remember(draft(f"fact number {i}"), now=NOW)
        indexer.drain()

        assert memory_service.reindex() == 5
        assert memory_service.index_status().consistent is True


class TestSweep:
    def test_expired_memories_are_demoted_not_deleted(self, memory_service: MemoryService) -> None:
        memory = memory_service.remember(draft("temporary note", expires_at=NOW - 1), now=NOW)

        report = memory_service.sweep(now=NOW)

        assert report.expired == 1
        assert memory_service.get(memory.id).status is MemoryStatus.FORGOTTEN
        assert memory_service.get(memory.id).content == "temporary note"

    def test_decayed_memories_are_demoted(self, memory_service: MemoryService) -> None:
        memory_service.remember(draft("trivial passing remark", importance=0.1), now=NOW)

        # Far enough out that even 0.1 importance falls under the threshold.
        report = memory_service.sweep(now=NOW + 400 * DAY)

        assert report.decayed == 1

    def test_pinned_memories_survive_any_sweep(self, memory_service: MemoryService) -> None:
        memory = memory_service.remember(
            draft("never forget this", importance=0.0, pinned=True), now=NOW
        )

        memory_service.sweep(now=NOW + 10_000 * DAY)

        assert memory_service.get(memory.id).status is MemoryStatus.ACTIVE

    def test_an_expired_but_pinned_memory_also_survives(
        self, memory_service: MemoryService
    ) -> None:
        # Explicit user intent outranks a TTL the user may have set long ago.
        memory = memory_service.remember(
            draft("pinned yet expiring", expires_at=NOW - 1, pinned=True), now=NOW
        )

        memory_service.sweep(now=NOW)

        assert memory_service.get(memory.id).status is MemoryStatus.ACTIVE

    def test_frequently_used_memories_resist_decay(
        self, memory_service: MemoryService, repository: MemoryRepository
    ) -> None:
        used = memory_service.remember(draft("a fact I keep needing", importance=0.1), now=NOW)
        ignored = memory_service.remember(draft("a fact I never need", importance=0.1), now=NOW)

        later = NOW + 400 * DAY
        for _ in range(50):
            repository.touch([used.seq], now=later)

        memory_service.sweep(now=later)

        assert memory_service.get(used.id).status is MemoryStatus.ACTIVE
        assert memory_service.get(ignored.id).status is MemoryStatus.FORGOTTEN

    def test_score_is_inspectable(self, memory_service: MemoryService) -> None:
        # A user told "this will be forgotten soon" deserves to see why.
        memory = memory_service.remember(draft("scored"), now=NOW)
        assert 0.0 < memory_service.score_of(memory, now=NOW) <= 1.0

    def test_sweep_on_an_empty_store_reports_nothing(self, memory_service: MemoryService) -> None:
        report = memory_service.sweep(now=NOW)
        assert report.total_forgotten == 0


class TestIndexer:
    def test_writing_a_memory_does_not_block_on_embedding(
        self, memory_service: MemoryService
    ) -> None:
        # "Remember this" must be instant; the vector arrives afterwards.
        memory_service.remember(draft("written now, indexed later"), now=NOW)

        assert memory_service.index_status().vector_count == 0
        assert memory_service.count() == 1

    def test_a_pass_indexes_outstanding_memories(
        self, memory_service: MemoryService, indexer: Indexer
    ) -> None:
        memory_service.remember(draft("one"), now=NOW)
        memory_service.remember(draft("two"), now=NOW)

        assert indexer.run_once().embedded == 2
        assert indexer.run_once().did_work is False

    def test_forgotten_memories_are_evicted_from_the_index(
        self, memory_service: MemoryService, indexer: Indexer
    ) -> None:
        memory = memory_service.remember(draft("to be forgotten"), now=NOW)
        indexer.drain()
        assert memory_service.index_status().vector_count == 1

        memory_service.forget(memory.id, now=NOW)

        assert indexer.run_once().removed == 1
        assert memory_service.index_status().vector_count == 0

    def test_a_model_change_makes_every_vector_stale(
        self,
        repository: MemoryRepository,
        migrated,  # type: ignore[no-untyped-def]
        paths,  # type: ignore[no-untyped-def]
    ) -> None:
        # What makes swapping the embedding model a migration rather than a
        # corruption: the staleness query notices on its own.
        from mitta.memory.embedding.deterministic import DeterministicEmbedder
        from mitta.memory.vectors.store import VectorStore, build_index

        first = DeterministicEmbedder(dim=384)
        store = VectorStore(migrated, build_index(paths.vectors / "a.faiss", first), first)
        store.open()
        indexer = Indexer(repository, store)

        repository.add(draft("stable content"), now=NOW)
        assert indexer.drain() == 1
        assert indexer.drain() == 0

        second = DeterministicEmbedder(dim=256)
        assert repository.find_stale_embeddings(model_id=second.descriptor.id) != []

    def test_a_failing_batch_does_not_lose_the_work(
        self, repository: MemoryRepository, indexer: Indexer
    ) -> None:
        # The backlog lives in durable state, so a crash mid-batch costs at most
        # a repeat rather than a permanently unindexed memory.
        repository.add(draft("survives a crash"), now=NOW)

        stale_before = repository.find_stale_embeddings(model_id="deterministic-hash-v1-384")
        assert len(stale_before) == 1

        indexer.drain()
        assert repository.find_stale_embeddings(model_id="deterministic-hash-v1-384") == []
