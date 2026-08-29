"""Poll admission, ephemeral credential, and retry-head recovery invariants."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import pytest

import friday.telegram_bridge._queue as queue_module
import friday.telegram_bridge._transport as transport_module
from friday.telegram_bridge import (
    MAX_ATTEMPTS,
    RETRY_DELAYS_SEC,
    PermanentUpdateError,
    TelegramBridge,
    TelegramConfig,
    _UpdateInbox,
)

CHAT_ID = 5001
UPDATE_ID = 980_001
ARCHIVE_PASSWORD = "Vault-secret-123"

FaultKind = Literal["full", "ioerr"]
CommitOutcome = Literal["before", "after"]
CleanupState = Literal["pending", "deleted", "terminal-notice"]


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
    """Delegate SQLite with one deterministic commit acknowledgement fault."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        object.__setattr__(self, "connection", connection)
        object.__setattr__(self, "fault", None)
        object.__setattr__(self, "closed", False)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"connection", "fault", "closed"}:
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
        self.closed = True
        self.connection.close()


class _DuplicatePollTelegram:
    """Return the same update twice, even after its durable offset advanced."""

    def __init__(
        self,
        bridge: TelegramBridge,
        first_update: dict[str, Any],
        duplicate_update: dict[str, Any],
    ) -> None:
        self.bridge = bridge
        self.pages = (first_update, duplicate_update)
        self.offsets: list[int] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        assert url.endswith("/getUpdates")
        payload = dict(kwargs.get("json") or {})
        self.offsets.append(int(payload.get("offset") or 0))
        if len(self.offsets) == 2:
            self.bridge._running = False  # noqa: SLF001
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            json={"ok": True, "result": [self.pages[len(self.offsets) - 1]]},
            request=request,
        )


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


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


def _message_update(
    update_id: int = UPDATE_ID,
    *,
    chat_id: int = CHAT_ID,
    text: str = "Обычное сообщение",
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": "Owner"},
            "text": text,
        },
    }


def _archive_update(*, password: str = ARCHIVE_PASSWORD) -> dict[str, Any]:
    return {
        "update_id": UPDATE_ID,
        "message": {
            "message_id": UPDATE_ID,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "first_name": "Owner"},
            "document": {
                "file_id": "telegram-archive-file",
                "file_unique_id": "telegram-archive-unique",
                "file_name": "protected.zip",
                "mime_type": "application/zip",
                "file_size": 128,
            },
            "caption": f"Открой архив\nпароль: {password}",
        },
    }


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


async def _no_storage_backoff(_error: BaseException) -> None:
    return None


