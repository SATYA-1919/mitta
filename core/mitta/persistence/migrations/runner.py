"""Forward-only migration runner (DEC-031).

Numbered, checksummed, applied in a single transaction, with a backup taken
first. No down-migrations: a rollback path that is never exercised is a rollback
path that does not work, and for a single-user local database the honest
recovery story is the pre-migration backup.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from mitta.errors import MigrationError
from mitta.persistence.database import Database
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

SQL_DIR = Path(__file__).parent / "sql"
_FILENAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    checksum   TEXT    NOT NULL,
    applied_at INTEGER NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str

    @classmethod
    def from_path(cls, path: Path) -> Migration:
        match = _FILENAME.match(path.name)
        if match is None:
            raise MigrationError(
                f"Migration filename must be NNNN_snake_case.sql: {path.name}",
                details={"path": str(path)},
            )
        sql = path.read_text(encoding="utf-8")
        return cls(
            version=int(match.group("version")),
            name=match.group("name"),
            path=path,
            sql=sql,
            checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        )


def split_statements(script: str) -> list[str]:
    """Split a SQL script into individual statements.

    `Connection.executescript` cannot be used here: it issues an implicit COMMIT
    before running, which would end the transaction the runner opened and leave
    the DDL and its `schema_migrations` row in *separate* transactions. A crash
    between them yields a schema that is applied but unrecorded — the next boot
    then fails with "table already exists" and needs manual repair.

    Splitting on `;` naively would break `CREATE TRIGGER … BEGIN … END;` bodies,
    which contain their own semicolons. `sqlite3.complete_statement` implements
    SQLite's own rule and handles trigger bodies correctly.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("--")):
            continue  # leading comment or blank line between statements
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise MigrationError(
            "Migration ends with an incomplete SQL statement",
            details={"fragment": buffer.strip()[:120]},
        )
    return statements


def discover(directory: Path = SQL_DIR) -> list[Migration]:
    """Load every migration, ordered by version, rejecting duplicates."""
    if not directory.exists():
        raise MigrationError(f"Migration directory not found: {directory}")

    migrations = sorted(
        (Migration.from_path(p) for p in directory.glob("*.sql")),
        key=lambda m: m.version,
    )
    seen: dict[int, str] = {}
    for migration in migrations:
        if migration.version in seen:
            raise MigrationError(
                f"Duplicate migration version {migration.version:04d}: "
                f"{seen[migration.version]} and {migration.name}",
                details={"version": migration.version},
            )
        seen[migration.version] = migration.name
    return migrations


def applied_versions(conn: sqlite3.Connection) -> dict[int, str]:
    """Map of applied version → recorded checksum."""
    conn.execute(_BOOTSTRAP)
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {int(row["version"]): str(row["checksum"]) for row in rows}


def _verify_checksums(applied: dict[int, str], migrations: list[Migration]) -> None:
    """Catch a migration file edited after it was already applied.

    The failure this prevents: two machines both claim schema version 7 and have
    different schemas. Silent, and extremely unpleasant to diagnose later.
    """
    by_version = {m.version: m for m in migrations}
    for version, recorded in applied.items():
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(
                f"Database has migration {version:04d} applied, but no such file exists. "
                "The database is newer than this build.",
                details={"version": version},
            )
        if migration.checksum != recorded:
            raise MigrationError(
                f"Migration {version:04d}_{migration.name} was modified after it was applied. "
                "Applied migrations are immutable; add a new migration instead.",
                details={
                    "version": version,
                    "expected": recorded,
                    "actual": migration.checksum,
                },
            )


def migrate(
    database: Database,
    *,
    directory: Path = SQL_DIR,
    backup_dir: Path | None = None,
) -> list[Migration]:
    """Apply every pending migration. Returns those applied, in order."""
    migrations = discover(directory)

    with database.read() as conn:
        # query_only blocks the bootstrap CREATE TABLE, so probe instead.
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        applied = (
            {
                int(r["version"]): str(r["checksum"])
                for r in conn.execute("SELECT version, checksum FROM schema_migrations")
            }
            if row is not None
            else {}
        )

    _verify_checksums(applied, migrations)

    pending = [m for m in migrations if m.version not in applied]
    if not pending:
        log.debug("migrations.up_to_date", extra={"version": max(applied, default=0)})
        return []

    if backup_dir is not None and applied:
        # Only back up an existing schema. A fresh database has nothing to lose,
        # and writing an empty backup on first launch is noise.
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        database.backup_to(backup_dir / f"mitta-pre-{max(applied):04d}-{stamp}.db")

    for migration in pending:
        log.info(
            "migrations.applying",
            extra={"version": migration.version, "name": migration.name},
        )
        try:
            # One transaction covering the DDL *and* its bookkeeping row, so the
            # schema and the recorded version can never disagree.
            with database.write() as conn:
                conn.execute(_BOOTSTRAP)
                for statement in split_statements(migration.sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        int(time.time() * 1000),
                    ),
                )
        except sqlite3.Error as exc:
            raise MigrationError(
                f"Migration {migration.version:04d}_{migration.name} failed: {exc}",
                details={"version": migration.version, "name": migration.name},
                cause=exc,
            ) from exc

    log.info(
        "migrations.complete",
        extra={"applied": len(pending), "version": pending[-1].version},
    )
    return pending


def current_version(database: Database) -> int:
    with database.read() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if row is None:
            return 0
        result = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    return int(result["v"]) if result and result["v"] is not None else 0
