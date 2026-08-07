"""Провалившееся подтверждение больше не доставляет пачку заново.

`_drain_outbound` забирает у бэкенда до двадцати ожидающих уведомлений,
отправляет их и подтверждает ОДНИМ вызовом в конце. Пока признак «доставлено»
жил в списке в памяти процесса, единственное провалившееся подтверждение
означало, что все двадцать сообщений останутся `pending` — и придут человеку
снова через пятнадцать секунд, и ещё через пятнадцать, пока какое-нибудь
подтверждение не пройдёт.

Теперь факт доставки записывается в собственную durable-очередь моста в момент
доставки, поэтому следующий оборот узнаёт эти номера в выдаче бэкенда и
повторяет ПОДТВЕРЖДЕНИЕ, а не отправку. Подтверждать каждое сообщение отдельным
сетевым вызовом по-прежнему нельзя: мост подписывает служебные вызовы как
владелец, и двадцать подтверждений на оборот съели бы его бюджет частоты.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig


class _Telegram:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        if url.endswith("/sendMessage"):
            self.sent.append(str((kwargs.get("json") or {}).get("text", "")))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}, request=request)


class _Backend:
    """Честный бэкенд: пока подтверждение не пришло, строки остаются `pending`.

    Именно это и есть условие находки — бэкенд не виноват и не сломан, он просто
    не знает того, чего ему не сказали.
    """

    def __init__(self, *, ack_fails: bool) -> None:
        self.ack_fails = ack_fails
        self.pending: dict[str, str] = {"notif_1": "первое", "notif_2": "второе"}
        self.acked_sent: list[list[str]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, url)
        if "/api/notifications/pending" in url:
            items = [{"id": key, "chat_id": "5001", "body": body} for key, body in self.pending.items()]
            return httpx.Response(200, json={"items": items, "count": len(items)}, request=request)
        if "/api/notifications/ack" in url:
            if self.ack_fails:
                return httpx.Response(503, json={"detail": "backend restarting"}, request=request)
            payload = json.loads(kwargs.get("content") or b"{}")
            acked = [str(value) for value in (payload.get("sent") or [])]
            self.acked_sent.append(acked)
            for notif_id in acked:
                self.pending.pop(notif_id, None)
            return httpx.Response(200, json={"sent": len(acked), "failed": 0}, request=request)
        return httpx.Response(200, json={}, request=request)


def _bridge(tmp_path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


@pytest.mark.asyncio
async def test_a_failed_ack_does_not_deliver_the_same_notification_twice(tmp_path):
    """Два оборота подряд при неработающем подтверждении — одна доставка."""
    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend(ack_fails=True)
    try:
        await bridge._drain_outbound(telegram, backend)  # noqa: SLF001
        await bridge._drain_outbound(telegram, backend)  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert telegram.sent == ["первое", "второе"], f"человек получил пачку заново: {telegram.sent}"


@pytest.mark.asyncio
async def test_the_second_drain_re_acks_what_it_did_not_re_send(tmp_path):
    """Пропуск отправки не значит забыть: номер должен уйти в подтверждение снова.

    Иначе строка навсегда останется `pending` у бэкенда — очередь встанет,
    потому что она разбирается двадцатью старейшими.
    """
    bridge = _bridge(tmp_path)
    telegram = _Telegram()
    failing, working = _Backend(ack_fails=True), _Backend(ack_fails=False)
    try:
        await bridge._drain_outbound(telegram, failing)  # noqa: SLF001
        await bridge._drain_outbound(telegram, working)  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert telegram.sent == ["первое", "второе"]
    assert working.acked_sent == [["notif_1", "notif_2"]], (
        f"второй оборот не подтвердил доставленное: {working.acked_sent}"
    )
    assert working.pending == {}, "очередь бэкенда не сдвинулась"


@pytest.mark.asyncio
async def test_the_memory_of_delivery_survives_a_restart_of_the_bridge(tmp_path):
    """Тот же файл очереди, другой процесс: доставленное остаётся доставленным.

    Список в памяти умирает вместе с процессом, и падение моста между отправкой
    и подтверждением — самый вероятный способ потерять подтверждение вообще.
    """
    telegram = _Telegram()
    first, second = _Backend(ack_fails=True), _Backend(ack_fails=False)

    bridge = _bridge(tmp_path)
    try:
        await bridge._drain_outbound(telegram, first)  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    restarted = _bridge(tmp_path)
    try:
        await restarted._drain_outbound(telegram, second)  # noqa: SLF001
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert telegram.sent == ["первое", "второе"], (
        f"после перезапуска моста человек получил дубль: {telegram.sent}"
    )
    assert second.acked_sent == [["notif_1", "notif_2"]]


@pytest.mark.asyncio
async def test_a_landed_ack_retires_the_local_record(tmp_path):
    """Подтверждение прошло — помнить об этом дальше незачем и вредно.

    Дальше помнит бэкенд; локальная строка после этого только копит мусор в
    очереди, которая живёт вместе с мостом годами.
    """
    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend(ack_fails=False)
    try:
        await bridge._drain_outbound(telegram, backend)  # noqa: SLF001
        remembered = bridge._inbox.delivered_notification_ids()  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert backend.acked_sent == [["notif_1", "notif_2"]]
    assert remembered == set(), f"подтверждённое осталось в очереди моста: {remembered}"
