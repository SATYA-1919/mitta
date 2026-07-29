"""Telemetry layer — structured logging, local only.

No metrics, no traces, no crash reports leave the machine (R5).
"""

from mitta.telemetry.logging import (
    ContextFilter,
    JSONFormatter,
    RedactionFilter,
    SafeExtraAdapter,
    get_logger,
    request_id_var,
    setup_logging,
    turn_id_var,
)
from mitta.telemetry.redaction import REDACTED, SecretRedactor

__all__ = [
    "REDACTED",
    "ContextFilter",
    "JSONFormatter",
    "RedactionFilter",
    "SafeExtraAdapter",
    "SecretRedactor",
    "get_logger",
    "request_id_var",
    "setup_logging",
    "turn_id_var",
]
