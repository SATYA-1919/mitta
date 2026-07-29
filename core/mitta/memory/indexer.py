"""Background embedding worker.

Finds its own work by querying for memories whose vector is missing, stale, or
from a different model (`DATABASE_DESIGN.md` §4.3), embeds them in batches, and
records the result.

**Why a poll rather than a queue.** An in-memory queue loses its contents on
crash, and the memories it was holding would then never be indexed — invisibly,
because nothing would be left to say they were pending. The staleness query
derives the backlog from durable state, so a crash costs at most one batch of
repeated work and the system is self-correcting by construction. It also means
"re-embed everything with a new model" needs no special code path: change the
model id and every row becomes eligible.

**Why it runs off the write path.** `remember this` must be instant. Embedding
a batch takes tens of milliseconds with a warm model and several seconds with a
cold one, and no user should wait on that to be told their note was saved.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from mitta.memory.repository import MemoryRepository
from mitta.memory.vectors.store import VectorStore
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

DEFAULT_BATCH_SIZE = 64
DEFAULT_IDLE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class IndexPass:
    """What one call to `run_once` accomplished."""

    embedded: int
    removed: int

    @property
    def did_work(self) -> bool:
        return self.embedded > 0 or self.removed > 0


class Indexer:
    def __init__(
        self,
        repository: MemoryRepository,
        store: VectorStore,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
    ) -> None:
        self._repository = repository
        self._store = store
        self._batch_size = batch_size
        self._idle_seconds = idle_seconds

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Set when a memory is written, so a new note is indexed promptly
        # instead of waiting out the poll interval.
        self._wake = threading.Event()

    # -- one pass ------------------------------------------------------------ #

    def run_once(self) -> IndexPass:
        """Do at most one batch of work. Safe to call from anywhere."""
        removed = self._drop_orphans()
        embedded = self._embed_batch()
        return IndexPass(embedded=embedded, removed=removed)

    def _drop_orphans(self) -> int:
        """Remove vectors for memories that are no longer active.

        Done first. A forgotten memory that is still in the index is worse than
        one missing from it: the first returns content the user asked not to
        see, the second merely under-retrieves.
        """
        orphans = self._repository.find_orphaned_vectors(index_name=self._store.index_name)
        if not orphans:
            return 0
        self._store.remove(orphans)
        log.info("indexer.orphans_removed", extra={"count": len(orphans)})
        return len(orphans)

    def _embed_batch(self) -> int:
        model_id = self._store.provider.descriptor.id
        stale = self._repository.find_stale_embeddings(model_id=model_id, limit=self._batch_size)
        if not stale:
            return 0

        started = time.monotonic()
        vectors = self._store.provider.embed_documents([item.text for item in stale])
        # The hash recorded is the one read alongside the text, not a fresh one.
        # If the memory is edited between the read and this write, the recorded
        # hash no longer matches and the next pass re-embeds it — which is
        # correct. Re-hashing here would record the *new* hash against the *old*
        # vector and the staleness would become permanently invisible.
        self._store.upsert([(item.seq, item.content_hash) for item in stale], vectors)

        log.info(
            "indexer.batch_embedded",
            extra={
                "count": len(stale),
                "model_id": model_id,
                "seconds": round(time.monotonic() - started, 3),
            },
        )
        return len(stale)

    def drain(self, *, max_batches: int = 1000) -> int:
        """Index everything outstanding. Returns the number embedded.

        Bounded rather than `while True`: a bug that keeps returning the same
        stale rows would otherwise spin forever holding the write lock.
        """
        total = 0
        for _ in range(max_batches):
            result = self.run_once()
            if not result.did_work:
                break
            total += result.embedded
        return total

    # -- background loop ------------------------------------------------------ #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mitta-indexer", daemon=True)
        self._thread.start()
        log.info("indexer.started")

    def stop(self, *, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():  # pragma: no cover - only on a wedged batch
            log.warning("indexer.stop_timed_out")
        self._thread = None
        log.info("indexer.stopped")

    def notify(self) -> None:
        """Signal that there is likely new work."""
        self._wake.set()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.run_once()
            except Exception:
                # A failing batch must not kill the worker: the next pass finds
                # the same rows, and a permanently failing row is visible in the
                # logs rather than as silence.
                log.exception("indexer.pass_failed")
                result = IndexPass(embedded=0, removed=0)

            if result.did_work:
                continue  # more may be waiting; do not sleep on a full batch
            self._wake.wait(timeout=self._idle_seconds)
            self._wake.clear()
