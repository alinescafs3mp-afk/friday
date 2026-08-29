"""End-to-end recovery at the backend-cache and completed-row commit seams."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest

import friday.telegram_bridge._transport as transport_module
from friday.telegram_bridge import TelegramBridge, TelegramConfig, _UpdateInbox

CHAT_ID = 5001
UPDATE_ID = 970_001
ANSWER = "Ответ из одного устойчивого backend-эффекта."

FaultKind = Literal["full", "ioerr"]
CommitOutcome = Literal["before", "after"]


def _sqlite_fault(kind: FaultKind) -> sqlite3.OperationalError:
    if kind == "full":
        error = sqlite3.OperationalError("database or disk is full")
        code = sqlite3.SQLITE_FULL
        name = "SQLITE_FULL"
    else:
        error = sqlite3.OperationalError("disk I/O error")
        code = sqlite3.SQLITE_IOERR
        name = "SQLITE_IOERR"
    error.sqlite_errorcode = code  # type: ignore[attr-defined]
    error.sqlite_errorname = name  # type: ignore[attr-defined]
    return error


class _CommitFaultConnection:
    """Delegate SQLite except for one commit failure before or after durability."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        object.__setattr__(self, "connection", connection)
        object.__setattr__(self, "fault", None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"connection", "fault"}:
            object.__setattr__(self, name, value)
            return
        setattr(self.connection, name, value)

    def arm(self, kind: FaultKind, outcome: CommitOutcome) -> None:
        assert self.fault is None
        self.fault = (kind, outcome)

    def commit(self) -> None:
        fault = self.fault
        self.fault = None
        if fault is None:
            self.connection.commit()
            return
        kind, outcome = fault
        if outcome == "after":
            self.connection.commit()
        raise _sqlite_fault(kind)

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()


class _Telegram:
    def __init__(self) -> None:
        self.send_messages: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        if url.endswith("/sendMessage"):
            self.send_messages.append(dict(kwargs.get("json") or {}))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 80_000 + len(self.send_messages)}},
            request=request,
        )


class _IdempotentBackend:
    """Model durable ingress: repeated source_ref requests have one effect."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.effects: dict[str, dict[str, Any]] = {}

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, url)
        if not url.endswith("/api/chat"):
            return httpx.Response(200, json={}, request=request)
        payload = json.loads(bytes(kwargs.get("content") or b"{}"))
        source_ref = str(payload.get("source_ref") or "")
        assert source_ref
        self.requests.append(source_ref)
        response = self.effects.setdefault(
            source_ref,
            {"message": ANSWER, "message_format": "plain"},
        )
        return httpx.Response(200, json=response, request=request)


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
            "message_id": 71,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "first_name": "Owner"},
            "text": "Проверь восстановление backend cache",
        },
    }


def _opened(bridge: TelegramBridge) -> _UpdateInbox:
    return bridge._inbox._opened()  # type: ignore[no-any-return] # noqa: SLF001


def _row(inbox: _UpdateInbox) -> dict[str, Any] | None:
    found = inbox._conn.execute(  # noqa: SLF001
        "SELECT * FROM updates WHERE update_id=?",
        (UPDATE_ID,),
    ).fetchone()
    return dict(found) if found is not None else None


def _pending_row(inbox: _UpdateInbox) -> dict[str, Any]:
    return next(row for row in inbox.pending() if int(row["update_id"]) == UPDATE_ID)


def _wrap_connection(inbox: _UpdateInbox) -> _CommitFaultConnection:
    proxy = _CommitFaultConnection(inbox._conn)  # noqa: SLF001
    inbox._conn = proxy  # type: ignore[assignment] # noqa: SLF001
    return proxy


async def _no_storage_backoff(_error: BaseException) -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["full", "ioerr"])
@pytest.mark.parametrize("outcome", ["before", "after"])
async def test_backend_cache_commit_fault_recovers_one_effect_and_one_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: FaultKind,
    outcome: CommitOutcome,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    backend = _IdempotentBackend()
    telegram = _Telegram()
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.admit_polled_update(_update())[0] is True
    proxy = _wrap_connection(inbox)
    original_cache = inbox.cache_backend_response

    def faulting_cache(update_id: int, response: dict[str, Any]) -> None:
        proxy.arm(kind, outcome)
        original_cache(update_id, response)

    monkeypatch.setattr(inbox, "cache_backend_response", faulting_cache)
    monkeypatch.setattr(transport_module, "_back_off_local_storage_failure", _no_storage_backoff)
    try:
        await first._run_update(telegram, backend, _pending_row(inbox))  # type: ignore[arg-type] # noqa: SLF001
        retained = _row(inbox)
        assert retained is not None
        assert (retained["backend_response_json"] is not None) is (outcome == "after")
        assert int(retained["chunks_sent"]) == 0
        assert int(retained["attempts"]) == 0
        assert telegram.send_messages == []
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    reopened = _opened(restarted)
    try:
        await restarted._run_update(  # type: ignore[arg-type] # noqa: SLF001
            telegram,
            backend,
            _pending_row(reopened),
        )
        assert _row(reopened) is None
    finally:
        restarted._inbox.close()  # noqa: SLF001

    source_ref = f"telegram-update:{UPDATE_ID}"
    assert set(backend.effects) == {source_ref}
    assert backend.requests == ([source_ref, source_ref] if outcome == "before" else [source_ref])
    assert [payload.get("text") for payload in telegram.send_messages] == [ANSWER]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["full", "ioerr"])
@pytest.mark.parametrize("outcome", ["before", "after"])
async def test_cached_answer_removal_fault_never_replays_backend_or_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: FaultKind,
    outcome: CommitOutcome,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    backend = _IdempotentBackend()
    telegram = _Telegram()
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.admit_polled_update(_update())[0] is True
    inbox.cache_backend_response(
        UPDATE_ID,
        {"message": ANSWER, "message_format": "plain"},
    )
    proxy = _wrap_connection(inbox)
    original_remove = inbox.remove_many

    def faulting_remove(update_ids: list[int]) -> None:
        proxy.arm(kind, outcome)
        original_remove(update_ids)

    monkeypatch.setattr(inbox, "remove_many", faulting_remove)
    monkeypatch.setattr(transport_module, "_back_off_local_storage_failure", _no_storage_backoff)
    try:
        await first._run_update(telegram, backend, _pending_row(inbox))  # type: ignore[arg-type] # noqa: SLF001
        retained = _row(inbox)
        assert (retained is None) is (outcome == "after")
        if retained is not None:
            assert retained["backend_response_json"] is not None
            assert int(retained["chunks_sent"]) == 1
            assert int(retained["delivery_uncertainty"]) == 0
            assert int(retained["attempts"]) == 0
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    reopened = _opened(restarted)
    try:
        if outcome == "before":
            await restarted._run_update(  # type: ignore[arg-type] # noqa: SLF001
                telegram,
                backend,
                _pending_row(reopened),
            )
        assert _row(reopened) is None
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert backend.requests == []
    assert backend.effects == {}
    assert [payload.get("text") for payload in telegram.send_messages] == [ANSWER]
