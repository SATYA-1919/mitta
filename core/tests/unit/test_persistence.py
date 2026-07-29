"""Database and migration behaviour."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mitta.config.settings import DatabaseSettings
from mitta.errors import MigrationError, StorageError
from mitta.persistence.database import Database
from mitta.persistence.migrations import current_version, discover, migrate
from mitta.persistence.migrations.runner import SQL_DIR

# -- connection configuration ---------------------------------------------- #


def test_wal_and_foreign_keys_are_enabled(database: Database) -> None:
    """Foreign keys are OFF by default in SQLite — a classic silent-orphan source."""
    with database.read() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_database_file_is_user_only(database: Database) -> None:
    assert database.path.stat().st_mode & 0o077 == 0


def test_read_connections_reject_writes(migrated: Database) -> None:
    with migrated.read() as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES ('k','1',0)")


def test_access_after_close_raises(paths, db_settings: DatabaseSettings) -> None:  # type: ignore[no-untyped-def]
    db = Database(paths.database, db_settings)
    db.connect()
    db.close()
    with pytest.raises(StorageError), db.read():
        pass


# -- transactions ----------------------------------------------------------- #


def test_write_commits_on_success(migrated: Database) -> None:
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('theme', '\"dark\"', 1)"
        )
    with migrated.read() as conn:
        assert conn.execute("SELECT value FROM app_settings WHERE key='theme'").fetchone()[0] == (
            '"dark"'
        )


def test_write_rolls_back_on_error(migrated: Database) -> None:
    with pytest.raises(RuntimeError), migrated.write() as conn:
        conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES ('a','1',1)")
        raise RuntimeError("boom")
    with migrated.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0] == 0


def test_nested_write_joins_the_outer_transaction(migrated: Database) -> None:
    """A repository method must work standalone or inside a larger unit of work."""
    with pytest.raises(RuntimeError), migrated.write() as outer:
        outer.execute("INSERT INTO app_settings (key, value, updated_at) VALUES ('a','1',1)")
        with migrated.write() as inner:
            inner.execute("INSERT INTO app_settings (key, value, updated_at) VALUES ('b','2',1)")
        raise RuntimeError("outer fails after inner 'commits'")

    with migrated.read() as conn:
        # Both rows must be gone: the inner block never committed independently.
        assert conn.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0] == 0


def test_read_inside_a_write_sees_uncommitted_rows(migrated: Database) -> None:
    """Read-your-own-writes must hold inside an open transaction.

    A pooled reader is on a different connection and, under WAL, sees only the
    last committed snapshot — so a repository method that inserts and then reads
    back through `read()` would find nothing. Harmless standalone, fatal the
    moment two such methods compose, which is precisely what reentrant `write()`
    exists to allow.
    """
    with migrated.write() as conn:
        conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES ('k','\"v\"',1)")

        with migrated.read() as reader:
            row = reader.execute("SELECT value FROM app_settings WHERE key='k'").fetchone()
            assert row[0] == '"v"'


def test_reads_return_to_the_pool_after_the_transaction(migrated: Database) -> None:
    """The write-connection redirect must not outlive the transaction."""
    with migrated.write() as conn:
        conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES ('k','\"v\"',1)")

    with migrated.read() as reader:
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1


# -- schema ----------------------------------------------------------------- #


def test_migration_creates_expected_tables(migrated: Database) -> None:
    with migrated.read() as conn:
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    for expected in (
        "memories",
        "memory_embeddings",
        "memory_links",
        "people",
        "episodes",
        "preferences",
        "conversations",
        "messages",
        "turns",
        "projects",
        "project_paths",
        "plans",
        "tasks",
        "task_dependencies",
        "task_checkpoints",
        "approval_tokens",
        "tool_invocations",
        "permission_rules",
        "audit_log",
        "llm_requests",
        "plugins",
        "notifications",
        "app_settings",
        "schema_migrations",
    ):
        assert expected in names, f"missing table: {expected}"


def test_foreign_keys_are_enforced(migrated: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError), migrated.write() as conn:
        conn.execute(
            "INSERT INTO turns (id, conversation_id, input_kind, started_at) "
            "VALUES ('trn_x', 'cnv_missing', 'text', 0)"
        )


def test_check_constraints_reject_invalid_enums(migrated: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError), migrated.write() as conn:
        conn.execute(
            "INSERT INTO memories "
            "(id, kind, content, source_kind, content_hash, created_at, updated_at) "
            "VALUES ('mem_x', 'not_a_kind', 'x', 'user', 'h', 0, 0)"
        )


def test_invalid_json_is_rejected(migrated: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError), migrated.write() as conn:
        conn.execute(
            "INSERT INTO memories "
            "(id, kind, content, attributes, source_kind, content_hash, created_at, updated_at) "
            "VALUES ('mem_y', 'long_term', 'x', 'NOT JSON', 'user', 'h', 0, 0)"
        )


def test_seq_is_never_reused_after_delete(migrated: Database) -> None:
    """DEC-025: a reused rowid would silently remap a stale FAISS vector."""
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO memories "
            "(id, kind, content, source_kind, content_hash, created_at, updated_at) "
            "VALUES ('mem_1', 'long_term', 'first', 'user', 'h1', 0, 0)"
        )
        first = conn.execute("SELECT seq FROM memories WHERE id='mem_1'").fetchone()[0]
        conn.execute("DELETE FROM memories WHERE id='mem_1'")
        conn.execute(
            "INSERT INTO memories "
            "(id, kind, content, source_kind, content_hash, created_at, updated_at) "
            "VALUES ('mem_2', 'long_term', 'second', 'user', 'h2', 0, 0)"
        )
        second = conn.execute("SELECT seq FROM memories WHERE id='mem_2'").fetchone()[0]
    assert second > first


def test_fts_index_tracks_memories(migrated: Database) -> None:
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO memories "
            "(id, kind, content, source_kind, content_hash, created_at, updated_at) "
            "VALUES ('mem_f', 'long_term', 'the auth flow uses PKCE', 'user', 'h', 0, 0)"
        )
    with migrated.read() as conn:
        hits = conn.execute("SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'PKCE'")
        assert len(hits.fetchall()) == 1

    with migrated.write() as conn:
        conn.execute("UPDATE memories SET content='now about OAuth' WHERE id='mem_f'")
    with migrated.read() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH 'PKCE'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH 'OAuth'"
            ).fetchone()[0]
            == 1
        )


def test_index_health_view_reports_pending_embeddings(migrated: Database) -> None:
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO memories "
            "(id, kind, content, source_kind, content_hash, created_at, updated_at) "
            "VALUES ('mem_p', 'long_term', 'x', 'user', 'hash-a', 0, 0)"
        )
    with migrated.read() as conn:
        row = conn.execute("SELECT * FROM v_index_health").fetchone()
    assert row["active_memories"] == 1
    assert row["pending"] == 1


def test_stale_embedding_is_detected_by_hash(migrated: Database) -> None:
    """The self-healing property from DATABASE_DESIGN.md §4.3."""
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO memories "
            "(id, kind, content, source_kind, content_hash, created_at, updated_at) "
            "VALUES ('mem_s', 'long_term', 'x', 'user', 'hash-a', 0, 0)"
        )
        seq = conn.execute("SELECT seq FROM memories WHERE id='mem_s'").fetchone()[0]
        conn.execute(
            "INSERT INTO memory_embeddings "
            "(memory_seq, index_name, model_id, dim, content_hash, indexed_at) "
            "VALUES (?, 'default', 'bge-small-en-v1.5', 384, 'hash-a', 0)",
            (seq,),
        )
    with migrated.read() as conn:
        assert conn.execute("SELECT pending FROM v_index_health").fetchone()[0] == 0

    with migrated.write() as conn:
        conn.execute("UPDATE memories SET content_hash='hash-b' WHERE id='mem_s'")
    with migrated.read() as conn:
        assert conn.execute("SELECT pending FROM v_index_health").fetchone()[0] == 1


# -- migration runner -------------------------------------------------------- #


def test_migrations_are_idempotent(migrated: Database) -> None:
    assert migrate(migrated) == []


def test_current_version_reports_latest(migrated: Database) -> None:
    assert current_version(migrated) == max(m.version for m in discover())


def test_current_version_is_zero_before_migration(database: Database) -> None:
    assert current_version(database) == 0


def test_modified_migration_is_rejected(migrated: Database, tmp_path: Path) -> None:
    """Catches the "two machines at version 7 with different schemas" failure."""
    tampered = tmp_path / "sql"
    tampered.mkdir()
    for source in SQL_DIR.glob("*.sql"):
        (tampered / source.name).write_text(
            source.read_text(encoding="utf-8") + "\n-- edited after apply\n",
            encoding="utf-8",
        )
    with pytest.raises(MigrationError, match="modified after it was applied"):
        migrate(migrated, directory=tampered)


def test_duplicate_version_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "0001_one.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0001_two.sql").write_text("SELECT 2;", encoding="utf-8")
    with pytest.raises(MigrationError, match="Duplicate migration version"):
        discover(tmp_path)


def test_bad_filename_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "initial.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="NNNN_snake_case"):
        discover(tmp_path)


def test_database_newer_than_build_is_rejected(migrated: Database, tmp_path: Path) -> None:
    empty = tmp_path / "sql"
    empty.mkdir()
    with pytest.raises(MigrationError, match="newer than this build"):
        migrate(migrated, directory=empty)


def test_backup_is_a_valid_database(migrated: Database, tmp_path: Path) -> None:
    backup = migrated.backup_to(tmp_path / "backup.db")
    assert backup.stat().st_mode & 0o077 == 0
    conn = sqlite3.connect(backup)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "memories" in names


def test_integrity_check_passes(migrated: Database) -> None:
    assert migrated.integrity_check() is True
