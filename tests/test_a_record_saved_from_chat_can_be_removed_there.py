"""Запись, созданную из чата, можно удалить из чата же.

`PATCH` и `DELETE` живут в `friday/api/knowledge.py:168-204` с самого начала, а
мост звал только `GET`. Человек говорил «запомни», тут же замечал ошибку — и шёл
в админку, при том что Telegram — первичный интерфейс владельца.

Удаление мягкое и обратимое, но подтверждение всё равно спрашивается: одно
нажатие мимо не должно уносить запись.

Правка ТЕКСТА добавлена позже, в 0.178.0, и порядок был выбран намеренно: она
требует захвата следующей реплики как ответа на конкретное сообщение, а чтение
`reply_to_message` появилось только в 0.175.0. Сделать правку раньше означало бы
завести второй механизм ввода и выбросить его, когда появится первый.

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
async def test_an_openable_boundary_id_never_breaks_the_full_document_send(tmp_path):
    """Every derived button has its own Telegram 64-byte boundary.

    A document id can fit ``doc:show`` exactly while no longer fitting the
    longer edit/delete/more actions.  Those actions are optional; rejecting the
    complete document message is not.
    """

    boundary_id = "k" * (64 - len("doc:show:"))
    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend()
    try:
        await bridge._process_callback_query(telegram, backend, _press(f"doc:show:{boundary_id}"))
    finally:
        bridge._inbox.close()

    sent = telegram.sent()[-1]
    assert "Текст заметки" in sent["text"]
    assert all(
        len(button["callback_data"].encode("utf-8")) <= 64
        for row in sent.get("reply_markup", {}).get("inline_keyboard", [])
        for button in row
    )


def test_an_oversized_next_page_button_is_omitted():
    boundary_id = "k" * (64 - len("doc:show:"))
    document = {"item": {"content": "x" * (TelegramBridge._FULL_DOCUMENT_CHARS + 1)}}

    assert TelegramBridge._document_more_markup(document, boundary_id, 0) is None


@pytest.mark.asyncio
async def test_a_legacy_delete_button_never_emits_an_oversized_confirmation(tmp_path):
    legacy_id = "k" * (64 - len("know:del:") - len(".5001"))
    assert len(f"know:del:{legacy_id}.5001".encode()) == 64
    assert len(f"know:delok:{legacy_id}.5001".encode()) > 64

    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend()
    try:
        await bridge._process_callback_query(telegram, backend, _press(f"know:del:{legacy_id}.5001"))
    finally:
        bridge._inbox.close()

    sent = telegram.sent()[-1]
    assert "Запись не удалена" in sent["text"]
    assert "reply_markup" not in sent


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

        # 2. Нажали — мост прислал приглашение и запомнил его В ОЧЕРЕДИ, а не в
        # памяти процесса: человек может ответить через час, а мост между тем
        # перезапускается, и потерянная связь молча превратила бы правку в
        # обычный вопрос к модели.
        await bridge._process_callback_query(telegram, backend, _press(f"know:fix:{DOCUMENT_ID}.5001"))
        # Приглашение отправлено, следом ушёл ответ на нажатие — счётчик уехал на один.
        prompt_id = telegram.next_id - 1
        assert bridge._inbox.take_edit_prompt(prompt_id) == DOCUMENT_ID, (
            "приглашение не запомнено — ответ будет некуда адресовать"
        )
        # Забрали ради проверки — вернуть, иначе следующий шаг проверял бы пустоту.
        bridge._inbox.remember_edit_prompt(prompt_id, DOCUMENT_ID)

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
        leftover = bridge._inbox.take_edit_prompt(prompt_id)
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
    assert leftover == "", "приглашение осталось действующим после использования"


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
