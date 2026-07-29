"""On-device embedding via ONNX.

`bge-small-en-v1.5` — 384 dimensions, ~67 MB, MIT licensed, run through
`fastembed`'s ONNX runtime rather than `sentence-transformers`. That choice is
worth 2 GB: sentence-transformers pulls PyTorch, which would dominate the
installed size of the entire application for a model that is 67 MB.

**The download is explicit.** Acquiring the weights is the only network call
this module can make, and it happens when the user asks for it — never as a
side-effect of writing a memory. R5 says nothing leaves the machine except
requests to the configured LLM APIs; a silent fetch from a third-party host on
first use would violate the spirit of that even though weights are not user
data. `ensure_available()` reports what is missing; `download()` gets it.

Until the model is present, the memory engine runs on `DeterministicEmbedder`
and every vector is automatically re-embedded once the real model lands, because
`model_id` changes and the staleness query notices.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import numpy as np

from mitta.errors import StorageError
from mitta.memory.embedding.base import ModelDescriptor, Vector, l2_normalise
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

DEFAULT_MODEL: Final = "BAAI/bge-small-en-v1.5"
DEFAULT_DIM: Final = 384

# BGE is asymmetric: it was trained with this instruction on queries and nothing
# on documents. Omitting it costs several points of retrieval accuracy, and the
# loss is invisible without a benchmark — which is exactly why it is a constant
# here rather than something a caller is trusted to remember.
QUERY_INSTRUCTION: Final = "Represent this sentence for searching relevant passages: "

# Measured, not guessed. On this model a clearly related query/document pair
# scores ~0.85 and a clearly unrelated one ~0.36, so 0.55 sits in the gap with
# room on both sides. BGE compresses everything into a narrow high band — a
# floor borrowed from a different model family would silently reject everything
# or nothing.
MIN_SIMILARITY: Final = 0.55


class EmbeddingModelUnavailableError(StorageError):
    """The weights are not on disk and no download has been authorised."""

    code = "storage.embedding_model_unavailable"


class LocalEmbedder:
    """Lazy, thread-safe wrapper around a fastembed `TextEmbedding`."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        model_id: str = DEFAULT_MODEL,
        dim: int = DEFAULT_DIM,
        threads: int | None = None,
    ) -> None:
        self._cache_dir = cache_dir
        self._model_id = model_id
        self._threads = threads
        self._descriptor = ModelDescriptor(id=model_id, dim=dim, min_similarity=MIN_SIMILARITY)
        self._model: Any | None = None
        # Loading is not reentrant-safe in onnxruntime and the indexer runs on a
        # worker thread while the API may warm on another.
        self._lock = threading.Lock()

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    # -- availability -------------------------------------------------------- #

    def is_available(self) -> bool:
        """Whether the weights are already on disk.

        Detected by looking for the ONNX file rather than by attempting a load,
        so the check stays cheap enough to call from a status endpoint.
        """
        if not self._cache_dir.exists():
            return False
        return any(self._cache_dir.rglob("*.onnx"))

    def ensure_available(self) -> None:
        if not self.is_available():
            raise EmbeddingModelUnavailableError(
                f"Embedding model '{self._model_id}' is not downloaded",
                details={"model_id": self._model_id, "cache_dir": str(self._cache_dir)},
            )

    def download(self) -> ModelDescriptor:
        """Fetch the weights. The one network call in this module.

        Idempotent — fastembed reuses an existing cache — so a caller that is
        unsure whether the model is present may simply call this.
        """
        log.info(
            "embedding.download_started",
            extra={"model_id": self._model_id, "cache_dir": str(self._cache_dir)},
        )
        started = time.monotonic()
        self._cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._lock:
            self._model = self._construct()
        log.info(
            "embedding.download_finished",
            extra={"model_id": self._model_id, "seconds": round(time.monotonic() - started, 2)},
        )
        return self._descriptor

    # -- inference ----------------------------------------------------------- #

    def _construct(self) -> Any:
        # Deferred: importing fastembed pulls ~130 MB of ONNX runtime, which no
        # caller should pay for unless they are actually going to embed something.
        from fastembed import TextEmbedding

        return TextEmbedding(
            model_name=self._model_id,
            cache_dir=str(self._cache_dir),
            threads=self._threads,
        )

    def warm(self) -> None:
        """Load the model from disk. Never downloads."""
        if self._model is not None:
            return
        self.ensure_available()
        with self._lock:
            if self._model is None:
                started = time.monotonic()
                self._model = self._construct()
                log.info(
                    "embedding.model_loaded",
                    extra={
                        "model_id": self._model_id,
                        "seconds": round(time.monotonic() - started, 2),
                    },
                )

    def _require_model(self) -> Any:
        if self._model is None:
            self.warm()
        assert self._model is not None
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> Vector:
        if not texts:
            return np.zeros((0, self._descriptor.dim), dtype=np.float32)
        model = self._require_model()
        vectors = np.asarray(list(model.embed(list(texts))), dtype=np.float32)
        self._check_dim(vectors)
        # fastembed normalises already; doing it again is cheap and makes the
        # inner-product-is-cosine assumption hold regardless of that staying true.
        return l2_normalise(vectors)

    def embed_query(self, text: str) -> Vector:
        model = self._require_model()
        prompted = f"{QUERY_INSTRUCTION}{text}"
        vectors = np.asarray(list(model.embed([prompted])), dtype=np.float32)
        self._check_dim(vectors)
        return np.asarray(l2_normalise(vectors)[0], dtype=np.float32)

    def _check_dim(self, vectors: Vector) -> None:
        """Fail loudly on a dimension mismatch.

        A model whose real width differs from the descriptor would be silently
        rejected by FAISS' `add` or, worse, indexed into the wrong index. Both
        are far harder to diagnose than an exception naming both numbers.
        """
        if vectors.ndim != 2 or vectors.shape[1] != self._descriptor.dim:
            raise StorageError(
                f"Embedding model '{self._model_id}' returned width "
                f"{vectors.shape[-1] if vectors.size else 'none'}, "
                f"expected {self._descriptor.dim}"
            )
