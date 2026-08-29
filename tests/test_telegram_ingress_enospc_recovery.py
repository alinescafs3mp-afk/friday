"""Adversarial local-storage seams for the durable Telegram ingress queue."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest

import friday.telegram_bridge._queue as queue_module
import friday.telegram_bridge._transport as transport_module
from friday.telegram_bridge import (
    MAX_ATTEMPTS,
    PermanentUpdateError,
    TelegramBridge,
    TelegramConfig,
    _UpdateInbox,
)

CHAT_ID = 5001
UPDATE_ID = 960_001
ANSWER = "Детерминированный ответ после восстановления хранилища."
NOTICE = "доставка не подтверждена, не дублирую; повторите запрос если фрагмент не пришёл"

FaultKind = Literal["full", "ioerr"]
CommitOutcome = Literal["before", "after"]
QueueMutation = Literal["status", "cache", "remove"]


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
    """Delegate SQLite except for one deterministic commit acknowledgement."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        object.__setattr__(self, "connection", connection)
        object.__setattr__(self, "fault", None)
        object.__setattr__(self, "rollback_fault", None)
        object.__setattr__(self, "closed", False)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"connection", "fault", "rollback_fault", "closed"}:
            object.__setattr__(self, name, value)
            return
        setattr(self.connection, name, value)

    def __enter__(self) -> _CommitFaultConnection:
        self.connection.__enter__()
        return self

    def __exit__(self, *args: Any) -> bool | None:
        return self.connection.__exit__(*args)

    def arm(
        self,
        kind: FaultKind,
        outcome: CommitOutcome,
        *,
        rollback_fails: bool = False,
    ) -> None:
        assert self.fault is None
        self.fault = (kind, outcome)
        self.rollback_fault = kind if rollback_fails else None

    def commit(self) -> None:
        fault = self.fault
        self.fault = None
        if fault is None:
            self.connection.commit()
            return
        kind, outcome = fault
        if outcome == "after":
            self.connection.commit()
        # In the before case the transaction deliberately remains open.  The
        # production guard, not this fake, must discard it.
        raise _sqlite_fault(kind)

    def rollback(self) -> None:
        fault = self.rollback_fault
        self.rollback_fault = None
        if fault is not None:
            raise _sqlite_fault(fault)
        self.connection.rollback()

    def close(self) -> None:
        self.closed = True
        self.connection.close()


class _Telegram:
    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = list(statuses or [])
        self.payloads: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        if not url.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {}}, request=request)
        self.payloads.append(dict(kwargs.get("json") or {}))
        status = self.statuses.pop(0) if self.statuses else 200
        body = (
            {"ok": True, "result": {"message_id": 70_000 + len(self.payloads)}}
            if 200 <= status < 300
            else {"ok": False, "error_code": status, "description": "synthetic upstream response"}
        )
        return httpx.Response(status, json=body, request=request)


def _update(update_id: int = UPDATE_ID, *, chat_id: int = CHAT_ID) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": "Owner"},
            "text": "Проверь восстановление Telegram ingress",
        },
    }


def _bridge(path: Path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[CHAT_ID],
            inbox_db_path=str(path),
        )
    )


def _opened(bridge: TelegramBridge) -> _UpdateInbox:
    return bridge._inbox._opened()  # type: ignore[no-any-return] # noqa: SLF001


def _row(inbox: _UpdateInbox, update_id: int = UPDATE_ID) -> dict[str, Any] | None:
    found = inbox._conn.execute(  # noqa: SLF001
        "SELECT * FROM updates WHERE update_id=?",
        (update_id,),
    ).fetchone()
    return dict(found) if found is not None else None


def _wrap_connection(inbox: _UpdateInbox) -> _CommitFaultConnection:
    proxy = _CommitFaultConnection(inbox._conn)  # noqa: SLF001
    inbox._conn = proxy  # type: ignore[assignment] # noqa: SLF001
    return proxy


