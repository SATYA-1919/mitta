"""Project persistence.

Three tables behind one repository, because they are never useful apart: a
project with no paths scopes nothing, and a path with no project belongs to
nobody. Splitting them into three repositories would put a join in every caller.

`paths_containing` is the method that matters. It is the query the policy engine
runs before a filesystem action, and it is written as an exact-match lookup over
the target's ancestors rather than a `LIKE` prefix scan — `idx_project_paths_lookup`
turns it into a handful of index probes, and `DATABASE_DESIGN.md` §6 requires
that it not be a scan.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path

from mitta.errors import NotFoundError, ValidationError
from mitta.ids import PROJECT, prefixed
from mitta.persistence.database import Database
from mitta.projects.boundary import canonicalise
from mitta.projects.models import (
    PathKind,
    Project,
    ProjectDraft,
    ProjectPath,
    ProjectPathDraft,
    ProjectStatus,
    TimelineEvent,
)
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

_PROJECT_COLUMNS = """
    seq, id, name, description, color, status, settings, created_at, updated_at
"""

_PATH_COLUMNS = "seq, project_id, path, kind, writable, created_at"

_EPISODE_COLUMNS = """
    seq, id, occurred_at, event_type, title, detail, project_id, payload
"""


def _json_object(raw: str | None) -> dict[str, object]:
    if not raw:
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        seq=row["seq"],
        id=row["id"],
        name=row["name"],
        description=row["description"],
        color=row["color"],
        status=ProjectStatus(row["status"]),
        settings=_json_object(row["settings"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_path(row: sqlite3.Row) -> ProjectPath:
    return ProjectPath(
        seq=row["seq"],
        project_id=row["project_id"],
        path=row["path"],
        kind=PathKind(row["kind"]),
        writable=bool(row["writable"]),
        created_at=row["created_at"],
    )


def _row_to_event(row: sqlite3.Row) -> TimelineEvent:
    return TimelineEvent(
        seq=row["seq"],
        id=row["id"],
        occurred_at=row["occurred_at"],
        event_type=row["event_type"],
        title=row["title"],
        detail=row["detail"],
        project_id=row["project_id"],
        payload=_json_object(row["payload"]),
    )


class ProjectRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- projects ------------------------------------------------------------- #

    def create(self, draft: ProjectDraft, *, now: int | None = None) -> Project:
        ts = now if now is not None else int(time.time())
        project_id = prefixed(PROJECT)

        with self._db.write() as conn:
            conn.execute(
                """
                INSERT INTO projects
                    (id, name, description, color, status, settings,
                     created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    project_id,
                    draft.name,
                    draft.description,
                    draft.color,
                    ProjectStatus.ACTIVE.value,
                    json.dumps(draft.settings),
                    ts,
                    ts,
                ),
            )
        log.info("project.created", extra={"project_id": project_id})
        return self.get(project_id)

    def get(self, project_id: str) -> Project:
        with self._db.read() as conn:
            row = conn.execute(
                f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE id = ?",  # noqa: S608 - constant columns and literal clauses; values are bound
                (project_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("project", project_id)
        return _row_to_project(row)

    def list_projects(
        self,
        *,
        status: ProjectStatus | None = ProjectStatus.ACTIVE,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Project]:
        clause = ""
        params: list[object] = []
        if status is not None:
            clause = "WHERE status = ? "
            params.append(status.value)
        params.extend((limit, offset))

        sql = (
            f"SELECT {_PROJECT_COLUMNS} FROM projects "  # noqa: S608 - constant columns and literal clauses; values are bound
            f"{clause}"
            # Most recently touched first, matching `idx_projects_status`.
            "ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        )
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_project(row) for row in rows]

    def count(self, *, status: ProjectStatus | None = ProjectStatus.ACTIVE) -> int:
        with self._db.read() as conn:
            if status is None:
                row = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM projects WHERE status = ?", (status.value,)
                ).fetchone()
        return int(row["n"])

    def update(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        color: str | None = None,
        settings: dict[str, object] | None = None,
        now: int | None = None,
    ) -> Project:
        """Patch semantics: `None` means "leave alone", not "set to null".

        The distinction is why this takes keyword arguments rather than a draft.
        Clearing a description is a separate act from not mentioning it, and a
        draft-shaped update cannot tell the two apart.
        """
        ts = now if now is not None else int(time.time())
        self.get(project_id)  # 404 before UPDATE, which would silently no-op

        assignments: list[str] = []
        params: list[object] = []
        for column, value in (
            ("name", name),
            ("description", description),
            ("color", color),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                params.append(value)
        if settings is not None:
            assignments.append("settings = ?")
            params.append(json.dumps(settings))

        if not assignments:
            return self.get(project_id)

        assignments.append("updated_at = ?")
        params.extend((ts, project_id))
        sql = f"UPDATE projects SET {', '.join(assignments)} WHERE id = ?"  # noqa: S608 - column names are literals from the loop above; values are bound
        with self._db.write() as conn:
            conn.execute(sql, params)
        return self.get(project_id)

    def set_status(
        self, project_id: str, status: ProjectStatus, *, now: int | None = None
    ) -> Project:
        ts = now if now is not None else int(time.time())
        with self._db.write() as conn:
            cur = conn.execute(
                "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, ts, project_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("project", project_id)
        return self.get(project_id)

    def delete(self, project_id: str) -> None:
        """Permanent.

        Cascades to paths and resources, and — by the schema's own foreign keys —
        to project-scoped memories and episodes. Conversations and turns are
        `ON DELETE SET NULL`, so a thread survives its project and becomes
        unscoped rather than disappearing. That asymmetry is deliberate in
        `DATABASE_DESIGN.md`: a project memory is meaningless without its
        project (see `ProjectAttributes`), a conversation is not.
        """
        with self._db.write() as conn:
            cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            if cur.rowcount == 0:
                raise NotFoundError("project", project_id)
        log.warning("project.deleted", extra={"project_id": project_id})

    # -- paths (the security boundary) ---------------------------------------- #

    def add_path(
        self, project_id: str, draft: ProjectPathDraft, *, now: int | None = None
    ) -> ProjectPath:
        """Register a filesystem path, canonicalised.

        Canonicalisation happens here rather than at the API edge so that no
        caller can insert an unresolved path — a stored `~/work/../.ssh` would
        make every later prefix check wrong, and the boundary would be unsound
        for as long as the row lived.
        """
        ts = now if now is not None else int(time.time())
        self.get(project_id)

        resolved = canonicalise(draft.path)
        if not resolved.is_absolute():  # pragma: no cover - resolve() always absolutises
            raise ValidationError(f"{draft.path!r} did not resolve to an absolute path")

        with self._db.write() as conn:
            # Re-registering a path updates it, so the UI's "add" button is
            # idempotent and changing a path from read-only to writable does not
            # require deleting a row first.
            conn.execute(
                """
                INSERT INTO project_paths (project_id, path, kind, writable, created_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT (project_id, path) DO UPDATE
                SET kind = excluded.kind, writable = excluded.writable
                """,
                (project_id, str(resolved), draft.kind.value, int(draft.writable), ts),
            )
            conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (ts, project_id))

        log.info(
            "project.path_registered",
            extra={
                "project_id": project_id,
                "path": str(resolved),
                "kind": draft.kind.value,
                "writable": draft.writable,
            },
        )
        return self.get_path(project_id, resolved)

    def get_path(self, project_id: str, path: str | Path) -> ProjectPath:
        resolved = canonicalise(path)
        with self._db.read() as conn:
            row = conn.execute(
                f"SELECT {_PATH_COLUMNS} FROM project_paths WHERE project_id = ? AND path = ?",  # noqa: S608 - constant columns and literal clauses; values are bound
                (project_id, str(resolved)),
            ).fetchone()
        if row is None:
            raise NotFoundError("project_path", str(resolved))
        return _row_to_path(row)

    def path_counts(self) -> dict[str, int]:
        """How many paths each project has registered.

        One grouped query for the whole list view. The alternative — a count per
        row — is the N+1 the denormalised `message_count` on conversations exists
        to avoid, and there is no reason to reintroduce it here.
        """
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT project_id, COUNT(*) AS n FROM project_paths GROUP BY project_id"
            ).fetchall()
        return {row["project_id"]: int(row["n"]) for row in rows}

    def paths(self, project_id: str) -> list[ProjectPath]:
        with self._db.read() as conn:
            rows = conn.execute(
                f"SELECT {_PATH_COLUMNS} FROM project_paths WHERE project_id = ? ORDER BY path",  # noqa: S608 - constant columns and literal clauses; values are bound
                (project_id,),
            ).fetchall()
        return [_row_to_path(row) for row in rows]

    def remove_path(self, project_id: str, path: str | Path, *, now: int | None = None) -> None:
        ts = now if now is not None else int(time.time())
        resolved = canonicalise(path)
        with self._db.write() as conn:
            cur = conn.execute(
                "DELETE FROM project_paths WHERE project_id = ? AND path = ?",
                (project_id, str(resolved)),
            )
            if cur.rowcount == 0:
                raise NotFoundError("project_path", str(resolved))
            conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (ts, project_id))
        log.warning(
            "project.path_removed",
            extra={"project_id": project_id, "path": str(resolved)},
        )

    def paths_containing(self, target: Path) -> Sequence[ProjectPath]:
        """Every registered path that contains `target`, including itself.

        Implements `PathLookup`. The candidate set is the target and its
        ancestors — a path can only be contained by one of those — so this is an
        `IN` over at most a few dozen exact values and uses
        `idx_project_paths_lookup`. A `LIKE 'prefix%'` formulation would scan,
        and would also reintroduce the character-comparison bug that
        `boundary._is_within` exists to avoid: `/home/satya-backup` matches
        `/home/satya%`.

        Only `active` projects are consulted. Archiving a project withdraws its
        write grants, which is the behaviour a user archiving something expects;
        the rows are kept so unarchiving restores them.
        """
        resolved = canonicalise(target)
        candidates = [str(resolved), *(str(parent) for parent in resolved.parents)]
        placeholders = ",".join("?" * len(candidates))
        sql = (
            "SELECT p.seq, p.project_id, p.path, p.kind, p.writable, p.created_at "  # noqa: S608 - constant columns; every value is bound
            "FROM project_paths p "
            "JOIN projects pr ON pr.id = p.project_id "
            f"WHERE pr.status = ? AND p.path IN ({placeholders})"
        )
        with self._db.read() as conn:
            rows = conn.execute(sql, (ProjectStatus.ACTIVE.value, *candidates)).fetchall()
        return [_row_to_path(row) for row in rows]

    # -- timeline ------------------------------------------------------------- #

    def timeline(self, project_id: str, *, limit: int = 100) -> list[TimelineEvent]:
        """Episodic events for this project, most recent first.

        Served by `idx_episodes_project`. Read-only: episodes are written by
        whatever observed the event, and nothing in this phase writes one — the
        endpoint exists so the surface is not lying about having a timeline it
        cannot query, and it returns an empty list until an event producer lands.
        """
        with self._db.read() as conn:
            rows = conn.execute(
                f"SELECT {_EPISODE_COLUMNS} FROM episodes "  # noqa: S608 - constant columns and literal clauses; values are bound
                "WHERE project_id = ? ORDER BY occurred_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [_row_to_event(row) for row in rows]
