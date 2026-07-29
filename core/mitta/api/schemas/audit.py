"""Audit wire schemas."""

from __future__ import annotations

from typing import Literal

from mitta.api.schemas.common import Schema


class AuditEntryResource(Schema):
    id: str
    at: int
    actor: Literal["user", "agent", "plugin", "scheduler", "system"]
    action: str
    subject: str | None
    verdict: Literal["allow", "confirm", "deny"] | None


class AuditResponse(Schema):
    entries: list[AuditEntryResource]
    # Recomputed on every read, not cached. A cached "intact" is a claim the
    # user has to take on faith, which is the thing the chain exists to avoid.
    chain_intact: bool
    #: `seq` of the first broken entry, when the chain does not verify.
    broken_at: int | None