def _assert_mutation_state(
    inbox: _UpdateInbox,
    operation: QueueMutation,
    *,
    committed: bool,
) -> None:
    current = _row(inbox)
    if operation == "remove":
        assert (current is None) is committed
        return

    assert current is not None
    if operation == "status":
        assert int(current["attempts"]) == (1 if committed else 0)
        assert str(current["last_error"]) == ("OperationalError" if committed else "")
        return

    cached = current["backend_response_json"]
    assert (cached is not None) is committed
    if cached is not None:
        assert json.loads(str(cached)) == {"message": ANSWER}


def test_atomic_admission_offset_is_a_tombstone_after_completed_row_removal(tmp_path: Path) -> None:
    path = tmp_path / "telegram.sqlite3"
    inbox = _UpdateInbox(str(path))
    try:
        assert inbox.admit_polled_update(_update()) == (True, UPDATE_ID + 1)
        assert inbox.get_offset() == UPDATE_ID + 1
        inbox.remove(UPDATE_ID)

        # A duplicate page can arrive after the completed row itself is gone.
        # The durable offset remains its body-free tombstone.
        assert inbox.admit_polled_update(_update()) == (False, UPDATE_ID + 1)
        assert _row(inbox) is None
        assert inbox.get_offset() == UPDATE_ID + 1

        assert inbox.admit_polled_update(_update(UPDATE_ID - 1)) == (False, UPDATE_ID + 1)
        assert _row(inbox, UPDATE_ID - 1) is None
    finally:
        inbox.close()


@pytest.mark.parametrize("kind", ["full", "ioerr"])
@pytest.mark.parametrize("outcome", ["before", "after"])
def test_admission_commit_fault_never_splits_row_from_offset(
    tmp_path: Path,
    kind: FaultKind,
    outcome: CommitOutcome,
) -> None:
    inbox = _UpdateInbox(str(tmp_path / "telegram.sqlite3"))
    proxy = _wrap_connection(inbox)
    proxy.arm(kind, outcome)
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            inbox.admit_polled_update(_update())
        assert getattr(caught.value, "sqlite_errorcode", None) in {
            sqlite3.SQLITE_FULL,
            sqlite3.SQLITE_IOERR,
        }

        # A later unrelated commit must not publish a dirty pre-fault write.
        inbox.set_offset(inbox.get_offset())
        row_exists = _row(inbox) is not None
        offset_advanced = inbox.get_offset() == UPDATE_ID + 1
        assert row_exists is offset_advanced
        assert row_exists is (outcome == "after")
    finally:
        inbox.close()


