"""Embedding provider, FAISS index and the store that keeps them in step."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from mitta.errors import VectorIndexError
from mitta.memory.embedding.deterministic import DeterministicEmbedder
from mitta.memory.embedding.local import EmbeddingModelUnavailableError, LocalEmbedder
from mitta.memory.indexer import Indexer
from mitta.memory.models import MemoryDraft, MemoryKind
from mitta.memory.repository import MemoryRepository
from mitta.memory.vectors.index import FaissIndex
from mitta.memory.vectors.store import VectorStore


def draft(content: str, **kwargs: object) -> MemoryDraft:
    payload: dict[str, object] = {"kind": MemoryKind.LONG_TERM, "content": content}
    payload.update(kwargs)
    return MemoryDraft.model_validate(payload)


class TestEmbedder:
    def test_vectors_are_unit_length(self, embedder: DeterministicEmbedder) -> None:
        # Inner product only equals cosine similarity on normalised vectors, and
        # the whole index is configured for inner product.
        vectors = embedder.embed_documents(["alpha beta", "gamma"])
        assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0)

    def test_embedding_is_deterministic(self, embedder: DeterministicEmbedder) -> None:
        assert np.array_equal(
            embedder.embed_documents(["same text"]), embedder.embed_documents(["same text"])
        )

    def test_shared_tokens_score_higher_than_unrelated_text(
        self, embedder: DeterministicEmbedder
    ) -> None:
        query = embedder.embed_query("kubernetes deployment")
        related, unrelated = embedder.embed_documents(
            ["kubernetes deployment failed", "my cat is called mochi"]
        )
        assert float(query @ related) > float(query @ unrelated)

    def test_empty_input_yields_a_correctly_shaped_array(
        self, embedder: DeterministicEmbedder
    ) -> None:
        vectors = embedder.embed_documents([])
        assert vectors.shape == (0, embedder.descriptor.dim)

    def test_text_with_no_tokens_does_not_produce_nan(
        self, embedder: DeterministicEmbedder
    ) -> None:
        # A zero vector divided by its zero norm is NaN, and one NaN in a FAISS
        # index corrupts every subsequent search result, not just its own.
        vectors = embedder.embed_documents(["!!! ???"])
        assert not np.isnan(vectors).any()


class TestLocalEmbedder:
    def test_reports_unavailable_rather_than_downloading(self, tmp_path: Path) -> None:
        # R5: the weights fetch is an explicit user action, never a side effect
        # of writing a memory.
        embedder = LocalEmbedder(tmp_path / "models")

        assert embedder.is_available() is False
        with pytest.raises(EmbeddingModelUnavailableError):
            embedder.warm()

    def test_detects_an_existing_model(self, tmp_path: Path) -> None:
        cache = tmp_path / "models" / "bge"
        cache.mkdir(parents=True)
        (cache / "model.onnx").write_bytes(b"")

        assert LocalEmbedder(tmp_path / "models").is_available() is True

    def test_the_similarity_floor_is_model_specific(self, tmp_path: Path) -> None:
        # BGE rarely scores unrelated English below ~0.35; hashed bag-of-tokens
        # sits near zero. One global constant would be wrong for both.
        assert LocalEmbedder(tmp_path).descriptor.min_similarity > 0.5
        assert DeterministicEmbedder().descriptor.min_similarity < 0.1

    def test_descriptor_matches_the_deterministic_double(self, tmp_path: Path) -> None:
        # Same width, so swapping providers changes the index contents but not
        # its geometry.
        assert LocalEmbedder(tmp_path).descriptor.dim == DeterministicEmbedder().descriptor.dim


class TestFaissIndex:
    def test_round_trips_through_disk(self, tmp_path: Path) -> None:
        index = FaissIndex(tmp_path / "i.faiss", dim=4)
        index.load()
        index.add([7], np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32))
        index.save()

        reopened = FaissIndex(tmp_path / "i.faiss", dim=4)
        reopened.load()

        assert reopened.count == 1
        assert (
            reopened.search(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), limit=1)[0].seq == 7
        )

    def test_re_adding_an_id_replaces_rather_than_duplicates(self, tmp_path: Path) -> None:
        # `add_with_ids` appends. Without the remove-first in `add`, an edited
        # memory would hold two vectors and the stale one could outrank the new.
        index = FaissIndex(tmp_path / "i.faiss", dim=4)
        index.load()
        index.add([1], np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32))
        index.add([1], np.array([[0.0, 1.0, 0.0, 0.0]], dtype=np.float32))

        assert index.count == 1
        best = index.search(np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32), limit=5)
        assert len(best) == 1
        assert best[0].score == pytest.approx(1.0)

    def test_remove_drops_the_vector(self, tmp_path: Path) -> None:
        index = FaissIndex(tmp_path / "i.faiss", dim=4)
        index.load()
        index.add([1, 2], np.eye(4, dtype=np.float32)[:2])

        assert index.remove([1]) == 1
        assert index.count == 1
        assert index.contains(1) is False

    def test_a_similarity_floor_discards_poor_matches(self, tmp_path: Path) -> None:
        # A flat index has no notion of "no good match" — it returns the k
        # closest vectors however far away they are. With four memories stored,
        # every question otherwise retrieves all four.
        index = FaissIndex(tmp_path / "i.faiss", dim=4)
        index.load()
        index.add([1, 2], np.eye(4, dtype=np.float32)[:2])

        probe = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        assert len(index.search(probe, limit=5)) == 2
        assert len(index.search(probe, limit=5, min_score=0.5)) == 1

    def test_searching_an_empty_index_returns_nothing(self, tmp_path: Path) -> None:
        index = FaissIndex(tmp_path / "i.faiss", dim=4)
        index.load()
        assert index.search(np.zeros(4, dtype=np.float32), limit=5) == []

    def test_mismatched_ids_and_vectors_are_rejected(self, tmp_path: Path) -> None:
        index = FaissIndex(tmp_path / "i.faiss", dim=4)
        index.load()
        with pytest.raises(VectorIndexError):
            index.add([1, 2], np.zeros((1, 4), dtype=np.float32))

    def test_wrong_width_is_rejected(self, tmp_path: Path) -> None:
        index = FaissIndex(tmp_path / "i.faiss", dim=4)
        index.load()
        with pytest.raises(VectorIndexError):
            index.add([1], np.zeros((1, 8), dtype=np.float32))

    def test_a_corrupt_index_is_rebuilt_rather_than_fatal(self, tmp_path: Path) -> None:
        # Derived state. Refusing to start because a rebuildable cache is damaged
        # would turn a self-healing situation into an outage.
        path = tmp_path / "i.faiss"
        path.write_bytes(b"this is not a faiss index")

        index = FaissIndex(path, dim=4)
        index.load()

        assert index.count == 0

    def test_a_width_change_discards_the_old_index(self, tmp_path: Path) -> None:
        path = tmp_path / "i.faiss"
        old = FaissIndex(path, dim=4)
        old.load()
        old.add([1], np.zeros((1, 4), dtype=np.float32))
        old.save()

        # A new embedding model with a different width: every vector is wrong.
        new = FaissIndex(path, dim=8)
        new.load()

        assert new.count == 0

    def test_save_is_atomic(self, tmp_path: Path) -> None:
        index = FaissIndex(tmp_path / "i.faiss", dim=4)
        index.load()
        index.add([1], np.zeros((1, 4), dtype=np.float32))
        index.save()

        assert list(tmp_path.glob("*.tmp")) == []
        assert (tmp_path / "i.faiss").stat().st_mode & 0o777 == 0o600


class TestVectorStore:
    def test_open_registers_the_index(self, vector_store: VectorStore) -> None:
        status = vector_store.status()

        assert status.model_id.startswith("deterministic-hash")
        assert status.consistent is True

    def test_upsert_records_bookkeeping_alongside_the_vector(
        self, repository: MemoryRepository, vector_store: VectorStore
    ) -> None:
        memory = repository.add(draft("indexed content"))
        vectors = vector_store.provider.embed_documents(["indexed content"])

        vector_store.upsert([(memory.seq, memory.content_hash)], vectors)

        status = vector_store.status()
        assert status.vector_count == 1
        assert status.tracked_count == 1
        assert status.consistent is True

    def test_a_missing_index_file_is_recovered_by_rebuilding(
        self,
        repository: MemoryRepository,
        vector_store: VectorStore,
        indexer: Indexer,
        paths,  # type: ignore[no-untyped-def]
        migrated,  # type: ignore[no-untyped-def]
        embedder: DeterministicEmbedder,
    ) -> None:
        # SQLite commits and FAISS writes are separate operations with no shared
        # transaction. When they disagree, the derived side loses.
        repository.add(draft("first"))
        repository.add(draft("second"))
        indexer.drain()
        assert vector_store.status().vector_count == 2

        index_path = paths.vectors / "memories.faiss"
        index_path.unlink()

        fresh = VectorStore(migrated, FaissIndex(index_path, dim=embedder.descriptor.dim), embedder)
        status = fresh.open()

        assert status.consistent is True
        assert status.vector_count == 0  # tracking cleared, ready for re-index
        assert Indexer(repository, fresh).drain() == 2

    def test_remove_clears_both_sides(
        self, repository: MemoryRepository, vector_store: VectorStore, indexer: Indexer
    ) -> None:
        memory = repository.add(draft("transient"))
        indexer.drain()

        vector_store.remove([memory.seq])

        status = vector_store.status()
        assert status.vector_count == 0
        assert status.tracked_count == 0
