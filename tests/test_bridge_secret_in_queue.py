"""The health check must not print the credential it is checking.

The Telegram API URL CONTAINS the bot token — `.../bot<token>/sendMessage` — and
httpx quotes the URL inside every `HTTPStatusError`. That string was stored on
the queue row verbatim by `mark_failure`/`mark_dead_letter`, and diagnostics then
surfaced the newest dead-letter error through `jericho doctor`, `jericho status
--json` and `GET /api/admin/diagnostics`.

So the one command an operator runs when something is broken — and pastes into a
bug report — printed the bot token. `install_secret_redaction` covers the log
handlers; it does not cover what is written into a database.
"""

from __future__ import annotations

import httpx

from jericho.telegram_bridge import TelegramBridge, TelegramConfig

TOKEN = "123456789:AAHqwertyuiopASDFGHJKLzxcvbnm-Test01"


def _bridge(tmp_path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token=TOKEN,
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


def test_a_telegram_error_does_not_carry_the_token_into_the_queue(tmp_path):
    bridge = _bridge(tmp_path)
    try:
        # Exactly the shape httpx produces: the failing URL, token included.
        request = httpx.Request("POST", f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        response = httpx.Response(400, request=request, json={"description": "Bad Request"})
        try:
            response.raise_for_status()  # the real message, built by httpx itself
        except httpx.HTTPStatusError as raised:
            error = raised
        assert TOKEN in f"{type(error).__name__}: {error}", "the premise: the raw text leaks it"

        redacted = bridge._redact(f"{type(error).__name__}: {error}")  # noqa: SLF001
        assert TOKEN not in redacted
        assert "[redacted]" in redacted
        # Still useful for diagnosis: the failure is named and the endpoint is
        # recognisable, only the credential is gone.
        assert "HTTPStatusError" in redacted
    finally:
        bridge._inbox.close()  # noqa: SLF001


def test_the_bridge_secret_and_proxy_password_go_too(tmp_path):
    bridge = _bridge(tmp_path)
    try:
        text = bridge._redact(f"secret={'B' * 48} token={TOKEN}")  # noqa: SLF001
        assert "B" * 48 not in text
        assert TOKEN not in text
    finally:
        bridge._inbox.close()  # noqa: SLF001


def test_diagnostics_redacts_what_it_reads_back(tmp_path):
    """Belt and braces: a row written by an older build must not leak either."""
    import sqlite3

    from jericho.diagnostics import _bridge_queue_status

    database = tmp_path / "telegram.sqlite3"
    bridge = _bridge(tmp_path)
    bridge._inbox.close()  # noqa: SLF001 - created the schema, now write into it directly

    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO updates(update_id, payload_json, status, attempts, last_error, failed_at,"
        " created_at) VALUES(1, '{}', 'dead_letter', 5, ?, 1, 1)",
        (f"HTTPStatusError: 400 for url https://api.telegram.org/bot{TOKEN}/sendMessage",),
    )
    connection.commit()
    connection.close()

    status = _bridge_queue_status(database)
    assert status["dead_letter"] == 1
    assert TOKEN not in status["last_dead_letter_error"], status["last_dead_letter_error"]
