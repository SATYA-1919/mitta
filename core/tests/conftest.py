"""Shared fixtures.

Every fixture is fully isolated to a tmp_path. No test may touch the real
storage root — a test that wrote to `~/Library/Application Support/MITTA` would
be modifying the developer's own memory database.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mitta.api.app import create_app
from mitta.config.paths import Paths
from mitta.config.settings import DatabaseSettings, Settings
from mitta.os_adapter.mac import MacAdapter
from mitta.persistence.database import Database
from mitta.persistence.migrations import migrate

TEST_TOKEN = "test-session-token-0123456789abcdef"


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Stop `setup_logging` in one test leaking handlers into the next."""
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    resolved = Paths(
        storage_root=tmp_path / "storage",
        runtime_dir=tmp_path / "runtime",
        log_dir=tmp_path / "logs",
    )
    resolved.ensure()
    return resolved


@pytest.fixture
def db_settings() -> DatabaseSettings:
    return DatabaseSettings(read_pool_size=2, backup_before_migration=False)


@pytest.fixture
def database(paths: Paths, db_settings: DatabaseSettings) -> Iterator[Database]:
    db = Database(paths.database, db_settings)
    db.connect()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def migrated(database: Database) -> Database:
    migrate(database)
    return database


@pytest.fixture
def settings(paths: Paths, db_settings: DatabaseSettings) -> Settings:
    return Settings(
        storage_root=paths.storage_root,
        runtime_dir=paths.runtime_dir,
        log_dir=paths.log_dir,
        session_token=TEST_TOKEN,
        database=db_settings,
    )


@pytest.fixture
def client(settings: Settings, paths: Paths, migrated: Database) -> Iterator[TestClient]:
    app = create_app(
        settings=settings,
        paths=paths,
        database=migrated,
        os_adapter=MacAdapter(),
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}
