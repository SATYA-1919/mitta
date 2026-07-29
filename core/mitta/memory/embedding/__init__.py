"""Local embedding providers."""

from mitta.memory.embedding.base import (
    EmbeddingProvider,
    ModelDescriptor,
    Vector,
    l2_normalise,
)
from mitta.memory.embedding.deterministic import DeterministicEmbedder
from mitta.memory.embedding.local import (
    DEFAULT_MODEL,
    EmbeddingModelUnavailableError,
    LocalEmbedder,
)

__all__ = [
    "DEFAULT_MODEL",
    "DeterministicEmbedder",
    "EmbeddingModelUnavailableError",
    "EmbeddingProvider",
    "LocalEmbedder",
    "ModelDescriptor",
    "Vector",
    "l2_normalise",
]
