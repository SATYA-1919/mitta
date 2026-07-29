"""Embedding provider contract.

Embeddings are the one model MITTA runs locally, and that is what makes R5
achievable rather than aspirational: semantic memory search never touches the
network, so the memory database never needs to leave the machine to be useful.

The protocol exists so that everything downstream — the indexer, the vector
store, retrieval — can be built and tested without loading a model. That is not
only a test convenience: a 130 MB ONNX runtime import in the middle of a unit
test suite is the difference between a 2-second and a 40-second feedback loop,
and slow tests get run less.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

# float32 throughout: FAISS stores float32 regardless, so using float64 above it
# only buys a conversion on every call.
type Vector = NDArray[np.float32]


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    """Identity of an embedding model.

    `id` is written into `memory_embeddings.model_id` and is the key that makes
    a model change self-healing — every row embedded by a different model is
    automatically stale (`DATABASE_DESIGN.md` §4.3). It must therefore change
    whenever the *output* changes, which includes a revision of the same model.
    """

    id: str
    dim: int
    normalised: bool = True

    # Cosine similarity below which a hit is noise rather than a weak match.
    #
    # This exists because a flat index returns the `k` nearest neighbours
    # unconditionally — with four memories stored, a search for "drug allergies"
    # returns all four, and the cat comes back at 99% of the top score. Ranking
    # cannot fix that; only a floor can. Without one, context assembly would
    # pack unrelated personal facts into every prompt, which is a retrieval
    # quality problem and an R5 problem at the same time.
    #
    # The value belongs to the model because the scales differ wildly: BGE
    # rarely scores unrelated English below ~0.35, while hashed bag-of-tokens
    # sits near zero. A single global constant would be wrong for both.
    min_similarity: float = 0.0

    # Whether this provider is a stand-in for the real model. Declared by the
    # provider rather than inferred by callers: a status endpoint that guesses
    # will eventually guess wrong, and reporting "model available" while running
    # on a fallback is the specific lie this field exists to prevent.
    degraded: bool = False

    @property
    def metric(self) -> str:
        """FAISS metric this model's vectors want.

        Inner product on unit-normalised vectors is cosine similarity, without
        the per-query normalisation L2 would need.
        """
        return "ip" if self.normalised else "l2"


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns text into vectors."""

    @property
    def descriptor(self) -> ModelDescriptor: ...

    def embed_documents(self, texts: Sequence[str]) -> Vector:
        """Embed stored content. Returns shape `(len(texts), dim)`."""
        ...

    def embed_query(self, text: str) -> Vector:
        """Embed a search query. Returns shape `(dim,)`.

        Separate from `embed_documents` because asymmetric models (BGE, E5,
        GTE) prepend a different instruction to queries than to documents, and
        embedding a query as a document measurably degrades recall. Collapsing
        these into one method makes that mistake invisible.
        """
        ...

    def warm(self) -> None:
        """Load whatever is needed to serve the first call promptly."""
        ...


def l2_normalise(vectors: Vector) -> Vector:
    """Scale each row to unit length, leaving zero rows alone.

    The zero guard matters: an all-zero row divided by its zero norm yields NaN,
    and a single NaN in a FAISS index poisons every subsequent search result
    rather than just its own.
    """
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    safe = np.where(norms == 0, 1.0, norms)
    return np.asarray(vectors / safe, dtype=np.float32)
