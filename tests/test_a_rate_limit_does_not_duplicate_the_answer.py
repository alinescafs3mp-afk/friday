"""Ограничение частоты у Telegram не должно удваивать доставленный ответ.

`_send_message` читал только `400` (сломанная разметка). На `429`
`raise_for_status` ронял ВЕСЬ ход: обновление уходило в повтор, и уже
доставленные куски длинного ответа приходили человеку второй раз. Сам ответ
модели при этом не пересчитывался — он лежит в кеше обновления, — то есть платил
за это только читающий, дубликатами.

Telegram сам говорит, сколько ждать, в `parameters.retry_after`. Ждём столько и
повторяем ТОТ ЖЕ кусок: доставка продолжается с места остановки.

Обязательные мутации перечислены в `sol/PROPOSALS.md` #47.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig


class _RateLimited:
    """Первые `limited` вызовов sendMessage отвечают 429, дальше — успех."""

    def __init__(self, limited: int, *, retry_after: float = 2.0) -> None:
        self.limited = limited
        self.retry_after = retry_after
        self.sent: list[str] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        if not url.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {}}, request=request)
        if self.limited > 0:
            self.limited -= 1
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests: retry after 2",
                    "parameters": {"retry_after": self.retry_after},
                },
                request=request,
            )
        self.sent.append(str((kwargs.get("json") or {}).get("text") or ""))
        return httpx.Response(200, json={"ok": True, "result": {}}, request=request)


def _bridge(tmp_path):
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


@pytest.mark.asyncio
async def test_a_rate_limited_chunk_is_resent_not_the_whole_answer(tmp_path, monkeypatch):
    """Мутация: убрать обработку 429 — краснеет.

    Проверяется, что кусок ушёл РОВНО ОДИН раз и что ход не упал: падение здесь
    означало бы повтор всего обновления и дубликаты у человека."""

    slept: list[float] = []

    async def _no_real_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _no_real_sleep)
    bridge = _bridge(tmp_path)
    telegram = _RateLimited(limited=1)

    try:
        await bridge._send_message(telegram, 5001, "Короткий ответ")
    finally:
        bridge._inbox.close()

    assert telegram.sent == ["Короткий ответ"], (
        f"кусок доставлен {len(telegram.sent)} раз вместо одного: {telegram.sent}"
    )
    assert slept == [2.0], f"мост не подождал столько, сколько попросил Telegram: {slept}"


@pytest.mark.asyncio
async def test_the_wait_is_taken_from_telegram_and_bounded(tmp_path, monkeypatch):
    """Мутация: убрать потолок ожидания — краснеет.

    При жёстком лимите Telegram просит и несколько минут. Столько держать ход
    нельзя: слот занят, человек ждёт молча, а честный отказ наверх лучше."""

    slept: list[float] = []

    async def _no_real_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _no_real_sleep)
    bridge = _bridge(tmp_path)
    telegram = _RateLimited(limited=1, retry_after=600.0)

    try:
        await bridge._send_message(telegram, 5001, "Ответ")
    finally:
        bridge._inbox.close()

    assert slept and slept[0] <= TelegramBridge._RATE_LIMIT_MAX_WAIT_SEC, (
        f"мост согласился ждать {slept} с — это дольше объявленного потолка"
    )


@pytest.mark.asyncio
async def test_a_persistent_rate_limit_is_still_reported(tmp_path, monkeypatch):
    """Мутация: повторять бесконечно — краснеет.

    Молча ждать вечно хуже, чем отдать отказ: у обновления есть свой счётчик
    попыток и dead-letter, и он должен работать."""

    async def _no_real_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_real_sleep)
    bridge = _bridge(tmp_path)
    telegram = _RateLimited(limited=99)

    try:
        with pytest.raises(httpx.HTTPStatusError):
            await bridge._send_message(telegram, 5001, "Ответ")
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_broken_markup_still_falls_back_to_plain_text(tmp_path):
    """Прежнее поведение на 400 не должно было пострадать от новой ветки."""

    class _RejectsMarkup:
        def __init__(self) -> None:
            self.attempts: list[dict[str, Any]] = []

        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            request = httpx.Request("POST", url)
            payload = dict(kwargs.get("json") or {})
            self.attempts.append(payload)
            if payload.get("parse_mode") == "HTML":
                return httpx.Response(
                    400, json={"ok": False, "description": "can't parse entities"}, request=request
                )
            return httpx.Response(200, json={"ok": True, "result": {}}, request=request)

    bridge = _bridge(tmp_path)
    telegram = _RejectsMarkup()
    try:
        await bridge._send_message(telegram, 5001, "**жирный** текст")
    finally:
        bridge._inbox.close()

    assert len(telegram.attempts) == 2
    assert "parse_mode" not in telegram.attempts[1]
    assert telegram.attempts[1]["text"] == "**жирный** текст"


@pytest.mark.asyncio
async def test_a_permission_refusal_says_what_is_missing(tmp_path, monkeypatch):
    """«Действие уже недоступно» — правда только для устаревшей кнопки.

    Отказ ПО ПРАВАМ и «нет такого» — утверждения о разных вещах, а человек,
    которому не хватает права, принимал общую фразу за поломку и жал ещё раз.
    `refusal_notice` умел их различать с самого начала, но на кнопках его никто
    не звал: тот же разбор стоял только на текстовых командах.

    Мутация: вернуть общую фразу — краснеет.
    """
    from friday.telegram_bridge._base import PermanentUpdateError

    class _Telegram:
        def __init__(self) -> None:
            self.toasts: list[str] = []

        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            payload = dict(kwargs.get("json") or {})
            if url.endswith("/answerCallbackQuery"):
                self.toasts.append(str(payload.get("text") or ""))
            return httpx.Response(200, json={"ok": True, "result": {}}, request=httpx.Request("POST", url))

    bridge = _bridge(tmp_path)
    telegram = _Telegram()

    async def _forbidden(*_args, **_kwargs):
        raise PermanentUpdateError("Backend rejected update (403): forbidden", status_code=403)

    monkeypatch.setattr(bridge, "_backend_json", _forbidden)
    callback = {
        "id": "cb-403",
        "from": {"id": 5001, "first_name": "Гость"},
        "data": "inbox:promote:inb_1.5001",
        "message": {"message_id": 7, "chat": {"id": 5001}},
    }
    try:
        await bridge._process_callback_query(telegram, object(), callback)
    finally:
        bridge._inbox.close()

    assert telegram.toasts, "человеку вообще ничего не сказали"
    assert "не разрешено" in telegram.toasts[0], (
        f"отказ по правам показан общей фразой: {telegram.toasts[0]!r}"
    )
