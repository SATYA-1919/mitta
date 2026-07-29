"""Memory persistence.

The only module that writes SQL against `memories`. Everything above it works
in domain objects, so a schema change has one blast radius.

Two invariants this module exists to hold:

**Nothing is destroyed.** `forget()` sets a status. `supersede()` links old to
new and keeps both. There is no method here that deletes a memory, because a
system holding someone's accumulated context should not have a code path that
loses it by accident. Hard deletion is a separate, explicit, user-initiated
operation (`purge`), and it says so in its name.

**The write path never blocks on inference.** Writing a memory records content
and a hash; the vector arrives later, found by the indexer via that hash. This
is what keeps "remember this" instant even when the embedding model is cold.

A note on the `# noqa: S608` markers. Every one of them interpolates exactly one
of three things: `_COLUMNS` (a module constant), a run of `?` placeholders whose
*count* varies but whose values are always bound, or a clause list built from a
literal allowlist of field names. No caller-supplied value is ever interpolated
into SQL in this file. The rule stays enabled rather than being switched off
per-file, so a future query that breaks that invariant is still flagged.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass

from mitta.errors import NotFoundError, StorageError
from mitta.ids import MEMORY, prefixed
from mitta.memory.models import (
    Memory,
    MemoryDraft,
    MemoryKind,
    MemoryPatch,
    MemoryStatus,
    SourceKind,
    parse_attributes,
)
from mitta.memory.normalise import content_hash
from mitta.persistence.database import Database
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

_COLUMNS = """
    seq, id, kind, project_id, content, summary, attributes,
    importance, confidence, status, superseded_by,
    source_kind, source_message_id, content_hash, pinned,
    access_count, last_accessed_at, expires_at, created_at, updated_at
