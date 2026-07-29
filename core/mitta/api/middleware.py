"""Request-scoped context and access logging."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from mitta import ids
from mitta.telemetry.logging import get_logger, request_id_var

log = get_logger("mitta.api.access")

_QUIET_PATHS = frozenset({"/health"})
"""Rust polls /health continuously; logging it would bury everything else."""


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, binds it to the logging context, and times the call.

    The id is bound to a `ContextVar`, so every log record emitted anywhere
    downstream carries it without a single function having to thread it through
    its signature.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = ids.prefixed(ids.REQUEST)
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        if request.url.path not in _QUIET_PATHS:
            log.info(
                "api.request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round(duration_ms, 2),
                    "request_id": request_id,
                },
            )
        return response
