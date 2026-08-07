"""Запись, созданную из чата, можно удалить из чата же.

`PATCH` и `DELETE` живут в `friday/api/knowledge.py:168-204` с самого начала, а
мост звал только `GET`. Человек говорил «запомни», тут же замечал ошибку — и шёл
в админку, при том что Telegram основной интерфейс владельца и закон проекта
(`sol/SOL.md` §1.6) требует обратного: новая возможность сначала работает в чате.

Удаление мягкое и обратимое, но подтверждение всё равно спрашивается: одно
нажатие мимо не должно уносить запись.

Правка ТЕКСТА записи в этот заход намеренно не делается, и причина не в объёме.
Она требует захвата следующей реплики как ответа на конкретное сообщение, а
чтение `reply_to_message` в мосте сейчас отсутствует вовсе (отдельная находка
разведки). Сделать правку раньше означало бы завести второй механизм ввода и
выбросить его, когда появится первый.

Обязательные мутации перечислены в `sol/PROPOSALS.md` #46.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig

DOCUMENT_ID = "ko_0000000000000042"


class _Telegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((url, dict(kwargs.get("json") or {})))
        return httpx.Response(200, json={"ok": True, "result": {}}, request=httpx.Request("POST", url))

    def sent(self) -> list[dict[str, Any]]:
        return [payload for url, payload in self.calls if url.endswith("/sendMessage")]

    def buttons(self) -> list[dict[str, Any]]:
        found = []
        for payload in self.sent():
            markup = payload.get("reply_markup")
            if isinstance(markup, str):
                markup = json.loads(markup)
            if isinstance(markup, dict):
                for row in markup.get("inline_keyboard", []):
                    found.extend(row)
        return found


class _Backend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(f"{method} {url.split('/api/', 1)[-1]}")
        request = httpx.Request(method, url)
        if method == "DELETE":
            return httpx.Response(200, json={"status": "soft_deleted"}, request=request)
        return httpx.Response(
            200,
            json={"item": {"id": DOCUMENT_ID, "title": "Заметка", "content": "Текст заметки"}},
            request=request,
        )

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)


def _bridge(tmp_path):
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


def _press(data: str) -> dict[str, Any]:
    return {
        "id": f"cb-{data}",
        "from": {"id": 5001, "first_name": "Владелец"},
        "data": data,
        "message": {"message_id": 12, "chat": {"id": 5001}},
    }


@pytest.mark.asyncio
async def test_an_opened_record_offers_deletion(tmp_path):
    """Мутация: убрать кнопку у открытого документа — краснеет.

    Кнопка едет ВМЕСТЕ с документом: искать идентификатор глазами и набирать
    команду — не тот интерфейс, ради которого чат основной."""

    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend()
    try:
        await bridge._process_callback_query(telegram, backend, _press(f"doc:show:{DOCUMENT_ID}"))
    finally:
        bridge._inbox.close()

    targets = [button["callback_data"] for button in telegram.buttons()]
    assert f"know:del:{DOCUMENT_ID}" in targets, (
        "у открытой записи нет кнопки удаления — из чата её по-прежнему не убрать"
    )


@pytest.mark.asyncio
async def test_the_first_press_asks_instead_of_deleting(tmp_path):
    """Мутация: удалять сразу по `know:del` — краснеет.

    Удаление мягкое, но человек об этом не знает, а кнопка стоит рядом с текстом:
    промах пальцем не должен уносить запись."""

    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend()
    try:
        await bridge._process_callback_query(telegram, backend, _press(f"know:del:{DOCUMENT_ID}"))
    finally:
        bridge._inbox.close()

    assert not [call for call in backend.calls if call.startswith("DELETE")], (
        "первое нажатие уже удалило запись, ничего не спросив"
    )
    targets = [button["callback_data"] for button in telegram.buttons()]
    assert f"know:delok:{DOCUMENT_ID}" in targets, "подтверждения не предложено"


@pytest.mark.asyncio
async def test_the_confirmation_actually_deletes(tmp_path):
    """Мутация: не звать `DELETE` по подтверждению — краснеет."""

    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend()
    try:
        await bridge._process_callback_query(telegram, backend, _press(f"know:delok:{DOCUMENT_ID}"))
    finally:
        bridge._inbox.close()

    assert f"DELETE knowledge/{DOCUMENT_ID}" in backend.calls, (
        f"подтверждение не дошло до маршрута удаления: {backend.calls}"
    )
    said = " ".join(payload.get("text", "") for payload in telegram.sent())
    assert "удален" in said.lower(), "человеку не сказали, что запись убрана"


@pytest.mark.asyncio
async def test_a_malformed_target_is_refused(tmp_path):
    """Идентификатор приходит из кнопки, но кнопку можно подделать.

    Мутация: снять проверку формата цели — краснеет: `know:delok:../../etc` ушёл
    бы в путь маршрута."""

    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend()
    press = _press("know:delok:../../secret")
    try:
        await bridge._process_callback_query(telegram, backend, press)
    except Exception as exc:  # noqa: BLE001
        assert "target" in str(exc).lower() or "callback" in str(exc).lower(), exc
    finally:
        bridge._inbox.close()

    assert not [call for call in backend.calls if call.startswith("DELETE")], (
        "подделанная цель дошла до удаления"
    )