"""


def _row_to_memory(row: sqlite3.Row) -> Memory:
    return Memory(
        seq=row["seq"],
        id=row["id"],
        kind=MemoryKind(row["kind"]),
        project_id=row["project_id"],
        content=row["content"],
        summary=row["summary"],
        attributes=json.loads(row["attributes"]),
        importance=row["importance"],
        confidence=row["confidence"],
        status=MemoryStatus(row["status"]),
        superseded_by=row["superseded_by"],
        source_kind=SourceKind(row["source_kind"]),
        source_message_id=row["source_message_id"],
        content_hash=row["content_hash"],
        pinned=bool(row["pinned"]),
        access_count=row["access_count"],
        last_accessed_at=row["last_accessed_at"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _placeholders(count: int) -> str:
    """`?,?,?` for an `IN` clause.

    sqlite3 cannot bind a sequence to a single parameter, so the placeholder
    count must be interpolated. Only the count varies — the values themselves
    are always bound — which is what keeps every `IN` query here injection-free.
    """
    return ",".join("?" * count)


@dataclass(frozen=True, slots=True)
class StaleEmbedding:
    """A memory whose vector is missing, outdated, or from the wrong model."""

    seq: int
    text: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class KeywordHit:
    seq: int
    rank: float


class MemoryRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- write -------------------------------------------------------------- #

    def add(self, draft: MemoryDraft, *, now: int | None = None) -> Memory:
        """Insert a memory and return it as stored."""
        ts = now if now is not None else int(time.time())
        # Validated again here rather than trusting the caller's model: `add` is
        # reachable from the API, from consolidation and from import, and only
        # one of those constructs a MemoryDraft under our control.
        attributes = parse_attributes(draft.kind, draft.attributes)
        memory_id = prefixed(MEMORY)
        digest = content_hash(draft.content)

        with self._db.write() as conn:
            cur = conn.execute(
                """
                INSERT INTO memories (
                    id, kind, project_id, content, summary, attributes,
                    importance, confidence, status, source_kind, source_message_id,
                    content_hash, pinned, access_count, expires_at,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)
                """,
                (
                    memory_id,
                    draft.kind.value,
                    draft.project_id,
                    draft.content,
                    draft.summary,
                    attributes.model_dump_json(),
                    draft.importance,
                    draft.confidence,
                    MemoryStatus.ACTIVE.value,
                    draft.source_kind.value,
                    draft.source_message_id,
                    digest,
                    int(draft.pinned),
                    draft.expires_at,
                    ts,
                    ts,
                ),
            )
            seq = cur.lastrowid
            if seq is None:  # pragma: no cover - sqlite always sets this on INSERT
                raise StorageError("INSERT did not yield a rowid")

        log.info("memory.added", extra={"memory_id": memory_id, "kind": draft.kind.value})
        return self.get_by_seq(seq)

    def update(self, memory_id: str, patch: MemoryPatch, *, now: int | None = None) -> Memory:
        """Apply a partial update.

        Only fields explicitly present in the patch are touched — absence and
        `None` are different, and conflating them would let a caller updating
        `importance` silently clear `summary`.
        """
        ts = now if now is not None else int(time.time())
        supplied = patch.model_dump(exclude_unset=True)
        if not supplied:
            return self.get(memory_id)

        current = self.get(memory_id)
        assignments: list[str] = []
        values: list[object] = []

        for field in ("content", "summary", "importance", "confidence", "expires_at"):
            if field in supplied:
                assignments.append(f"{field} = ?")
                values.append(supplied[field])

        if "pinned" in supplied:
            assignments.append("pinned = ?")
            values.append(int(bool(supplied["pinned"])))

        if "attributes" in supplied:
            attributes = parse_attributes(current.kind, supplied["attributes"])
            assignments.append("attributes = ?")
            values.append(attributes.model_dump_json())

        if "content" in supplied:
            # Rehash so the indexer notices. Without this the memory reads as
            # updated but keeps serving its old vector — the exact silent-staleness
            # failure `content_hash` exists to prevent.
            assignments.append("content_hash = ?")
            values.append(content_hash(str(supplied["content"])))

        assignments.append("updated_at = ?")
        values.append(ts)
        values.append(memory_id)

        with self._db.write() as conn:
            sql = f"UPDATE memories SET {', '.join(assignments)} WHERE id = ?"  # noqa: S608 - see module docstring
            conn.execute(sql, values)

        return self.get(memory_id)

    def supersede(self, old_id: str, draft: MemoryDraft, *, now: int | None = None) -> Memory:
        """Replace a memory with a corrected one, preserving the original.

        Correction is not deletion. "I moved to Bangalore" does not make "I lived
        in Hyderabad" false — it makes it historical, and an assistant that
        cannot distinguish those two has no way to answer "where did I used to
        live". Both rows survive; the link records which replaced which.
        """
        ts = now if now is not None else int(time.time())
        with self._db.write() as conn:
            old = self.get(old_id)
            replacement = self.add(draft, now=ts)
            conn.execute(
                "UPDATE memories SET status = ?, superseded_by = ?, updated_at = ? WHERE seq = ?",
                (MemoryStatus.SUPERSEDED.value, replacement.id, ts, old.seq),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_links (from_seq, to_seq, relation, created_at)
                VALUES (?, ?, 'supersedes', ?)
                """,
                (replacement.seq, old.seq, ts),
            )

        log.info("memory.superseded", extra={"old_id": old_id, "new_id": replacement.id})
        return replacement

    def forget(self, memory_id: str, *, now: int | None = None) -> Memory:
        """Demote to `forgotten`. The row survives; only its visibility changes."""
        ts = now if now is not None else int(time.time())
        with self._db.write() as conn:
            cur = conn.execute(
                "UPDATE memories SET status = ?, updated_at = ? WHERE id = ? AND pinned = 0",
                (MemoryStatus.FORGOTTEN.value, ts, memory_id),
            )
            if cur.rowcount == 0:
                # Either it does not exist or it is pinned. `get` distinguishes.
                existing = self.get(memory_id)
                log.info("memory.forget_skipped_pinned", extra={"memory_id": memory_id})
                return existing
        return self.get(memory_id)

    def restore(self, memory_id: str, *, now: int | None = None) -> Memory:
        """Return a forgotten memory to active. The inverse of `forget`."""
        ts = now if now is not None else int(time.time())
        with self._db.write() as conn:
            conn.execute(
                "UPDATE memories SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (MemoryStatus.ACTIVE.value, ts, memory_id, MemoryStatus.FORGOTTEN.value),
            )
        return self.get(memory_id)

    def purge(self, memory_id: str) -> None:
        """Delete permanently. The only method here that destroys data.

        Reachable only from an explicit user action. Cascades remove the FTS
        row, the embedding bookkeeping and any links; the FAISS vector is
        removed by the vector store, which watches for exactly this.
        """
        with self._db.write() as conn:
            cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            if cur.rowcount == 0:
                raise NotFoundError("memory", memory_id)
        log.warning("memory.purged", extra={"memory_id": memory_id})

    def touch(self, seqs: Sequence[int], *, now: int | None = None) -> None:
        """Record that these memories were retrieved.

        Feeds the access term of the retention score, so a memory that keeps
        proving useful stops decaying. Deliberately does not bump `updated_at`:
        being read is not being changed, and conflating them would make every
        retrieval look like an edit to the indexer.
        """
        if not seqs:
            return
        ts = now if now is not None else int(time.time())
        placeholders = _placeholders(len(seqs))
        sql = (
            "UPDATE memories SET access_count = access_count + 1, last_accessed_at = ? "  # noqa: S608 - see module docstring
            f"WHERE seq IN ({placeholders})"
        )
        with self._db.write() as conn:
            conn.execute(sql, (ts, *seqs))

    # -- read --------------------------------------------------------------- #

    def get(self, memory_id: str) -> Memory:
        with self._db.read() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM memories WHERE id = ?",  # noqa: S608 - see module docstring
                (memory_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("memory", memory_id)
        return _row_to_memory(row)

    def get_by_seq(self, seq: int) -> Memory:
        with self._db.read() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM memories WHERE seq = ?",  # noqa: S608 - see module docstring
                (seq,),
            ).fetchone()
        if row is None:
            raise NotFoundError("memory", str(seq))
        return _row_to_memory(row)

    def get_many(self, seqs: Sequence[int]) -> list[Memory]:
        """Fetch by seq, preserving the caller's ordering.

        Retrieval ranks first and hydrates second, so the order coming in is the
        ranked order and SQL's arbitrary row order would discard the ranking.
        """
        if not seqs:
            return []
        placeholders = _placeholders(len(seqs))
        with self._db.read() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM memories WHERE seq IN ({placeholders})",  # noqa: S608 - see module docstring
                tuple(seqs),
            ).fetchall()
        by_seq = {row["seq"]: _row_to_memory(row) for row in rows}
        return [by_seq[seq] for seq in seqs if seq in by_seq]

    def list_memories(
        self,
        *,
        kind: MemoryKind | None = None,
        project_id: str | None = None,
        status: MemoryStatus = MemoryStatus.ACTIVE,
        pinned_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Memory]:
        clauses = ["status = ?"]
        params: list[object] = [status.value]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if pinned_only:
            clauses.append("pinned = 1")

        params.extend((limit, offset))
        sql = (
            f"SELECT {_COLUMNS} FROM memories "  # noqa: S608 - see module docstring
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY importance DESC, created_at DESC LIMIT ? OFFSET ?"
        )
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_memory(row) for row in rows]

    def count(self, *, status: MemoryStatus | None = MemoryStatus.ACTIVE) -> int:
        with self._db.read() as conn:
            if status is None:
                row = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM memories WHERE status = ?", (status.value,)
                ).fetchone()
        return int(row["n"])

    def find_by_hash(self, digest: str) -> Memory | None:
        """Exact-duplicate lookup, used to avoid re-storing a known fact."""
        with self._db.read() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM memories WHERE content_hash = ? AND status = ? LIMIT 1",  # noqa: S608 - see module docstring
                (digest, MemoryStatus.ACTIVE.value),
            ).fetchone()
        return _row_to_memory(row) if row is not None else None

    # -- keyword search ------------------------------------------------------ #

    def search_keyword(
        self,
        query: str,
        *,
        limit: int = 20,
        project_id: str | None = None,
    ) -> list[KeywordHit]:
        """BM25 search over the FTS5 index.

        Returns seqs and ranks rather than memories: the caller fuses these with
        vector ranks before deciding what is worth hydrating, and hydrating rows
        that fusion then discards is wasted work.
        """
        match = _to_fts_query(query)
        if match is None:
            return []

        sql = """
            SELECT m.seq AS seq, bm25(memories_fts) AS rank
            FROM   memories_fts
            JOIN   memories m ON m.seq = memories_fts.rowid
            WHERE  memories_fts MATCH ?
              AND  m.status = 'active'
        """
        params: list[object] = [match]
        if project_id is not None:
            sql += " AND m.project_id = ?"
            params.append(project_id)
        # bm25() returns *negative* scores, better matches being more negative.
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)

        try:
            with self._db.read() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            # A user typing `foo AND` produces a syntax error from FTS5. An empty
            # result is the right answer to an unfinished query; an exception
            # would turn a half-typed search box into an error dialog.
            log.debug("memory.fts_query_rejected", extra={"error": str(exc)})
            return []

        return [KeywordHit(seq=row["seq"], rank=float(row["rank"])) for row in rows]

    # -- indexer support ----------------------------------------------------- #

    def find_stale_embeddings(self, *, model_id: str, limit: int = 128) -> list[StaleEmbedding]:
        """Memories needing (re-)embedding, per `DATABASE_DESIGN.md` §4.3.

        The worker discovers its own work from durable state rather than from an
        in-memory queue, so a crash mid-batch costs nothing: the same query
        returns the same rows on restart.

        The `model_id` clause is what makes changing the embedding model a
        migration instead of a corruption — every row becomes eligible at once.
        """
        with self._db.read() as conn:
            rows = conn.execute(
                """
                SELECT m.seq AS seq, m.content_hash AS content_hash,
                       COALESCE(m.summary, m.content) AS text
                FROM   memories m
                LEFT   JOIN memory_embeddings e ON e.memory_seq = m.seq
                WHERE  m.status = 'active'
                  AND (e.memory_seq IS NULL
                   OR  e.content_hash <> m.content_hash
                   OR  e.model_id <> ?)
                ORDER  BY m.importance DESC, m.created_at DESC
                LIMIT  ?
                """,
                (model_id, limit),
            ).fetchall()
        return [
            StaleEmbedding(seq=row["seq"], text=row["text"], content_hash=row["content_hash"])
            for row in rows
        ]

    def find_orphaned_vectors(self, *, index_name: str) -> list[int]:
        """Embedding rows whose memory is no longer active.

        These vectors are still in FAISS and will still be returned by a search,
        so they must be removed from the index. The row itself is cleaned up with
        them.
        """
        with self._db.read() as conn:
            rows = conn.execute(
                """
                SELECT e.memory_seq AS seq
                FROM   memory_embeddings e
                LEFT   JOIN memories m ON m.seq = e.memory_seq
                WHERE  e.index_name = ?
                  AND (m.seq IS NULL OR m.status <> 'active')
                """,
                (index_name,),
            ).fetchall()
        return [int(row["seq"]) for row in rows]

    def expired(self, *, now: int | None = None, limit: int = 500) -> list[int]:
        """Active memories past their TTL."""
        ts = now if now is not None else int(time.time())
        with self._db.read() as conn:
            rows = conn.execute(
                """
                SELECT seq FROM memories
                WHERE  status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?
                LIMIT  ?
                """,
                (ts, limit),
            ).fetchall()
        return [int(row["seq"]) for row in rows]

    def forget_seqs(self, seqs: Sequence[int], *, now: int | None = None) -> int:
        """Bulk-demote by seq, skipping pinned rows. Returns rows affected."""
        if not seqs:
            return 0
        ts = now if now is not None else int(time.time())
        placeholders = _placeholders(len(seqs))
        sql = (
            "UPDATE memories SET status = ?, updated_at = ? "  # noqa: S608 - see module docstring
            f"WHERE seq IN ({placeholders}) "
            "AND pinned = 0 AND status = 'active'"
        )
        with self._db.write() as conn:
            cur = conn.execute(sql, (MemoryStatus.FORGOTTEN.value, ts, *seqs))
            return cur.rowcount


