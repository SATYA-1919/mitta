"""Structured logging.

Stdlib ``logging`` with a JSON formatter, not `structlog` — see DEC-034. The
short version: the redaction filter must intercept *every* record, including
those emitted by uvicorn, httpx and any dependency that logs a header. A
handler-level ``logging.Filter`` does that unconditionally; a structlog
processor chain only sees records that went through structlog.

The filter is attached to **handlers**, never to loggers. A filter on a Logger
is not applied to records propagated from its children, so a root-logger filter
would silently miss everything a library logs through its own logger — which is
precisely the traffic most likely to contain a key.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import sys
import time
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

from mitta.config.settings import LoggingSettings
from mitta.telemetry.redaction import SecretRedactor

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mitta_request_id", default=None
)
turn_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mitta_turn_id", default=None
)

_RESERVED: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class RedactionFilter(logging.Filter):
    """Applies `SecretRedactor` to the message, args and extras of every record.

    Returns True always — this filter redacts, it never drops. A filter that can
    drop records is a filter that can hide an error, and losing an error to
    protect a secret is the wrong trade in a local application.
    """

    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redactor.redact_text(record.msg)
        if record.args:
            record.args = self._redactor.redact(record.args)
        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED:
                record.__dict__[key] = self._redactor.redact(value)
        if record.exc_text:
            record.exc_text = self._redactor.redact_text(record.exc_text)
        return True


class ContextFilter(logging.Filter):
    """Stamps the ambient request and turn ids onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if (request_id := request_id_var.get()) is not None:
            record.request_id = request_id
        if (turn_id := turn_id_var.get()) is not None:
            record.turn_id = turn_id
        return True


class JSONFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable, for development. Never used for the file handler."""

    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} " \
               f"{record.name:<28} {record.getMessage()}"
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        if extras:
            base += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging(
    settings: LoggingSettings,
    log_dir: Path,
    redactor: SecretRedactor,
) -> None:
    """Configure the root logger. Idempotent — safe to call twice in tests."""
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.setLevel(settings.level)

    filters = (ContextFilter(), RedactionFilter(redactor))

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "mitta.log",
        maxBytes=settings.max_bytes,
        backupCount=settings.backup_count,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(JSONFormatter() if settings.json_output else ConsoleFormatter())
    for f in filters:
        file_handler.addFilter(f)
    root.addHandler(file_handler)

    if settings.console:
        # stderr, never stdout: stdout carries the `MITTA_READY <port>` line the
        # supervisor parses, and a log line there would corrupt the handshake.
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(ConsoleFormatter())
        for f in filters:
            console.addFilter(f)
        root.addHandler(console)

    # uvicorn installs its own handlers and would double-log through them.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(noisy)
        logger.handlers.clear()
        logger.propagate = True


class SafeExtraAdapter(logging.LoggerAdapter[logging.Logger]):
    """Prevents `extra=` keys from colliding with `LogRecord` attributes.

    `Logger.makeRecord` raises ``KeyError: Attempt to overwrite 'name'`` if an
    `extra` dict contains any reserved attribute name — `name`, `module`,
    `filename`, `process`, `args` and about fifteen others. The names that
    collide are exactly the ones a caller reaches for naturally
    (``extra={"name": migration.name}``), the failure happens at log time rather
    than at import time, and it takes down whatever was being logged about.

    Renaming with a trailing underscore keeps the value rather than dropping it,
    and makes the collision visible in the output instead of silent.
    """

    def process(
        self, msg: object, kwargs: MutableMapping[str, object]
    ) -> tuple[object, MutableMapping[str, object]]:
        extra = kwargs.get("extra")
        if isinstance(extra, dict):
            kwargs["extra"] = {
                (f"{key}_" if key in _RESERVED else key): value for key, value in extra.items()
            }
        return msg, kwargs


def get_logger(name: str) -> SafeExtraAdapter:
    return SafeExtraAdapter(logging.getLogger(name), {})
