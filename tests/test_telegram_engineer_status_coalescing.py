"""Structured Engineer notifications edit one Telegram status message."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig


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


class _Telegram:
    def __init__(self, final_messages: list[str]) -> None:
        self.final_messages = final_messages
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        method = url.rsplit("/", 1)[-1]
        payload = dict(kwargs.get("json") or {})
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

    async def backend_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"items": items, "count": len(items)}

    async def send_message(_client: object, _chat_id: int, text: str, **_kwargs: Any) -> None:
        final_messages.append(text)

    async def ack(
        _backend: object,
        _signer: str,
        sent: list[str],
        failed: list[str],
        **_kwargs: Any,
    ) -> None:
        acknowledgements.append((list(sent), list(failed)))

    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_send_message", send_message)
    monkeypatch.setattr(bridge, "_ack_outbound", ack)
    try:
        await bridge._drain_outbound(telegram, object())  # type: ignore[arg-type]  # noqa: SLF001
        snapshot = bridge._inbox.telegram_status_message(5001, f"engineer:{job_id}")  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert [method for method, _payload in telegram.calls] == ["sendMessage", "editMessageText"]
    running = telegram.calls[0][1]["text"]
    assert "Контрольный замер: 1 мин 15 с" in running
    assert "stdout 2.0 КиБ" in running and "stderr 17 Б" in running
    assert "оставалось 3 мин 45 с" in running
    assert "99%" not in running and "secret model output" not in running
    assert telegram.calls[1][1]["text"] == (
        f"✅ Engineer-задача завершена. Результат отправлен.\nJob: {job_id}."
    )
    assert final_messages == ["Engineer result with bounded stdout"]
    assert acknowledgements == [(["notif_progress_1", "notif_terminal_text_1"], [])]
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
            status_attempts += 1
            request = httpx.Request("POST", url)
            if status_attempts == 1:
                raise httpx.ConnectError("synthetic pre-accept failure", request=request)
            return httpx.Response(
                200,
                json={"ok": True, "result": {"message_id": 702}},
                request=request,
            )

    async def backend_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"items": [item], "count": 1}

    async def send_message(_client: object, _chat_id: int, text: str, **_kwargs: Any) -> None:
        final_messages.append(text)

    async def ack(
        _backend: object,
        _signer: str,
        sent: list[str],
        _failed: list[str],
        **_kwargs: Any,
    ) -> None:
        acknowledgements.append(list(sent))

    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_send_message", send_message)
    monkeypatch.setattr(bridge, "_ack_outbound", ack)
    try:
        await bridge._drain_outbound(Telegram(), object())  # type: ignore[arg-type]  # noqa: SLF001
        await bridge._drain_outbound(Telegram(), object())  # type: ignore[arg-type]  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert final_messages == ["Engineer result with bounded stdout"]
    assert status_attempts == 2
    assert acknowledgements == [["notif_terminal_text_1"]]
