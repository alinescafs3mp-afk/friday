"""Telegram post-write timeouts are durable at-most-once delivery gaps."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig, _UpdateInbox
from friday.telegram_bridge._base import split_for_telegram
from friday.telegram_bridge._markup import to_telegram_html

CHAT_ID = 5001
UPDATE_ID = 900200
ORIGINAL_MESSAGE_ID = 31
NOTICE = "доставка не подтверждена, не дублирую; повторите запрос если фрагмент не пришёл"
TIMEOUT_CANARY = "RAW-TELEGRAM-READ-TIMEOUT-CANARY"
LONG_ANSWER = "\n".join(f"Уникальный абзац {index}: " + chr(1072 + index % 20) * 300 for index in range(40))


class _Telegram:
    def __init__(
        self,
        *,
        accept_then_timeout_at: int | None = None,
        connect_before_accept_at: int | None = None,
    ) -> None:
        self.accept_then_timeout_at = accept_then_timeout_at
        self.connect_before_accept_at = connect_before_accept_at
        self.accepted_payloads: list[dict[str, Any]] = []
        self.send_attempts = 0
        self._timeout_fired = False
        self._connect_fired = False

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        if not url.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {}}, request=request)
        payload = dict(kwargs.get("json") or {})
        attempt = self.send_attempts
        self.send_attempts += 1
        if self.connect_before_accept_at == attempt and not self._connect_fired:
            self._connect_fired = True
            raise httpx.ConnectError("pre-accept connection failure", request=request)
        if self.accept_then_timeout_at == attempt and not self._timeout_fired:
            self._timeout_fired = True
            self.accepted_payloads.append(payload)
            raise httpx.ReadTimeout(TIMEOUT_CANARY, request=request)
        self.accepted_payloads.append(payload)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 7000 + len(self.accepted_payloads)}},
            request=request,
        )


class _HardDeliveryCrash(BaseException):
    """Synthetic process death: ordinary bridge exception handlers cannot run."""


class _HardCrashTelegram(_Telegram):
    def __init__(self, *, after_accept: bool) -> None:
        super().__init__()
        self.after_accept = after_accept

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        if not url.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {}}, request=request)
        payload = dict(kwargs.get("json") or {})
        self.send_attempts += 1
        if self.after_accept:
            self.accepted_payloads.append(payload)
        raise _HardDeliveryCrash("synthetic hard delivery crash")


class _ConcreteRejectTelegram(_Telegram):
    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        if not url.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {}}, request=request)
        self.send_attempts += 1
        return httpx.Response(503, json={"ok": False}, request=request)


class _AcceptedNoticeTransportFailure(_Telegram):
    def __init__(self, error_type: type[httpx.RequestError]) -> None:
        super().__init__()
        self.error_type = error_type

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        if not url.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {}}, request=request)
        payload = dict(kwargs.get("json") or {})
        self.send_attempts += 1
        self.accepted_payloads.append(payload)
        raise self.error_type("ambiguous post-write transport failure", request=request)


class _Backend:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.chat_calls = 0

    async def request(self, method: str, url: str, **_kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, url)
        if "/api/chat" in url:
            self.chat_calls += 1
            return httpx.Response(
                200,
                json={"message": self.answer, "message_id": "msg_delivery_1", "citations": []},
                request=request,
            )
        return httpx.Response(200, json={}, request=request)


def _bridge(path: Path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[CHAT_ID],
            inbox_db_path=str(path),
        )
    )


def _update() -> dict[str, Any]:
    return {
        "update_id": UPDATE_ID,
        "message": {
            "message_id": ORIGINAL_MESSAGE_ID,
            "chat": {"id": CHAT_ID},
            "from": {"id": CHAT_ID, "first_name": "Владелец"},
            "text": "синтетический запрос",
        },
    }


def _queued(bridge: TelegramBridge) -> list[dict[str, Any]]:
    return bridge._inbox.pending(now=time.time() + 3600)  # noqa: SLF001


async def _run_once(bridge: TelegramBridge, telegram: _Telegram, backend: _Backend) -> None:
    row = next(row for row in _queued(bridge) if int(row["update_id"]) == UPDATE_ID)
    await bridge._run_update(telegram, backend, row)  # type: ignore[arg-type] # noqa: SLF001


def _rendered_chunks(answer: str) -> list[str]:
    return [to_telegram_html(chunk) or chunk for chunk in split_for_telegram(answer)]


def _assert_replies_to_original(payloads: list[dict[str, Any]]) -> None:
    expected = {
        "message_id": ORIGINAL_MESSAGE_ID,
        "allow_sending_without_reply": True,
    }
    assert payloads
    assert all(payload.get("reply_parameters") == expected for payload in payloads)


def test_existing_inbox_schema_adds_uncertainty_without_losing_cached_progress(
    tmp_path: Path,
) -> None:
    path = tmp_path / "existing-telegram.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE updates (
            update_id INTEGER PRIMARY KEY,
            payload_json TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            backend_response_json TEXT,
            created_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            next_attempt_at REAL NOT NULL DEFAULT 0,
            failed_at REAL,
            ordering_key TEXT NOT NULL DEFAULT '',
            chunks_sent INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO updates(
            update_id, payload_json, backend_response_json, created_at,
            ordering_key, chunks_sent
        ) VALUES(
            900200,
            '{"update_id":900200,"message":{"message_id":31,"chat":{"id":5001}}}',
            '{"message":"cached"}',
            1,
            'chat:5001',
            2
        );
        """
    )
    connection.commit()
    connection.close()

    inbox = _UpdateInbox(str(path))
    try:
        columns = {
            str(row["name"])
            for row in inbox._conn.execute("PRAGMA table_info(updates)").fetchall()  # noqa: SLF001
        }
        row = inbox.pending(now=time.time() + 3600)[0]
        assert "delivery_uncertainty" in columns
        assert int(row["chunks_sent"]) == 2
        assert int(row["delivery_uncertainty"]) == 0
        assert row["backend_response_json"] == '{"message":"cached"}'
        inbox.record_uncertain_answer_chunk(UPDATE_ID, 3)
    finally:
        inbox.close()

    reopened = _UpdateInbox(str(path))
    try:
        row = reopened.pending(now=time.time() + 3600)[0]
        assert int(row["chunks_sent"]) == 3
        assert int(row["delivery_uncertainty"]) == 1
        assert reopened.answer_delivery_uncertainty_pending(UPDATE_ID) is True
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_single_chunk_accepted_then_read_timeout_is_not_resent_after_restart(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    answer = "Короткий синтетический ответ."
    backend = _Backend(answer)
    bridge = _bridge(path)
    uncertain = _Telegram(accept_then_timeout_at=0)
    try:
        bridge._inbox.store(_update())  # noqa: SLF001
        await _run_once(bridge, uncertain, backend)
        row = _queued(bridge)[0]
        assert int(row["chunks_sent"]) == 1
        assert int(row["delivery_uncertainty"]) == 1
        assert row["last_error"] == "ReadTimeout"
        assert TIMEOUT_CANARY not in str(row["last_error"])
        assert TIMEOUT_CANARY not in "\n".join(str(value) for value in row.values())
    finally:
        bridge._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    healed = _Telegram()
    try:
        await _run_once(restarted, healed, backend)
        assert _queued(restarted) == []
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert [payload["text"] for payload in uncertain.accepted_payloads] == _rendered_chunks(answer)
    assert [payload["text"] for payload in healed.accepted_payloads] == [NOTICE]
    assert backend.chat_calls == 1
    assert TIMEOUT_CANARY not in caplog.text
    _assert_replies_to_original(uncertain.accepted_payloads + healed.accepted_payloads)


@pytest.mark.asyncio
async def test_multi_chunk_timeout_warns_once_then_continues_without_duplicates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    backend = _Backend(LONG_ANSWER)
    expected = _rendered_chunks(LONG_ANSWER)
    assert len(expected) >= 3
    bridge = _bridge(path)
    uncertain = _Telegram(accept_then_timeout_at=1)
    try:
        bridge._inbox.store(_update())  # noqa: SLF001
        await _run_once(bridge, uncertain, backend)
        row = _queued(bridge)[0]
        assert int(row["chunks_sent"]) == 2
        assert int(row["delivery_uncertainty"]) == 1
    finally:
        bridge._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    healed = _Telegram()
    try:
        await _run_once(restarted, healed, backend)
        assert _queued(restarted) == []
    finally:
        restarted._inbox.close()  # noqa: SLF001

    first_attempt = [payload["text"] for payload in uncertain.accepted_payloads]
    retry = [payload["text"] for payload in healed.accepted_payloads]
    assert first_attempt == expected[:2]
    assert retry == [NOTICE, *expected[2:]]
    assert first_attempt + retry[1:] == expected
    assert sum(payload["text"] == NOTICE for payload in healed.accepted_payloads) == 1
    assert backend.chat_calls == 1
    _assert_replies_to_original(uncertain.accepted_payloads + healed.accepted_payloads)


@pytest.mark.asyncio
async def test_connect_error_before_accept_retries_chunk_without_uncertainty_notice(
    tmp_path: Path,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    answer = "Короткий повторяемый ответ."
    backend = _Backend(answer)
    bridge = _bridge(path)
    disconnected = _Telegram(connect_before_accept_at=0)
    try:
        bridge._inbox.store(_update())  # noqa: SLF001
        await _run_once(bridge, disconnected, backend)
        row = _queued(bridge)[0]
        assert int(row["chunks_sent"]) == 0
        assert int(row["delivery_uncertainty"]) == 0

        healed = _Telegram()
        await _run_once(bridge, healed, backend)
        assert _queued(bridge) == []
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert disconnected.accepted_payloads == []
    assert [payload["text"] for payload in healed.accepted_payloads] == _rendered_chunks(answer)
    assert all(payload["text"] != NOTICE for payload in healed.accepted_payloads)
    assert backend.chat_calls == 1
    _assert_replies_to_original(healed.accepted_payloads)


@pytest.mark.asyncio
@pytest.mark.parametrize("after_accept", [False, True])
async def test_prewrite_fence_survives_hard_crash_without_resending_inflight_chunk(
    tmp_path: Path,
    after_accept: bool,
) -> None:
    """Mutation: moving the fence below POST duplicates the accepted variant."""

    path = tmp_path / "telegram.sqlite3"
    answer = "Ответ вокруг жёсткого падения процесса."
    backend = _Backend(answer)
    bridge = _bridge(path)
    crashed = _HardCrashTelegram(after_accept=after_accept)
    try:
        bridge._inbox.store(_update())  # noqa: SLF001
        bridge._stopping = True  # noqa: SLF001 - a dead process cannot dispatch another task
        with pytest.raises(_HardDeliveryCrash, match="synthetic hard delivery crash"):
            await _run_once(bridge, crashed, backend)
        row = _queued(bridge)[0]
        assert int(row["chunks_sent"]) == 1
        assert int(row["delivery_uncertainty"]) == 1
        assert row["last_error"] == ""
    finally:
        bridge._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    healed = _Telegram()
    try:
        await _run_once(restarted, healed, backend)
        assert _queued(restarted) == []
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert [payload["text"] for payload in healed.accepted_payloads] == [NOTICE]
    assert all(payload["text"] != to_telegram_html(answer) for payload in healed.accepted_payloads)
    assert [payload["text"] for payload in crashed.accepted_payloads] == (
        _rendered_chunks(answer) if after_accept else []
    )
    assert backend.chat_calls == 1
    _assert_replies_to_original(healed.accepted_payloads)


@pytest.mark.asyncio
async def test_concrete_http_rejection_rolls_back_prewrite_fence_and_retries_exact_chunk(
    tmp_path: Path,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    answer = "Ответ после доказанного HTTP-отказа."
    backend = _Backend(answer)
    bridge = _bridge(path)
    rejected = _ConcreteRejectTelegram()
    try:
        bridge._inbox.store(_update())  # noqa: SLF001
        await _run_once(bridge, rejected, backend)
        row = _queued(bridge)[0]
        assert int(row["chunks_sent"]) == 0
        assert int(row["delivery_uncertainty"]) == 0

        healed = _Telegram()
        await _run_once(bridge, healed, backend)
        assert _queued(bridge) == []
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert rejected.accepted_payloads == []
    assert [payload["text"] for payload in healed.accepted_payloads] == _rendered_chunks(answer)
    assert all(payload["text"] != NOTICE for payload in healed.accepted_payloads)
    assert backend.chat_calls == 1


@pytest.mark.asyncio
async def test_uncertainty_notice_read_timeout_is_itself_never_resent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    answer = "Короткий ответ с неопределённой доставкой."
    backend = _Backend(answer)
    bridge = _bridge(path)
    first = _Telegram(accept_then_timeout_at=0)
    try:
        bridge._inbox.store(_update())  # noqa: SLF001
        await _run_once(bridge, first, backend)
    finally:
        bridge._inbox.close()  # noqa: SLF001

    second_bridge = _bridge(path)
    notice_timeout = _Telegram(accept_then_timeout_at=0)
    try:
        await _run_once(second_bridge, notice_timeout, backend)
        row = _queued(second_bridge)[0]
        assert int(row["delivery_uncertainty"]) == 2
    finally:
        second_bridge._inbox.close()  # noqa: SLF001

    final_bridge = _bridge(path)
    healed = _Telegram()
    try:
        await _run_once(final_bridge, healed, backend)
        assert _queued(final_bridge) == []
    finally:
        final_bridge._inbox.close()  # noqa: SLF001

    assert [payload["text"] for payload in notice_timeout.accepted_payloads] == [NOTICE]
    assert healed.accepted_payloads == []
    assert backend.chat_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.WriteError, httpx.RemoteProtocolError])
async def test_ambiguous_notice_transport_failure_never_rearms_or_duplicates(
    tmp_path: Path,
    error_type: type[httpx.RequestError],
) -> None:
    path = tmp_path / "telegram.sqlite3"
    backend = _Backend("Короткий ответ перед неоднозначной доставкой уведомления.")
    first = _bridge(path)
    try:
        first._inbox.store(_update())  # noqa: SLF001
        await _run_once(first, _Telegram(accept_then_timeout_at=0), backend)
    finally:
        first._inbox.close()  # noqa: SLF001

    second = _bridge(path)
    ambiguous = _AcceptedNoticeTransportFailure(error_type)
    try:
        await _run_once(second, ambiguous, backend)
        row = _queued(second)[0]
        assert int(row["delivery_uncertainty"]) == 2
    finally:
        second._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    healed = _Telegram()
    try:
        await _run_once(restarted, healed, backend)
        assert _queued(restarted) == []
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert [payload["text"] for payload in ambiguous.accepted_payloads] == [NOTICE]
    assert healed.accepted_payloads == []
    assert backend.chat_calls == 1
