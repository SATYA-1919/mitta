"""Exception handlers — one wire shape for every failure (API_DESIGN.md §5)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from mitta.errors import MittaError, ValidationError
from mitta.telemetry.logging import get_logger, request_id_var

log = get_logger(__name__)


def _response(error: MittaError, request_id: str | None) -> JSONResponse:
    return JSONResponse(status_code=error.http_status, content=error.to_payload(request_id))


async def _handle_mitta_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, MittaError)
    request_id = request_id_var.get()
    log.warning(
        "api.error",
        extra={"code": exc.code, "status": exc.http_status, "details": exc.details},
    )
    return _response(exc, request_id)


async def _handle_validation_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    error = ValidationError(
        "Request failed schema validation",
        details={"errors": _jsonable(exc.errors())},
    )
    return _response(error, request_id_var.get())


def _jsonable(value: Any) -> Any:
    """Coerce a Pydantic error report into something `json.dumps` can encode.

    When a field validator raises, Pydantic puts the **exception object itself**
    in the error's `ctx`. `JSONResponse` cannot encode that, so rendering the
    422 raised inside the handler and the client got a 500 — a validation error
    reported as a server fault, for the one class of mistake most likely to be a
    user's typo.

    Found by the first schema to use a custom validator (`CreateScheduleRequest`
    parses the cron expression). Every earlier schema validated with declarative
    constraints, whose errors are strings, so the defect was latent from the day
    this handler was written.
    """
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


async def _handle_http_exception(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    error = MittaError(str(exc.detail))
    error.code = f"http.{exc.status_code}"
    error.http_status = exc.status_code
    return _response(error, request_id_var.get())


async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
    """Last resort.

    The exception is logged in full locally, but the response says only that
    something failed. A stack trace in an HTTP body is a disclosure risk even on
    loopback — it can name paths, dependency versions and internal structure to
    anything that can reach the port.
    """
    request_id = request_id_var.get()
    log.exception("api.unhandled_exception", extra={"request_id": request_id})
    error = MittaError("An unexpected error occurred. See the local log for detail.")
    return _response(error, request_id)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(MittaError, _handle_mitta_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(HTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)
