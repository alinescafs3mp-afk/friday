"""Запись, созданную из чата, можно удалить из чата же.

`PATCH` и `DELETE` живут в `friday/api/knowledge.py:168-204` с самого начала, а
мост звал только `GET`. Человек говорил «запомни», тут же замечал ошибку — и шёл
в админку, при том что Telegram основной интерфейс владельца и закон проекта
(`sol/SOL.md` §1.6) требует обратного: новая возможность сначала работает в чате.

Удаление мягкое и обратимое, но подтверждение всё равно спрашивается: одно
нажатие мимо не должно уносить запись.

Правка ТЕКСТА добавлена позже, в 0.178.0, и порядок был выбран намеренно: она
требует захвата следующей реплики как ответа на конкретное сообщение, а чтение
`reply_to_message` появилось только в 0.175.0. Сделать правку раньше означало бы
завести второй механизм ввода и выбросить его, когда появится первый.

Обязательные мутации перечислены в `sol/PROPOSALS.md` #46 и #52.
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
    assert f"know:del:{DOCUMENT_ID}.5001" in targets, (
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
        await bridge._process_callback_query(telegram, backend, _press(f"know:del:{DOCUMENT_ID}.5001"))
    finally:
        bridge._inbox.close()

    assert not [call for call in backend.calls if call.startswith("DELETE")], (
        "первое нажатие уже удалило запись, ничего не спросив"
    )
    targets = [button["callback_data"] for button in telegram.buttons()]
    assert f"know:delok:{DOCUMENT_ID}.5001" in targets, "подтверждения не предложено"


@pytest.mark.asyncio
async def test_the_confirmation_actually_deletes(tmp_path):
    """Мутация: не звать `DELETE` по подтверждению — краснеет."""

    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend()
    try:
        await bridge._process_callback_query(telegram, backend, _press(f"know:delok:{DOCUMENT_ID}.5001"))
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
    press = _press("know:delok:../../secret.5001")
    try:
        await bridge._process_callback_query(telegram, backend, press)
    except Exception as exc:  # noqa: BLE001
        assert "target" in str(exc).lower() or "callback" in str(exc).lower(), exc
    finally:
        bridge._inbox.close()

    assert not [call for call in backend.calls if call.startswith("DELETE")], (
        "подделанная цель дошла до удаления"
    )


@pytest.mark.asyncio
async def test_a_record_can_be_corrected_by_replying(tmp_path):
    """Правка использует ТОТ ЖЕ механизм ответа на реплику, что появился в 0.175.0.

    Это не совпадение и не экономия: заводить ради правки второй способ ввода
    значило бы выбросить его при первой же встрече с первым. Человек отвечает НА
    приглашение, поэтому адресат однозначен даже в чате, где идёт несколько
    разговоров, — цепляемся за идентификатор конкретного сообщения, а не за
    «последнее действие».

    Мутации: убрать кнопку «Исправить» — краснеет первый assert; не запоминать
    приглашение — краснеет второй; не звать PATCH — краснеет третий.
    """

    class _TelegramWithIds:
        def __init__(self) -> None:
            self.next_id = 500
            self.sent: list[dict[str, Any]] = []

        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            payload = dict(kwargs.get("json") or {})
            self.sent.append(payload)
            self.next_id += 1
            return httpx.Response(
                200,
                json={"ok": True, "result": {"message_id": self.next_id}},
                request=httpx.Request("POST", url),
            )

    class _PatchBackend:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.bodies: list[dict[str, Any]] = []

        async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
            self.calls.append(f"{method} {url.split('/api/', 1)[-1]}")
            body = kwargs.get("content")
            if body:
                self.bodies.append(json.loads(body))
            return httpx.Response(
                200,
                json={"item": {"id": DOCUMENT_ID, "title": "Заметка", "content": "Текст"}},
                request=httpx.Request(method, url),
            )

        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            return await self.request("POST", url, **kwargs)

    bridge = _bridge(tmp_path)
    telegram, backend = _TelegramWithIds(), _PatchBackend()
    try:
        # 1. Открыли запись — у неё есть кнопка «Исправить».
        await bridge._process_callback_query(telegram, backend, _press(f"doc:show:{DOCUMENT_ID}"))
        targets = [
            button["callback_data"]
            for payload in telegram.sent
            for row in (payload.get("reply_markup") or {}).get("inline_keyboard", [])
            for button in row
        ]
        assert f"know:fix:{DOCUMENT_ID}.5001" in targets, "у записи нет кнопки «Исправить»"

        # 2. Нажали — мост прислал приглашение и запомнил его.
        await bridge._process_callback_query(telegram, backend, _press(f"know:fix:{DOCUMENT_ID}.5001"))
        assert bridge._edit_targets, "приглашение не запомнено — ответ будет некуда адресовать"
        prompt_id = next(iter(bridge._edit_targets))
        assert bridge._edit_targets[prompt_id] == DOCUMENT_ID

        # 3. Ответили репликой на приглашение — запись исправлена, а не задан вопрос.
        update = {
            "update_id": 950,
            "message": {
                "message_id": 12,
                "chat": {"id": 5001},
                "from": {"id": 5001, "first_name": "Владелец"},
                "text": "Правильный текст записи",
                "reply_to_message": {"message_id": prompt_id, "text": "Ответьте на ЭТО сообщение"},
            },
        }
        await bridge._process_update(telegram, backend, update, cached_response=None)
    finally:
        bridge._inbox.close()

    assert f"PATCH knowledge/{DOCUMENT_ID}" in backend.calls, (
        f"правка не дошла до маршрута исправления: {backend.calls}"
    )
    patched = [body for body in backend.bodies if "content" in body]
    assert patched and patched[-1]["content"] == "Правильный текст записи"
    assert not any(call.endswith("api/chat") for call in backend.calls), (
        "текст правки уехал к модели как обычный вопрос"
    )
    assert bridge._edit_targets == {}, "приглашение осталось в памяти после использования"


@pytest.mark.asyncio
async def test_someone_elses_button_is_refused(tmp_path):
    """Кнопка привязана к тому, КОМУ её показали.

    Сообщение с кнопкой видно всему чату, и без привязки любая другая способная
    учётка, нажав первой, действовала бы на чужом экране. Соседние семейства
    (`conv`, `ent`, `relation`) привязку имели с самого начала; у заведённого мной
    `know` её не было, и это нашёл аудит Grok по пути ответа.

    Мутация: снять проверку нажавшего — краснеет.
    """

    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend()
    press = _press(f"know:delok:{DOCUMENT_ID}.9999")  # кнопку показали не этому человеку
    try:
        await bridge._process_callback_query(telegram, backend, press)
    finally:
        bridge._inbox.close()

    assert not [call for call in backend.calls if call.startswith("DELETE")], (
        "чужая кнопка сработала: запись удалил не тот, кому её показали"
    )
