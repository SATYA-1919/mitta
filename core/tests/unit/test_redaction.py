"""Redaction is a security control, so it is tested as one (DEC-017)."""

from __future__ import annotations

import logging

import pytest

from mitta.telemetry.logging import RedactionFilter, get_logger
from mitta.telemetry.redaction import REDACTED, SecretRedactor

# check-no-secrets: allow — the fixtures below are synthetic, and they must look
# like real keys because verifying that the redactor catches those shapes is the
# entire purpose of this file.


@pytest.mark.parametrize(
    "text",
    [
        "gsk_abcdefghijklmnopqrstuvwxyz012345",
        "sk-or-v1-abcdefghijklmnopqrstuvwxyz0123",
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
        "sk-proj-abcdefghijklmnopqrstuvwxyz01",
        "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ01234",
        "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "xoxb-1234567890-abcdefghijkl",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
    ],
)
def test_known_key_formats_are_redacted(text: str) -> None:
    assert SecretRedactor().redact_text(f"using key {text} now") == f"using key {REDACTED} now"


def test_authorization_header_is_redacted() -> None:
    result = SecretRedactor().redact_text("Authorization: Bearer abcdef0123456789ghijkl")
    assert "abcdef0123456789ghijkl" not in result


def test_assignment_form_is_redacted() -> None:
    redactor = SecretRedactor()
    assert "hunter2hunter2" not in redactor.redact_text('{"password": "hunter2hunter2"}')
    assert "topsecretvalue" not in redactor.redact_text("api_key=topsecretvalue")


def test_registered_literal_is_redacted_regardless_of_shape() -> None:
    """The session token has no recognisable format — only literal matching catches it."""
    redactor = SecretRedactor()
    redactor.register("plain-unremarkable-token-value")
    assert redactor.redact_text("token is plain-unremarkable-token-value") == (
        f"token is {REDACTED}"
    )


def test_short_literals_are_ignored() -> None:
    """Registering "abc" must not blank out every occurrence of "abc" in prose."""
    redactor = SecretRedactor()
    redactor.register("abc")
    assert redactor.redact_text("abcdef") == "abcdef"


def test_sensitive_key_names_are_redacted_by_name() -> None:
    redacted = SecretRedactor().redact({"api_key": "x", "note": "fine", "TOKEN": "y"})
    assert redacted == {"api_key": REDACTED, "note": "fine", "TOKEN": REDACTED}


def test_nested_containers_are_traversed() -> None:
    payload = {"outer": [{"headers": {"authorization": "Bearer abcdef0123456789ghijkl"}}]}
    result = SecretRedactor().redact(payload)
    assert "abcdef0123456789ghijkl" not in str(result)


def test_recursion_is_depth_capped() -> None:
    """A pathological payload must not make a log call raise."""
    payload: dict[str, object] = {}
    node = payload
    for _ in range(50):
        child: dict[str, object] = {}
        node["next"] = child
        node = child
    SecretRedactor().redact(payload)  # must not raise


def test_filter_redacts_message_args_and_extras() -> None:
    redactor = SecretRedactor()
    redactor.register("session-token-abcdef012345")
    log_filter = RedactionFilter(redactor)

    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="token %s",
        args=("session-token-abcdef012345",),
        exc_info=None,
    )
    record.api_key = "gsk_abcdefghijklmnopqrstuvwxyz012345"

    assert log_filter.filter(record) is True
    assert "session-token-abcdef012345" not in record.getMessage()
    assert record.api_key == REDACTED


def test_filter_never_drops_records() -> None:
    """Redaction must not be able to hide an error."""
    log_filter = RedactionFilter(SecretRedactor(["gsk_aaaaaaaaaaaaaaaaaaaaaaaaaaaa"]))
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, None)
    assert log_filter.filter(record) is True


@pytest.mark.parametrize("reserved", ["name", "module", "filename", "process", "args", "msg"])
def test_extra_keys_colliding_with_record_attributes_do_not_raise(
    reserved: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: `extra={"name": ...}` used to raise KeyError at log time.

    stdlib `Logger.makeRecord` rejects any `extra` key that shadows a LogRecord
    attribute, and the colliding names are exactly the ones a caller reaches for
    naturally. Caught by the end-to-end smoke test, not by unit tests, which is
    why it is pinned here.
    """
    log = get_logger("mitta.test.collision")
    with caplog.at_level(logging.INFO):
        log.info("event", extra={reserved: "value"})
    assert caplog.records[-1].__dict__[f"{reserved}_"] == "value"


def test_non_colliding_extras_are_untouched(caplog: pytest.LogCaptureFixture) -> None:
    log = get_logger("mitta.test.collision")
    with caplog.at_level(logging.INFO):
        log.info("event", extra={"migration_name": "initial"})
    assert caplog.records[-1].migration_name == "initial"