def _create_legacy_inbox(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE updates (
                update_id INTEGER PRIMARY KEY,
                payload_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                backend_response_json TEXT,
                created_at REAL NOT NULL
            );
            CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO updates(update_id, payload_json, created_at)
            VALUES(7, '{"update_id":7}', 1);
            """
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize("layout", ["fresh", "legacy"])
@pytest.mark.parametrize("kind", ["full", "ioerr"])
def test_initialization_or_migration_fault_closes_connection_and_reopens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
    kind: FaultKind,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    if layout == "legacy":
        _create_legacy_inbox(path)

    real_connect = sqlite3.connect
    proxies: list[_CommitFaultConnection] = []

    def faulting_connect(*args: Any, **kwargs: Any) -> _CommitFaultConnection:
        proxy = _CommitFaultConnection(real_connect(*args, **kwargs))
        proxy.arm(kind, "before")
        proxies.append(proxy)
        return proxy

    monkeypatch.setattr(queue_module.sqlite3, "connect", faulting_connect)
    with pytest.raises(sqlite3.OperationalError):
        _UpdateInbox(str(path))
    assert len(proxies) == 1
    assert proxies[0].closed is True

    monkeypatch.setattr(queue_module.sqlite3, "connect", real_connect)
    reopened = _UpdateInbox(str(path))
    try:
        assert reopened.get_offset() == 0
        if layout == "legacy":
            assert _row(reopened, 7) is not None
            columns = {
                str(row["name"])
                for row in reopened._conn.execute("PRAGMA table_info(updates)").fetchall()  # noqa: SLF001
            }
            assert {"status", "ordering_key", "chunks_sent", "delivery_uncertainty"} <= columns
    finally:
        reopened.close()


@pytest.mark.parametrize("operation", ["status", "cache", "remove"])
@pytest.mark.parametrize("kind", ["full", "ioerr"])
@pytest.mark.parametrize("outcome", ["before", "after"])
def test_queue_mutation_fault_retains_exact_last_commit_state(
    tmp_path: Path,
    operation: QueueMutation,
    kind: FaultKind,
    outcome: CommitOutcome,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    inbox = _UpdateInbox(str(path))
    assert inbox.admit_polled_update(_update())[0] is True
    proxy = _wrap_connection(inbox)
    proxy.arm(kind, outcome)

    try:
        with pytest.raises(sqlite3.OperationalError):
            if operation == "status":
                inbox.mark_failure(UPDATE_ID, "OperationalError")
            elif operation == "cache":
                inbox.cache_backend_response(UPDATE_ID, {"message": ANSWER})
            else:
                inbox.remove_many([UPDATE_ID])

        # Prove the failed write cannot hitch a ride on an unrelated commit.
        inbox.set_offset(inbox.get_offset())
        _assert_mutation_state(inbox, operation, committed=outcome == "after")
    finally:
        inbox.close()

    reopened = _UpdateInbox(str(path))
    try:
        _assert_mutation_state(reopened, operation, committed=outcome == "after")
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["full", "ioerr"])
@pytest.mark.parametrize("outcome", ["before", "after"])
async def test_delivery_begin_fault_posts_zero_bytes_then_retries_exact_chunk(
    tmp_path: Path,
    kind: FaultKind,
    outcome: CommitOutcome,
) -> None:
    bridge = _bridge(tmp_path / "telegram.sqlite3")
    inbox = _opened(bridge)
    assert inbox.admit_polled_update(_update())[0] is True
    inbox.cache_backend_response(UPDATE_ID, {"message": ANSWER})
    proxy = _wrap_connection(inbox)
    proxy.arm(kind, outcome)
    telegram = _Telegram()

    try:
        with pytest.raises(sqlite3.OperationalError):
            await bridge._send_message(  # noqa: SLF001
                telegram,
                CHAT_ID,
                ANSWER,
                resume_key=UPDATE_ID,
                text_format="plain",
            )
        assert telegram.payloads == []
        failed = _row(inbox)
        assert failed is not None
        assert (int(failed["chunks_sent"]), int(failed["delivery_uncertainty"])) == (0, 0)

        await bridge._send_message(  # noqa: SLF001
            telegram,
            CHAT_ID,
            ANSWER,
            resume_key=UPDATE_ID,
            text_format="plain",
        )
        assert [payload["text"] for payload in telegram.payloads] == [ANSWER]
        recovered = _row(inbox)
        assert recovered is not None
        assert (int(recovered["chunks_sent"]), int(recovered["delivery_uncertainty"])) == (1, 0)
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["full", "ioerr"])
@pytest.mark.parametrize("outcome", ["before", "after"])
async def test_uncertainty_notice_begin_fault_posts_zero_bytes_then_retries_notice(
    tmp_path: Path,
    kind: FaultKind,
    outcome: CommitOutcome,
) -> None:
    bridge = _bridge(tmp_path / "telegram.sqlite3")
    inbox = _opened(bridge)
    assert inbox.admit_polled_update(_update())[0] is True
    inbox.cache_backend_response(UPDATE_ID, {"message": ANSWER})
    inbox.record_uncertain_answer_chunk(UPDATE_ID, 1)
    proxy = _wrap_connection(inbox)
    proxy.arm(kind, outcome)
    telegram = _Telegram()

    try:
        with pytest.raises(sqlite3.OperationalError):
            await bridge._send_message(  # noqa: SLF001
                telegram,
                CHAT_ID,
                ANSWER,
                resume_key=UPDATE_ID,
                text_format="plain",
            )
        assert telegram.payloads == []
        failed = _row(inbox)
        assert failed is not None
        assert (int(failed["chunks_sent"]), int(failed["delivery_uncertainty"])) == (1, 1)

        await bridge._send_message(  # noqa: SLF001
            telegram,
            CHAT_ID,
            ANSWER,
            resume_key=UPDATE_ID,
            text_format="plain",
        )
        assert [payload["text"] for payload in telegram.payloads] == [NOTICE]
        recovered = _row(inbox)
        assert recovered is not None
        assert (int(recovered["chunks_sent"]), int(recovered["delivery_uncertainty"])) == (1, 2)
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_chunk_5xx_keeps_uncertainty_and_restart_never_resends_answer(tmp_path: Path) -> None:
    path = tmp_path / "telegram.sqlite3"
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.admit_polled_update(_update())[0] is True
    inbox.cache_backend_response(UPDATE_ID, {"message": ANSWER})
    rejected = _Telegram([503])
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await first._send_message(  # noqa: SLF001
                rejected,
                CHAT_ID,
                ANSWER,
                resume_key=UPDATE_ID,
                text_format="plain",
            )
        uncertain = _row(inbox)
        assert uncertain is not None
        assert (int(uncertain["chunks_sent"]), int(uncertain["delivery_uncertainty"])) == (1, 1)
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    healed = _Telegram()
    try:
        await restarted._send_message(  # noqa: SLF001
            healed,
            CHAT_ID,
            ANSWER,
            resume_key=UPDATE_ID,
            text_format="plain",
        )
        assert [payload["text"] for payload in rejected.payloads] == [ANSWER]
        assert [payload["text"] for payload in healed.payloads] == [NOTICE]
        durable = _row(_opened(restarted))
        assert durable is not None and int(durable["delivery_uncertainty"]) == 2
    finally:
        restarted._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_uncertainty_notice_5xx_is_absorbing_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "telegram.sqlite3"
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.admit_polled_update(_update())[0] is True
    inbox.cache_backend_response(UPDATE_ID, {"message": ANSWER})
    inbox.record_uncertain_answer_chunk(UPDATE_ID, 1)
    rejected = _Telegram([503])
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await first._send_message(  # noqa: SLF001
                rejected,
                CHAT_ID,
                ANSWER,
                resume_key=UPDATE_ID,
                text_format="plain",
            )
        assert [payload["text"] for payload in rejected.payloads] == [NOTICE]
        durable = _row(inbox)
        assert durable is not None and int(durable["delivery_uncertainty"]) == 2
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    healed = _Telegram()
    try:
        await restarted._send_message(  # noqa: SLF001
            healed,
            CHAT_ID,
            ANSWER,
            resume_key=UPDATE_ID,
            text_format="plain",
        )
        assert healed.payloads == []
    finally:
        restarted._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["full", "ioerr"])
async def test_storage_error_spends_no_attempt_and_waits_before_redispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: FaultKind,
) -> None:
    bridge = _bridge(tmp_path / "telegram.sqlite3")
    inbox = _opened(bridge)
    assert inbox.admit_polled_update(_update())[0] is True
    process_calls = 0
    backoffs: list[int | None] = []

    async def fail_storage(*_args: Any, **_kwargs: Any) -> None:
        nonlocal process_calls
        process_calls += 1
        raise _sqlite_fault(kind)

    async def bounded_backoff(error: BaseException) -> None:
        backoffs.append(getattr(error, "sqlite_errorcode", None))
        # Stop only after proving the storage-specific backoff branch was used;
        # this keeps the direct test from scheduling its deliberate next retry.
        bridge._stopping = True  # noqa: SLF001

    monkeypatch.setattr(bridge, "_process_update", fail_storage)
    monkeypatch.setattr(transport_module, "_back_off_local_storage_failure", bounded_backoff)
    row = inbox.pending()[0]
    try:
        await bridge._run_update(object(), object(), row)  # noqa: SLF001
        durable = _row(inbox)
        assert durable is not None
        assert int(durable["attempts"]) == 0
        assert durable["last_error"] == ""
        assert process_calls == 1
        assert len(backoffs) == 1
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_removal_commit_fault_reuses_cache_and_cursor_without_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.admit_polled_update(_update())[0] is True
    proxy = _wrap_connection(inbox)
    telegram = _Telegram()
    backend_effects = 0

    async def process_first(
        _telegram: object,
        _backend: object,
        update: dict[str, Any],
        *,
        cached_response: dict[str, Any] | None,
    ) -> None:
        nonlocal backend_effects
        assert cached_response is None
        backend_effects += 1
        response = {"message": ANSWER}
        inbox.cache_backend_response(int(update["update_id"]), response)
        await first._send_message(  # noqa: SLF001
            telegram,
            CHAT_ID,
            ANSWER,
            resume_key=UPDATE_ID,
            text_format="plain",
        )
        proxy.arm("full", "before")

    async def stop_after_backoff(_error: BaseException) -> None:
        first._stopping = True  # noqa: SLF001

    monkeypatch.setattr(first, "_process_update", process_first)
    monkeypatch.setattr(transport_module, "_back_off_local_storage_failure", stop_after_backoff)
    try:
        await first._run_update(object(), object(), inbox.pending()[0])  # noqa: SLF001
        retained = _row(inbox)
        assert retained is not None
        assert retained["backend_response_json"] is not None
        assert int(retained["chunks_sent"]) == 1
        assert int(retained["attempts"]) == 0
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    reopened = _opened(restarted)
    restarted._stopping = True  # noqa: SLF001

    async def process_cached(
        _telegram: object,
        _backend: object,
        _update_payload: dict[str, Any],
        *,
        cached_response: dict[str, Any] | None,
    ) -> None:
        assert cached_response == {"message": ANSWER}
        await restarted._send_message(  # noqa: SLF001
            telegram,
            CHAT_ID,
            ANSWER,
            resume_key=UPDATE_ID,
            text_format="plain",
        )

    monkeypatch.setattr(restarted, "_process_update", process_cached)
    try:
        await restarted._run_update(object(), object(), reopened.pending()[0])  # noqa: SLF001
        assert _row(reopened) is None
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert backend_effects == 1
    assert [payload["text"] for payload in telegram.payloads] == [ANSWER]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["full", "ioerr"])
@pytest.mark.parametrize("outcome", ["before", "after"])
@pytest.mark.parametrize("terminal_path", ["permanent", "exhausted"])
async def test_terminal_commit_fault_reconciles_one_dead_letter_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: FaultKind,
    outcome: CommitOutcome,
    terminal_path: str,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    telegram = _Telegram()
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.admit_polled_update(_update())[0] is True
    if terminal_path == "exhausted":
        inbox._conn.execute(  # noqa: SLF001
            "UPDATE updates SET attempts=? WHERE update_id=?",
            (MAX_ATTEMPTS - 1, UPDATE_ID),
        )
        inbox._conn.commit()  # noqa: SLF001
    proxy = _wrap_connection(inbox)

    async def fail_terminally(*_args: Any, **_kwargs: Any) -> None:
        proxy.arm(kind, outcome)
        if terminal_path == "permanent":
            raise PermanentUpdateError("synthetic permanent update failure")
        raise RuntimeError("synthetic final retry failure")

    async def no_wait(_error: BaseException) -> None:
        return None

    monkeypatch.setattr(first, "_process_update", fail_terminally)
    monkeypatch.setattr(transport_module, "_back_off_local_storage_failure", no_wait)
    try:
        await first._run_update(telegram, object(), inbox.pending()[0])  # noqa: SLF001
        durable = _row(inbox)
        assert durable is not None
        assert str(durable["status"]) == "pending"
        assert str(durable["last_error"]).startswith("friday.telegram-dead-letter-notice.v1:") is (
            outcome == "after"
        )
        assert telegram.payloads == []
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    reopened = _opened(restarted)
    retried_processing = 0

    async def fail_without_storage_fault(*_args: Any, **_kwargs: Any) -> None:
        nonlocal retried_processing
        retried_processing += 1
        if terminal_path == "permanent":
            raise PermanentUpdateError("synthetic permanent update failure")
        raise RuntimeError("synthetic final retry failure")

    monkeypatch.setattr(restarted, "_process_update", fail_without_storage_fault)
    try:
        pending = reopened.pending(now=10**12)
        assert len(pending) == 1
        await restarted._run_update(telegram, object(), pending[0])  # noqa: SLF001
        durable = _row(reopened)
        assert durable is not None and durable["status"] == "dead_letter"
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert retried_processing == (1 if outcome == "before" else 0)
    assert len(telegram.payloads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["full", "ioerr"])
@pytest.mark.parametrize("terminal_path", ["permanent", "exhausted"])
async def test_quarantined_connection_keeps_terminal_notice_restartable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: FaultKind,
    terminal_path: str,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.admit_polled_update(_update())[0] is True
    if terminal_path == "exhausted":
        inbox._conn.execute(  # noqa: SLF001
            "UPDATE updates SET attempts=? WHERE update_id=?",
            (MAX_ATTEMPTS - 1, UPDATE_ID),
        )
        inbox._conn.commit()  # noqa: SLF001
    proxy = _wrap_connection(inbox)

    async def fail_terminally(*_args: Any, **_kwargs: Any) -> None:
        proxy.arm(kind, "after", rollback_fails=True)
        if terminal_path == "permanent":
            raise PermanentUpdateError("synthetic permanent update failure")
        raise RuntimeError("synthetic final retry failure")

    async def no_wait(_error: BaseException) -> None:
        return None

    monkeypatch.setattr(first, "_process_update", fail_terminally)
    monkeypatch.setattr(transport_module, "_back_off_local_storage_failure", no_wait)
    await first._run_update(_Telegram(), object(), inbox.pending()[0])  # noqa: SLF001
    assert proxy.closed is True
    first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    reopened = _opened(restarted)

    async def processing_must_not_repeat(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("durable terminal notice must bypass update processing")

    telegram = _Telegram()
    monkeypatch.setattr(restarted, "_process_update", processing_must_not_repeat)
    try:
        pending = reopened.pending(now=10**12)
        assert len(pending) == 1
        await restarted._run_update(telegram, object(), pending[0])  # noqa: SLF001
        durable = _row(reopened)
        assert durable is not None and durable["status"] == "dead_letter"
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert len(telegram.payloads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 503])
async def test_terminal_notice_uses_disjoint_cursor_after_existing_answer_parts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.admit_polled_update(_update())[0] is True
    inbox._conn.execute(  # noqa: SLF001
        "UPDATE updates SET attempts=?, chunks_sent=3 WHERE update_id=?",
        (MAX_ATTEMPTS - 1, UPDATE_ID),
    )
    inbox._conn.commit()  # noqa: SLF001

    async def exhaust(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("synthetic final retry failure")

    telegram = _Telegram(statuses=[status])
    monkeypatch.setattr(first, "_process_update", exhaust)
    try:
        await first._run_update(telegram, object(), inbox.pending()[0])  # noqa: SLF001
        durable = _row(inbox)
        assert durable is not None
        assert int(durable["chunks_sent"]) == 4
        assert durable["status"] == ("dead_letter" if status == 200 else "pending")
    finally:
        first._inbox.close()  # noqa: SLF001

    if status == 503:
        restarted = _bridge(path)
        reopened = _opened(restarted)

        async def processing_must_not_repeat(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("durable terminal notice must bypass update processing")

        monkeypatch.setattr(restarted, "_process_update", processing_must_not_repeat)
        try:
            pending = reopened.pending(now=10**12)
            assert len(pending) == 1
            await restarted._run_update(telegram, object(), pending[0])  # noqa: SLF001
            durable = _row(reopened)
            assert durable is not None and durable["status"] == "dead_letter"
        finally:
            restarted._inbox.close()  # noqa: SLF001

    texts = [str(payload["text"]) for payload in telegram.payloads]
    assert texts[0].startswith("⚠️ Не удалось обработать это сообщение")
    if status == 503:
        assert texts[1:] == [transport_module._DELIVERY_UNCERTAINTY_NOTICE]  # noqa: SLF001
    else:
        assert len(texts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 404, 413])
async def test_permanently_rejected_terminal_notice_cannot_head_block_follower(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    bridge = _bridge(tmp_path / "telegram.sqlite3")
    inbox = _opened(bridge)
    follower_id = UPDATE_ID + 1
    assert inbox.admit_polled_update(_update())[0] is True
    assert inbox.admit_polled_update(_update(follower_id))[0] is True
    inbox._conn.execute(  # noqa: SLF001
        "UPDATE updates SET attempts=? WHERE update_id=?",
        (MAX_ATTEMPTS - 1, UPDATE_ID),
    )
    inbox._conn.commit()  # noqa: SLF001

    async def exhaust(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("synthetic final retry failure")

    telegram = _Telegram(statuses=[status])
    monkeypatch.setattr(bridge, "_process_update", exhaust)
    bridge._stopping = True  # noqa: SLF001
    try:
        await bridge._run_update(telegram, object(), inbox.pending()[0])  # noqa: SLF001
        terminal = _row(inbox)
        assert terminal is not None and terminal["status"] == "dead_letter"
        assert int(terminal["attempts"]) == MAX_ATTEMPTS
        assert [int(row["update_id"]) for row in inbox.pending(now=10**12)] == [follower_id]
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert len(telegram.payloads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["ambiguous-send", "finish-before"])
async def test_album_reparse_resumes_existing_terminal_notice_without_rebasing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    sibling_id = UPDATE_ID + 1

    async def reject_album(*_args: Any, **_kwargs: Any) -> Any:
        error = PermanentUpdateError("synthetic permanent album rejection")
        error.update_ids = [UPDATE_ID, sibling_id]  # type: ignore[attr-defined]
        raise error

    async def no_wait(_error: BaseException) -> None:
        return None

    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.admit_polled_update(_update())[0] is True
    assert inbox.admit_polled_update(_update(sibling_id))[0] is True
    monkeypatch.setattr(first, "_collect_media_group", reject_album)
    monkeypatch.setattr(transport_module, "_back_off_local_storage_failure", no_wait)
    telegram = _Telegram(statuses=[503] if interruption == "ambiguous-send" else [200])

    if interruption == "finish-before":
        proxy = _wrap_connection(inbox)
        original_finish = inbox.finish_dead_letter_notice_many

        def fail_finish_once(update_ids: list[int]) -> None:
            proxy.arm("full", "before")
            original_finish(update_ids)

        monkeypatch.setattr(inbox, "finish_dead_letter_notice_many", fail_finish_once)

    try:
        await first._run_update(telegram, object(), inbox.pending()[0])  # noqa: SLF001
        first_row = _row(inbox)
        second_row = _row(inbox, sibling_id)
        assert first_row is not None and second_row is not None
        assert first_row["status"] == second_row["status"] == "pending"
        assert inbox.pending_dead_letter_notice([UPDATE_ID, sibling_id]) == (
            True,
            "PermanentUpdateError",
            0,
        )
        assert int(first_row["chunks_sent"]) == 1
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    reopened = _opened(restarted)
    monkeypatch.setattr(restarted, "_collect_media_group", reject_album)
    try:
        pending = reopened.pending(now=10**12)
        assert len(pending) == 1
        await restarted._run_update(telegram, object(), pending[0])  # noqa: SLF001
        assert _row(reopened)["status"] == "dead_letter"  # type: ignore[index]
        assert _row(reopened, sibling_id)["status"] == "dead_letter"  # type: ignore[index]
    finally:
        restarted._inbox.close()  # noqa: SLF001

    texts = [str(payload["text"]) for payload in telegram.payloads]
    terminal = [text for text in texts if text.startswith("⚠️ Не удалось обработать это сообщение")]
    assert len(terminal) == 1
    if interruption == "ambiguous-send":
        assert texts[-1] == transport_module._DELIVERY_UNCERTAINTY_NOTICE  # noqa: SLF001
