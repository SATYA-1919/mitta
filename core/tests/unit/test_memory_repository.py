"""Repository behaviour against a real migrated database."""

from __future__ import annotations

import pytest

from mitta.errors import NotFoundError
from mitta.memory.models import MemoryDraft, MemoryKind, MemoryPatch, MemoryStatus
from mitta.memory.repository import MemoryRepository
from mitta.persistence.database import Database


@pytest.fixture
def projects(migrated: Database) -> None:
    """Two real project rows.

    `memories.project_id` has a foreign key, so a project memory cannot be
    written against an id that does not exist — which is the constraint doing
    its job, and the reason these must be created rather than invented.
    """
    with migrated.write() as conn:
        conn.executemany(
            "INSERT INTO projects (id, name, status, created_at, updated_at) "
            "VALUES (?,?, 'active', 0, 0)",
            [("prj_a", "Project A"), ("prj_b", "Project B")],
        )


def draft(content: str, **kwargs: object) -> MemoryDraft:
    payload: dict[str, object] = {"kind": MemoryKind.LONG_TERM, "content": content}
    payload.update(kwargs)
    return MemoryDraft.model_validate(payload)


class TestWrite:
    def test_add_returns_the_stored_row(self, repository: MemoryRepository) -> None:
        memory = repository.add(draft("Satya prefers dark mode", importance=0.8))

        assert memory.id.startswith("mem_")
        assert memory.seq > 0
        assert memory.status is MemoryStatus.ACTIVE
        assert memory.importance == 0.8
        assert memory.access_count == 0
        assert memory.content_hash

    def test_partial_update_leaves_unmentioned_fields_alone(
        self, repository: MemoryRepository
    ) -> None:
        memory = repository.add(draft("original", summary="a summary"))

        updated = repository.update(memory.id, MemoryPatch(importance=0.9))

        assert updated.importance == 0.9
        assert updated.summary == "a summary"  # not cleared by omission
        assert updated.content == "original"

    def test_editing_content_rehashes_so_the_indexer_notices(
        self, repository: MemoryRepository
    ) -> None:
        memory = repository.add(draft("lives in Hyderabad"))
        before = memory.content_hash

        updated = repository.update(memory.id, MemoryPatch(content="lives in Bangalore"))

        assert updated.content_hash != before

    def test_whitespace_only_edit_does_not_rehash(self, repository: MemoryRepository) -> None:
        # Otherwise every trailing newline costs a re-embed of an unchanged fact.
        memory = repository.add(draft("a fact"))
        updated = repository.update(memory.id, MemoryPatch(content="a fact   \n"))
        assert updated.content_hash == memory.content_hash

    def test_supersede_keeps_both_rows_and_links_them(self, repository: MemoryRepository) -> None:
        original = repository.add(draft("lives in Hyderabad"))

        replacement = repository.supersede(original.id, draft("lives in Bangalore"))

        old = repository.get(original.id)
        assert old.status is MemoryStatus.SUPERSEDED
        assert old.superseded_by == replacement.id
        assert old.content == "lives in Hyderabad"  # history survives correction
        assert replacement.status is MemoryStatus.ACTIVE

    def test_forget_demotes_but_does_not_delete(self, repository: MemoryRepository) -> None:
        memory = repository.add(draft("something trivial"))

        repository.forget(memory.id)

        assert repository.get(memory.id).status is MemoryStatus.FORGOTTEN
        assert repository.get(memory.id).content == "something trivial"

    def test_pinned_memories_resist_forget(self, repository: MemoryRepository) -> None:
        memory = repository.add(draft("my passport number rule", pinned=True))

        result = repository.forget(memory.id)

        assert result.status is MemoryStatus.ACTIVE

    def test_restore_reverses_forget(self, repository: MemoryRepository) -> None:
        memory = repository.add(draft("recoverable"))
        repository.forget(memory.id)

        assert repository.restore(memory.id).status is MemoryStatus.ACTIVE

    def test_purge_is_the_only_destructive_path(self, repository: MemoryRepository) -> None:
        memory = repository.add(draft("delete me"))

        repository.purge(memory.id)

        with pytest.raises(NotFoundError):
            repository.get(memory.id)

    def test_purging_a_missing_memory_raises(self, repository: MemoryRepository) -> None:
        with pytest.raises(NotFoundError):
            repository.purge("mem_does_not_exist")

    def test_touch_records_access_without_looking_like_an_edit(
        self, repository: MemoryRepository
    ) -> None:
        memory = repository.add(draft("read me"), now=1000)

        repository.touch([memory.seq], now=2000)

        after = repository.get(memory.id)
        assert after.access_count == 1
        assert after.last_accessed_at == 2000
        # Being read is not being changed; bumping updated_at would make every
        # retrieval look like an edit to the staleness query.
        assert after.updated_at == 1000

    def test_touch_of_nothing_is_a_no_op(self, repository: MemoryRepository) -> None:
        repository.touch([])


