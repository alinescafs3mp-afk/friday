"""Bounded Telegram progress for opaque, potentially long /api/chat turns."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx
import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig
from friday.telegram_bridge import _commands as commands


def _bridge(tmp_path) -> TelegramBridge:  # noqa: ANN001
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


def _update(text: str, *, update_id: int = 8801) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 91,
            "chat": {"id": 5001, "type": "private"},
            "from": {"id": 5001, "first_name": "Owner"},
            "text": text,
        },
    }


def _client_stub() -> httpx.AsyncClient:
    """Typed opaque client: every network seam is replaced in these tests."""

    return cast(httpx.AsyncClient, object())


async def _never_typing(*_args: Any, **_kwargs: Any) -> None:
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_decompile_turn_gets_two_elapsed_time_notices_without_fake_phase_or_percent(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    sent: list[tuple[str, dict[str, Any]]] = []
    backend_payloads: list[dict[str, Any]] = []

    async def immediate_progress_delay(_delay: float) -> None:
        return None

    async def backend_json(
        _client: object,
        method: str,
        path: str,
        payload: dict[str, Any],
        _user: str,
        _chat: str,
    ) -> dict[str, Any]:
        assert method == "POST" and path == "/api/chat"
        backend_payloads.append(dict(payload))
        # Yield exactly once so the independently scheduled notifier owns the
        # loop; its injected delay completes synchronously and deterministically.
        await asyncio.sleep(0)
        return {"message": "Готово", "message_format": "plain"}

    async def send(_client: object, _chat_id: int, text: str, **kwargs: Any) -> None:
        sent.append((text, kwargs))

    monkeypatch.setattr(commands, "_progress_sleep", immediate_progress_delay)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_typing_loop", _never_typing)
    monkeypatch.setattr(bridge, "_send_message", send)
    try:
        await bridge._process_update(  # noqa: SLF001
            _client_stub(),
            _client_stub(),
            _update("Декомпилируй его."),
            cached_response=None,
        )
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert len(backend_payloads) == 1
    assert [item[0] for item in sent[-1:]] == ["Готово"]
    progress = sent[:-1]
    assert len(progress) == 2
    assert "четырьмя минутами" in progress[0][0]
    assert "прошло около минуты" in progress[1][0].casefold()
    assert all("%" not in text and "готов" not in text.casefold() for text, _ in progress)
    assert all(kwargs["text_format"] == "plain" for _text, kwargs in progress)
    assert all(kwargs["reply_to_message_id"] == 91 for _text, kwargs in progress)


@pytest.mark.asyncio
async def test_opaque_chat_turn_gets_one_generic_notice_without_invented_percent(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    sent: list[str] = []

    async def immediate_progress_delay(_delay: float) -> None:
        return None

    async def backend_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"message": "Итог", "message_format": "plain"}

    async def send(_client: object, _chat_id: int, text: str, **_kwargs: Any) -> None:
        sent.append(text)

    monkeypatch.setattr(commands, "_progress_sleep", immediate_progress_delay)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_typing_loop", _never_typing)
    monkeypatch.setattr(bridge, "_send_message", send)
    try:
        await bridge._process_update(  # noqa: SLF001
            _client_stub(),
            _client_stub(),
            _update("Проверь этот документ", update_id=8802),
            cached_response=None,
        )
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert sent[-1] == "Итог"
    assert len(sent[:-1]) == 1
    assert "ещё выполняется" in sent[0]
    assert "%" not in sent[0]


@pytest.mark.asyncio
async def test_progress_is_cancelled_before_the_final_answer_can_proceed(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    progress_waiting = asyncio.Event()
    progress_cancelled = asyncio.Event()
    sent: list[str] = []

    async def blocked_progress_delay(_delay: float) -> None:
        progress_waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            progress_cancelled.set()

    async def backend_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        await progress_waiting.wait()
        return {"message": "Итог", "message_format": "plain"}

    async def send(_client: object, _chat_id: int, text: str, **_kwargs: Any) -> None:
        if text == "Итог":
            assert progress_cancelled.is_set()
        sent.append(text)

    monkeypatch.setattr(commands, "_progress_sleep", blocked_progress_delay)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_typing_loop", _never_typing)
    monkeypatch.setattr(bridge, "_send_message", send)
    try:
        await bridge._process_update(  # noqa: SLF001
            _client_stub(),
            _client_stub(),
            _update("Обычный долгий запрос", update_id=8803),
            cached_response=None,
        )
        await asyncio.sleep(0)
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert sent == ["Итог"]


@pytest.mark.asyncio
async def test_progress_delivery_failure_never_fails_the_backend_turn(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    final_messages: list[str] = []

    async def immediate_progress_delay(_delay: float) -> None:
        return None

    async def backend_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"message": "Итог сохранён", "message_format": "plain"}

    async def send(_client: object, _chat_id: int, text: str, **_kwargs: Any) -> None:
        if text.startswith("⏳"):
            raise RuntimeError("synthetic Telegram outage")
        final_messages.append(text)

    monkeypatch.setattr(commands, "_progress_sleep", immediate_progress_delay)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_typing_loop", _never_typing)
    monkeypatch.setattr(bridge, "_send_message", send)
    try:
        await bridge._process_update(  # noqa: SLF001
            _client_stub(),
            _client_stub(),
            _update("Собери подробный ответ", update_id=8804),
            cached_response=None,
        )
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert final_messages == ["Итог сохранён"]


@pytest.mark.asyncio
async def test_recovery_calls_share_one_finite_decompile_notice_budget(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    sent: list[str] = []
    backend_calls = 0

    async def immediate_progress_delay(_delay: float) -> None:
        return None

    async def backend_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal backend_calls
        backend_calls += 1
        await asyncio.sleep(0)
        return {"message": "ok"}

    async def send(_client: object, _chat_id: int, text: str, **_kwargs: Any) -> None:
        sent.append(text)

    monkeypatch.setattr(commands, "_progress_sleep", immediate_progress_delay)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_send_message", send)
    state = commands._ChatProgressState("Декомпилируй этот файл")
    try:
        for _ in range(2):
            await commands._final_chat_request_with_progress(
                bridge,
                _client_stub(),
                _client_stub(),
                {"message": "Декомпилируй этот файл"},
                "5001",
                5001,
                91,
                state,
            )
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert backend_calls == 2
    assert len(sent) == 2
