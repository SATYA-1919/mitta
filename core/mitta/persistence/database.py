"""SQLite connection management.

Concurrency model (DATABASE_DESIGN.md §2): SQLite permits exactly one writer.
Rather than open several read/write connections and let `busy_timeout` sort out
the collisions — which produces intermittent, load-dependent `SQLITE_BUSY`
failures that only appear under real use — this module serialises writes through
a **single write connection guarded by a lock**, and serves reads from a small
pool. The constraint becomes explicit and testable instead of emergent.

WAL is what makes that acceptable: the background consolidation writer never
blocks a reader, so the UI does not stutter while memory is being consolidated.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from mitta.config.settings import DatabaseSettings
from mitta.errors import StorageError
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)


def _configure(conn: sqlite3.Connection, settings: DatabaseSettings, *, writer: bool) -> None:
    """Apply per-connection pragmas.

    `journal_mode` is a database-level property and only needs setting once, but
    it is idempotent and cheap, and setting it on every connection removes an
    ordering dependency between whoever connects first and everyone else.
    """
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute("PRAGMA foreign_keys = ON")
        cur.execute(f"PRAGMA busy_timeout = {settings.busy_timeout_ms}")
        cur.execute("PRAGMA temp_store = MEMORY")
        cur.execute(f"PRAGMA cache_size = -{settings.cache_size_kib}")
        cur.execute(f"PRAGMA mmap_size = {settings.mmap_size_bytes}")
        if writer:
            cur.execute("PRAGMA synchronous = NORMAL")
            cur.execute("PRAGMA auto_vacuum = INCREMENTAL")
        else:
            cur.execute("PRAGMA query_only = ON")
    finally:
        cur.close()


class Database:
    """Owns every connection to `mitta.db`.

    Not a repository. Repositories are built on top of this in Phase 5; this
    class knows about connections and transactions and nothing about tables.
    """

    def __init__(self, path: Path, settings: DatabaseSettings) -> None:
        self._path = path
        self._settings = settings
        self._write_lock = threading.RLock()
        # Transaction depth for the calling thread. Thread-local rather than a
        # plain counter: the indexer writes on a worker thread while the API
        # reads on another, and only the writer's own reads must be redirected.
        self._local = threading.local()
        self._write_conn: sqlite3.Connection | None = None
        self._read_pool: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(
            maxsize=settings.read_pool_size
        )
        self._closed = False

    # -- lifecycle ---------------------------------------------------------- #

    def connect(self) -> None:
        """Open the write connection and fill the read pool."""
        if self._write_conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        self._write_conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
            isolation_level=None,  # explicit transaction control
        )
        _configure(self._write_conn, self._settings, writer=True)

        for _ in range(self._settings.read_pool_size):
            conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
            _configure(conn, self._settings, writer=False)
            self._read_pool.put(conn)

        # 0600 — the memory database, the LLM payload record and the audit log
        # all live here (R5).
        self._path.chmod(0o600)
        log.info("database.connected", extra={"path": str(self._path)})

    def close(self) -> None:
        self._closed = True
        with self._write_lock:
            if self._write_conn is not None:
                self._write_conn.close()
                self._write_conn = None
        while not self._read_pool.empty():
            try:
                self._read_pool.get_nowait().close()
            except queue.Empty:  # pragma: no cover - race on shutdown
                break
        log.info("database.closed")

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- access ------------------------------------------------------------- #

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Borrow a connection for reading.

        If the calling thread is inside a write transaction, the *write*
        connection is handed back instead of one from the pool. This is not an
        optimisation — it is a correctness requirement.

        Under WAL, a pooled reader sees the last committed snapshot, so a
        repository method that inserts a row and then reads it back through
        `read()` finds nothing: the insert is still uncommitted on another
        connection. That breaks the moment two repository methods compose, which
        is exactly what `write()` being reentrant invites. Routing reads through
        the open transaction makes read-your-own-writes hold, so a method behaves
        the same standalone as it does nested.
        """
        if self._closed or self._write_conn is None:
            raise StorageError("Database is not connected")

        if getattr(self._local, "depth", 0) > 0:
            yield self._write_conn
            return

        conn = self._read_pool.get()
        try:
            yield conn
        finally:
            self._read_pool.put(conn)

    @contextmanager
    def write(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Enter a write transaction. Commits on success, rolls back on error.

        `BEGIN IMMEDIATE` acquires the write lock up front. The default
        (deferred) would acquire it at the first write statement, which means a
        transaction that reads, decides, then writes can fail *after* the read —
        the classic upgrade deadlock. Taking the lock at the start converts that
        into a clean wait bounded by `busy_timeout`.

        Reentrant: a nested `write()` joins the outer transaction rather than
        starting a second one, so a repository method can be called standalone
        or as part of a larger unit of work without knowing which.
        """
        if self._closed or self._write_conn is None:
            raise StorageError("Database is not connected")

        with self._write_lock:
            conn = self._write_conn
            if conn.in_transaction:
                yield conn  # join the enclosing transaction
                return
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            self._local.depth = getattr(self._local, "depth", 0) + 1
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
            finally:
                # Cleared before the lock is released, so a later `read()` on
                # this thread goes back to the pool rather than holding onto a
                # writer it no longer owns.
                self._local.depth -= 1

    # -- maintenance -------------------------------------------------------- #

    def backup_to(self, destination: Path) -> Path:
        """Consistent online backup via SQLite's backup API.

        A file copy of a WAL-mode database is not safe — the `-wal` file may
        hold committed pages the main file does not. `Connection.backup` takes
        the right locks and produces a coherent single file.
        """
        if self._write_conn is None:
            raise StorageError("Database is not connected")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._write_lock:
            target = sqlite3.connect(destination)
            try:
                self._write_conn.backup(target)
            finally:
                target.close()
        destination.chmod(0o600)
        log.info("database.backup_created", extra={"path": str(destination)})
        return destination

    def integrity_check(self) -> bool:
        with self.read() as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row is not None and row[0] == "ok")

    def incremental_vacuum(self, pages: int = 1000) -> None:
        """Reclaim freed pages. Never full VACUUM — that rewrites the whole file
        and needs double the disk space, which is not something to do unasked."""
        with self._write_lock:
            if self._write_conn is None:
                raise StorageError("Database is not connected")
            self._write_conn.execute(f"PRAGMA incremental_vacuum({pages})")

    def stats(self) -> dict[str, Any]:
        with self.read() as conn:
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        return {
            "path": str(self._path),
            "size_bytes": page_count * page_size,
            "free_pages": freelist,
            "journal_mode": journal,
        }

    @property
    def path(self) -> Path:
        return self._path
