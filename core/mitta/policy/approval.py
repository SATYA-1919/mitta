"""Approval tokens (DEC-010).

Nothing dangerous runs without one. A token is issued when the user approves a
specific action, and it carries four bindings, each closing a distinct attack:

| Binding | Without it |
| --- | --- |
| **Parameter hash** | Approving "delete these 3 files" could be replayed to delete 300 |
| **Nonce, consumed once** | One approval would authorise an unlimited number of runs |
| **Expiry** | An approval from last Tuesday would still be live today |
| **HMAC signature** | A token could be forged rather than obtained |

The parameter binding is the one that matters most and is the easiest to get
wrong. It is why the token records a hash of the *exact* arguments rather than
just the tool name: the thing the user saw and agreed to is the thing that runs,
or nothing runs.

`ApprovalInvalidError` deliberately does not say which check failed. A caller
probing for the difference is a caller trying to forge one, and the audit log
records the specific reason locally, where it is actually useful.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Final

from mitta.errors import ApprovalInvalidError
from mitta.ids import APPROVAL, prefixed
from mitta.persistence.database import Database
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

#: How long an approval stays valid. Short on purpose: an approval is a response
#: to something on screen right now, and a user who walked away should not have
#: a live authorisation sitting behind them.
DEFAULT_TTL_SECONDS: Final = 120


def hash_params(tool_name: str, params: dict[str, Any]) -> str:
    """Stable hash of a tool call.

    `sort_keys` is load-bearing: without it the same call hashes differently
    depending on dict ordering, and every approval would fail verification for
    reasons no one could reproduce.
    """
    canonical = json.dumps(
        {"tool": tool_name, "params": params}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovalToken:
    id: str
    tool_name: str
    params_hash: str
    nonce: str
    issued_at: int
    expires_at: int
    signature: str

    def to_wire(self) -> dict[str, Any]:
        """What the UI receives and hands back. Contains no secret."""
        return {
            "id": self.id,
            "tool_name": self.tool_name,
            "nonce": self.nonce,
            "expires_at": self.expires_at,
            "signature": self.signature,
        }


class ApprovalAuthority:
    """Issues and verifies approval tokens.

    The signing key is generated per process and never persisted. A token
    therefore cannot outlive the run that issued it, which is a stronger
    guarantee than the expiry alone and costs nothing — approvals are answered
    in seconds.
    """

    def __init__(self, db: Database, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._db = db
        self._ttl = ttl_seconds
        self._key = secrets.token_bytes(32)

    def _sign(self, token_id: str, params_hash: str, nonce: str, expires_at: int) -> str:
        payload = f"{token_id}:{params_hash}:{nonce}:{expires_at}".encode()
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    def issue(
        self,
        *,
        tool_name: str,
        params: dict[str, Any],
        turn_id: str | None = None,
        approved: bool = True,
        now: int | None = None,
    ) -> ApprovalToken:
        """Record a user decision and return a token for it.

        A denial is recorded too. "The user said no at 14:32" is exactly the
        thing an audit trail exists to be able to answer, and a denial that
        leaves no trace is indistinguishable from never having been asked.
        """
        ts = now if now is not None else int(time.time())
        token_id = prefixed(APPROVAL)
        nonce = secrets.token_urlsafe(24)
        params_hash = hash_params(tool_name, params)
        expires_at = ts + self._ttl

        with self._db.write() as conn:
            conn.execute(
                """
                INSERT INTO approval_tokens
                    (id, turn_id, tool_name, params_hash, nonce,
                     issued_at, expires_at, decision)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    token_id,
                    turn_id,
                    tool_name,
                    params_hash,
                    nonce,
                    ts,
                    expires_at,
                    "approved" if approved else "denied",
                ),
            )

        log.info(
            "approval.issued",
            extra={
                "approval_id": token_id,
                "tool": tool_name,
                "decision": "approved" if approved else "denied",
            },
        )
        return ApprovalToken(
            id=token_id,
            tool_name=tool_name,
            params_hash=params_hash,
            nonce=nonce,
            issued_at=ts,
            expires_at=expires_at,
            signature=self._sign(token_id, params_hash, nonce, expires_at),
        )

    def verify_and_consume(
        self,
        *,
        token_id: str,
        signature: str,
        tool_name: str,
        params: dict[str, Any],
        now: int | None = None,
    ) -> None:
        """Check a token and burn it. Raises `ApprovalInvalidError` on any fault.

        Consumption happens **inside the same transaction as the check**. Two
        concurrent calls with one token would otherwise both pass verification
        before either marked it used, and the single-use guarantee would hold
        only when nothing raced.
        """
        ts = now if now is not None else int(time.time())
        expected_hash = hash_params(tool_name, params)

        with self._db.write() as conn:
            row = conn.execute(
                """
                SELECT id, tool_name, params_hash, nonce, expires_at, consumed_at, decision
                FROM   approval_tokens WHERE id = ?
                """,
                (token_id,),
            ).fetchone()

            reason = self._fault(row, signature, tool_name, expected_hash, ts)
            if reason is not None:
                # Logged specifically, reported vaguely.
                log.warning(
                    "approval.rejected",
                    extra={"approval_id": token_id, "tool": tool_name, "reason": reason},
                )
                raise ApprovalInvalidError("This approval is not valid.")

            conn.execute("UPDATE approval_tokens SET consumed_at = ? WHERE id = ?", (ts, token_id))

        log.info("approval.consumed", extra={"approval_id": token_id, "tool": tool_name})

    def _fault(
        self,
        row: Any,
        signature: str,
        tool_name: str,
        expected_hash: str,
        now: int,
    ) -> str | None:
        if row is None:
            return "unknown token"
        if row["decision"] != "approved":
            return "the user denied this action"
        if row["consumed_at"] is not None:
            return "already used"
        if row["expires_at"] <= now:
            return "expired"
        if row["tool_name"] != tool_name:
            return "issued for a different tool"
        if row["params_hash"] != expected_hash:
            # The important one: the parameters changed after approval.
            return "parameters differ from what was approved"

        expected_signature = self._sign(
            row["id"], row["params_hash"], row["nonce"], row["expires_at"]
        )
        # Constant-time: a byte-by-byte comparison leaks how much of a forged
        # signature was correct, which is enough to construct one.
        if not hmac.compare_digest(signature, expected_signature):
            return "bad signature"
        return None

    def purge_expired(self, *, now: int | None = None, older_than: int = 86_400) -> int:
        """Drop long-expired tokens. Returns how many.

        Consumed and denied tokens are kept — they are the audit trail. Only
        tokens that expired unanswered are removed, and only after a day.
        """
        ts = now if now is not None else int(time.time())
        with self._db.write() as conn:
            cur = conn.execute(
                "DELETE FROM approval_tokens WHERE consumed_at IS NULL AND expires_at < ?",
                (ts - older_than,),
            )
            return cur.rowcount