_FTS_SPECIALS = str.maketrans(dict.fromkeys('"*():^-', " "))

#: Dropped from queries. Not a linguistic stopword list — just the words that
#: appear in almost every English question and therefore carry no signal about
#: which memory is wanted. Retrieval is OR-based (see `_to_fts_query`), so
#: leaving them in would let "what" alone match half the corpus.
# fmt: off
_NOISE_WORDS = frozenset({
    "a", "am", "an", "and", "are", "as", "at", "be", "by", "can", "could",
    "did", "do", "does", "for", "from", "had", "has", "have", "how", "i",
    "if", "in", "is", "it", "its", "me", "my", "of", "on", "or", "should",
    "that", "the", "their", "them", "then", "there", "these", "they", "this",
    "to", "was", "were", "what", "when", "where", "which", "who", "whom",
    "why", "will", "with", "would", "you", "your",
})
# fmt: on


def _to_fts_query(raw: str) -> str | None:
    """Turn user input into a safe FTS5 MATCH expression.

    Two things are happening here, and the second was a bug worth recording.

    **Quoting.** Every term is quoted and FTS5's operators are stripped. Users
    type `C++` and `error: -1` into search boxes, and FTS5 reads those as
    syntax; quoting turns them back into what the user meant.

    **OR, not AND.** FTS5 treats space-separated terms as an implicit `AND`, so
    a natural-language query required *every* word to be present. Asking "what
    am I building?" found nothing, because no memory contains "am". Almost
    nothing ever matched, and the failure was silent — an empty result set looks
    identical to having no relevant memory.

    OR is correct here because ranking is not this function's job: BM25 orders
    by term rarity, RRF fuses that with the vector leg (DEC-051), and the
    re-ranker takes it from there. Common words are dropped first so a query
    does not match half the corpus on "the".
    """
    cleaned = raw.translate(_FTS_SPECIALS)
    terms = [term for term in cleaned.split() if term.strip()]

    # Keep rare words; fall back to everything if the query was *only* noise,
    # since matching weakly beats refusing to search.
    meaningful = [t for t in terms if t.lower() not in _NOISE_WORDS and len(t) > 1]
    chosen = meaningful or terms
    if not chosen:
        return None
    return " OR ".join(f'"{term}"' for term in chosen)
