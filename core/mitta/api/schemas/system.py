"""Health, status and capability schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from mitta.api.schemas.common import Schema

ComponentState = Literal["ok", "degraded", "unavailable"]


class HealthResponse(Schema):
    """Liveness only. Deliberately carries nothing sensitive — this endpoint is
    unauthenticated because Rust polls it before the token exchange completes."""

    status: Literal["ok"] = "ok"
    api_version: str
    uptime_seconds: float


class ComponentStatus(Schema):
    name: str
    state: ComponentState
    detail: str | None = None


class StatusResponse(Schema):
    """Readiness. `ready` false means the process is up but cannot serve a turn."""

    ready: bool
    api_version: str
    schema_version: int
    platform: str
    storage_root: str
    uptime_seconds: float
    components: list[ComponentStatus] = Field(default_factory=list)


class CapabilitiesResponse(Schema):
    """Feature flags the UI branches on, so it never hardcodes build assumptions."""

    api_version: str
    personality: bool
    voice: bool
    plugins: bool
    offline_reasoning: bool = Field(
        default=False,
        description="False in v1 — both providers are cloud (R8, DEC-020).",
    )
    providers: list[str] = Field(default_factory=list)