@pytest.mark.asyncio
async def test_poll_loop_duplicate_page_keeps_password_until_durable_row_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    bridge = _bridge(path)
    duplicate_page = _DuplicatePollTelegram(
        bridge,
        _archive_update(),
        _archive_update(password="duplicate-page-must-not-replace-first"),
    )

    async def do_not_process_pending(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_journal(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(bridge, "_drain_inbox", do_not_process_pending)
    monkeypatch.setattr(bridge, "_journal_transition", no_journal)
    bridge._running = True  # noqa: SLF001
    try:
        await bridge._poll_loop(  # noqa: SLF001
            cast(httpx.AsyncClient, duplicate_page),
            cast(httpx.AsyncClient, object()),
        )
        inbox = _opened(bridge)
        durable = _row(inbox)
        assert durable is not None
        assert inbox.get_offset() == UPDATE_ID + 1
        assert duplicate_page.offsets == [0, UPDATE_ID + 1]
        assert bridge._archive_passwords == {UPDATE_ID: ARCHIVE_PASSWORD}  # noqa: SLF001
        assert ARCHIVE_PASSWORD not in str(durable["payload_json"])
        assert (
            int(
                inbox._conn.execute(  # noqa: SLF001
                    "SELECT COUNT(*) AS count FROM updates WHERE update_id=?",
                    (UPDATE_ID,),
                ).fetchone()["count"]
            )
            == 1
        )

        observed_passwords: list[str | None] = []

        async def process(
            _telegram: object,
            _backend: object,
            _update: dict[str, Any],
            *,
            cached_response: dict[str, Any] | None,
        ) -> None:
            assert cached_response is None
            observed_passwords.append(bridge._archive_passwords.get(UPDATE_ID))  # noqa: SLF001

        monkeypatch.setattr(bridge, "_process_update", process)
        bridge._stopping = True  # noqa: SLF001
        await bridge._run_update(  # noqa: SLF001
            cast(httpx.AsyncClient, object()),
            cast(httpx.AsyncClient, object()),
            inbox.pending()[0],
        )
        assert observed_passwords == [ARCHIVE_PASSWORD]
        assert UPDATE_ID not in bridge._archive_passwords  # noqa: SLF001
        assert _row(inbox) is None
        assert inbox.get_offset() == UPDATE_ID + 1
    finally:
        bridge._inbox.close()  # noqa: SLF001


def test_concurrent_stale_admission_cannot_outlive_offset_tombstone(tmp_path: Path) -> None:
    path = tmp_path / "telegram.sqlite3"
    owner = _UpdateInbox(str(path))
    contender_ready = threading.Event()
    start_contender = threading.Event()
    contender_entered = threading.Event()
    contender_finished = threading.Event()
    result: list[tuple[bool, int]] = []
    errors: list[BaseException] = []

    def contend() -> None:
        contender = _UpdateInbox(str(path))
        contender_ready.set()
        start_contender.wait()
        contender_entered.set()
        try:
            result.append(contender.admit_polled_update(_message_update()))
        except BaseException as exc:  # pragma: no cover - assertion carrier
            errors.append(exc)
        finally:
            contender.close()
            contender_finished.set()

    thread = threading.Thread(target=contend, daemon=True)
    thread.start()
    assert contender_ready.wait(timeout=2.0)
    try:
        # Hold the exact write lock an owner has while publishing admission and
        # its offset tombstone. The contender must block before reading offset,
        # not retain a stale scalar and insert after this transaction commits.
        owner._conn.execute("BEGIN IMMEDIATE")  # noqa: SLF001
        owner._conn.execute(  # noqa: SLF001
            "INSERT INTO state(key, value) VALUES('offset', ?)",
            (str(UPDATE_ID + 1),),
        )
        start_contender.set()
        assert contender_entered.wait(timeout=2.0)
        assert not contender_finished.wait(timeout=0.1)
        owner._conn.commit()  # noqa: SLF001
        owner.remove(UPDATE_ID)
        assert contender_finished.wait(timeout=2.0)
    finally:
        if owner._conn.in_transaction:  # noqa: SLF001
            owner._conn.rollback()  # noqa: SLF001
        owner.close()
        start_contender.set()
        thread.join(timeout=2.0)

    assert errors == []
    assert result == [(False, UPDATE_ID + 1)]
    reopened = _UpdateInbox(str(path))
    try:
        assert _row(reopened) is None
        assert reopened.get_offset() == UPDATE_ID + 1
    finally:
        reopened.close()


@pytest.mark.parametrize("kind", ["full", "ioerr"])
@pytest.mark.parametrize("outcome", ["before", "after"])
def test_admission_commit_fault_converges_after_process_restart(
    tmp_path: Path,
    kind: FaultKind,
    outcome: CommitOutcome,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    update = _message_update()
    inbox = _UpdateInbox(str(path))
    proxy = _wrap_connection(inbox)
    proxy.arm(kind, outcome)
    try:
        with pytest.raises(sqlite3.OperationalError):
            inbox.admit_polled_update(update)
    finally:
        inbox.close()

    restarted = _UpdateInbox(str(path))
    try:
        if outcome == "before":
            assert restarted.get_offset() == 0
            assert _row(restarted) is None
            assert restarted.admit_polled_update(update) == (True, UPDATE_ID + 1)
        else:
            assert restarted.get_offset() == UPDATE_ID + 1
            assert _row(restarted) is not None
            assert restarted.admit_polled_update(update) == (False, UPDATE_ID + 1)
    finally:
        restarted.close()

    final = _UpdateInbox(str(path))
    try:
        assert final.get_offset() == UPDATE_ID + 1
        assert (
            int(
                final._conn.execute(  # noqa: SLF001
                    "SELECT COUNT(*) AS count FROM updates WHERE update_id=?",
                    (UPDATE_ID,),
                ).fetchone()["count"]
            )
            == 1
        )
    finally:
        final.close()


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
def test_init_commit_after_fault_closes_and_reopens_complete_schema(
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
        proxy.arm(kind, "after")
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
        columns = {
            str(row["name"])
            for row in reopened._conn.execute("PRAGMA table_info(updates)").fetchall()  # noqa: SLF001
        }
        assert {"status", "next_attempt_at", "ordering_key", "chunks_sent"} <= columns
        assert "delivery_uncertainty" in columns
        if layout == "legacy":
            assert _row(reopened, 7) is not None
        else:
            assert reopened.admit_polled_update(_message_update()) == (True, UPDATE_ID + 1)
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "deleted", "terminal-notice"])
async def test_ephemeral_password_cleanup_uses_surviving_retry_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: CleanupState,
) -> None:
    bridge = _bridge(tmp_path / "telegram.sqlite3")
    inbox = _opened(bridge)
    assert inbox.store(_message_update()) is True
    bridge._archive_passwords[UPDATE_ID] = ARCHIVE_PASSWORD  # noqa: SLF001
    proxy = _wrap_connection(inbox) if state != "pending" else None

    async def process(
        _telegram: object,
        _backend: object,
        _update: dict[str, Any],
        *,
        cached_response: dict[str, Any] | None,
    ) -> None:
        assert cached_response is None
        if state == "pending":
            raise _sqlite_fault("full")
        assert proxy is not None
        proxy.arm("ioerr", "after")
        if state == "terminal-notice":
            raise PermanentUpdateError("synthetic permanent rejection")

    monkeypatch.setattr(bridge, "_process_update", process)
    monkeypatch.setattr(transport_module, "_back_off_local_storage_failure", _no_storage_backoff)
    bridge._stopping = True  # noqa: SLF001
    try:
        await bridge._run_update(  # noqa: SLF001
            cast(httpx.AsyncClient, object()),
            cast(httpx.AsyncClient, object()),
            inbox.pending()[0],
        )
        durable = _row(inbox)
        assert inbox.update_requires_retry(UPDATE_ID) is (state == "pending")
        assert (UPDATE_ID in bridge._archive_passwords) is (state == "pending")  # noqa: SLF001
        if state == "pending":
            assert durable is not None and durable["status"] == "pending"
        elif state == "deleted":
            assert durable is None
        else:
            assert durable is not None and durable["status"] == "pending"
            assert inbox.pending_dead_letter_notice([UPDATE_ID]) == (
                True,
                "PermanentUpdateError",
                0,
            )
    finally:
        bridge._inbox.close()  # noqa: SLF001


def test_same_chat_follower_runs_after_retry_budget_and_terminal_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox = _UpdateInbox(str(tmp_path / "telegram.sqlite3"))
    head_id = UPDATE_ID
    follower_id = UPDATE_ID + 1
    assert inbox.store(_message_update(head_id, text="head")) is True
    assert inbox.store(_message_update(follower_id, text="follower")) is True
    clock = _Clock(10_000.0)
    monkeypatch.setattr(queue_module, "time", clock)
    expected_delay = sum(
        RETRY_DELAYS_SEC[min(index, len(RETRY_DELAYS_SEC) - 1)] for index in range(MAX_ATTEMPTS - 1)
    )
    assert expected_delay == 85_002.0
    started_at = clock.now

    try:
        assert [int(row["update_id"]) for row in inbox.pending(now=clock.now)] == [head_id]
        for attempt in range(1, MAX_ATTEMPTS + 1):
            assert inbox.mark_failure_many([head_id], "SyntheticFailure") is (attempt == MAX_ATTEMPTS)
            durable = _row(inbox, head_id)
            assert durable is not None
            if attempt < MAX_ATTEMPTS:
                assert int(durable["attempts"]) == attempt
                next_attempt = float(durable["next_attempt_at"])
                assert inbox.pending(now=next_attempt - 0.001) == []
                clock.now = next_attempt
                assert [int(row["update_id"]) for row in inbox.pending(now=clock.now)] == [head_id]
            else:
                assert int(durable["attempts"]) == MAX_ATTEMPTS - 1

        assert clock.now - started_at == 85_002.0
        assert inbox.pending_dead_letter_notice([head_id]) == (
            False,
            "SyntheticFailure",
            0,
        )
        assert [int(row["update_id"]) for row in inbox.pending(now=10**12)] == [head_id]
        inbox.finish_dead_letter_notice_many([head_id])
        dead = _row(inbox, head_id)
        assert dead is not None
        assert dead["status"] == "dead_letter"
        assert int(dead["attempts"]) == MAX_ATTEMPTS
        assert inbox.update_requires_retry(head_id) is False
        assert [int(row["update_id"]) for row in inbox.pending(now=clock.now)] == [follower_id]
    finally:
        inbox.close()
