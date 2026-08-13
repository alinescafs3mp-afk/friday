"""Cheap admin navigation is a product boundary, not a benchmark accident."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from pathlib import Path

import httpx
import pytest

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage.models import RawObject, new_id


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


def test_admin_file_reads_leave_the_serving_event_loop() -> None:
    from friday.admin_api._files import download_file, list_files

    for endpoint in (list_files, download_file):
        source = inspect.getsource(endpoint)
        assert "await run_blocking(" in source
        assert "storage." not in source


@pytest.mark.asyncio
async def test_admin_file_page_does_not_block_the_event_loop(settings, monkeypatch) -> None:
    """The loop must advance while the SQLite-backed file page is being collected.

    The response assertions pin the existing pagination and privacy projection too:
    moving the route to a worker must not change what an authorized administrator
    can see, or leak the storage-only provenance that the projection removes.
    """

    from friday.server import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        storage = app.state.storage
        sentinel = "SYNTHETIC-ADMIN-FILE-PRIVATE-b8b65c"
        made: list[str] = []
        for index in range(2):
            raw = storage.store_raw_object(
                RawObject(
                    id=new_id("raw"),
                    user_id=LEGACY_OWNER_USER_ID,
                    source="test",
                    source_ref=f"{sentinel}/source-{index}",
                    raw_content=f"private body {sentinel} {index}",
                    content_type="file",
                    content_hash=f"admin-file-latency-{index}",
                    metadata_json={
                        "filename": f"visible-{index}.txt",
                        "mime_type": "text/plain",
                        "size_bytes": 10 + index,
                        "stored_path": f"private/{sentinel}/{index}.txt",
                        "uploaded_by": sentinel,
                    },
                    received_at=f"2026-08-13T10:00:0{index}+00:00",
                )
            )
            made.append(raw.id)

        real_execute = storage.execute
        loop = asyncio.get_running_loop()
        loop_progress = asyncio.Event()
        release_query = threading.Event()
        handshake_succeeded: list[bool] = []
        query_threads: list[int] = []

        def _slow_file_query(sql: str, params=()):
            if "FROM raw_objects r" in sql and "content_type='file'" in sql:
                query_threads.append(threading.get_ident())
                if not handshake_succeeded:
                    loop.call_soon_threadsafe(loop_progress.set)
                    handshake_succeeded.append(release_query.wait(2.0))
            return real_execute(sql, params)

        monkeypatch.setattr(storage, "execute", _slow_file_query)

        async def _release_after_loop_progress() -> None:
            await loop_progress.wait()
            release_query.set()

        release_task = asyncio.create_task(_release_after_loop_progress())
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9000))
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
                response = await client.get(
                    "/api/admin/files",
                    params={"user_id": LEGACY_OWNER_USER_ID, "limit": 1, "offset": 0},
                    headers={"Authorization": f"Bearer {settings.api_token}"},
                )
        finally:
            release_query.set()
            loop_progress.set()
            await release_task

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["count"] == 1
        assert payload["total"] == 2
        assert payload["limit"] == 1
        assert payload["offset"] == 0
        assert payload["items"][0]["id"] == made[-1]
        assert payload["items"][0]["metadata"] == {
            "filename": "visible-1.txt",
            "mime_type": "text/plain",
            "size_bytes": 11,
        }
        assert sentinel not in json.dumps(payload, ensure_ascii=False)
        assert handshake_succeeded == [True], "SQLite file listing held the serving event loop"
        assert query_threads and all(thread_id != threading.get_ident() for thread_id in query_threads)
