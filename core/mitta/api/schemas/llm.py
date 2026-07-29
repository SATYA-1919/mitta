"""Provider status schemas.

Deliberately contains no field that could carry a key value. The UI needs to
know *whether* a provider is configured and *how it is behaving*; it never needs
the credential, and a schema with a `key` field is one refactor away from
serialising one.
"""

from __future__ import annotations

from typing import Literal

from mitta.api.schemas.common import Schema


class ProviderStatusResource(Schema):
    name: str
    configured: bool
    state: Literal["healthy", "degraded", "unavailable"]
    # Present so a failing provider explains itself. Passed through the
    # redactor at the logging layer, and provider error bodies never echo the
    # key they were sent.
    last_error: str | None
    model_count: int


class ProvidersResponse(Schema):
    providers: list[ProviderStatusResource]
    # False means reasoning is unavailable. The UI says so plainly rather than
    # accepting a message it already knows will fail (R8).
    reasoning_available: bool
    # Which path supplied a key, so Settings can explain itself instead of the
    # user guessing whether their `.env` was picked up.
    key_source: Literal["keychain", "env_file", "none"]
