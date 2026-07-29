"""Wire schemas — the source of truth for generated TypeScript types (DEC-028)."""

from mitta.api.schemas.common import (
    Envelope,
    ErrorDetail,
    ErrorResponse,
    Register,
    Schema,
)
from mitta.api.schemas.system import (
    CapabilitiesResponse,
    ComponentState,
    ComponentStatus,
    HealthResponse,
    StatusResponse,
)

__all__ = [
    "CapabilitiesResponse",
    "ComponentState",
    "ComponentStatus",
    "Envelope",
    "ErrorDetail",
    "ErrorResponse",
    "HealthResponse",
    "Register",
    "Schema",
    "StatusResponse",
]
