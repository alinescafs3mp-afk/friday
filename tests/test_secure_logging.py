from __future__ import annotations

import logging
from urllib.parse import quote

from jericho.telemetry.logging import (
    SecretRedactingFormatter,
    redact_text,
    secrets_from_environment,
)


def test_redact_text_covers_common_credential_shapes():
    rendered = redact_text(
        "Authorization: Bearer abcdefghijklmnop api_token=super-secret "
        "https://user:password@example.test/path"
    )
    assert "abcdefghijklmnop" not in rendered
    assert "super-secret" not in rendered
    assert "user:password" not in rendered


def test_formatter_redacts_telegram_token_in_urls_and_tracebacks():
    token = "123456789:ABCdef_secret-value"
    formatter = SecretRedactingFormatter((token,))
    raw_url = f"https://api.telegram.org/bot{token}/getUpdates"
    encoded_url = f"https://example.test/?token={quote(token, safe='')}"
    try:
        raise RuntimeError(f"request failed at {raw_url} and {encoded_url}")
    except RuntimeError:
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "bridge failure: %s",
            (raw_url,),
            exc_info=__import__("sys").exc_info(),
        )
    rendered = formatter.format(record)
    assert token not in rendered
    assert quote(token, safe="") not in rendered
    assert "[REDACTED]" in rendered


def test_environment_secret_detection_does_not_treat_limits_as_credentials(monkeypatch):
    monkeypatch.setenv("JERICHO_API_TOKEN", "real-api-token-value")
    monkeypatch.setenv("JERICHO_LLM_MAX_TOKENS", "2048")
    monkeypatch.setenv("JERICHO_API_REQUIRE_TOKEN_ON_LOOPBACK", "1")
    secrets = secrets_from_environment()
    assert "real-api-token-value" in secrets
    assert "2048" not in secrets
    assert "1" not in secrets
