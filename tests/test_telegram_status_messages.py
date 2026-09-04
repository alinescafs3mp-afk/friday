"""Durable one-message Telegram progress transport."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from friday.telegram_bridge import _UpdateInbox
from friday.telegram_bridge._status import (
    TelegramStatusMessageManager,
    TelegramStatusStage,
    build_chat_operation_progress,
    render_chat_status,
)


def _manager(inbox: _UpdateInbox) -> TelegramStatusMessageManager:
    return TelegramStatusMessageManager(inbox, api_url="https://telegram.invalid/bot-redacted")


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _payload(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))


@pytest.mark.asyncio
async def test_status_sends_once_then_edits_same_persisted_message_after_restart(tmp_path) -> None:
    db_path = tmp_path / "telegram.sqlite3"
    calls: list[tuple[str, dict[str, Any]]] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        payload = _payload(request)
        calls.append((request.url.path.rsplit("/", 1)[-1], payload))
        message_id = 771 if calls[-1][0] == "sendMessage" else payload["message_id"]
        return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})

    inbox = _UpdateInbox(str(db_path))
    async with _client(telegram) as client:
        assert (
            await _manager(inbox).publish(
                client,
                5001,
                "chat:8801",
                1,
                render_chat_status(TelegramStatusStage.RECEIVING_MEDIA, 12),
                reply_to_message_id=91,
            )
            == "sent"
        )
        assert (
            await _manager(inbox).publish(
                client,
                5001,
                "chat:8801",
                2,
                render_chat_status(TelegramStatusStage.BACKEND_WAIT, 18),
            )
            == "edited"
        )
    inbox.close()

    reopened = _UpdateInbox(str(db_path))
    async with _client(telegram) as client:
        assert (
            await _manager(reopened).publish(
                client,
                5001,
                "chat:8801",
                3,
                render_chat_status(TelegramStatusStage.DELIVERING_RESULT, 24),
            )
            == "edited"
        )
    try:
        assert calls[0][0] == "sendMessage"
        assert calls[0][1]["reply_parameters"] == {
            "message_id": 91,
            "allow_sending_without_reply": True,
        }
        assert "parse_mode" not in calls[0][1]
        assert [method for method, _payload_value in calls] == [
            "sendMessage",
            "editMessageText",
            "editMessageText",
        ]
        assert [payload["message_id"] for _method, payload in calls[1:]] == [771, 771]
        assert reopened.telegram_status_message(5001, "chat:8801") == {
            "message_id": 771,
            "revision": 3,
            "terminal": False,
        }
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_stale_revisions_are_ignored_and_terminal_revision_is_absorbing(tmp_path) -> None:
    inbox = _UpdateInbox(str(tmp_path / "telegram.sqlite3"))
    calls: list[str] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append(method)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 411}})

    manager = _manager(inbox)
    async with _client(telegram) as client:
        assert await manager.publish(client, 5001, "job:alpha", 4, "⏳ Технический этап.") == "sent"
        assert await manager.publish(client, 5001, "job:alpha", 4, "⏳ Устаревший этап.") == "stale"
        assert await manager.publish(client, 5001, "job:alpha", 3, "⏳ Старый этап.") == "stale"
        assert (
            await manager.publish(
                client,
                5001,
                "job:alpha",
                5,
                render_chat_status(TelegramStatusStage.COMPLETE, 20),
                terminal=True,
            )
            == "edited"
        )
        assert await manager.publish(client, 5001, "job:alpha", 99, "⏳ Поздний этап.") == "terminal"
    try:
        assert calls == ["sendMessage", "editMessageText"]
        assert inbox.telegram_status_message(5001, "job:alpha") == {
            "message_id": 411,
            "revision": 5,
            "terminal": True,
        }
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_edit_rejection_falls_back_to_new_message_and_replaces_persisted_id(tmp_path) -> None:
    inbox = _UpdateInbox(str(tmp_path / "telegram.sqlite3"))
    calls: list[str] = []
    send_ids = iter((611, 612))

    def telegram(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "editMessageText":
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "message to edit not found",
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": next(send_ids)}})

    manager = _manager(inbox)
    async with _client(telegram) as client:
        assert await manager.publish(client, -1005001, "job:replace", 1, "⏳ Первый этап.") == "sent"
        assert await manager.publish(client, -1005001, "job:replace", 2, "⏳ Второй этап.") == "replaced"
    try:
        assert calls == ["sendMessage", "editMessageText", "sendMessage"]
        assert inbox.telegram_status_message(-1005001, "job:replace") == {
            "message_id": 612,
            "revision": 2,
            "terminal": False,
        }
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_ambiguous_edit_failure_does_not_create_a_duplicate_status(tmp_path) -> None:
    inbox = _UpdateInbox(str(tmp_path / "telegram.sqlite3"))
    calls: list[str] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "editMessageText":
            raise httpx.ReadTimeout("accepted response may be lost", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 811}})

    manager = _manager(inbox)
    async with _client(telegram) as client:
        assert await manager.publish(client, 5001, "job:uncertain", 1, "⏳ Первый этап.") == "sent"
        with pytest.raises(httpx.ReadTimeout):
            await manager.publish(client, 5001, "job:uncertain", 2, "⏳ Второй этап.")
    try:
        assert calls == ["sendMessage", "editMessageText"]
        assert inbox.telegram_status_message(5001, "job:uncertain") == {
            "message_id": 811,
            "revision": 1,
            "terminal": False,
        }
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_ambiguous_initial_send_is_fenced_across_restart_without_blind_retry(tmp_path) -> None:
    db_path = tmp_path / "telegram.sqlite3"
    calls: list[str] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        raise httpx.ReadTimeout("accepted response may be lost", request=request)

    inbox = _UpdateInbox(str(db_path))
    async with _client(telegram) as client:
        assert (
            await _manager(inbox).publish(
                client,
                5001,
                "chat:ambiguous-create",
                1,
                "⏳ Первый этап.",
            )
            == "uncertain"
        )
        assert _manager(inbox).snapshot(5001, "chat:ambiguous-create") == {
            "revision": 1,
            "terminal": False,
            "ambiguous": True,
        }
    inbox.close()

    reopened = _UpdateInbox(str(db_path))
    async with _client(telegram) as client:
        assert (
            await _manager(reopened).publish(
                client,
                5001,
                "chat:ambiguous-create",
                2,
                "⏳ Следующий этап.",
            )
            == "uncertain"
        )
    try:
        assert calls == ["sendMessage"]
        assert reopened.telegram_status_send_fence(5001, "chat:ambiguous-create") == {
            "revision": 1,
        }
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_proven_connect_rejection_disarms_initial_send_for_safe_retry(tmp_path) -> None:
    inbox = _UpdateInbox(str(tmp_path / "telegram.sqlite3"))
    calls = 0

    def telegram(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("not connected", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 901}})

    manager = _manager(inbox)
    async with _client(telegram) as client:
        with pytest.raises(httpx.ConnectError):
            await manager.publish(client, 5001, "chat:retry-create", 1, "⏳ Первый этап.")
        assert inbox.telegram_status_send_fence(5001, "chat:retry-create") is None
        assert await manager.publish(client, 5001, "chat:retry-create", 2, "⏳ Второй этап.") == "sent"
    try:
        assert calls == 2
        assert inbox.telegram_status_message(5001, "chat:retry-create") == {
            "message_id": 901,
            "revision": 2,
            "terminal": False,
        }
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_ambiguous_replacement_send_is_fenced_without_blind_retry(tmp_path) -> None:
    inbox = _UpdateInbox(str(tmp_path / "telegram.sqlite3"))
    calls: list[str] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append(method)
        if calls == ["sendMessage"]:
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 611}})
        if method == "editMessageText":
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "message to edit not found",
                },
            )
        raise httpx.ReadTimeout("replacement response may be lost", request=request)

    manager = _manager(inbox)
    async with _client(telegram) as client:
        assert (
            await manager.publish(
                client,
                5001,
                "chat:ambiguous-replacement",
                1,
                "⏳ Первый этап.",
            )
            == "sent"
        )
        assert (
            await manager.publish(
                client,
                5001,
                "chat:ambiguous-replacement",
                2,
                "⏳ Второй этап.",
            )
            == "uncertain"
        )
        assert (
            await manager.publish(
                client,
                5001,
                "chat:ambiguous-replacement",
                3,
                "✅ Готово.",
                terminal=True,
            )
            == "uncertain"
        )
    try:
        assert calls == ["sendMessage", "editMessageText", "sendMessage"]
        assert inbox.telegram_status_send_fence(5001, "chat:ambiguous-replacement") == {
            "revision": 2,
        }
        assert inbox.telegram_status_message(5001, "chat:ambiguous-replacement") == {
            "message_id": 611,
            "revision": 1,
            "terminal": False,
        }
    finally:
        inbox.close()


def test_status_renderer_contains_only_closed_stage_and_exact_elapsed_time() -> None:
    prompt = "секретный пользовательский запрос"
    model_output = "ответ модели 87% готов, ETA 5 минут"
    texts = [render_chat_status(stage, 75) for stage in TelegramStatusStage]

    assert all(prompt not in text and model_output not in text for text in texts)
    assert all("ETA" not in text for text in texts)
    assert all(token in {"0%", "100%"} for text in texts for token in re.findall(r"\d+%", text))
    assert "Прошло: 1 мин 15 с" in texts[0]
    backend = render_chat_status(TelegramStatusStage.BACKEND_WAIT, 75)
    assert "ядро обрабатывает запрос" in backend
    assert "63%" not in backend
    assert render_chat_status(TelegramStatusStage.COMPLETE, 75).startswith("✅")
    assert render_chat_status(TelegramStatusStage.STOPPED, 75).startswith("⏹")


def test_chat_status_projection_uses_measured_file_counts_without_eta() -> None:
    projection = build_chat_operation_progress(
        TelegramStatusStage.STAGING_DOCUMENTS,
        42,
        item_total=4,
        received_items=4,
        received_bytes=2048,
        staged_items=1,
        staged_bytes=512,
        operation_id="chat:8801",
        authenticated_turn_id="chat:8801",
        revision=2,
    )
    text = render_chat_status(
        TelegramStatusStage.STAGING_DOCUMENTS,
        42,
        item_total=4,
        received_items=4,
        staged_items=1,
        operation_id="chat:8801",
        authenticated_turn_id="chat:8801",
        revision=2,
    )
    assert projection.active_step_id == "staging_documents"
    assert projection.ordered_steps[0].state.value == "completed"
    assert projection.ordered_steps[1].completed_units == 1
    assert projection.ordered_steps[1].total_units == 4
    assert projection.ordered_steps[1].percentage is None
    assert "1 из 4 файлов" in text
    assert "ETA" not in text
    assert "63%" not in text
    assert text.startswith("⏳ Выполняю задачу")
