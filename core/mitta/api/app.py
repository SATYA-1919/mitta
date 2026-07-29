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

from mitta.api.auth import TokenVerifier
from mitta.api.exception_handlers import register_exception_handlers
from mitta.api.http import memory_router, system_router
from mitta.api.middleware import RequestContextMiddleware
from mitta.config.paths import Paths
from mitta.config.settings import Settings
from mitta.memory.embedding.base import EmbeddingProvider
from mitta.memory.indexer import Indexer
from mitta.memory.service import MemoryService
from mitta.os_adapter.base import OSAdapter
from mitta.persistence.database import Database
from mitta.telemetry.logging import get_logger

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

    # No CORS middleware. The only legitimate client is the Tauri webview, which
    # is same-origin through the shell; adding CORS would be adding a way in.
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(system_router)
    # Mounted only when an engine was supplied. A router whose handlers would
    # dereference `None` is worse than a 404: it turns a wiring mistake into a
    # 500 at request time instead of an obviously absent endpoint.
    if memory is not None:
        app.include_router(memory_router)

    return app
