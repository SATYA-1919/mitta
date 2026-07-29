"""Session-token authentication (DEC-004, DEC-026).

The sidecar binds loopback, which stops remote access but not *local* access —
any process running as this user can reach the port. The session token is what
makes the agent drivable only by the shell that spawned it.

Two transports, because the browser gives us no choice:

* HTTP — ``Authorization: Bearer <token>``.
* WebSocket — the token as the second value of ``Sec-WebSocket-Protocol``.
  Browsers forbid custom headers on the WS handshake, and the usual workaround
  (``?token=``) writes the credential into every access log, which is the exact
  leak class DEC-017 exists to close.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Request, WebSocket

from mitta.errors import AuthError, ForbiddenOriginError, MissingTokenError

_SUBPROTOCOL = "mitta.v1"


class TokenVerifier:
    """Constant-time session-token comparison.

    `hmac.compare_digest` rather than `==` — string equality short-circuits on
    the first differing byte, and over a loopback socket a local attacker can
    make enough requests for that timing difference to be measurable.
    """

    __slots__ = ("_allowed_origins", "_dev_mode", "_token")

    def __init__(
        self,
        token: str | None,
        allowed_origins: tuple[str, ...] = (),
        *,
        dev_mode: bool = False,
    ) -> None:
        self._token = token
        self._allowed_origins = allowed_origins
        self._dev_mode = dev_mode

    @property
    def enabled(self) -> bool:
        """False only when no token was injected — development, never a release.

        The supervisor always provides one, so an unset token means someone ran
        the sidecar by hand. It is logged loudly at startup rather than silently
        tolerated.
        """
        return self._token is not None

    def verify(self, presented: str | None) -> None:
        if not self.enabled:
            return
        if not presented:
            raise MissingTokenError("Missing session token")
        assert self._token is not None
        if not hmac.compare_digest(presented, self._token):
            raise AuthError("Invalid session token")

    def verify_origin(self, origin: str | None) -> None:
        """Reject cross-origin WebSocket upgrades.

        Without this, a page open in the user's normal browser could connect to
        the sidecar and — if it ever learned the token — drive the agent. The
        origin check closes that path independently of token secrecy.
        """
        if self._dev_mode or not self._allowed_origins:
            return
        if origin is None:
            return  # non-browser client; the token remains the control
        if origin not in self._allowed_origins:
            raise ForbiddenOriginError("Origin not permitted", details={"origin": origin})

    @staticmethod
    def extract_bearer(header: str | None) -> str | None:
        if not header:
            return None
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value:
            return None
        return value.strip()

    @staticmethod
    def extract_subprotocol(header: str | None) -> str | None:
        """Parse ``Sec-WebSocket-Protocol: mitta.v1, <token>``."""
        if not header:
            return None
        parts = [p.strip() for p in header.split(",")]
        if len(parts) < 2 or parts[0] != _SUBPROTOCOL:
            return None
        return parts[1] or None


def get_verifier(request: Request) -> TokenVerifier:
    verifier: TokenVerifier = request.app.state.token_verifier
    return verifier


async def require_token(request: Request) -> None:
    """FastAPI dependency guarding every authenticated HTTP route."""
    verifier = get_verifier(request)
    verifier.verify(TokenVerifier.extract_bearer(request.headers.get("authorization")))


async def authenticate_websocket(websocket: WebSocket) -> bool:
    """Authenticate a WS upgrade. Closes with 4401/4403 and returns False on failure.

    Rejection happens *before* `accept()`, so an unauthenticated peer never holds
    an open socket.
    """
    verifier: TokenVerifier = websocket.app.state.token_verifier
    try:
        verifier.verify_origin(websocket.headers.get("origin"))
        verifier.verify(
            TokenVerifier.extract_subprotocol(websocket.headers.get("sec-websocket-protocol"))
        )
    except ForbiddenOriginError:
        await websocket.close(code=4403)
        return False
    except AuthError:
        await websocket.close(code=4401)
        return False
    return True


RequireToken = Annotated[None, Depends(require_token)]
