"""Обрыв посреди длинного ответа не присылает человеку начало заново.

Ответ длиннее 4096 знаков уходит в Telegram несколькими сообщениями. Строка
обновления остаётся в очереди, пока ответ не доставлен целиком, — и это верно:
иначе обрыв сети на третьем куске из пяти терял бы хвост ответа навсегда.

Но повтор до сих пор слал ВСЕ куски заново: первые два приходили человеку
дважды. 429 от Telegram эту болезнь уже не вызывает (0.173.0 повторяет ровно тот
кусок, который не прошёл); осталась вторая половина — обрыв соединения, после
которого повторяется весь ход.

Продолжение возможно только потому, что ответ ядра кешируется в строке очереди
ДО первой отправки: повтор режет тот же самый текст и получает те же границы.
Если бы текст мог измениться между попытками, номер куска ничего не значил бы —
поэтому счётчик живёт на той же строке, что и кеш, и исчезает вместе с ней.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

import httpx
import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig

CHAT_ID = 5001
UPDATE_ID = 900100
#: Три куска: 4096 — потолок Telegram, считаемый в кодовых единицах UTF-16.
LONG_ANSWER = "\n".join(f"Абзац номер {index} " + "я" * 300 for index in range(40))


class _Telegram:
    """Telegram, у которого обрывается соединение на заданном куске."""

    def __init__(self, *, break_at: int | None = None) -> None:
        self.break_at = break_at
        self.chunks: list[str] = []
        self.status_chunks: list[str] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        if not url.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}, request=request)
        text = str((kwargs.get("json") or {}).get("text", ""))
        # The fault belongs to answer delivery, after the separate status message.
        if text.startswith("⏳ ") and "\n\nПрошло:" in text:
            self.status_chunks.append(text)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 9000}}, request=request)
        if self.break_at is not None and len(self.chunks) == self.break_at:
            raise httpx.ConnectError("network is gone", request=request)
        self.chunks.append(text)
        message_id = (1000 if self.break_at is not None else 2000) + len(self.chunks)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}}, request=request)


class _Backend:
    """Ядро отвечает один раз; повтор обязан идти из кеша, а не сюда."""

    def __init__(self) -> None:
        self.chat_calls = 0
        self.authority_checks = 0

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, url)
        if "/api/chat" in url:
            self.chat_calls += 1
            return httpx.Response(
                200,
                json={
                    "message": LONG_ANSWER,
                    "message_id": "msg_1",
                    "conversation_id": "conv_resume",
                    "citations": [],
                },
                request=request,
            )
        if method == "GET" and request.url.path == "/api/me/mixed-deliveries/msg_1":
            self.authority_checks += 1
            return httpx.Response(
                200,
                json={
                    "authorized": request.url.params.get("answer_sha256")
                    == hashlib.sha256(LONG_ANSWER.encode()).hexdigest(),
                    "mixed_required": False,
                    "conversation_id": "conv_resume",
                },
                request=request,
            )
        return httpx.Response(200, json={}, request=request)


def _bridge(tmp_path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[CHAT_ID],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


def _update() -> dict[str, Any]:
    return {
        "update_id": UPDATE_ID,
        "message": {
            "message_id": 11,
            "chat": {"id": CHAT_ID},
            "from": {"id": CHAT_ID, "first_name": "Владелец"},
            "text": "расскажи подробно",
        },
    }


def _queued(bridge: TelegramBridge) -> list[dict[str, Any]]:
    """Строки, готовые к работе. Время сдвинуто вперёд: после неудачи строка ждёт
    свою паузу перед повтором, и без сдвига очередь выглядела бы пустой."""
    return bridge._inbox.pending(now=time.time() + 3600)  # noqa: SLF001


async def _run_once(bridge: TelegramBridge, telegram: _Telegram, backend: _Backend) -> None:
    """Один оборот очереди: взять строку и попытаться её отработать."""
    row = next(row for row in _queued(bridge) if int(row["update_id"]) == UPDATE_ID)
    await bridge._run_update(telegram, backend, row)  # noqa: SLF001


@pytest.mark.asyncio
async def test_the_answer_needs_more_than_one_message(tmp_path):
    """Опора всей пробы: текст обязан резаться на несколько кусков.

    Иначе «не переслал начало заново» было бы верно и без единой строки кода.
    """
    from friday.telegram_bridge._base import split_for_telegram

    assert len(split_for_telegram(LONG_ANSWER)) >= 3


@pytest.mark.asyncio
async def test_a_broken_connection_resumes_where_it_stopped(tmp_path):
    bridge = _bridge(tmp_path)
    backend = _Backend()
    bridge._inbox.store(_update())  # noqa: SLF001
    broken = _Telegram(break_at=2)
    try:
        await _run_once(bridge, broken, backend)
        delivered_first = list(broken.chunks)
        # Строка осталась в очереди — хвост ответа ещё не у человека.
        assert _queued(bridge), "обновление ушло из очереди с недоставленным хвостом"

        healed = _Telegram()
        await _run_once(bridge, healed, backend)
        delivered_second = list(healed.chunks)
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert len(delivered_first) == 2, f"до обрыва должно уйти два куска: {len(delivered_first)}"
    assert delivered_second, "после починки сети ответ не досылался вовсе"
    repeated = [chunk for chunk in delivered_second if chunk in delivered_first]
    assert not repeated, f"человек получил заново то, что уже читал: {len(repeated)} кусков"
    assert backend.chat_calls == 1, "повтор сходил в ядро второй раз вместо кеша"
    assert backend.authority_checks == 1, "повтор не проверил актуальное право доставки"
    assert broken.status_chunks, "перед ответом не было отдельного статуса"


@pytest.mark.asyncio
async def test_the_whole_answer_still_arrives(tmp_path):
    """Продолжение не должно превратиться в потерю: вместе куски дают весь ответ."""
    bridge = _bridge(tmp_path)
    backend = _Backend()
    bridge._inbox.store(_update())  # noqa: SLF001
    broken, healed = _Telegram(break_at=2), _Telegram()
    try:
        await _run_once(bridge, broken, backend)
        await _run_once(bridge, healed, backend)
        left = _queued(bridge)
    finally:
        bridge._inbox.close()  # noqa: SLF001

    from friday.telegram_bridge._base import split_for_telegram

    assert broken.chunks + healed.chunks == [html for html in _rendered(split_for_telegram(LONG_ANSWER))], (
        "склеенные попытки не дают ровно один полный ответ"
    )
    assert not left, "доставленное обновление осталось в очереди"
    assert backend.chat_calls == 1
    assert backend.authority_checks == 1
    assert broken.status_chunks


def _rendered(chunks: list[str]) -> list[str]:
    from friday.telegram_bridge._markup import to_telegram_html

    return [to_telegram_html(chunk) or chunk for chunk in chunks]


@pytest.mark.asyncio
async def test_the_progress_survives_a_restart_of_the_bridge(tmp_path):
    """Счётчик кусков durable: падение процесса — самый частый способ оборваться."""
    backend = _Backend()
    bridge = _bridge(tmp_path)
    broken = _Telegram(break_at=2)
    try:
        bridge._inbox.store(_update())  # noqa: SLF001
        await _run_once(bridge, broken, backend)
    finally:
        bridge._inbox.close()  # noqa: SLF001

    restarted = _bridge(tmp_path)
    healed = _Telegram()
    try:
        await _run_once(restarted, healed, backend)
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert not [chunk for chunk in healed.chunks if chunk in broken.chunks], (
        "после перезапуска моста человек получил начало ответа заново"
    )
    assert backend.chat_calls == 1
    assert backend.authority_checks == 1


@pytest.mark.asyncio
async def test_a_service_message_is_not_resumable(tmp_path):
    """Служебные сообщения продолжения не получают, и это осознанно.

    Они короткие (один кусок) и не привязаны к строке очереди; общий счётчик на
    обновление заставил бы второе служебное сообщение считать себя уже
    отправленным.
    """
    bridge = _bridge(tmp_path)
    telegram = _Telegram()
    try:
        bridge._inbox.store(_update())  # noqa: SLF001
        bridge._inbox.record_answer_chunks_sent(UPDATE_ID, 5)  # noqa: SLF001
        await bridge._send_message(telegram, CHAT_ID, "Готово.")  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert telegram.chunks, "служебное сообщение не ушло из-за чужого счётчика"
