"""Conversation persistence.

The transcript is the other half of memory. Extracted facts live in `memories`;
this is the record they were extracted *from*, and the thing the user actually
scrolls back through.

`message_count` on the conversation is denormalised deliberately. The list view
shows it for every row, and a `COUNT(*)` per row turns opening the sidebar into
N queries. It is maintained inside the same transaction as the insert, so it
cannot drift — a trigger would work too, but the write path is here and one
place is easier to reason about than two.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence

from mitta.conversations.models import (
    Conversation,
    ConversationDraft,
    ConversationStatus,
    InputKind,
    Message,
    MessageDraft,
    MessageRole,
    Register,
    Turn,
    TurnStatus,
)
from mitta.errors import NotFoundError, StorageError
from mitta.ids import CONVERSATION, MESSAGE, TURN, prefixed
from mitta.persistence.database import Database
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

_CONVERSATION_COLUMNS = """
    seq, id, title, project_id, status, pinned, forked_from, summary,
    message_count, created_at, updated_at
"""

_TURN_COLUMNS = """
    seq, id, conversation_id, project_id, status, input_kind, register,
    plan_id, tokens_in, tokens_out, tool_call_count, error, started_at, ended_at
"""

_MESSAGE_COLUMNS = """
    seq, id, conversation_id, turn_id, role, content, content_raw, tool_calls,
    tool_call_id, input_kind, model_id, provider, register, token_input,
    token_output, latency_ms, styled, error, created_at