class TestRead:
    def test_list_filters_by_kind_and_status(self, repository: MemoryRepository) -> None:
        repository.add(draft("fact one"))
        repository.add(MemoryDraft(kind=MemoryKind.PREFERENCE, content="prefers tabs"))
        forgotten = repository.add(draft("gone"))
        repository.forget(forgotten.id)

        assert len(repository.list_memories()) == 2
        assert len(repository.list_memories(kind=MemoryKind.PREFERENCE)) == 1
        assert len(repository.list_memories(status=MemoryStatus.FORGOTTEN)) == 1

    def test_list_scopes_to_a_project(self, repository: MemoryRepository, projects: None) -> None:
        repository.add(
            MemoryDraft(kind=MemoryKind.PROJECT, content="uses pnpm", project_id="prj_a")
        )
        repository.add(MemoryDraft(kind=MemoryKind.PROJECT, content="uses uv", project_id="prj_b"))

        assert len(repository.list_memories(project_id="prj_a")) == 1

    def test_get_many_preserves_the_caller_ordering(self, repository: MemoryRepository) -> None:
        # Retrieval ranks first and hydrates second; SQL row order would discard
        # the ranking entirely.
        first = repository.add(draft("first"))
        second = repository.add(draft("second"))
        third = repository.add(draft("third"))

        ordered = repository.get_many([third.seq, first.seq, second.seq])

        assert [m.content for m in ordered] == ["third", "first", "second"]

    def test_get_many_skips_missing_rows(self, repository: MemoryRepository) -> None:
        memory = repository.add(draft("present"))
        assert len(repository.get_many([memory.seq, 999_999])) == 1

    def test_find_by_hash_matches_normalised_content(self, repository: MemoryRepository) -> None:
        repository.add(draft("the same fact"))
        from mitta.memory.normalise import content_hash

        assert repository.find_by_hash(content_hash("the same fact\n")) is not None

    def test_count_respects_status(self, repository: MemoryRepository) -> None:
        memory = repository.add(draft("counted"))
        repository.add(draft("also counted"))
        repository.forget(memory.id)

        assert repository.count() == 1
        assert repository.count(status=None) == 2


