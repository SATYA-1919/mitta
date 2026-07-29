"""Memory engine — the centre of the product.

Six conceptual stores over one table (DEC-023), a local embedding model, a FAISS
index derived from SQLite, and hybrid retrieval that fuses vector and keyword
search.
"""

from mitta.memory.models import (
    Memory,
    MemoryDraft,
    MemoryKind,
    MemoryPatch,
    MemoryStatus,
    SourceKind,
)
from mitta.memory.repository import MemoryRepository
from mitta.memory.retrieval import HybridRetriever, RetrievalQuery, RetrievalResult
from mitta.memory.service import MemoryService, SweepReport

__all__ = [
    "HybridRetriever",
    "Memory",
    "MemoryDraft",
    "MemoryKind",
    "MemoryPatch",
    "MemoryRepository",
    "MemoryService",
    "MemoryStatus",
    "RetrievalQuery",
    "RetrievalResult",
    "SourceKind",
    "SweepReport",
]
