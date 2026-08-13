from __future__ import annotations

import logging
from urllib.parse import quote

from friday.telemetry.logging import (
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


def test_redact_text_covers_a_foreign_friday_api_token_but_not_short_labels():
    token = "jrc_" + "Ab0_-xYz9" * 4 + "QrsTuvw"

    rendered = redact_text(f"received {token}; labels jrc_demo jrc_short_test_word")

    assert token not in rendered
    assert "[redacted:token]" in rendered
    assert "jrc_demo" in rendered
    assert "jrc_short_test_word" in rendered


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
    monkeypatch.setenv("FRIDAY_API_TOKEN", "real-api-token-value")
    monkeypatch.setenv("FRIDAY_LLM_MAX_TOKENS", "2048")
    monkeypatch.setenv("FRIDAY_API_REQUIRE_TOKEN_ON_LOOPBACK", "1")
    secrets = secrets_from_environment()
    assert "real-api-token-value" in secrets
    assert "2048" not in secrets
    assert "1" not in secrets


def test_split_packages_keep_their_logger_names() -> None:
    """Splitting a module into a package silently renames its logger.

    ``logging.getLogger(__name__)`` moved into ``_base.py`` starts emitting under
    ``friday.storage._base`` instead of ``friday.storage``. Nothing here configures
    loggers by name, so nothing breaks — but the name is what an operator greps for,
    and three refactors changed it without anyone asking. These are named explicitly
    for that reason; this test is what keeps the next split honest.
    """
    from friday.ingestion import _base as ingestion_base
    from friday.storage import _base as storage_base
    from friday.telegram_bridge import _base as bridge_base

    assert storage_base.LOGGER.name == "friday.storage"
    assert ingestion_base.LOGGER.name == "friday.ingestion"
    assert bridge_base.LOGGER.name == "friday.telegram_bridge"
