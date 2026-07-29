"""Exception hierarchy.

Every error crossing the API boundary carries a stable, dot-namespaced code.
Clients switch on `code`; `message` is for humans and may be reworded freely
without breaking anything (API_DESIGN.md §5).

This module sits at the bottom of the layer stack and imports nothing from the
rest of the package — every layer needs to raise, so it can depend on nothing.
"""

from __future__ import annotations

from typing import Any


class MittaError(Exception):
    """Base for every error MITTA raises deliberately.

    `retryable` is part of the contract rather than an afterthought: the UI
    decides whether to offer a retry from this flag, and the LLM gateway decides
    whether to fail over from it. Defaulting it per-subclass is what keeps that
    decision out of twenty call sites.
    """

    code: str = "internal.error"
    http_status: int = 500
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.__cause__ = cause

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        """Serialise to the wire shape defined in API_DESIGN.md §5."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
                "request_id": request_id,
            }
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# --------------------------------------------------------------------------- #
# auth.*                                                                       #
# --------------------------------------------------------------------------- #


class AuthError(MittaError):
    code = "auth.invalid_token"
    http_status = 401


class MissingTokenError(AuthError):
    code = "auth.missing_token"


class ForbiddenOriginError(AuthError):
    code = "auth.forbidden_origin"
    http_status = 403


# --------------------------------------------------------------------------- #
# validation.* / not_found.*                                                   #
# --------------------------------------------------------------------------- #


class ValidationError(MittaError):
    code = "validation.failed"
    http_status = 422


class NotFoundError(MittaError):
    code = "not_found.resource"
    http_status = 404

    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(
            f"{resource} '{identifier}' not found",
            details={"resource": resource, "id": identifier},
        )
        self.code = f"not_found.{resource}"


class ConflictError(MittaError):
    code = "conflict.state"
    http_status = 409


# --------------------------------------------------------------------------- #
# policy.*                                                                     #
# --------------------------------------------------------------------------- #


class PolicyError(MittaError):
    code = "policy.denied"
    http_status = 403


class ConfirmationRequiredError(PolicyError):
    code = "policy.confirmation_required"


class ApprovalInvalidError(PolicyError):
    """Signature, expiry, single-use or parameter-hash verification failed.

    Deliberately does not distinguish which check failed in `message`. A caller
    probing for the difference is a caller trying to forge one, and the audit
    log records the specific reason locally where it is actually useful.
    """

    code = "policy.approval_invalid"


# --------------------------------------------------------------------------- #
# provider.*                                                                   #
# --------------------------------------------------------------------------- #


class ProviderError(MittaError):
    code = "provider.error"
    http_status = 502
    retryable = True


class ProviderUnavailableError(ProviderError):
    code = "provider.unavailable"
    http_status = 503


class ProviderRateLimitedError(ProviderError):
    code = "provider.rate_limited"
    http_status = 503


class ProviderAuthError(ProviderError):
    """A key is missing, invalid or revoked. Not retryable — failover, or ask."""

    code = "provider.auth_failed"
    retryable = False


# --------------------------------------------------------------------------- #
# storage.* / plugin.* / config.*                                              #
# --------------------------------------------------------------------------- #


class StorageError(MittaError):
    code = "storage.error"


class MigrationError(StorageError):
    code = "storage.migration_failed"


class VectorIndexError(StorageError):
    """FAISS read, write or rebuild failure.

    Named for the vector index specifically rather than `IndexError`, which
    would shadow the builtin and make every `except IndexError` in the codebase
    ambiguous to a reader.
    """

    code = "storage.index_failed"


class PluginError(MittaError):
    code = "plugin.error"


class ConfigError(MittaError):
    code = "config.invalid"
