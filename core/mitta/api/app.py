"""FastAPI application factory.

The factory takes fully-constructed collaborators rather than building them.
Wiring happens exactly once, in `bootstrap.py` — this module knows how to expose
a runtime over HTTP and nothing about how to assemble one. That is what makes
the app testable without a real storage root.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mitta.agent.orchestrator import Orchestrator
from mitta.api.auth import TokenVerifier
from mitta.api.exception_handlers import register_exception_handlers
from mitta.api.http import (
    audit_router,
    conversations_router,
    memory_router,
    projects_router,
    providers_router,
    system_router,
)
from mitta.api.middleware import RequestContextMiddleware
from mitta.api.ws import router as ws_router
from mitta.config.paths import Paths
from mitta.config.settings import Settings
from mitta.conversations.repository import ConversationRepository
from mitta.llm.gateway import LLMGateway
from mitta.memory.embedding.base import EmbeddingProvider
from mitta.memory.indexer import Indexer
from mitta.memory.service import MemoryService
from mitta.os_adapter.base import OSAdapter
from mitta.persistence.database import Database
from mitta.policy.audit import AuditLog
from mitta.policy.broker import ApprovalBroker
from mitta.policy.engine import PolicyEngine
from mitta.projects.boundary import PathBoundary
from mitta.projects.repository import ProjectRepository
from mitta.telemetry.logging import get_logger
from mitta.tools.registry import ToolRegistry

API_VERSION = "1"

log = get_logger(__name__)


def create_app(
    *,
    settings: Settings,
    paths: Paths,
    database: Database,
    os_adapter: OSAdapter,
    memory: MemoryService | None = None,
    indexer: Indexer | None = None,
    embedder: EmbeddingProvider | None = None,
    gateway: LLMGateway | None = None,
    conversations: ConversationRepository | None = None,
    projects: ProjectRepository | None = None,
    path_boundary: PathBoundary | None = None,
    orchestrator: Orchestrator | None = None,
    approval_broker: ApprovalBroker | None = None,
    policy: PolicyEngine | None = None,
    tool_registry: ToolRegistry | None = None,
    audit: AuditLog | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "api.startup",
            extra={"api_version": API_VERSION, "platform": os_adapter.platform_name},
        )
        # The indexer runs for as long as the app serves requests, and not one
        # moment longer — tying it to the lifespan means a shutdown cannot leave
        # a thread writing to a database the process is closing.
        if indexer is not None:
            indexer.start()
        try:
            yield
        finally:
            if indexer is not None:
                indexer.stop()
            # Release any turn waiting on a human, or the loop stays alive
            # past the point the process was asked to stop.
            if approval_broker is not None:
                approval_broker.cancel_all("MITTA is shutting down")
            log.info("api.shutdown")

    app = FastAPI(
        title="MITTA Core",
        version=API_VERSION,
        lifespan=lifespan,
        # Interactive docs render request bodies, which for this API means
        # conversation content and retrieved memories. Off unless dev_mode.
        docs_url="/docs" if settings.dev_mode else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.dev_mode else None,
    )

    app.state.settings = settings
    app.state.paths = paths
    app.state.database = database
    app.state.os_adapter = os_adapter
    app.state.memory = memory
    app.state.indexer = indexer
    app.state.embedder = embedder
    app.state.gateway = gateway
    app.state.conversations = conversations
    app.state.projects = projects
    app.state.path_boundary = path_boundary
    app.state.orchestrator = orchestrator
    app.state.approval_broker = approval_broker
    app.state.policy = policy
    app.state.tool_registry = tool_registry
    app.state.audit = audit
    app.state.api_version = API_VERSION
    app.state.started_at = time.monotonic()
    app.state.token_verifier = TokenVerifier(
        settings.session_token,
        settings.allowed_origins,
        dev_mode=settings.dev_mode,
    )

    if not app.state.token_verifier.enabled:
        log.warning(
            "api.auth_disabled",
            extra={"reason": "no MITTA_SESSION_TOKEN in environment"},
        )

    # CORS, restricted to an explicit allowlist.
    #
    # An earlier version of this file asserted that no CORS was needed because
    # the Tauri webview is "same-origin through the shell". That is false, and
    # running the shell proved it: Tauri serves the frontend from
    # `tauri://localhost` (and `http://127.0.0.1:1420` under `devUrl`), while the
    # sidecar listens on `http://127.0.0.1:<ephemeral>`. Every request is
    # cross-origin, the preflight was answered with 405, and no response carried
    # `Access-Control-Allow-Origin` — so the browser blocked everything. Nothing
    # worked at all (DEC-058).
    #
    # This is an allowlist of exactly the origins the shell can serve from, not
    # a wildcard. It grants nothing that the loopback bind, the 256-bit session
    # token and the WebSocket origin check did not already gate; it only stops
    # the browser from blocking the one client that is supposed to work.
    allowed_origins = list(settings.allowed_origins)
    if settings.dev_mode:
        # `devUrl` in tauri.conf.json. Dev-only: a release build never loads the
        # frontend over http, so this origin must not be permitted there.
        allowed_origins += ["http://127.0.0.1:1420", "http://localhost:1420"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        # Credentials travel in the Authorization header, never in cookies, so
        # `allow_credentials` stays off. Turning it on is what makes a wildcard
        # origin dangerous, and it buys nothing here.
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(system_router)
    # Mounted only when an engine was supplied. A router whose handlers would
    # dereference `None` is worse than a 404: it turns a wiring mistake into a
    # 500 at request time instead of an obviously absent endpoint.
    if memory is not None:
        app.include_router(memory_router)
    if gateway is not None:
        app.include_router(providers_router)
    if conversations is not None:
        app.include_router(conversations_router)
    # Four collaborators, because this router uses all four: the repository, the
    # boundary for `/resolve-path`, the audit log for the two routes that edit a
    # permission, and the memory engine for `/{id}/memory`. Mounting it with any
    # of them missing would turn a wiring mistake into a 500 at request time.
    if (
        projects is not None
        and path_boundary is not None
        and audit is not None
        and memory is not None
    ):
        app.include_router(projects_router)
    # Always mounted: the socket authenticates and reports agent unavailability
    # itself, and a client that cannot connect at all has no way to be told why.
    if audit is not None:
        app.include_router(audit_router)
    app.include_router(ws_router)

    return app
