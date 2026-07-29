"""Shared wire schemas.

These Pydantic models are the single source of truth for the TypeScript types
(DEC-028) — `scripts/gen-types.sh` generates the frontend types from the OpenAPI
document FastAPI derives from them. Nothing here is edited to match the
frontend; the frontend is regenerated to match this.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Schema(BaseModel):
    """Base for every wire model."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ErrorDetail(Schema):
    code: str = Field(description="Stable dot-namespaced code. Clients switch on this.")
    message: str = Field(description="Human-readable. May be reworded without notice.")
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ErrorResponse(Schema):
    error: ErrorDetail


class Envelope(Schema):
    """WebSocket frame envelope (API_DESIGN.md §4.1).

    Declared in Phase 3 so the contract exists before the socket does; the
    endpoint itself lands in Phase 7.
    """

    id: str
    type: str = Field(description="Dot-namespaced; the namespace is the subsystem.")
    ts: str
    ref: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


Register = Literal["playful", "serious"]
"""DEC-033. Computed upstream of the personality layer, never by it."""
