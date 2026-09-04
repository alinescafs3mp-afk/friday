"""Completion fences for non-replayable Telegram updates and reply parts."""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import pytest

import friday.telegram_bridge._commands as commands_module
import friday.telegram_bridge._transport as transport_module
from friday.telegram_bridge import TelegramBridge, TelegramConfig, _UpdateInbox

CHAT_ID = 5001
UPDATE_ID = 970_001
ORIGINAL_MESSAGE_ID = 801
BODY_CANARY = "PRIVATE-UPDATE-BODY-MUST-NOT-ENTER-WITNESS"
ANSWER = "Кэшированный ответ с дополнительным результатом."
DELIVERY_UNKNOWN = "доставка не подтверждена, не дублирую; повторите запрос если фрагмент не пришёл"

FaultKind = Literal["full", "ioerr"]
CommitOutcome = Literal["before", "after"]
ArtifactKind = Literal["voice", "file"]
FailureMode = Literal[
    "connect",
    "timeout",
    "hard-crash",
    "confirm-full-before",
    "confirm-full-after",
    "confirm-ioerr-before",
    "confirm-ioerr-after",
]


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
    """One-shot commit fault with an explicit before/after durable outcome."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.fault: tuple[FaultKind, CommitOutcome] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)

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


class _HardCrash(BaseException):
    """Synthetic process death outside ordinary bridge exception handlers."""


class _Telegram:
    """Record accepted network effects and inject one artifact failure."""

    def __init__(
        self,
        *,
        target: str = "",
        mode: FailureMode | None = None,
        target_ordinal: int = 1,
        commit_proxy: _CommitFaultConnection | None = None,
    ) -> None:
        self.target = target
        self.mode = mode
        self.target_ordinal = target_ordinal
        self.commit_proxy = commit_proxy
        self.target_attempts = 0
        self.failure_fired = False
        self.accepted: list[dict[str, Any]] = []

    @staticmethod
    def _record(endpoint: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {"endpoint": endpoint}
        payload = kwargs.get("json")
        if isinstance(payload, dict):
            record["text"] = payload.get("text")
        files = kwargs.get("files")
        if isinstance(files, dict) and files:
            value = next(iter(files.values()))
            if isinstance(value, tuple) and value:
                record["filename"] = value[0]
        return record

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        endpoint = url.rsplit("/", 1)[-1]
        request = httpx.Request("POST", url)
        targeted = endpoint == self.target
        if targeted:
            self.target_attempts += 1
        should_fail = (
            targeted
            and self.target_attempts == self.target_ordinal
            and not self.failure_fired
            and self.mode is not None
        )
        if should_fail:
            self.failure_fired = True
            if self.mode == "connect":
                raise httpx.ConnectError("synthetic pre-accept disconnect", request=request)
            self.accepted.append(self._record(endpoint, kwargs))
            if self.mode == "timeout":
                raise httpx.ReadTimeout("synthetic accepted-write timeout", request=request)
            if self.mode == "hard-crash":
                raise _HardCrash("synthetic hard crash after acceptance")
            assert self.commit_proxy is not None
            outcome: CommitOutcome = "before" if self.mode.endswith("before") else "after"
            kind: FaultKind = "ioerr" if "ioerr" in self.mode else "full"
            self.commit_proxy.arm(kind, outcome)
        else:
            self.accepted.append(self._record(endpoint, kwargs))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 80_000 + len(self.accepted)}},
            request=request,
        )


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


def _message_update(text: str, *, update_id: int = UPDATE_ID) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": ORIGINAL_MESSAGE_ID,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "first_name": "Owner"},
            "text": text,
        },
    }


def _edited_update(*, update_id: int = UPDATE_ID) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "edited_message": {
            "message_id": ORIGINAL_MESSAGE_ID,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "first_name": "Owner"},
            "text": "Исправленный текст",
        },
    }


def _callback_update(*, update_id: int = UPDATE_ID) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "callback-completion-fence",
            "from": {"id": CHAT_ID, "first_name": "Owner"},
            "message": {
                "message_id": ORIGINAL_MESSAGE_ID,
                "chat": {"id": CHAT_ID, "type": "private"},
                "reply_markup": {
                    "inline_keyboard": [[{"text": "👍", "callback_data": "feedback:up:msg_completion_1"}]]
                },
            },
            "data": "feedback:up:msg_completion_1",
        },
    }


def _row(inbox: _UpdateInbox, update_id: int = UPDATE_ID) -> dict[str, Any] | None:
    found = inbox._conn.execute(  # noqa: SLF001
        "SELECT * FROM updates WHERE update_id=?",
        (update_id,),
    ).fetchone()
    return dict(found) if found is not None else None


def _raw_cache(inbox: _UpdateInbox, update_id: int = UPDATE_ID) -> str | None:
    row = _row(inbox, update_id)
    if row is None or row["backend_response_json"] is None:
        return None
    return str(row["backend_response_json"])


def _wrap_connection(inbox: _UpdateInbox) -> _CommitFaultConnection:
    proxy = _CommitFaultConnection(inbox._conn)  # noqa: SLF001
    inbox._conn = proxy  # type: ignore[assignment] # noqa: SLF001
    return proxy


def _client(telegram: _Telegram) -> httpx.AsyncClient:
    return cast(httpx.AsyncClient, telegram)


async def _no_storage_backoff(_error: BaseException) -> None:
    return None


async def _run_pending(
    bridge: TelegramBridge,
    telegram: _Telegram,
    *,
    update_id: int = UPDATE_ID,
) -> bool:
    rows = bridge._inbox.pending(now=time.time() + 86_400)  # noqa: SLF001
    row = next((item for item in rows if int(item["update_id"]) == update_id), None)
    if row is None:
        return False
    bridge._stopping = True  # noqa: SLF001
    await bridge._run_update(_client(telegram), cast(httpx.AsyncClient, object()), row)  # noqa: SLF001
    return True


def _patch_backend_json(
    monkeypatch: pytest.MonkeyPatch,
    bridge: TelegramBridge,
    calls: list[tuple[str, str]],
) -> None:
    async def backend_json(
        _backend: object,
        method: str,
        path: str,
        _payload: object,
        _external_user_id: str,
        _chat_id: str,
    ) -> dict[str, Any]:
        calls.append((method, path))
        return {}

    monkeypatch.setattr(bridge, "_backend_json", backend_json)


def _arm_remove_after_processing(
    monkeypatch: pytest.MonkeyPatch,
    bridge: TelegramBridge,
    proxy: _CommitFaultConnection,
    *,
    kind: FaultKind,
    outcome: CommitOutcome,
) -> None:
    process = bridge._process_update  # noqa: SLF001

    async def process_then_arm(*args: Any, **kwargs: Any) -> None:
        await process(*args, **kwargs)
        proxy.arm(kind, outcome)

    monkeypatch.setattr(bridge, "_process_update", process_then_arm)


def _artifact_response(kind: ArtifactKind) -> dict[str, Any]:
    response: dict[str, Any] = {"message": ANSWER, "message_format": "plain"}
    if kind == "voice":
        response["voice"] = {
            "kind": "voice",
            "audio_base64": base64.b64encode(b"OggS-accepted-voice").decode("ascii"),
        }
    else:
        response["files"] = [
            {
                "id": "artifact-completion-1",
                "filename": "result-one.bin",
                "mime_type": "application/octet-stream",
                "content_base64": base64.b64encode(b"first-file-bytes").decode("ascii"),
            }
        ]
    return response


def _accepted(telegrams: list[_Telegram], endpoint: str) -> list[dict[str, Any]]:
    return [
        record for telegram in telegrams for record in telegram.accepted if record["endpoint"] == endpoint
    ]


def test_claim_witness_is_canonical_body_free_and_opaque(tmp_path: Path) -> None:
    path = tmp_path / "telegram.sqlite3"
    inbox = _UpdateInbox(str(path))
    update = _message_update(BODY_CANARY)
    try:
        assert inbox.store(update) is True
        assert inbox.claim_update_effect_attempt(UPDATE_ID, "command") == "claimed"
        raw = _raw_cache(inbox)
        assert raw is not None
        document = json.loads(raw)
        assert set(document) == {"fingerprint_sha256", "kind", "schema"}
        assert document["kind"] == "command"
        assert document["schema"] == "friday.telegram-update-effect-attempt.v1"
        assert len(document["fingerprint_sha256"]) == 64
        int(document["fingerprint_sha256"], 16)
        assert raw == json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        assert BODY_CANARY not in raw
        assert "update_id" not in raw
        assert "chat_id" not in raw
        assert "message" not in raw
        assert inbox.update_effect_attempt_kind(UPDATE_ID) == "command"
        assert inbox.claim_update_effect_attempt(UPDATE_ID, "command") == "already_attempted"
    finally:
        inbox.close()


@pytest.mark.parametrize("kind", ["full", "ioerr"])
@pytest.mark.parametrize("outcome", ["before", "after"])
def test_claim_commit_fault_restores_one_unsplit_retryable_state(
    tmp_path: Path,
    kind: FaultKind,
    outcome: CommitOutcome,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    inbox = _UpdateInbox(str(path))
    assert inbox.store(_message_update(BODY_CANARY)) is True
    proxy = _wrap_connection(inbox)
    proxy.arm(kind, outcome)
    try:
        with pytest.raises(sqlite3.OperationalError):
            inbox.claim_update_effect_attempt(UPDATE_ID, "command")

        # An unrelated later commit must not publish a pre-fault witness.
        inbox.set_offset(11)
        assert _raw_cache(inbox) is None
        assert inbox.update_effect_attempt_kind(UPDATE_ID) is None
        assert inbox.claim_update_effect_attempt(UPDATE_ID, "command") == "claimed"
        assert inbox.update_effect_attempt_kind(UPDATE_ID) == "command"
    finally:
        inbox.close()

    reopened = _UpdateInbox(str(path))
    try:
        assert reopened.update_effect_attempt_kind(UPDATE_ID) == "command"
    finally:
        reopened.close()


@pytest.mark.parametrize("kind", ["full", "ioerr"])
@pytest.mark.parametrize("outcome", ["before", "after"])
def test_proven_pre_effect_release_commit_fault_converges_safely(
    tmp_path: Path,
    kind: FaultKind,
    outcome: CommitOutcome,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    inbox = _UpdateInbox(str(path))
    assert inbox.store(_message_update("/new")) is True
    assert inbox.claim_update_effect_attempt(UPDATE_ID, "command") == "claimed"
    proxy = _wrap_connection(inbox)
    proxy.arm(kind, outcome)
    try:
        with pytest.raises(sqlite3.OperationalError):
            inbox.release_update_effect_attempt(UPDATE_ID, "command")
    finally:
        inbox.close()

    reopened = _UpdateInbox(str(path))
    try:
        assert reopened.update_effect_attempt_kind(UPDATE_ID) == ("command" if outcome == "before" else None)
        if outcome == "before":
            assert reopened.release_update_effect_attempt(UPDATE_ID, "command") is True
        assert reopened.claim_update_effect_attempt(UPDATE_ID, "command") == "claimed"
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["full", "ioerr"])
@pytest.mark.parametrize("outcome", ["before", "after"])
async def test_command_completion_fault_never_repeats_effect_or_final_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: FaultKind,
    outcome: CommitOutcome,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    update = _message_update("/new")
    backend_calls: list[tuple[str, str]] = []
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(update) is True
    proxy = _wrap_connection(inbox)
    _patch_backend_json(monkeypatch, first, backend_calls)
    _arm_remove_after_processing(monkeypatch, first, proxy, kind=kind, outcome=outcome)
    first_telegram = _Telegram()
    monkeypatch.setattr(transport_module, "_back_off_local_storage_failure", _no_storage_backoff)
    try:
        assert await _run_pending(first, first_telegram) is True
        assert (_row(inbox) is None) is (outcome == "after")
    finally:
        first._inbox.close()  # noqa: SLF001

    retries: list[_Telegram] = []
    if outcome == "before":
        # Fail removal once more after the UNKNOWN was durably delivered.  A
        # second restart must consume the row without a second UNKNOWN.
        retry = _bridge(path)
        retry_inbox = _opened(retry)
        retry_proxy = _wrap_connection(retry_inbox)
        _patch_backend_json(monkeypatch, retry, backend_calls)
        _arm_remove_after_processing(
            monkeypatch,
            retry,
            retry_proxy,
            kind="ioerr",
            outcome="before",
        )
        retry_telegram = _Telegram()
        retries.append(retry_telegram)
        try:
            assert await _run_pending(retry, retry_telegram) is True
            assert _row(retry_inbox) is not None
        finally:
            retry._inbox.close()  # noqa: SLF001

        final = _bridge(path)
        _patch_backend_json(monkeypatch, final, backend_calls)
        final_telegram = _Telegram()
        retries.append(final_telegram)
        try:
            assert await _run_pending(final, final_telegram) is True
            assert _row(_opened(final)) is None
        finally:
            final._inbox.close()  # noqa: SLF001

    all_telegrams = [first_telegram, *retries]
    texts = [str(record.get("text") or "") for record in _accepted(all_telegrams, "sendMessage")]
    final_reply = "Новый диалог начат в обычном режиме. Сама база знаний не очищена."
    assert texts.count(final_reply) == 1
    assert texts.count(commands_module._UPDATE_EFFECT_UNCERTAINTY_NOTICE) <= 1  # noqa: SLF001
    assert backend_calls == [("POST", "/api/conversations/channel/reset")]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["full", "ioerr"])
@pytest.mark.parametrize("outcome", ["before", "after"])
async def test_callback_completion_fault_replays_only_bounded_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: FaultKind,
    outcome: CommitOutcome,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    update = _callback_update()
    backend_calls: list[tuple[str, str]] = []
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(update) is True
    proxy = _wrap_connection(inbox)
    _patch_backend_json(monkeypatch, first, backend_calls)
    _arm_remove_after_processing(monkeypatch, first, proxy, kind=kind, outcome=outcome)
    first_telegram = _Telegram()
    monkeypatch.setattr(transport_module, "_back_off_local_storage_failure", _no_storage_backoff)
    try:
        assert await _run_pending(first, first_telegram) is True
    finally:
        first._inbox.close()  # noqa: SLF001

    retry_telegram = _Telegram()
    if outcome == "before":
        retry = _bridge(path)
        _patch_backend_json(monkeypatch, retry, backend_calls)
        try:
            assert await _run_pending(retry, retry_telegram) is True
            assert _row(_opened(retry)) is None
        finally:
            retry._inbox.close()  # noqa: SLF001

    telegrams = [first_telegram, retry_telegram]
    texts = [str(record.get("text") or "") for record in _accepted(telegrams, "sendMessage")]
    callback_acks = _accepted(telegrams, "answerCallbackQuery")
    assert backend_calls == [("POST", "/api/feedback")]
    assert (
        texts.count(
            "Исход нажатия неизвестен; автоматически действие не повторяю. "
            "Проверьте состояние и при необходимости нажмите кнопку снова."
        )
        <= 1
    )
    assert len(callback_acks) == 1 + (1 if outcome == "before" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["edited", "local"])
@pytest.mark.parametrize("kind", ["full", "ioerr"])
async def test_early_local_completion_fault_never_repeats_original_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    kind: FaultKind,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    update = _edited_update() if surface == "edited" else _message_update("/note")
    backend_calls: list[tuple[str, str]] = []
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(update) is True
    proxy = _wrap_connection(inbox)
    _patch_backend_json(monkeypatch, first, backend_calls)
    _arm_remove_after_processing(monkeypatch, first, proxy, kind=kind, outcome="before")
    first_telegram = _Telegram()
    monkeypatch.setattr(transport_module, "_back_off_local_storage_failure", _no_storage_backoff)
    try:
        assert await _run_pending(first, first_telegram) is True
        assert inbox.update_effect_attempt_kind(UPDATE_ID) == (
            "edited-message" if surface == "edited" else "local-reply"
        )
    finally:
        first._inbox.close()  # noqa: SLF001

    retry = _bridge(path)
    _patch_backend_json(monkeypatch, retry, backend_calls)
    retry_telegram = _Telegram()
    try:
        assert await _run_pending(retry, retry_telegram) is True
        assert _row(_opened(retry)) is None
    finally:
        retry._inbox.close()  # noqa: SLF001

    first_texts = [str(item.get("text") or "") for item in _accepted([first_telegram], "sendMessage")]
    retry_texts = [str(item.get("text") or "") for item in _accepted([retry_telegram], "sendMessage")]
    assert len(first_texts) == 1
    assert first_texts[0] not in retry_texts
    assert retry_texts in [[], [commands_module._UPDATE_EFFECT_UNCERTAINTY_NOTICE]]  # noqa: SLF001
    expected_backend = [] if surface == "edited" else [("GET", "/api/me")]
    assert backend_calls == expected_backend


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact", ["voice", "file"])
@pytest.mark.parametrize(
    "mode",
    [
        "connect",
        "timeout",
        "hard-crash",
        "confirm-full-before",
        "confirm-full-after",
        "confirm-ioerr-before",
        "confirm-ioerr-after",
    ],
)
async def test_cached_artifact_failure_never_duplicates_and_connect_retries_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: ArtifactKind,
    mode: FailureMode,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    response = _artifact_response(artifact)
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(_message_update("Обычный запрос с артефактом")) is True
    inbox.cache_backend_response(UPDATE_ID, response)
    proxy = _wrap_connection(inbox) if mode.startswith("confirm-") else None
    endpoint = "sendVoice" if artifact == "voice" else "sendDocument"
    faulted = _Telegram(target=endpoint, mode=mode, commit_proxy=proxy)
    monkeypatch.setattr(transport_module, "_back_off_local_storage_failure", _no_storage_backoff)
    try:
        if mode == "hard-crash":
            with pytest.raises(_HardCrash, match="synthetic hard crash"):
                await _run_pending(first, faulted)
        else:
            assert await _run_pending(first, faulted) is True
        durable = _row(inbox)
        assert durable is not None
        expected_cursor = 1 if mode == "connect" else 2
        expected_uncertainty = int(
            mode
            in {
                "timeout",
                "hard-crash",
                "confirm-full-before",
                "confirm-ioerr-before",
            }
        )
        assert int(durable["chunks_sent"]) == expected_cursor
        assert int(durable["delivery_uncertainty"]) == expected_uncertainty
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    healed = _Telegram()
    try:
        assert await _run_pending(restarted, healed) is True
        assert _row(_opened(restarted)) is None
    finally:
        restarted._inbox.close()  # noqa: SLF001

    artifact_posts = _accepted([faulted, healed], endpoint)
    assert len(artifact_posts) == 1
    if mode == "connect":
        assert _accepted([faulted], endpoint) == []
        assert len(_accepted([healed], endpoint)) == 1
    else:
        assert len(_accepted([faulted], endpoint)) == 1
        assert _accepted([healed], endpoint) == []
    texts = [str(item.get("text") or "") for item in _accepted([faulted, healed], "sendMessage")]
    assert texts.count(ANSWER) == 1
    assert texts.count(DELIVERY_UNKNOWN) <= 1


@pytest.mark.asyncio
async def test_retry_command_caches_one_response_and_resumes_its_file_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    response = _artifact_response("file")
    backend_calls: list[tuple[str, str]] = []
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(_message_update("/retry")) is True

    async def backend_json(
        _backend: object,
        method: str,
        endpoint: str,
        _payload: object,
        _external_user_id: str,
        _chat_id: str,
    ) -> dict[str, Any]:
        backend_calls.append((method, endpoint))
        return response if endpoint == "/api/me/regenerate" else {}

    monkeypatch.setattr(first, "_backend_json", backend_json)
    rejected = _Telegram(target="sendDocument", mode="connect")
    try:
        assert await _run_pending(first, rejected) is True
        durable = _row(inbox)
        assert durable is not None
        assert json.loads(str(durable["backend_response_json"])) == response
        assert (int(durable["chunks_sent"]), int(durable["delivery_uncertainty"])) == (1, 0)
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)

    async def unexpected_backend(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("cached /retry response must not regenerate")

    monkeypatch.setattr(restarted, "_backend_json", unexpected_backend)
    healed = _Telegram()
    try:
        assert await _run_pending(restarted, healed) is True
        assert _row(_opened(restarted)) is None
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert backend_calls == [("GET", "/api/me"), ("POST", "/api/me/regenerate")]
    assert len(_accepted([rejected, healed], "sendMessage")) == 1
    assert len(_accepted([rejected, healed], "sendDocument")) == 1


@pytest.mark.asyncio
async def test_multi_file_cursor_packs_one_archive_and_absorbs_timeout(
    tmp_path: Path,
) -> None:
    from friday.orchestration.operation_result_carrier import OPERATION_RESULT_ARCHIVE_FILENAME

    path = tmp_path / "telegram.sqlite3"
    response = {
        "message": ANSWER,
        "message_format": "plain",
        "files": [
            {
                "id": "artifact-ordered-1",
                "filename": "first.bin",
                "content_base64": base64.b64encode(b"first").decode("ascii"),
            },
            {
                "id": "artifact-ordered-2",
                "filename": "second.bin",
                "content_base64": base64.b64encode(b"second").decode("ascii"),
            },
        ],
    }
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(_message_update("Верни два файла")) is True
    inbox.cache_backend_response(UPDATE_ID, response)
    faulted = _Telegram(target="sendDocument", mode="timeout", target_ordinal=1)
    try:
        assert await _run_pending(first, faulted) is True
        durable = _row(inbox)
        assert durable is not None
        assert (int(durable["chunks_sent"]), int(durable["delivery_uncertainty"])) == (2, 1)
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    healed = _Telegram()
    try:
        assert await _run_pending(restarted, healed) is True
        marker_count = (
            _opened(restarted)
            ._conn.execute(  # noqa: SLF001
                "SELECT COUNT(*) AS count FROM delivered_generated_files"
            )
            .fetchone()
        )
        assert marker_count is not None and int(marker_count["count"]) == 1
    finally:
        restarted._inbox.close()  # noqa: SLF001

    documents = _accepted([faulted], "sendDocument")
    assert [item["filename"] for item in documents] == [OPERATION_RESULT_ARCHIVE_FILENAME]
    assert _accepted([healed], "sendDocument") == []
    healed_texts = [str(item.get("text") or "") for item in _accepted([healed], "sendMessage")]
    assert healed_texts == [DELIVERY_UNKNOWN]


@pytest.mark.asyncio
async def test_legacy_generated_file_marker_advances_new_ordered_cursor_without_upload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    response = _artifact_response("file")
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(_message_update("Старый marker для файла")) is True
    inbox.cache_backend_response(UPDATE_ID, response)
    inbox.record_answer_chunks_sent(UPDATE_ID, 1)
    telegram = _Telegram()
    try:
        assert (
            await first._deliver_generated_files(  # noqa: SLF001
                _client(telegram),
                CHAT_ID,
                response,
            )
            == 1
        )
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    try:
        assert (
            await restarted._deliver_generated_files(  # noqa: SLF001
                _client(telegram),
                CHAT_ID,
                response,
                resume_key=UPDATE_ID,
                after_part=1,
            )
            == 2
        )
        durable = _row(_opened(restarted))
        assert durable is not None
        assert (int(durable["chunks_sent"]), int(durable["delivery_uncertainty"])) == (2, 0)
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert len(_accepted([telegram], "sendDocument")) == 1
