"""Health, status and capabilities (API_DESIGN.md §3.1)."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request

from mitta.api.auth import RequireToken
from mitta.api.schemas.system import (
    CapabilitiesResponse,
    ComponentStatus,
    HealthResponse,
    StatusResponse,
)
from mitta.persistence.migrations import current_version

router = APIRouter(tags=["system"])


def _uptime(request: Request) -> float:
    started: float = request.app.state.started_at
    return round(time.monotonic() - started, 3)


@router.get("/health", response_model=HealthResponse, summary="Liveness")
async def health(request: Request) -> HealthResponse:
    """Unauthenticated by design.

    The supervisor polls this to decide whether to restart the sidecar, and it
    must work before the token exchange has completed. It therefore returns
    nothing that isn't already known to whoever spawned the process.
    """
    return HealthResponse(
        api_version=request.app.state.api_version,
        uptime_seconds=_uptime(request),
    )


@router.get("/v1/status", response_model=StatusResponse, summary="Readiness")
async def status(request: Request, _: RequireToken) -> StatusResponse:
    """Detailed readiness. `ready` false means up but unable to serve a turn."""
    state = request.app.state
    components: list[ComponentStatus] = []

    try:
        schema_version = current_version(state.database)
        healthy = state.database.integrity_check()
        components.append(
            ComponentStatus(
                name="database",
                state="ok" if healthy else "degraded",
                detail=None if healthy else "integrity_check reported errors",
            )
        )
    except Exception as exc:
        schema_version = 0
        healthy = False
        components.append(ComponentStatus(name="database", state="unavailable", detail=str(exc)))

    # Landing in later phases. Reported as unavailable rather than omitted, so
    # the UI can distinguish "not built yet" from "silently missing".
    components.extend(
        [
            ComponentStatus(name="memory", state="unavailable", detail="Phase 5"),
            ComponentStatus(name="llm_gateway", state="unavailable", detail="Phase 7"),
            ComponentStatus(name="voice", state="unavailable", detail="Phase 6"),
        ]
    )

    return StatusResponse(
        ready=healthy,
        api_version=state.api_version,
        schema_version=schema_version,
        platform=state.os_adapter.platform_name,
        storage_root=str(state.paths.storage_root),
        uptime_seconds=_uptime(request),
        components=components,
    )


@router.get("/v1/capabilities", response_model=CapabilitiesResponse)
async def capabilities(request: Request, _: RequireToken) -> CapabilitiesResponse:
    state = request.app.state
    return CapabilitiesResponse(
        api_version=state.api_version,
        personality=state.settings.personality.enabled,
        voice=False,
        plugins=False,
        offline_reasoning=False,
        providers=[],
    )
