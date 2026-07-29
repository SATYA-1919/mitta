"""The audit trail (DEC-082).

Read-only, and there is deliberately no endpoint that deletes from it. A log the
subject of the log can quietly edit is not an audit trail.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from mitta.api.auth import RequireToken
from mitta.api.schemas.audit import AuditEntryResource, AuditResponse
from mitta.policy.audit import AuditLog

router = APIRouter(prefix="/v1/audit", tags=["audit"])


@router.get("", response_model=AuditResponse, summary="What MITTA has done")
async def audit(
    request: Request,
    _: RequireToken,
    limit: int = Query(default=100, ge=1, le=500),
) -> AuditResponse:
    log: AuditLog = request.app.state.audit
    broken_at = log.verify_chain()
    return AuditResponse(
        entries=[
            AuditEntryResource(
                id=entry.id,
                at=entry.at,
                actor=entry.actor,
                action=entry.action,
                subject=entry.subject,
                verdict=entry.verdict,
            )
            for entry in log.recent(limit=limit)
        ],
        chain_intact=broken_at is None,
        broken_at=broken_at,
    )
