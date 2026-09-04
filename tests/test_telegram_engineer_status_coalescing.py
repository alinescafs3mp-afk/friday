"""Structured Engineer notifications edit one Telegram status message."""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig
from friday.telegram_bridge import _transport as bridge_transport
from friday.telegram_bridge._status import render_engineer_status


def _bridge(tmp_path) -> TelegramBridge:  # noqa: ANN001
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


def _progress(job_id: str) -> dict[str, Any]:
    return {
        "id": "notif_progress_1",
        "chat_id": "5001",
        "body": "untrusted compatibility body: 99% ETA secret model output",
        "kind": "engineer_command_progress",
        "dedup_key": f"engineer-progress:v1:{job_id}:60",
        "status_update": {
            "schema": "friday.telegram-status.v1",
            "operation_id": f"engineer:{job_id}",
            "revision": 60,
            "terminal": False,
            "stage": "command_running",
            "elapsed_sec": 75,
            "timeout_sec": 300,
            "remaining_sec": 225,
            "stdout_bytes": 2048,
            "stderr_bytes": 17,
            "output_activity": True,
        },
    }


def _terminal_text(job_id: str) -> dict[str, Any]:
    return {
        "id": "notif_terminal_text_1",
        "chat_id": "5001",
        "body": "Engineer result with bounded stdout",
        "kind": "engineer_command_terminal_text",
        "dedup_key": f"engineer-terminal:text:{job_id}:{'7' * 64}",
        "status_update": {
            "schema": "friday.telegram-status.v1",
            "operation_id": f"engineer:{job_id}",
            "revision": (1 << 63) - 1,
            "terminal": True,
            "stage": "completed",
        },
    }


def _unknown(job_id: str) -> dict[str, Any]:
    return {
        "id": "notif_unknown_1",
        "chat_id": "5001",
        "body": "Engineer state is truthfully unknown",
        "kind": "engineer_command_unknown",
        "dedup_key": f"engineer-unknown:v1:{job_id}:{'8' * 64}",
        "status_update": {
            "schema": "friday.telegram-status.v1",
            "operation_id": f"engineer:{job_id}",
            "revision": (1 << 63) - 1,
            "terminal": True,
            "stage": "unknown",
        },
    }


class _Telegram:
    def __init__(self, final_messages: list[str]) -> None:
        self.final_messages = final_messages
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        method = url.rsplit("/", 1)[-1]
        payload = dict(kwargs.get("json") or {})
        if method == "sendMessage" and payload.get("text") == "Engineer result with bounded stdout":
            self.final_messages.append(str(payload["text"]))
        if method == "editMessageText" and payload["text"].startswith("✅"):
            assert self.final_messages == ["Engineer result with bounded stdout"]
        self.calls.append((method, payload))
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 701}}, request=request)