"""


def _json_or_none(raw: str | None) -> dict[str, object] | None:
    if raw is None:
        return None
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else None


def _row_to_conversation(row: sqlite3.Row) -> Conversation:
    return Conversation(
        seq=row["seq"],
        id=row["id"],
        title=row["title"],
        project_id=row["project_id"],
        status=ConversationStatus(row["status"]),
        pinned=bool(row["pinned"]),
        forked_from=row["forked_from"],
        summary=row["summary"],
        message_count=row["message_count"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_turn(row: sqlite3.Row) -> Turn:
    return Turn(
        seq=row["seq"],
        id=row["id"],
        conversation_id=row["conversation_id"],
        project_id=row["project_id"],
        status=TurnStatus(row["status"]),
        input_kind=InputKind(row["input_kind"]),
        register=Register(row["register"]) if row["register"] else None,
        plan_id=row["plan_id"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        tool_call_count=row["tool_call_count"],
        error=_json_or_none(row["error"]),
        started_at=row["started_at"],
        ended_at=row["ended_at"],
    )


def _row_to_message(row: sqlite3.Row) -> Message:
    tool_calls = json.loads(row["tool_calls"]) if row["tool_calls"] else None
    return Message(
        seq=row["seq"],
        id=row["id"],
        conversation_id=row["conversation_id"],
        turn_id=row["turn_id"],
        role=MessageRole(row["role"]),
        content=row["content"],
        content_raw=row["content_raw"],
        tool_calls=tool_calls if isinstance(tool_calls, list) else None,
        tool_call_id=row["tool_call_id"],
        input_kind=InputKind(row["input_kind"]) if row["input_kind"] else None,
        model_id=row["model_id"],
        provider=row["provider"],
        register=Register(row["register"]) if row["register"] else None,
        token_input=row["token_input"],
        token_output=row["token_output"],
        latency_ms=row["latency_ms"],
        styled=bool(row["styled"]),
        error=_json_or_none(row["error"]),
        created_at=row["created_at"],
    )


class ConversationRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- conversations -------------------------------------------------------- #

    def create(self, draft: ConversationDraft, *, now: int | None = None) -> Conversation:
        ts = now if now is not None else int(time.time())
        conversation_id = prefixed(CONVERSATION)

        with self._db.write() as conn:
            conn.execute(
                """
                INSERT INTO conversations
                    (id, title, project_id, status, pinned, forked_from,
                     message_count, created_at, updated_at)
                VALUES (?,?,?,?,0,?,0,?,?)
                """,
                (
                    conversation_id,
                    draft.title,
                    draft.project_id,
                    ConversationStatus.ACTIVE.value,
                    draft.forked_from,
                    ts,
                    ts,
                ),
            )
        log.info("conversation.created", extra={"conversation_id": conversation_id})
        return self.get(conversation_id)

    def get(self, conversation_id: str) -> Conversation:
        with self._db.read() as conn:
            row = conn.execute(
                f"SELECT {_CONVERSATION_COLUMNS} FROM conversations WHERE id = ?",  # noqa: S608 - constant columns and literal clauses; values are bound
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("conversation", conversation_id)
        return _row_to_conversation(row)

    def list_conversations(
        self,
        *,
        status: ConversationStatus = ConversationStatus.ACTIVE,
        project_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Conversation]:
        clauses = ["status = ?"]
        params: list[object] = [status.value]
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        params.extend((limit, offset))

        sql = (
            f"SELECT {_CONVERSATION_COLUMNS} FROM conversations "  # noqa: S608 - constant columns and literal clauses; values are bound
            f"WHERE {' AND '.join(clauses)} "
            # Pinned first, then most recently touched. `updated_at` rather than
            # `created_at`: a thread returned to yesterday is more relevant than
            # one started yesterday and abandoned.
            "ORDER BY pinned DESC, updated_at DESC LIMIT ? OFFSET ?"
        )
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_conversation(row) for row in rows]

    def rename(self, conversation_id: str, title: str, *, now: int | None = None) -> Conversation:
        ts = now if now is not None else int(time.time())
        with self._db.write() as conn:
            conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, ts, conversation_id),
            )
        return self.get(conversation_id)

    def set_pinned(
        self, conversation_id: str, pinned: bool, *, now: int | None = None
    ) -> Conversation:
        ts = now if now is not None else int(time.time())
        with self._db.write() as conn:
            conn.execute(
                "UPDATE conversations SET pinned = ?, updated_at = ? WHERE id = ?",
                (int(pinned), ts, conversation_id),
            )
        return self.get(conversation_id)

    def archive(self, conversation_id: str, *, now: int | None = None) -> Conversation:
        """Hide from the default list. The transcript is untouched."""
        ts = now if now is not None else int(time.time())
        with self._db.write() as conn:
            conn.execute(
                "UPDATE conversations SET status = ?, updated_at = ? WHERE id = ?",
                (ConversationStatus.ARCHIVED.value, ts, conversation_id),
            )
        return self.get(conversation_id)

    def unarchive(self, conversation_id: str, *, now: int | None = None) -> Conversation:
        ts = now if now is not None else int(time.time())
        with self._db.write() as conn:
            conn.execute(
                "UPDATE conversations SET status = ?, updated_at = ? WHERE id = ?",
                (ConversationStatus.ACTIVE.value, ts, conversation_id),
            )
        return self.get(conversation_id)

    def delete(self, conversation_id: str) -> None:
        """Permanent, and cascades to turns and messages.

        Distinct from `archive` for the same reason `purge` is distinct from
        `forget` in the memory engine (DEC-053): one is reversible and one is
        not, and a UI author needs to be able to tell which is which from the
        method name alone.
        """
        with self._db.write() as conn:
            cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            if cur.rowcount == 0:
                raise NotFoundError("conversation", conversation_id)
        log.warning("conversation.deleted", extra={"conversation_id": conversation_id})

    def count(self, *, status: ConversationStatus | None = ConversationStatus.ACTIVE) -> int:
        with self._db.read() as conn:
            if status is None:
                row = conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM conversations WHERE status = ?", (status.value,)
                ).fetchone()
        return int(row["n"])

    # -- turns ---------------------------------------------------------------- #

    def begin_turn(
        self,
        conversation_id: str,
        *,
        input_kind: InputKind = InputKind.TEXT,
        project_id: str | None = None,
        now: int | None = None,
    ) -> Turn:
        ts = now if now is not None else int(time.time())
        turn_id = prefixed(TURN)
        with self._db.write() as conn:
            conn.execute(
                """
                INSERT INTO turns
                    (id, conversation_id, project_id, status, input_kind, started_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    turn_id,
                    conversation_id,
                    project_id,
                    TurnStatus.RUNNING.value,
                    input_kind.value,
                    ts,
                ),
            )
        return self.get_turn(turn_id)

    def end_turn(
        self,
        turn_id: str,
        *,
        status: TurnStatus,
        register: Register | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        tool_call_count: int = 0,
        error: dict[str, object] | None = None,
        now: int | None = None,
    ) -> Turn:
        ts = now if now is not None else int(time.time())
        with self._db.write() as conn:
            conn.execute(
                """
                UPDATE turns
                SET    status = ?, register = ?, tokens_in = ?, tokens_out = ?,
                       tool_call_count = ?, error = ?, ended_at = ?
                WHERE  id = ?
                """,
                (
                    status.value,
                    register.value if register else None,
                    tokens_in,
                    tokens_out,
                    tool_call_count,
                    json.dumps(error) if error is not None else None,
                    ts,
                    turn_id,
                ),
            )
        return self.get_turn(turn_id)

    def get_turn(self, turn_id: str) -> Turn:
        with self._db.read() as conn:
            row = conn.execute(
                f"SELECT {_TURN_COLUMNS} FROM turns WHERE id = ?",  # noqa: S608 - constant columns and literal clauses; values are bound
                (turn_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("turn", turn_id)
        return _row_to_turn(row)

    def running_turns(self) -> list[Turn]:
        """Turns still marked running.

        Used at startup: a turn left running is one the process died during, and
        it must be reconciled rather than left to sit in the UI as a spinner
        that will never resolve.
        """
        with self._db.read() as conn:
            rows = conn.execute(
                f"SELECT {_TURN_COLUMNS} FROM turns WHERE status = ? ORDER BY started_at",  # noqa: S608 - constant columns and literal clauses; values are bound
                (TurnStatus.RUNNING.value,),
            ).fetchall()
        return [_row_to_turn(row) for row in rows]

    def reconcile_orphaned_turns(self, *, now: int | None = None) -> int:
        """Mark turns orphaned by a crash as failed. Returns how many.

        Called at startup. Without it, a crash mid-turn leaves a row that says
        `running` forever, and the UI shows a thinking indicator for work no
        process is doing.
        """
        ts = now if now is not None else int(time.time())
        orphaned = self.running_turns()
        if not orphaned:
            return 0

        error = json.dumps(
            {
                "code": "turn.interrupted",
                "message": "MITTA stopped while this turn was running.",
            }
        )
        with self._db.write() as conn:
            conn.execute(
                "UPDATE turns SET status = ?, error = ?, ended_at = ? WHERE status = ?",
                (TurnStatus.FAILED.value, error, ts, TurnStatus.RUNNING.value),
            )
        log.warning("turn.orphans_reconciled", extra={"count": len(orphaned)})
        return len(orphaned)

    # -- messages ------------------------------------------------------------- #

    def add_message(
        self, conversation_id: str, draft: MessageDraft, *, now: int | None = None
    ) -> Message:
        """Append to a transcript and bump the conversation in one transaction.

        Atomicity matters here: a message written without the counter update
        would make the sidebar disagree with the thread, and the disagreement
        would be permanent because nothing recomputes it.
        """
        ts = now if now is not None else int(time.time())
        message_id = prefixed(MESSAGE)

        with self._db.write() as conn:
            cur = conn.execute(
                """
                INSERT INTO messages
                    (id, conversation_id, turn_id, role, content, content_raw,
                     tool_calls, tool_call_id, input_kind, model_id, provider,
                     register, token_input, token_output, latency_ms, styled,
                     error, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    message_id,
                    conversation_id,
                    draft.turn_id,
                    draft.role.value,
                    draft.content,
                    draft.content_raw,
                    json.dumps(draft.tool_calls) if draft.tool_calls is not None else None,
                    draft.tool_call_id,
                    draft.input_kind.value if draft.input_kind else None,
                    draft.model_id,
                    draft.provider,
                    draft.register.value if draft.register else None,
                    draft.token_input,
                    draft.token_output,
                    draft.latency_ms,
                    int(draft.styled),
                    json.dumps(draft.error) if draft.error is not None else None,
                    ts,
                ),
            )
            if cur.lastrowid is None:  # pragma: no cover - sqlite always sets this
                raise StorageError("INSERT did not yield a rowid")

            conn.execute(
                """
                UPDATE conversations
                SET    message_count = message_count + 1, updated_at = ?
                WHERE  id = ?
                """,
                (ts, conversation_id),
            )

        return self.get_message(message_id)

    def get_message(self, message_id: str) -> Message:
        with self._db.read() as conn:
            row = conn.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id = ?",  # noqa: S608 - constant columns and literal clauses; values are bound
                (message_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("message", message_id)
        return _row_to_message(row)

    def messages(
        self, conversation_id: str, *, limit: int = 200, before_seq: int | None = None
    ) -> list[Message]:
        """Transcript, oldest first.

        Paginated backwards from `before_seq` so a long thread loads its tail
        first — which is what the user is looking at — then earlier pages as
        they scroll up.
        """
        params: list[object] = [conversation_id]
        clause = ""
        if before_seq is not None:
            clause = "AND seq < ? "
            params.append(before_seq)
        params.append(limit)

        sql = (
            f"SELECT {_MESSAGE_COLUMNS} FROM messages "  # noqa: S608 - constant columns and literal clauses; values are bound
            f"WHERE conversation_id = ? {clause}"
            "ORDER BY seq DESC LIMIT ?"
        )
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_message(row) for row in reversed(rows)]

    def turn_messages(self, turn_id: str) -> list[Message]:
        with self._db.read() as conn:
            rows = conn.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE turn_id = ? ORDER BY seq",  # noqa: S608 - constant columns and literal clauses; values are bound
                (turn_id,),
            ).fetchall()
        return [_row_to_message(row) for row in rows]

    def recent_context(self, conversation_id: str, *, limit: int = 20) -> list[Message]:
        """The last few messages, for prompt assembly.

        Separate from `messages` because the caller is different: this feeds the
        context chokepoint (R5), which is budgeted, and the default is
        deliberately small. A UI page size doubling as a prompt size is how a
        context window silently overflows.
        """
        return self.messages(conversation_id, limit=limit)

    def delete_messages(self, seqs: Sequence[int]) -> int:
        if not seqs:
            return 0
        placeholders = ",".join("?" * len(seqs))
        sql = f"DELETE FROM messages WHERE seq IN ({placeholders})"  # noqa: S608 - constant columns and literal clauses; values are bound
        with self._db.write() as conn:
            cur = conn.execute(sql, tuple(seqs))
            return cur.rowcount
