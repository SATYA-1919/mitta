"""Provider status (API_DESIGN.md §3.3).

Read-only. There is no endpoint that accepts a key: a key arriving over HTTP is
a key in an access log the first time someone enables request logging, which is
why DEC-017 routes it through the Keychain or a gitignored file instead.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from mitta.api.auth import RequireToken
from mitta.api.schemas.llm import ProvidersResponse, ProviderStatusResource
from mitta.llm import keys
from mitta.llm.gateway import LLMGateway

router = APIRouter(prefix="/v1/providers", tags=["providers"])


@router.get("", response_model=ProvidersResponse, summary="Provider health")
async def providers(request: Request, _: RequireToken) -> ProvidersResponse:
    gateway: LLMGateway = request.app.state.gateway
    statuses = gateway.status()

    return ProvidersResponse(
        providers=[
            ProviderStatusResource(
                name=status.name,
                configured=status.configured,
                state=status.state,
                last_error=status.last_error,
                model_count=status.model_count,
            )
            for status in statuses
        ],
        reasoning_available=gateway.configured,
        key_source=_key_source(),
    )


def _key_source() -> str:
    """Which path supplied a key, for the Settings pane to explain itself."""
    if any(status.configured for status in keys.status()):
        return "env_file" if keys.default_env_file() is not None else "keychain"
    return "none"
