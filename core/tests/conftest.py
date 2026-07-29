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

from mitta.agent.orchestrator import Orchestrator
from mitta.api.app import create_app
from mitta.config.paths import Paths
from mitta.config.settings import DatabaseSettings, Settings
from mitta.conversations.repository import ConversationRepository
from mitta.llm.gateway import LLMGateway
from mitta.llm.providers.groq import GroqProvider
from mitta.llm.providers.openrouter import OpenRouterProvider
from mitta.memory.embedding.deterministic import DeterministicEmbedder
from mitta.memory.indexer import Indexer
from mitta.memory.repository import MemoryRepository
from mitta.memory.service import MemoryService
from mitta.memory.vectors.store import VectorStore, build_index
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
def client(
    settings: Settings,
    paths: Paths,
    migrated: Database,
    memory_service: MemoryService,
    indexer: Indexer,
    embedder: DeterministicEmbedder,
    gateway: LLMGateway,
    conversations: ConversationRepository,
    orchestrator: Orchestrator,
) -> Iterator[TestClient]:
    app = create_app(
        settings=settings,
        paths=paths,
        database=migrated,
        os_adapter=MacAdapter(),
        memory=memory_service,
        # No background thread. The indexer is driven explicitly from tests so
        # assertions about what is indexed are deterministic rather than a race
        # with a worker that may or may not have run yet. `test_api.py` covers
        # the lifespan actually starting it.
        indexer=None,
        embedder=embedder,
        gateway=gateway,
        conversations=conversations,
        orchestrator=orchestrator,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def bare_client(settings: Settings, paths: Paths, migrated: Database) -> Iterator[TestClient]:
    """An app built without a memory engine.

    Exists to prove the router is genuinely optional rather than merely
    defaulted — an endpoint that 500s on a missing dependency is worse than one
    that is honestly absent.
    """
    app = create_app(settings=settings, paths=paths, database=migrated, os_adapter=MacAdapter())
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


# ── Memory engine ──────────────────────────────────────────────────────────
#
# Built on `DeterministicEmbedder`, never the real model. Loading ONNX would add
# tens of seconds to every run and make the suite depend on a 67 MB download —
# and the double is a real provider, so what these tests exercise is the actual
# code path, just with a simpler function producing the vectors.


@pytest.fixture
def embedder() -> DeterministicEmbedder:
    return DeterministicEmbedder()


@pytest.fixture
def repository(migrated: Database) -> MemoryRepository:
    return MemoryRepository(migrated)


@pytest.fixture
def vector_store(migrated: Database, paths: Paths, embedder: DeterministicEmbedder) -> VectorStore:
    index = build_index(paths.vectors / "memories.faiss", embedder)
    store = VectorStore(migrated, index, embedder)
    store.open()
    return store


@pytest.fixture
def indexer(repository: MemoryRepository, vector_store: VectorStore) -> Indexer:
    # Never started as a thread in tests: `run_once`/`drain` are called
    # explicitly so indexing is deterministic rather than a race with the clock.
    return Indexer(repository, vector_store)


@pytest.fixture
def memory_service(
    repository: MemoryRepository, vector_store: VectorStore, indexer: Indexer
) -> MemoryService:
    return MemoryService(repository, vector_store, indexer)


@pytest.fixture
def gateway() -> LLMGateway:
    """A gateway with no keys.

    Deliberately unconfigured: the suite must never make a real provider call,
    and an accidental one would be a network request with someone's credential
    on it. Failover behaviour is covered against fakes in `test_llm.py`.
    """
    return LLMGateway([GroqProvider(None), OpenRouterProvider(None)])


@pytest.fixture
def conversations(migrated: Database) -> ConversationRepository:
    return ConversationRepository(migrated)


@pytest.fixture
def orchestrator(
    conversations: ConversationRepository,
    memory_service: MemoryService,
    gateway: LLMGateway,
) -> Orchestrator:
    """Built on the keyless gateway, so a turn fails at the provider rather than
    making a real call. Streaming behaviour is covered against fakes."""
    return Orchestrator(conversations, memory_service, gateway)
