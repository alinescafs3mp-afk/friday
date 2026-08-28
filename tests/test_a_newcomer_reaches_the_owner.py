"""Обещание `/start` про расширение доступа получило исполнителя.

`/start` говорил новичку «владелец может расширить доступ», а механизма В ЧАТЕ не
было вовсе: маршрут смены пресета существует, но владелец о новичке не узнавал
ниоткуда, кроме админки. Обещание без исполнителя — тот же мёртвый конец, что и
кнопка без обработчика; в этом проекте класс называется «у мёртвой цепочки два
конца», и здесь не хватало ровно второго.

"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig

OWNER_CHAT = 5001
NEWCOMER_CHAT = 7007
NEWCOMER_ID = "usr_newcomer"


class _Telegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        if url.endswith("/sendMessage"):
            self.sent.append(dict(kwargs.get("json") or {}))
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 1}}, request=httpx.Request("POST", url)
        )

    def buttons(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for payload in self.sent:
            markup = payload.get("reply_markup")
            if isinstance(markup, str):
                markup = json.loads(markup)
            if isinstance(markup, dict):
                for row in markup.get("inline_keyboard", []):
                    found.extend(row)
        return found


class _Backend:
    def __init__(self, preset: str = "newcomer") -> None:
        self.preset = preset
        self.calls: list[str] = []
        self.bodies: list[dict[str, Any]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(f"{method} {url.split('/api/', 1)[-1]}")
        body = kwargs.get("content")
        if body:
            self.bodies.append(json.loads(body))
        return httpx.Response(
            200,
            json={"actor": {"user_id": NEWCOMER_ID, "preset_key": self.preset}, "user": {}},
            request=httpx.Request(method, url),
        )

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)


def _bridge(tmp_path):
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[OWNER_CHAT, NEWCOMER_CHAT],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


def _start(chat_id: int) -> dict[str, Any]:
    return {
        "update_id": 990,
        "message": {
            "message_id": 3,
            "chat": {"id": chat_id},
            "from": {"id": chat_id, "first_name": "Новичок"},
            "text": "/start",
        },
    }


@pytest.mark.asyncio
async def test_the_owner_hears_about_a_newcomer_and_gets_a_button(tmp_path):
    """Мутация: не звать `_offer_access_to_owner` — краснеет."""

    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend(preset="newcomer")
    try:
        await bridge._process_update(telegram, backend, _start(NEWCOMER_CHAT), cached_response=None)
    finally:
        bridge._inbox.close()

    to_owner = [payload for payload in telegram.sent if payload.get("chat_id") == OWNER_CHAT]
    assert to_owner, "владелец не узнал о новичке ниоткуда, кроме админки"
    targets = [button["callback_data"] for button in telegram.buttons()]
    assert f"acc:grant:{NEWCOMER_ID}.{OWNER_CHAT}" in targets, "кнопки выдать доступ нет"


@pytest.mark.asyncio
async def test_an_ordinary_person_does_not_bother_the_owner(tmp_path):
    """Мутация: слать владельцу всегда — краснеет.

    `/start` от обычного участника — не событие: владелец получал бы уведомление
    на каждый перезапуск чужого чата."""

    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend(preset="user")
    try:
        await bridge._process_update(telegram, backend, _start(NEWCOMER_CHAT), cached_response=None)
    finally:
        bridge._inbox.close()

    to_owner = [payload for payload in telegram.sent if payload.get("chat_id") == OWNER_CHAT]
    assert not to_owner, f"владельца потревожили из-за обычного участника: {to_owner}"


@pytest.mark.asyncio
async def test_the_button_actually_grants_access(tmp_path):
    """Мутация: не звать маршрут смены пресета — краснеет.

    Право проверяет backend: мост его не дублирует и не ослабляет."""

    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend()
    press = {
        "id": "cb-grant",
        "from": {"id": OWNER_CHAT, "first_name": "Владелец"},
        "data": f"acc:grant:{NEWCOMER_ID}.{OWNER_CHAT}",
        "message": {"message_id": 9, "chat": {"id": OWNER_CHAT}},
    }
    try:
        await bridge._process_callback_query(telegram, backend, press)
    finally:
        bridge._inbox.close()

    assert f"POST admin/users/{NEWCOMER_ID}/preset" in backend.calls, (
        f"нажатие не дошло до маршрута выдачи доступа: {backend.calls}"
    )
    assert any(body.get("preset_key") == "user" for body in backend.bodies), (
        "пресет не назван — непонятно, какой доступ выдан"
    )
