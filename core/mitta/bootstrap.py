"""Composition root.

The **only** place collaborators are wired together. Every other module receives
what it needs through its constructor and never reaches out for a global.

This is not ceremony. Two guarantees in the architecture are enforced by
construction rather than by convention, and both live here:

* The Tool Manager is built without a reference to the OS Adapter — the policy
  engine holds it (ARCHITECTURE.md §3). Bypassing policy therefore requires
  editing this file, which is reviewed, and the import contract, which is
  checked (DEC-029).
* Nothing above the OS Adapter ever learns a platform-specific path, because
  paths are resolved once, here, from the adapter.

Landed so far: config, telemetry, OS adapter, persistence, API (Phase 3), the
memory engine (Phase 5), the LLM gateway and agent (Phase 7), the permission
model (Phase 8), the planner (Phase 9), personality (Phase 12) and projects
(Phase 10).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from mitta.agent.extraction import MemoryExtractor
from mitta.agent.orchestrator import Orchestrator
from mitta.api.app import create_app
from mitta.config.paths import Paths, resolve_paths
from mitta.config.settings import Settings, load_settings
from mitta.conversations.repository import ConversationRepository
from mitta.llm import keys
from mitta.llm.gateway import LLMGateway
from mitta.llm.providers.groq import GroqProvider
from mitta.llm.providers.openrouter import OpenRouterProvider
from mitta.memory.embedding.base import EmbeddingProvider
from mitta.memory.embedding.deterministic import DeterministicEmbedder
from mitta.memory.embedding.local import LocalEmbedder
from mitta.memory.indexer import Indexer
from mitta.memory.repository import MemoryRepository
from mitta.memory.service import MemoryService
from mitta.memory.vectors.store import VectorStore, build_index
from mitta.os_adapter.base import OSAdapter
from mitta.os_adapter.factory import create_os_adapter
from mitta.persistence.database import Database
from mitta.persistence.migrations import migrate
from mitta.personality.rewriter import PersonalityLayer
from mitta.policy.approval import ApprovalAuthority
from mitta.policy.audit import AuditLog
from mitta.policy.broker import ApprovalBroker
from mitta.policy.engine import PolicyEngine
from mitta.policy.executor import ToolExecutor
from mitta.projects.boundary import PathBoundary
from mitta.projects.repository import ProjectRepository
from mitta.telemetry.logging import get_logger, setup_logging
from mitta.telemetry.redaction import SecretRedactor
from mitta.tools.builtin.open_app import OpenAppTool
from mitta.tools.builtin.open_url import OpenUrlTool
from mitta.tools.builtin.web_search import WebSearchTool
from mitta.tools.builtin.write_note import WriteNoteTool
from mitta.tools.registry import ToolRegistry

log = get_logger(__name__)


@dataclass(slots=True)
class Runtime:
    """Everything the process owns. Constructed once, disposed once."""

    settings: Settings
    paths: Paths
    os_adapter: OSAdapter
    database: Database
    redactor: SecretRedactor
    memory: MemoryService
    indexer: Indexer
    gateway: LLMGateway
    audit: AuditLog
    conversations: ConversationRepository
    projects: ProjectRepository
    orchestrator: Orchestrator
    app: FastAPI

    def shutdown(self) -> None:
        # Indexer first. It writes to the database, so stopping it after closing
        # the connection would surface as a spurious "database is not connected"
        # on the way down.
        self.indexer.stop()
        self.database.close()


def build_runtime(
    *,
    config_file: Path | None = None,
    platform: str | None = None,
    **overrides: Any,
) -> Runtime:
    """Construct the full runtime.

    Order matters and is not arbitrary:

    1. OS adapter — nothing can resolve a path before it exists.
    2. Settings, then paths, then directories.
    3. Redactor, seeded with the session token, then logging. Logging is
       configured **before** anything else can log, so no line is ever emitted
       through an unredacted handler.
    4. Database, then migrations.
    5. The FastAPI app last, over collaborators that are already live.
    """
    os_adapter = create_os_adapter(platform)

    bootstrap_settings = load_settings(**overrides)
    bootstrap_paths = resolve_paths(bootstrap_settings, os_adapter)

    # The config file lives under the storage root, which the settings choose —
    # so settings are loaded twice: once to find the file, once including it.
    settings = load_settings(config_file or bootstrap_paths.config_file, **overrides)
    paths = resolve_paths(settings, os_adapter)
    paths.ensure()

    redactor = SecretRedactor()
    redactor.register(settings.session_token)
    setup_logging(settings.logging, paths.log_dir, redactor)

    log.info(
        "runtime.starting",
        extra={
            "platform": os_adapter.platform_name,
            "storage_root": str(paths.storage_root),
            "dev_mode": settings.dev_mode,
        },
    )

    database = Database(paths.database, settings.database)
    database.connect()
    migrate(
        database,
        backup_dir=paths.backups if settings.database.backup_before_migration else None,
    )

    # Before providers are constructed, and after logging is up so the redactor
    # is already guarding every handler.
    env_file = keys.default_env_file()
    if env_file is not None:
        keys.apply_env_file(env_file)

    gateway = _build_gateway(redactor)

    conversations = ConversationRepository(database)
    # A turn still marked `running` is one the process died during. Reconciled
    # at startup so the UI never shows a thinking indicator for work that no
    # process is doing.
    conversations.reconcile_orphaned_turns()

    embedder = _select_embedder(paths)
    repository = MemoryRepository(database)
    store = VectorStore(database, build_index(paths.vectors / "memories.faiss", embedder), embedder)
    index_status = store.open()
    indexer = Indexer(repository, store)
    memory = MemoryService(repository, store, indexer, settings=settings.memory)

    log.info(
        "memory.ready",
        extra={
            "model_id": index_status.model_id,
            "vectors": index_status.vector_count,
            "pending": memory.pending_count(),
        },
    )

    extractor = MemoryExtractor(memory, gateway, redactor=redactor)
    personality = PersonalityLayer(
        gateway,
        enabled=settings.personality.enabled,
        intensity=settings.personality.intensity,
    )
    audit = AuditLog(database)
    approvals = ApprovalAuthority(database)
    projects = ProjectRepository(database)
    # The engine receives the boundary, not the repository. It needs to resolve a
    # path; it has no business creating a project or granting one write access,
    # and the narrowest reference that does the job is the one that cannot be
    # misused later — the same reasoning that keeps the OS Adapter out of the
    # Tool Manager.
    boundary = PathBoundary(projects)
    policy = PolicyEngine(audit, approvals, boundary=boundary)

    # The registry is filled here and nowhere else. A capability that can act on
    # the user's machine should appear in the composition root, where it can be
    # read, rather than arriving because a module happened to be importable.
    registry = ToolRegistry()
    registry.register(WebSearchTool())
    # The opener is injected rather than imported: `mitta.tools` may not reach
    # `mitta.os_adapter`, and that contract is what keeps platform access behind
    # the policy engine (DEC-079).
    registry.register(OpenAppTool(os_adapter.open_application))
    registry.register(OpenUrlTool(os_adapter.open_url))
    registry.register(WriteNoteTool(paths.storage_root / "notes"))
    tools = ToolExecutor(registry, policy, database)
    broker = ApprovalBroker()

    orchestrator = Orchestrator(
        conversations,
        memory,
        gateway,
        extractor=extractor,
        personality=personality,
        tools=tools,
        broker=broker,
        policy=policy,
    )

    app = create_app(
        settings=settings,
        paths=paths,
        database=database,
        os_adapter=os_adapter,
        memory=memory,
        indexer=indexer,
        embedder=embedder,
        gateway=gateway,
        conversations=conversations,
        projects=projects,
        path_boundary=boundary,
        orchestrator=orchestrator,
        approval_broker=broker,
        policy=policy,
        tool_registry=registry,
        audit=audit,
    )

    return Runtime(
        settings=settings,
        paths=paths,
        os_adapter=os_adapter,
        database=database,
        redactor=redactor,
        memory=memory,
        indexer=indexer,
        gateway=gateway,
        audit=audit,
        conversations=conversations,
        projects=projects,
        orchestrator=orchestrator,
        app=app,
    )


def _build_gateway(redactor: SecretRedactor) -> LLMGateway:
    """Construct the provider chain. Order is preference order (R3).

    Every key found is registered with the redactor **before** any provider can
    use it, so a key cannot reach a log line even via an exception message from
    inside httpx.
    """
    resolved = {provider: keys.resolve(provider) for provider in keys.KEY_VARS}
    for value in resolved.values():
        if value is not None:
            redactor.register(value)

    configured = sorted(name for name, value in resolved.items() if value is not None)
    if configured:
        log.info("llm.providers_configured", extra={"providers": configured})
    else:
        # Not an error. The application runs without reasoning — memory, search
        # and the UI all work — and says so rather than failing to start (R8).
        log.warning(
            "llm.no_provider_configured",
            extra={"detail": "reasoning is unavailable until an API key is added"},
        )

    return LLMGateway(
        [
            GroqProvider(resolved.get("groq")),
            OpenRouterProvider(resolved.get("openrouter")),
        ]
    )


def _select_embedder(paths: Paths) -> EmbeddingProvider:
    """Pick the best embedding provider available *without* a network call.

    The real model is used when its weights are already on disk. When they are
    not, the deterministic provider takes over rather than the engine refusing
    to index — a first run that silently drops every memory's vector, with a
    backfill nobody knows to wait for, is a far worse failure than degraded
    recall that repairs itself.

    Nothing is stranded by that choice: the two providers report different
    `model_id`s, so every vector written by the fallback is automatically stale
    the moment the real model appears, and the indexer re-embeds without being
    told (DEC-050).
    """
    local = LocalEmbedder(paths.models)
    if local.is_available():
        return local

    log.warning(
        "memory.embedding_model_absent",
        extra={
            "model_id": local.descriptor.id,
            "fallback": "deterministic",
            "detail": "semantic recall is degraded until the model is downloaded",
        },
    )
    return DeterministicEmbedder()