class TestKeywordSearch:
    def test_finds_exact_identifiers(self, repository: MemoryRepository) -> None:
        # The reason FTS5 exists alongside FAISS: 384 floats cannot preserve a
        # literal string, and "find MITTA-1481" must find MITTA-1481.
        repository.add(draft("ticket MITTA-1481 covers the auth rewrite"))
        repository.add(draft("unrelated note about deployment"))

        hits = repository.search_keyword("MITTA-1481")

        assert len(hits) == 1

    def test_stems_verb_inflections(self, repository: MemoryRepository) -> None:
        repository.add(draft("deployed the service yesterday"))

        assert repository.search_keyword("deploying") != []
        assert repository.search_keyword("deploy") != []

    def test_does_not_stem_nominalisations(self, repository: MemoryRepository) -> None:
        # Porter reduces deployed/deploying to "deploy" but leaves "deployment"
        # alone. Documented rather than worked around: closing this gap is what
        # the semantic index is for, and a stemmer aggressive enough to catch it
        # would also merge words that mean different things.
        repository.add(draft("the deployment was flaky"))

        assert repository.search_keyword("deploy") == []
        assert repository.search_keyword("deployment") != []

    def test_forgotten_memories_are_excluded(self, repository: MemoryRepository) -> None:
        memory = repository.add(draft("secret plan"))
        repository.forget(memory.id)

        assert repository.search_keyword("secret") == []

    def test_edited_content_is_searchable_by_its_new_text(
        self, repository: MemoryRepository
    ) -> None:
        # Verifies the FTS update trigger fires — without it, search would keep
        # matching text the user has already replaced.
        memory = repository.add(draft("mentions kubernetes"))
        repository.update(memory.id, MemoryPatch(content="mentions nomad"))

        assert repository.search_keyword("kubernetes") == []
        assert repository.search_keyword("nomad") != []

    def test_purged_content_leaves_the_index(self, repository: MemoryRepository) -> None:
        memory = repository.add(draft("ephemeral"))
        repository.purge(memory.id)
        assert repository.search_keyword("ephemeral") == []

    @pytest.mark.parametrize("query", ["", "   ", '"', "AND", "foo AND", "*", "^", "(", "-"])
    def test_hostile_or_unfinished_queries_return_nothing_rather_than_raising(
        self, repository: MemoryRepository, query: str
    ) -> None:
        # A half-typed search box must not become an error dialog.
        repository.add(draft("some content"))
        repository.search_keyword(query)

    def test_punctuation_heavy_queries_still_match(self, repository: MemoryRepository) -> None:
        repository.add(draft("the build fails with error: -1 in C++"))
        assert repository.search_keyword("error: -1") != []

    def test_project_scope_is_honoured(self, repository: MemoryRepository, projects: None) -> None:
        repository.add(
            MemoryDraft(kind=MemoryKind.PROJECT, content="uses pnpm", project_id="prj_a")
        )
        repository.add(
            MemoryDraft(kind=MemoryKind.PROJECT, content="uses pnpm too", project_id="prj_b")
        )

        assert len(repository.search_keyword("pnpm", project_id="prj_a")) == 1


class TestIndexerSupport:
    def test_a_new_memory_is_reported_as_needing_a_vector(
        self, repository: MemoryRepository
    ) -> None:
        repository.add(draft("needs embedding"))

        stale = repository.find_stale_embeddings(model_id="model-a")

        assert len(stale) == 1
        assert stale[0].text == "needs embedding"

    def test_summary_is_preferred_over_content_for_embedding(
        self, repository: MemoryRepository
    ) -> None:
        repository.add(draft("a very long verbatim record", summary="the short form"))

        assert repository.find_stale_embeddings(model_id="model-a")[0].text == "the short form"

    def test_expired_returns_only_past_ttls(self, repository: MemoryRepository) -> None:
        repository.add(draft("temporary", expires_at=500))
        repository.add(draft("permanent"))
        repository.add(draft("future", expires_at=5000))

        assert len(repository.expired(now=1000)) == 1

    def test_forget_seqs_skips_pinned(self, repository: MemoryRepository) -> None:
        ordinary = repository.add(draft("ordinary"))
        pinned = repository.add(draft("pinned", pinned=True))

        affected = repository.forget_seqs([ordinary.seq, pinned.seq])

        assert affected == 1
        assert repository.get(pinned.id).status is MemoryStatus.ACTIVE

    def test_forget_seqs_of_nothing_is_a_no_op(self, repository: MemoryRepository) -> None:
        assert repository.forget_seqs([]) == 0

    def test_a_natural_language_question_matches(self, repository: MemoryRepository) -> None:
        """FTS5 implicitly ANDs space-separated terms.

        That made "what am I building?" find nothing, because no memory contains
        the word "am" — and the failure was silent, since an empty result set is
        indistinguishable from having no relevant memory.
        """
        repository.add(draft("Satya is building MITTA, an AI desktop companion"))

        assert repository.search_keyword("What am I building? One short sentence.") != []

    def test_noise_words_do_not_drag_in_unrelated_memories(
        self, repository: MemoryRepository
    ) -> None:
        # The cost of OR semantics, paid for by dropping words that appear in
        # almost every English question. Without that, "is" alone would match
        # both rows and the query would return the whole corpus.
        repository.add(draft("the deployment pipeline is flaky"))
        repository.add(draft("mochi is my cat"))

        assert len(repository.search_keyword("what is the deployment")) == 1

    def test_a_query_of_only_common_words_still_searches(
        self, repository: MemoryRepository
    ) -> None:
        # Matching weakly beats refusing to search at all.
        repository.add(draft("what"))
        assert repository.search_keyword("what") != []
