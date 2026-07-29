"""A hash-based embedding provider.

Not a mock — a real, deterministic provider that is used in two places:

1. **Tests.** Every layer above embeddings can be exercised at full fidelity
   without loading a 130 MB runtime.
2. **Before the model is downloaded.** The indexer runs, the vector store fills,
   and retrieval works — it just retrieves by token overlap instead of meaning.
   Degraded, and honestly labelled as such through its `model_id`, which is what
   makes every vector automatically re-embedded once the real model arrives.

The second use is why this is production code rather than a test fixture. The
alternative — refusing to index until a model is present — makes first-run
memory silently lossy, and the backfill would then be invisible work nobody
knows to wait for.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

import numpy as np

from mitta.memory.embedding.base import ModelDescriptor, Vector, l2_normalise

_TOKEN = re.compile(r"[a-z0-9]+")

# Same dimensionality as bge-small-en-v1.5, so swapping providers does not
# change the index geometry — only its contents.
_DIM = 384


class DeterministicEmbedder:
    """Hashed bag-of-tokens projected onto the unit sphere.

    Two texts sharing tokens get similar vectors; unrelated texts get near-
    orthogonal ones. That is enough for hybrid retrieval to behave sensibly and
    for its tests to assert real ranking behaviour rather than mock call counts.

    What it cannot do is synonymy — "car" and "automobile" are orthogonal here.
    That is precisely the capability the real model adds.
    """

    def __init__(self, dim: int = _DIM) -> None:
        # Signed hashing puts unrelated texts near zero rather than in BGE's
        # narrow high band, so the floor here is far lower — the same number
        # would reject every hit.
        self._descriptor = ModelDescriptor(
            id=f"deterministic-hash-v1-{dim}", dim=dim, min_similarity=0.05, degraded=True
        )

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    def _vector(self, text: str) -> Vector:
        dim = self._descriptor.dim
        acc = np.zeros(dim, dtype=np.float32)
        tokens = _TOKEN.findall(text.lower())
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % dim
            # Signed hashing: without it every vector lies in the non-negative
            # orthant and all pairs look similar, which would make the double
            # useless for testing ranking.
            sign = 1.0 if digest[4] & 1 else -1.0
            acc[bucket] += sign
        return acc

    def embed_documents(self, texts: Sequence[str]) -> Vector:
        if not texts:
            return np.zeros((0, self._descriptor.dim), dtype=np.float32)
        stacked = np.stack([self._vector(text) for text in texts])
        return l2_normalise(np.asarray(stacked, dtype=np.float32))

    def embed_query(self, text: str) -> Vector:
        return np.asarray(l2_normalise(self._vector(text)[np.newaxis, :])[0], dtype=np.float32)

    def warm(self) -> None:
        """No-op. Nothing to load."""
