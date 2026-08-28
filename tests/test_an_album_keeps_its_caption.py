"""Подпись альбома доходит до всех его частей, а не до одной.

Telegram шлёт альбом несколькими сообщениями с общим `media_group_id`, и подпись
стоит ровно у ОДНОЙ части — обычно у первой. Остальные приходили совсем пустыми:
«вот договор, пять страниц» относилось к одному файлу из пяти, а четыре попадали
в архив без единого слова о том, что это.

Части одного чата обрабатываются строго по очереди (`ordering_key` очереди
обновлений), поэтому достаточно помнить последнюю группу. Память живёт в
процессе: после рестарта поведение честно возвращается к прежнему, а не
притворяется, что помнит.

"""

from __future__ import annotations

import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig
from friday.telegram_bridge._media import _ALBUM_CAPTION_MEMORY


def _bridge(tmp_path):
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


def test_the_caption_reaches_the_rest_of_the_album(tmp_path):
    """Мутация: не запоминать подпись группы — краснеет."""

    bridge = _bridge(tmp_path)
    try:
        first = {"media_group_id": "g1", "caption": "Договор, пять страниц"}
        rest = {"media_group_id": "g1"}

        # У части С подписью своя подпись уже есть — второй раз её подставлять
        # нечего, иначе текст удвоился бы.
        assert bridge._album_caption(first) == ""
        assert bridge._album_caption(rest) == "Договор, пять страниц"
        assert bridge._album_caption(rest) == "Договор, пять страниц"
    finally:
        bridge._inbox.close()


def test_a_lone_file_borrows_nothing(tmp_path):
    """Файл без группы не должен получать чужую подпись.

    Мутация: отдавать подпись при пустом `media_group_id` — краснеет."""

    bridge = _bridge(tmp_path)
    try:
        bridge._album_caption({"media_group_id": "g1", "caption": "Договор"})
        assert bridge._album_caption({}) == ""
        assert bridge._album_caption({"caption": ""}) == ""
    finally:
        bridge._inbox.close()


def test_another_album_does_not_borrow_the_previous_caption(tmp_path):
    """Две группы подряд — обычное дело; подписи не должны перетекать."""

    bridge = _bridge(tmp_path)
    try:
        bridge._album_caption({"media_group_id": "g1", "caption": "Договор"})
        assert bridge._album_caption({"media_group_id": "g2"}) == ""
    finally:
        bridge._inbox.close()


def test_the_memory_is_bounded(tmp_path):
    """Мутация: снять потолок словаря — краснеет.

    Группа живёт секунды, а словарь без ограничения рос бы всю жизнь процесса."""

    bridge = _bridge(tmp_path)
    try:
        for index in range(_ALBUM_CAPTION_MEMORY * 3):
            bridge._album_caption({"media_group_id": f"g{index}", "caption": f"подпись {index}"})
        assert len(bridge._album_captions) <= _ALBUM_CAPTION_MEMORY
        # И вытесняется САМАЯ СТАРАЯ: свежая группа обязана помниться.
        last = _ALBUM_CAPTION_MEMORY * 3 - 1
        assert bridge._album_caption({"media_group_id": f"g{last}"}) == f"подпись {last}"
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_the_second_part_of_an_album_is_not_a_nameless_file(tmp_path):
    """Проводочная проба: смотрит, что уехало в запрос, а не что умеет помощник.

    Мутация: не звать `_album_caption` в разборе сообщения — краснеет."""
    from typing import Any

    import httpx

    class _Telegram:
        async def post(self, url: str, **kwargs: Any):
            return httpx.Response(200, json={"ok": True, "result": {}}, request=httpx.Request("POST", url))

    class _Backend:
        def __init__(self) -> None:
            self.bodies: list[dict[str, Any]] = []

        async def request(self, method: str, url: str, **kwargs: Any):
            import json as _json

            body = kwargs.get("content")
            if body:
                self.bodies.append(_json.loads(body))
            return httpx.Response(200, json={"message": "Готово"}, request=httpx.Request(method, url))

        async def post(self, url: str, **kwargs: Any):
            return await self.request("POST", url, **kwargs)

    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend()

    def _message(update_id: int, caption: str | None) -> dict[str, Any]:
        message: dict[str, Any] = {
            "message_id": update_id,
            "chat": {"id": 5001},
            "from": {"id": 1001, "first_name": "Alice"},
            "media_group_id": "album-1",
        }
        if caption:
            message["caption"] = caption
        return {"update_id": update_id, "message": message}

    try:
        await bridge._process_update(
            telegram, backend, _message(801, "Договор, пять страниц"), cached_response=None
        )
        await bridge._process_update(telegram, backend, _message(802, None), cached_response=None)
    finally:
        bridge._inbox.close()

    texts = [str(body.get("message") or "") for body in backend.bodies if "message" in body]
    assert texts, f"до бэкенда ничего не дошло: {backend.bodies}"
    assert any("Договор, пять страниц" in text for text in texts[1:]), (
        f"вторая часть альбома приехала без подписи первой: {texts}"
    )
