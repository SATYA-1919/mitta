"""Hash-chained audit log.

Every action MITTA takes on the user's behalf is recorded here, and each entry
carries a hash of its predecessor. Editing or removing a past row breaks the
chain from that point on, which `verify_chain` detects.

This is tamper-*evidence*, not tamper-proofing. Anyone with write access to the
database can rewrite the whole chain from scratch. What it defends against is
the realistic case: a bug, a partial delete, or a process that silently dropped
a write. Those all produce a broken chain, and the alternative is a log nobody
can tell has holes in it.

The user's own commitment is what makes this worth building: MITTA should not do
anything without telling them. A log they cannot check is a promise they have to
take on faith.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from mitta.ids import AUDIT, prefixed
from mitta.persistence.database import Database
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

type Actor = Literal["user", "agent", "plugin", "scheduler", "system"]
type Verdict = Literal["allow", "confirm", "deny"]


@dataclass(frozen=True, slots=True)
class AuditEntry:
    id: str
    at: int
    actor: Actor
    action: str
    subject: str | None
    verdict: Verdict | None
    detail: dict[str, Any]
    entry_hash: str
    prev_hash: str | None


def _entry_hash(
    *,
    entry_id: str,
    at: int,
    actor: str,
    action: str,
    subject: str | None,
    verdict: str | None,
    detail: str,
    prev_hash: str | None,
) -> str:
    payload = json.dumps(
        {
            "id": entry_id,
            "at": at,
            "actor": actor,
            "action": action,
            "subject": subject,
            "verdict": verdict,
            "detail": detail,
            "prev": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        actor: Actor,
        action: str,
        subject: str | None = None,
        verdict: Verdict | None = None,
        turn_id: str | None = None,
        invocation_id: str | None = None,
        detail: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> AuditEntry:
        """Append an entry.

        Reading the previous hash and writing the new row happen in one
        transaction. Two concurrent appends would otherwise both chain off the
        same predecessor, forking the chain and making verification fail on a
        database that is actually intact.
        """
        ts = now if now is not None else int(time.time())
        entry_id = prefixed(AUDIT)
        detail_json = json.dumps(detail or {}, sort_keys=True, separators=(",", ":"))

        with self._db.write() as conn:
            row = conn.execute(
                "SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = row["entry_hash"] if row is not None else None

            entry_hash = _entry_hash(
                entry_id=entry_id,
                at=ts,
                actor=actor,
                action=action,
                subject=subject,
                verdict=verdict,
                detail=detail_json,
                prev_hash=prev_hash,
            )

            conn.execute(
                """
                INSERT INTO audit_log
                    (id, at, actor, action, subject, verdict, turn_id,
                     invocation_id, detail, prev_hash, entry_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    entry_id,
                    ts,
                    actor,
                    action,
                    subject,
                    verdict,
                    turn_id,
                    invocation_id,
                    detail_json,
                    prev_hash,
                    entry_hash,
                ),
            )

        return AuditEntry(
            id=entry_id,
            at=ts,
            actor=actor,
            action=action,
            subject=subject,
            verdict=verdict,
            detail=detail or {},
            entry_hash=entry_hash,
            prev_hash=prev_hash,
        )

    def recent(self, *, limit: int = 100) -> list[AuditEntry]:
        """Newest first — what the user is looking for is what just happened."""
        with self._db.read() as conn:
            rows = conn.execute(
                """
                SELECT id, at, actor, action, subject, verdict, detail,
                       entry_hash, prev_hash
                FROM   audit_log ORDER BY seq DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            AuditEntry(
                id=row["id"],
                at=row["at"],
                actor=row["actor"],
                action=row["action"],
                subject=row["subject"],
                verdict=row["verdict"],
                detail=json.loads(row["detail"]),
                entry_hash=row["entry_hash"],
                prev_hash=row["prev_hash"],
            )
            for row in rows
        ]

    def verify_chain(self) -> int | None:
        """Recompute every hash. Returns the `seq` of the first broken entry.

        `None` means intact. Walked oldest-first because a break invalidates
        everything after it, and the useful answer is where the damage starts.
        """
        with self._db.read() as conn:
            rows = conn.execute(
                """
                SELECT seq, id, at, actor, action, subject, verdict,
                       detail, prev_hash, entry_hash
                FROM   audit_log ORDER BY seq
                """
            ).fetchall()

        expected_prev: str | None = None
        for row in rows:
            if row["prev_hash"] != expected_prev:
                return int(row["seq"])

            recomputed = _entry_hash(
                entry_id=row["id"],
                at=row["at"],
                actor=row["actor"],
                action=row["action"],
                subject=row["subject"],
                verdict=row["verdict"],
                detail=row["detail"],
                prev_hash=row["prev_hash"],
            )
            if recomputed != row["entry_hash"]:
                return int(row["seq"])
            expected_prev = row["entry_hash"]

        return None
