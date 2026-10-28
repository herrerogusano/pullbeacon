from __future__ import annotations

import io
import json
import logging

import pytest

from pullbeacon.credentials import Secret, github_token, provider_api_key
from pullbeacon.domain.errors import CredentialError
from pullbeacon.logging_utils import JsonFormatter, RedactingFilter, get_logger, log_event, redact

FAKE_GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
FAKE_ANTHROPIC = "sk-ant-api03-" + "x" * 40


def test_redact_known_token_shapes() -> None:
    text = f"token={FAKE_GH} key {FAKE_ANTHROPIC} Authorization: Bearer abc.def.ghi"
    out = redact(text)
    assert FAKE_GH not in out
    assert FAKE_ANTHROPIC not in out
    assert "abc.def.ghi" not in out


def test_redact_environment_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "custom-format-secret-value")
    assert "custom-format-secret-value" not in redact("oops custom-format-secret-value leaked")


def test_secret_wrapper_never_renders_value() -> None:
    secret = Secret(FAKE_GH)
    assert FAKE_GH not in repr(secret)
    assert FAKE_GH not in f"{secret}"
    assert secret.reveal() == FAKE_GH


def test_missing_credentials_are_readable_and_safe() -> None:
    with pytest.raises(CredentialError, match="GITHUB_TOKEN is not set"):
        github_token()
    with pytest.raises(CredentialError, match="ANTHROPIC_API_KEY is not set"):
        provider_api_key("anthropic")
    with pytest.raises(CredentialError, match="unsupported provider"):
        provider_api_key("mistral")


def test_credentials_are_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_GH)
    assert github_token().reveal() == FAKE_GH


def test_logs_are_json_and_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_GH)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    logger = get_logger("test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        log_event(logger, f"failed with {FAKE_GH}", detail={"header": f"Bearer {FAKE_GH}"})
        try:
            raise RuntimeError(FAKE_GH)
        except RuntimeError:
            logger.exception("boom")
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert FAKE_GH not in output
    first = json.loads(output.splitlines()[0])
    assert first["level"] == "info"
    assert "exception" in output
