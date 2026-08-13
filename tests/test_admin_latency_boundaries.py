"""Cheap admin navigation is a product boundary, not a benchmark accident."""

from __future__ import annotations

import inspect
from pathlib import Path


def _function_body(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end, source.index(start))]


def test_opening_a_person_does_not_reload_the_feed_or_waterfall_conversations() -> None:
    source = Path("friday/admin_ui/static/app.js").read_text(encoding="utf-8")
    opener = _function_body(source, "async function openChat", "// Живая переписка")
    loader = _function_body(source, "async function loadChatThread", "async function sendReply")

    assert "refresh()" not in opener
    assert "loadChatThread(userId)" in opener
    assert "/api/admin/chats/${q(userId)}/messages?limit=500" in loader
    assert "/api/admin/conversations?" not in loader
    assert "for (const conv" not in loader


def test_overview_never_invokes_the_full_database_diagnostic() -> None:
    from friday.admin_api._overview import _overview_sync

    source = inspect.getsource(_overview_sync)
    assert '"database": storage.diagnostics()' not in source
    assert '"PRAGMA integrity_check"' not in source
    assert '"integrity_check": "not_run"' in source
    assert "storage.list_backups(limit=5)" in source


def test_admin_transcript_reads_leave_the_serving_event_loop() -> None:
    from friday.admin_api._conversations import (
        chat_feed,
        chat_thread,
        conversation_messages,
        list_all_conversations,
    )

    for endpoint in (chat_feed, chat_thread, conversation_messages, list_all_conversations):
        source = inspect.getsource(endpoint)
        assert "await run_blocking(" in source
        assert "storage." not in source
