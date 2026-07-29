"""Authentication and the system endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mitta.api.auth import TokenVerifier
from mitta.errors import AuthError, ForbiddenOriginError, MissingTokenError

TOKEN = "test-session-token-0123456789abcdef"


# -- TokenVerifier ---------------------------------------------------------- #


def test_correct_token_passes() -> None:
    TokenVerifier(TOKEN).verify(TOKEN)


def test_wrong_token_is_rejected() -> None:
    with pytest.raises(AuthError):
        TokenVerifier(TOKEN).verify("wrong")


def test_missing_token_is_rejected() -> None:
    with pytest.raises(MissingTokenError):
        TokenVerifier(TOKEN).verify(None)


def test_verification_is_disabled_without_a_configured_token() -> None:
    verifier = TokenVerifier(None)
    assert verifier.enabled is False
    verifier.verify(None)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (f"Bearer {TOKEN}", TOKEN),
        (f"bearer {TOKEN}", TOKEN),
        (TOKEN, None),
        ("Basic abc", None),
        ("Bearer", None),
        ("", None),
        (None, None),
    ],
)
def test_bearer_extraction(header: str | None, expected: str | None) -> None:
    assert TokenVerifier.extract_bearer(header) == expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (f"mitta.v1, {TOKEN}", TOKEN),
        (f"mitta.v1,{TOKEN}", TOKEN),
        ("mitta.v1", None),
        (f"other.v1, {TOKEN}", None),
        (None, None),
    ],
)
def test_subprotocol_extraction(header: str | None, expected: str | None) -> None:
    """DEC-026: the token rides the subprotocol so it never lands in a URL."""
    assert TokenVerifier.extract_subprotocol(header) == expected


def test_origin_outside_the_allowlist_is_rejected() -> None:
    verifier = TokenVerifier(TOKEN, ("tauri://localhost",))
    verifier.verify_origin("tauri://localhost")
    with pytest.raises(ForbiddenOriginError):
        verifier.verify_origin("http://evil.example")


def test_origin_check_is_relaxed_in_dev_mode() -> None:
    TokenVerifier(TOKEN, ("tauri://localhost",), dev_mode=True).verify_origin("http://localhost")


# -- endpoints -------------------------------------------------------------- #


def test_health_needs_no_token(client: TestClient) -> None:
    """The supervisor polls this before the token exchange completes."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["api_version"] == "1"


def test_health_leaks_nothing_sensitive(client: TestClient) -> None:
    body = client.get("/health").json()
    assert set(body) == {"status", "api_version", "uptime_seconds"}


def test_status_requires_a_token(client: TestClient) -> None:
    response = client.get("/v1/status")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth.missing_token"


def test_status_rejects_a_wrong_token(client: TestClient) -> None:
    response = client.get("/v1/status", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth.invalid_token"


def test_status_reports_readiness(client: TestClient, auth_headers: dict[str, str]) -> None:
    body = client.get("/v1/status", headers=auth_headers).json()
    assert body["ready"] is True
    assert body["schema_version"] >= 1
    assert body["platform"] == "macos"
    names = {c["name"] for c in body["components"]}
    assert {"database", "memory", "llm_gateway", "voice"} <= names


def test_capabilities_declares_no_offline_reasoning(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """R8 / DEC-020 — v1 has no local model."""
    body = client.get("/v1/capabilities", headers=auth_headers).json()
    assert body["offline_reasoning"] is False


def test_docs_are_disabled_outside_dev_mode(client: TestClient) -> None:
    """Docs render request bodies, which here means conversation content."""
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_errors_use_the_documented_envelope(client: TestClient) -> None:
    error = client.get("/v1/status").json()["error"]
    assert set(error) == {"code", "message", "retryable", "details", "request_id"}


def test_responses_carry_a_request_id(client: TestClient) -> None:
    assert client.get("/health").headers["X-Request-ID"].startswith("req_")


# -- schema export ----------------------------------------------------------- #


def test_openapi_export_includes_every_optional_router() -> None:
    """The generated TypeScript is only as complete as this document.

    `create_app` omits routers whose collaborators are absent — correct at
    runtime, silently wrong for codegen. An unmounted router produces no paths,
    the frontend loses those endpoints, and nothing fails.
    """
    from mitta.api.schema_export import REQUIRED_PATH_PREFIXES, build_openapi

    paths = build_openapi()["paths"]

    for prefix in REQUIRED_PATH_PREFIXES:
        assert any(path.startswith(prefix) for path in paths), f"{prefix} missing from OpenAPI"


def test_openapi_export_fails_loudly_on_a_missing_router() -> None:
    from mitta.api import schema_export

    with pytest.raises(RuntimeError, match="missing"):
        schema_export._assert_complete({"paths": {"/health": {}}})