@pytest.mark.asyncio
async def test_progress_and_no_file_terminal_share_one_status_without_body_leakage(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    job_id = "a" * 32
    items = [_progress(job_id), _terminal_text(job_id)]
    final_messages: list[str] = []
    acknowledgements: list[tuple[list[str], list[str]]] = []
    telegram = _Telegram(final_messages)

    async def backend_json(*args: Any, **_kwargs: Any) -> dict[str, Any]:
        path = str(args[2])
        if path.endswith("/claim"):
            notification_id = path.split("/")[-2]
            return {"item": next(item for item in items if item["id"] == notification_id)}
        pointers = [{key: item[key] for key in ("id", "chat_id", "kind", "dedup_key")} for item in items]
        return {"items": pointers, "count": len(pointers)}

    async def ack(
        _backend: object,
        _signer: str,
        sent: list[str],
        failed: list[str],
        **_kwargs: Any,
    ) -> None:
        acknowledgements.append((list(sent), list(failed)))

    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_ack_outbound", ack)
    try:
        await bridge._drain_outbound(telegram, object())  # type: ignore[arg-type]  # noqa: SLF001
        snapshot = bridge._inbox.telegram_status_message(5001, f"engineer:{job_id}")  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert [method for method, _payload in telegram.calls] == [
        "sendMessage",
        "sendMessage",
        "editMessageText",
    ]
    running = telegram.calls[0][1]["text"]
    assert running == render_engineer_status(items[0]["status_update"])
    assert "Прошло: 1 мин 15 с" in running
    assert "stdout 2.0 КиБ" in running and "stderr 17 Б" in running
    assert "Тайм-аут: осталось 3 мин 45 с" in running
    assert "99%" not in running and "secret model output" not in running
    assert telegram.calls[1][1]["text"] == "Engineer result with bounded stdout"
    assert telegram.calls[2][1]["text"] == render_engineer_status(items[1]["status_update"])
    assert final_messages == ["Engineer result with bounded stdout"]
    assert acknowledgements == [(["notif_progress_1", "notif_terminal_text_1"], [])]
    assert snapshot == {
        "message_id": 701,
        "revision": (1 << 63) - 1,
        "terminal": True,
    }


@pytest.mark.asyncio
async def test_unknown_terminally_replaces_the_prior_running_status(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)
    job_id = "d" * 32
    items = [_progress(job_id), _unknown(job_id)]
    telegram = _Telegram([])

    async def backend_json(*args: Any, **_kwargs: Any) -> dict[str, Any]:
        path = str(args[2])
        if path.endswith("/claim"):
            notification_id = path.split("/")[-2]
            return {"item": next(item for item in items if item["id"] == notification_id)}
        return {
            "items": [{key: item[key] for key in ("id", "chat_id", "kind", "dedup_key")} for item in items],
            "count": len(items),
        }

    async def ack(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_ack_outbound", ack)
    try:
        await bridge._drain_outbound(telegram, object())  # type: ignore[arg-type]  # noqa: SLF001
        snapshot = bridge._inbox.telegram_status_message(5001, f"engineer:{job_id}")  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert [method for method, _payload in telegram.calls] == [
        "sendMessage",
        "sendMessage",
        "editMessageText",
    ]
    assert telegram.calls[1][1]["text"] == "Engineer state is truthfully unknown"
    assert telegram.calls[2][1]["text"] == render_engineer_status(items[1]["status_update"])
    assert snapshot == {
        "message_id": 701,
        "revision": (1 << 63) - 1,
        "terminal": True,
    }


@pytest.mark.asyncio
async def test_delivered_no_file_result_retries_only_missing_terminal_status(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    item = _terminal_text("b" * 32)
    final_messages: list[str] = []
    acknowledgements: list[list[str]] = []
    status_attempts = 0

    class Telegram:
        async def post(self, url: str, **_kwargs: Any) -> httpx.Response:
            nonlocal status_attempts
            payload = dict(_kwargs.get("json") or {})
            request = httpx.Request("POST", url)
            if payload.get("text") == "Engineer result with bounded stdout":
                final_messages.append(str(payload["text"]))
                return httpx.Response(
                    200,
                    json={"ok": True, "result": {"message_id": 701}},
                    request=request,
                )
            status_attempts += 1
            if status_attempts == 1:
                raise httpx.ConnectError("synthetic pre-accept failure", request=request)
            return httpx.Response(
                200,
                json={"ok": True, "result": {"message_id": 702}},
                request=request,
            )

    async def backend_json(*args: Any, **_kwargs: Any) -> dict[str, Any]:
        if str(args[2]).endswith("/claim"):
            return {"item": item}
        pointer = {key: item[key] for key in ("id", "chat_id", "kind", "dedup_key")}
        return {"items": [pointer], "count": 1}

    async def ack(
        _backend: object,
        _signer: str,
        sent: list[str],
        _failed: list[str],
        **_kwargs: Any,
    ) -> None:
        acknowledgements.append(list(sent))

    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_ack_outbound", ack)
    try:
        await bridge._drain_outbound(Telegram(), object())  # type: ignore[arg-type]  # noqa: SLF001
        await bridge._drain_outbound(Telegram(), object())  # type: ignore[arg-type]  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert final_messages == ["Engineer result with bounded stdout"]
    assert status_attempts == 2
    assert acknowledgements == [["notif_terminal_text_1"]]


@pytest.mark.asyncio
async def test_terminal_outcome_reconciliation_rechecks_chat_before_status_create(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    job_id = "c" * 32
    payload = b"PK\x03\x04result"
    notification_id = "notif_terminal_reconcile"
    digest = hashlib.sha256(payload).hexdigest()
    item = {
        "id": notification_id,
        "chat_id": "5001",
        "kind": "engineer_command_terminal",
        "dedup_key": f"engineer-terminal:archive:{job_id}:{digest}",
        "caption": "Engineer result archive",
        "artifact": {
            "filename": "result.zip",
            "mime_type": "application/zip",
            "size_bytes": len(payload),
            "sha256": digest,
            "path": f"/api/notifications/{notification_id}/artifact",
        },
        "status_update": {
            "schema": "friday.telegram-status.v1",
            "operation_id": f"engineer:{job_id}",
            "revision": (1 << 63) - 1,
            "terminal": True,
            "stage": "completed",
        },
    }
    envelope = bridge_transport._engineer_terminal_envelope(  # noqa: SLF001
        item,
        chat_id=5001,
        max_document_bytes=bridge.config.max_document_bytes,
    )
    assert envelope is not None
    fence_key = str(envelope["fence_key"])
    assert bridge._inbox.begin_notification_part_delivery(notification_id, fence_key) == "armed"  # noqa: SLF001
    assert bridge._inbox.confirm_notification_part_delivery(notification_id, fence_key)  # noqa: SLF001
    bridge._inbox.remember_notification_delivery_outcome(notification_id, "sent")  # noqa: SLF001
    telegram_calls: list[str] = []
    acknowledgements: list[tuple[list[str], list[str]]] = []

    class Telegram:
        async def post(self, url: str, **_kwargs: Any) -> httpx.Response:
            telegram_calls.append(url.rsplit("/", 1)[-1])
            return httpx.Response(200, request=httpx.Request("POST", url))

    async def backend_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"items": [item], "count": 1}

    async def ack(
        _backend: object,
        _signer: str,
        sent: list[str],
        failed: list[str],
        **_kwargs: Any,
    ) -> None:
        acknowledgements.append((list(sent), list(failed)))

    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_ack_outbound", ack)
    monkeypatch.setattr(bridge, "_may_message_chat", lambda _chat_id: False)
    try:
        await bridge._drain_outbound(Telegram(), object())  # type: ignore[arg-type]  # noqa: SLF001
        outcomes = bridge._inbox.notification_delivery_outcomes()  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert telegram_calls == []
    assert acknowledgements == []
    assert outcomes == {notification_id: "sent"}
