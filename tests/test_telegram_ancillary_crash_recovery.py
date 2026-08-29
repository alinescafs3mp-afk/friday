"""Crash and ambiguous-acceptance regressions for Telegram ancillary effects."""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import pytest

import friday.telegram_bridge._callbacks as callbacks_module
import friday.telegram_bridge._commands as commands_module
from friday.telegram_bridge import TelegramBridge, TelegramConfig, _UpdateInbox

CHAT_ID = 5001
UPDATE_ID = 980_001
MESSAGE_ID = 901
PROMPT_MESSAGE_ID = 902
ANSWER = "Ответ с дополнительным Telegram-результатом."
DELIVERY_UNKNOWN = "доставка не подтверждена, не дублирую; повторите запрос если фрагмент не пришёл"

BackendSurface = Literal["command", "callback"]
ArtifactKind = Literal["voice", "file"]
DeliveryFailure = Literal["503", "hard-crash"]
BackendFailure = Literal["503", "read-timeout"]


class _HardCrash(BaseException):
    """Synthetic process death outside the bridge's ordinary exception rail."""


class _Telegram:
    """Record possibly accepted effects and fail one exact Telegram request."""

    def __init__(
        self,
        *,
        target: str = "",
        failure: DeliveryFailure | None = None,
        target_ordinal: int = 1,
    ) -> None:
        self.target = target
        self.failure = failure
        self.target_ordinal = target_ordinal
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
        if endpoint == self.target:
            self.target_attempts += 1
        should_fail = (
            endpoint == self.target
            and self.target_attempts == self.target_ordinal
            and not self.failure_fired
            and self.failure is not None
        )
        self.accepted.append(self._record(endpoint, kwargs))
        if should_fail:
            self.failure_fired = True
            if self.failure == "hard-crash":
                raise _HardCrash("synthetic process death after possible Telegram acceptance")
            return httpx.Response(
                503,
                json={"ok": False, "description": "synthetic ambiguous upstream failure"},
                request=request,
            )
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 90_000 + len(self.accepted)}},
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


def _message_update(text: str, *, reply_to_message_id: int | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": MESSAGE_ID,
        "chat": {"id": CHAT_ID, "type": "private"},
        "from": {"id": CHAT_ID, "first_name": "Owner"},
        "text": text,
    }
    if reply_to_message_id is not None:
        message["reply_to_message"] = {
            "message_id": reply_to_message_id,
            "chat": {"id": CHAT_ID, "type": "private"},
        }
    return {"update_id": UPDATE_ID, "message": message}


def _callback_update() -> dict[str, Any]:
    return {
        "update_id": UPDATE_ID,
        "callback_query": {
            "id": "callback-hard-crash-recovery",
            "from": {"id": CHAT_ID, "first_name": "Owner"},
            "message": {
                "message_id": MESSAGE_ID,
                "chat": {"id": CHAT_ID, "type": "private"},
                "reply_markup": {
                    "inline_keyboard": [[{"text": "👍", "callback_data": "feedback:up:msg_crash_recovery"}]]
                },
            },
            "data": "feedback:up:msg_crash_recovery",
        },
    }


def _artifact_response(kind: ArtifactKind, *, truncated: bool = False) -> dict[str, Any]:
    response: dict[str, Any] = {"message": ANSWER, "message_format": "plain"}
    if kind == "voice":
        response["voice"] = {
            "kind": "voice",
            "audio_base64": base64.b64encode(b"OggS-ancillary-crash-recovery").decode("ascii"),
            "truncated": truncated,
        }
    else:
        response["files"] = [
            {
                "id": "artifact-ambiguous-503",
                "filename": "ambiguous.bin",
                "mime_type": "application/octet-stream",
                "content_base64": base64.b64encode(b"possibly-accepted-file").decode("ascii"),
            }
        ]
    return response


def _row(inbox: _UpdateInbox) -> dict[str, Any] | None:
    found = inbox._conn.execute(  # noqa: SLF001
        "SELECT * FROM updates WHERE update_id=?",
        (UPDATE_ID,),
    ).fetchone()
    return dict(found) if found is not None else None


def _client(telegram: _Telegram) -> httpx.AsyncClient:
    return cast(httpx.AsyncClient, telegram)


async def _run_pending(bridge: TelegramBridge, telegram: _Telegram) -> bool:
    row = next(
        (
            item
            for item in bridge._inbox.pending(now=time.time() + 86_400)  # noqa: SLF001
            if int(item["update_id"]) == UPDATE_ID
        ),
        None,
    )
    if row is None:
        return False
    bridge._stopping = True  # noqa: SLF001
    await bridge._run_update(  # noqa: SLF001
        _client(telegram),
        cast(httpx.AsyncClient, object()),
        row,
    )
    return True


def _accepted(telegrams: list[_Telegram], endpoint: str) -> list[dict[str, Any]]:
    return [item for telegram in telegrams for item in telegram.accepted if item["endpoint"] == endpoint]


def _patch_accepted_backend_crash(
    monkeypatch: pytest.MonkeyPatch,
    bridge: TelegramBridge,
    calls: list[tuple[str, str]],
) -> None:
    async def accepted_then_crash(
        _backend: object,
        method: str,
        endpoint: str,
        _payload: object,
        _external_user_id: str,
        _chat_id: str,
    ) -> dict[str, Any]:
        calls.append((method, endpoint))
        raise _HardCrash("synthetic process death after possible backend acceptance")

    monkeypatch.setattr(bridge, "_backend_json", accepted_then_crash)


def _patch_backend_must_not_repeat(
    monkeypatch: pytest.MonkeyPatch,
    bridge: TelegramBridge,
    unexpected_calls: list[tuple[str, str]],
) -> None:
    async def backend_json(
        _backend: object,
        method: str,
        endpoint: str,
        _payload: object,
        _external_user_id: str,
        _chat_id: str,
    ) -> dict[str, Any]:
        unexpected_calls.append((method, endpoint))
        return {}

    monkeypatch.setattr(bridge, "_backend_json", backend_json)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["command", "callback"])
async def test_hard_crash_after_command_or_callback_backend_accept_never_repeats_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: BackendSurface,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    update = _message_update("/new") if surface == "command" else _callback_update()
    expected_call = (
        ("POST", "/api/conversations/channel/reset") if surface == "command" else ("POST", "/api/feedback")
    )
    expected_kind = "command" if surface == "command" else "callback"
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(update) is True
    backend_effects: list[tuple[str, str]] = []
    _patch_accepted_backend_crash(monkeypatch, first, backend_effects)
    first_telegram = _Telegram()
    try:
        with pytest.raises(_HardCrash, match="possible backend acceptance"):
            await _run_pending(first, first_telegram)
        assert inbox.update_effect_attempt_kind(UPDATE_ID) == expected_kind
        assert _row(inbox) is not None
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    unexpected_backend: list[tuple[str, str]] = []
    _patch_backend_must_not_repeat(monkeypatch, restarted, unexpected_backend)
    healed = _Telegram()
    try:
        assert await _run_pending(restarted, healed) is True
        assert _row(_opened(restarted)) is None
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert backend_effects == [expected_call]
    assert unexpected_backend == []
    assert first_telegram.accepted == []
    texts = [str(item.get("text") or "") for item in _accepted([healed], "sendMessage")]
    expected_notice = (
        commands_module._UPDATE_EFFECT_UNCERTAINTY_NOTICE  # noqa: SLF001
        if surface == "command"
        else callbacks_module._CALLBACK_UNKNOWN_NOTICE  # noqa: SLF001
    )
    assert texts == [expected_notice]


@pytest.mark.asyncio
async def test_hard_crash_after_edit_reply_backend_accept_never_repeats_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    update = _message_update(
        "Исправленный текст",
        reply_to_message_id=PROMPT_MESSAGE_ID,
    )
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(update) is True
    inbox.remember_edit_prompt(PROMPT_MESSAGE_ID, "ko_crash_recovery_1")
    backend_effects: list[tuple[str, str]] = []
    _patch_accepted_backend_crash(monkeypatch, first, backend_effects)
    first_telegram = _Telegram()
    try:
        with pytest.raises(_HardCrash, match="possible backend acceptance"):
            await _run_pending(first, first_telegram)
        assert inbox.update_effect_attempt_kind(UPDATE_ID) == "edit-reply"
        assert _row(inbox) is not None
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    unexpected_backend: list[tuple[str, str]] = []
    _patch_backend_must_not_repeat(monkeypatch, restarted, unexpected_backend)
    healed = _Telegram()
    try:
        assert await _run_pending(restarted, healed) is True
        assert _row(_opened(restarted)) is None
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert backend_effects == [("PATCH", "/api/knowledge/ko_crash_recovery_1")]
    assert unexpected_backend == []
    assert first_telegram.accepted == []
    texts = [str(item.get("text") or "") for item in _accepted([healed], "sendMessage")]
    assert texts == [commands_module._UPDATE_EFFECT_UNCERTAINTY_NOTICE]  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["command", "callback", "edit-reply"])
async def test_backend_connect_error_releases_exact_witness_and_retries_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    update = (
        _callback_update()
        if surface == "callback"
        else _message_update(
            "Исправленный текст" if surface == "edit-reply" else "/new",
            reply_to_message_id=PROMPT_MESSAGE_ID if surface == "edit-reply" else None,
        )
    )
    expected_endpoint = {
        "command": "/api/conversations/channel/reset",
        "callback": "/api/feedback",
        "edit-reply": "/api/knowledge/ko_connect_retry_1",
    }[surface]
    expected_method = "PATCH" if surface == "edit-reply" else "POST"
    calls: list[tuple[str, str]] = []
    accepted_effects: list[str] = []

    async def connect_then_accept(
        _backend: object,
        method: str,
        endpoint: str,
        _payload: object,
        _external_user_id: str,
        _chat_id: str,
    ) -> dict[str, Any]:
        calls.append((method, endpoint))
        if len(calls) == 1:
            request = httpx.Request(method, f"http://backend.test{endpoint}")
            raise httpx.ConnectError("synthetic zero-byte connect failure", request=request)
        accepted_effects.append(endpoint)
        return {}

    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(update) is True
    if surface == "edit-reply":
        inbox.remember_edit_prompt(PROMPT_MESSAGE_ID, "ko_connect_retry_1")
    monkeypatch.setattr(first, "_backend_json", connect_then_accept)
    first_telegram = _Telegram()
    try:
        assert await _run_pending(first, first_telegram) is True
        durable = _row(inbox)
        assert durable is not None and int(durable["attempts"]) == 1
        assert inbox.update_effect_attempt_kind(UPDATE_ID) is None
        assert first_telegram.accepted == []
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    monkeypatch.setattr(restarted, "_backend_json", connect_then_accept)
    healed = _Telegram()
    try:
        assert await _run_pending(restarted, healed) is True
        assert _row(_opened(restarted)) is None
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert calls == [(expected_method, expected_endpoint), (expected_method, expected_endpoint)]
    assert accepted_effects == [expected_endpoint]
    if surface == "callback":
        assert _accepted([healed], "sendMessage") == []
        assert len(_accepted([healed], "answerCallbackQuery")) == 1
    else:
        assert len(_accepted([healed], "sendMessage")) == 1
    healed_texts = [str(item.get("text") or "") for item in _accepted([healed], "sendMessage")]
    assert commands_module._UPDATE_EFFECT_UNCERTAINTY_NOTICE not in healed_texts  # noqa: SLF001
    assert callbacks_module._CALLBACK_UNKNOWN_NOTICE not in healed_texts  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["503", "read-timeout"])
async def test_ambiguous_backend_failure_keeps_witness_and_never_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BackendFailure,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(_message_update("/new")) is True
    backend_calls: list[str] = []

    async def ambiguous_backend(
        _backend: object,
        method: str,
        endpoint: str,
        _payload: object,
        _external_user_id: str,
        _chat_id: str,
    ) -> dict[str, Any]:
        backend_calls.append(endpoint)
        request = httpx.Request(method, f"http://backend.test{endpoint}")
        if failure == "read-timeout":
            raise httpx.ReadTimeout("synthetic ambiguous read", request=request)
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("synthetic ambiguous upstream", request=request, response=response)

    monkeypatch.setattr(first, "_backend_json", ambiguous_backend)
    try:
        assert await _run_pending(first, _Telegram()) is True
        assert inbox.update_effect_attempt_kind(UPDATE_ID) == "command"
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    unexpected_backend: list[tuple[str, str]] = []
    _patch_backend_must_not_repeat(monkeypatch, restarted, unexpected_backend)
    healed = _Telegram()
    try:
        assert await _run_pending(restarted, healed) is True
        assert _row(_opened(restarted)) is None
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert backend_calls == ["/api/conversations/channel/reset"]
    assert unexpected_backend == []
    texts = [str(item.get("text") or "") for item in _accepted([healed], "sendMessage")]
    assert texts == [commands_module._UPDATE_EFFECT_UNCERTAINTY_NOTICE]  # noqa: SLF001


@pytest.mark.asyncio
async def test_export_backend_connect_error_releases_witness_and_retries_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    export_calls = 0

    async def backend_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def connect_then_export(*_args: Any, **_kwargs: Any) -> str:
        nonlocal export_calls
        export_calls += 1
        if export_calls == 1:
            request = httpx.Request("GET", "http://backend.test/api/conversations/current/export")
            raise httpx.ConnectError("synthetic zero-byte export failure", request=request)
        return "durable export"

    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(_message_update("/export")) is True
    monkeypatch.setattr(first, "_backend_json", backend_json)
    monkeypatch.setattr(first, "_backend_text", connect_then_export)
    try:
        assert await _run_pending(first, _Telegram()) is True
        durable = _row(inbox)
        assert durable is not None and int(durable["attempts"]) == 1
        assert inbox.update_effect_attempt_kind(UPDATE_ID) is None
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    monkeypatch.setattr(restarted, "_backend_json", backend_json)
    monkeypatch.setattr(restarted, "_backend_text", connect_then_export)
    healed = _Telegram()
    try:
        assert await _run_pending(restarted, healed) is True
        assert _row(_opened(restarted)) is None
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert export_calls == 2
    assert len(_accepted([healed], "sendDocument")) == 1
    assert _accepted([healed], "sendMessage") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["503", "read-timeout"])
async def test_ambiguous_export_backend_failure_keeps_witness_and_never_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BackendFailure,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    export_calls = 0

    async def backend_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def ambiguous_export(*_args: Any, **_kwargs: Any) -> str:
        nonlocal export_calls
        export_calls += 1
        request = httpx.Request("GET", "http://backend.test/api/conversations/current/export")
        if failure == "read-timeout":
            raise httpx.ReadTimeout("synthetic ambiguous export read", request=request)
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("synthetic ambiguous export upstream", request=request, response=response)

    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(_message_update("/export")) is True
    monkeypatch.setattr(first, "_backend_json", backend_json)
    monkeypatch.setattr(first, "_backend_text", ambiguous_export)
    try:
        assert await _run_pending(first, _Telegram()) is True
        assert inbox.update_effect_attempt_kind(UPDATE_ID) == "command"
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    unexpected_text_calls: list[str] = []

    async def backend_text_must_not_repeat(*_args: Any, **_kwargs: Any) -> str:
        unexpected_text_calls.append("called")
        return "unexpected export"

    monkeypatch.setattr(restarted, "_backend_json", backend_json)
    monkeypatch.setattr(restarted, "_backend_text", backend_text_must_not_repeat)
    healed = _Telegram()
    try:
        assert await _run_pending(restarted, healed) is True
        assert _row(_opened(restarted)) is None
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert export_calls == 1
    assert unexpected_text_calls == []
    assert _accepted([healed], "sendDocument") == []
    texts = [str(item.get("text") or "") for item in _accepted([healed], "sendMessage")]
    assert texts == [commands_module._UPDATE_EFFECT_UNCERTAINTY_NOTICE]  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "endpoint"),
    [
        ("/inbox", "/api/inbox?status=pending&limit=5"),
        ("/missions", "/api/missions?limit=8"),
    ],
)
async def test_delegated_view_backend_connect_error_releases_witness_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    endpoint: str,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    calls: list[str] = []

    async def connect_then_empty(
        _backend: object,
        method: str,
        actual_endpoint: str,
        _payload: object,
        _external_user_id: str,
        _chat_id: str,
    ) -> dict[str, Any]:
        calls.append(actual_endpoint)
        if len(calls) == 1:
            request = httpx.Request(method, f"http://127.0.0.1:8000{actual_endpoint}")
            raise httpx.ConnectError("synthetic delegated-view connect failure", request=request)
        return {"items": []}

    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(_message_update(command)) is True
    monkeypatch.setattr(first, "_backend_json", connect_then_empty)
    try:
        assert await _run_pending(first, _Telegram()) is True
        durable = _row(inbox)
        assert durable is not None and int(durable["attempts"]) == 1
        assert inbox.update_effect_attempt_kind(UPDATE_ID) is None
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    monkeypatch.setattr(restarted, "_backend_json", connect_then_empty)
    healed = _Telegram()
    try:
        assert await _run_pending(restarted, healed) is True
        assert _row(_opened(restarted)) is None
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert calls == [endpoint, endpoint]
    texts = [str(item.get("text") or "") for item in _accepted([healed], "sendMessage")]
    assert len(texts) == 1
    assert commands_module._UPDATE_EFFECT_UNCERTAINTY_NOTICE not in texts  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["503", "read-timeout"])
async def test_ambiguous_delegated_view_backend_failure_keeps_witness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BackendFailure,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(_message_update("/inbox")) is True
    backend_calls = 0

    async def ambiguous_view(
        _backend: object,
        method: str,
        endpoint: str,
        _payload: object,
        _external_user_id: str,
        _chat_id: str,
    ) -> dict[str, Any]:
        nonlocal backend_calls
        backend_calls += 1
        request = httpx.Request(method, f"http://127.0.0.1:8000{endpoint}")
        if failure == "read-timeout":
            raise httpx.ReadTimeout("synthetic delegated-view read ambiguity", request=request)
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("synthetic delegated-view upstream", request=request, response=response)

    monkeypatch.setattr(first, "_backend_json", ambiguous_view)
    try:
        assert await _run_pending(first, _Telegram()) is True
        assert inbox.update_effect_attempt_kind(UPDATE_ID) == "command"
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    unexpected_backend: list[tuple[str, str]] = []
    _patch_backend_must_not_repeat(monkeypatch, restarted, unexpected_backend)
    healed = _Telegram()
    try:
        assert await _run_pending(restarted, healed) is True
        assert _row(_opened(restarted)) is None
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert backend_calls == 1
    assert unexpected_backend == []
    texts = [str(item.get("text") or "") for item in _accepted([healed], "sendMessage")]
    assert texts == [commands_module._UPDATE_EFFECT_UNCERTAINTY_NOTICE]  # noqa: SLF001


@pytest.mark.asyncio
async def test_delegated_view_telegram_connect_error_never_releases_turn_witness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SecondMessageConnectError(_Telegram):
        def __init__(self) -> None:
            super().__init__()
            self.message_attempts = 0

        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            if url.endswith("/sendMessage"):
                self.message_attempts += 1
                if self.message_attempts == 2:
                    request = httpx.Request("POST", url)
                    raise httpx.ConnectError(
                        "synthetic Telegram zero-byte list-part failure",
                        request=request,
                    )
            return await super().post(url, **kwargs)

    path = tmp_path / "telegram.sqlite3"
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(_message_update("/missions")) is True
    backend_calls = 0

    async def mission_list(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal backend_calls
        backend_calls += 1
        return {
            "items": [
                {
                    "id": "mission_view_connect_1",
                    "title": "Одна миссия",
                    "status": "proposed",
                    "done_count": 0,
                    "task_count": 1,
                }
            ]
        }

    monkeypatch.setattr(first, "_backend_json", mission_list)
    interrupted = SecondMessageConnectError()
    try:
        assert await _run_pending(first, interrupted) is True
        assert inbox.update_effect_attempt_kind(UPDATE_ID) == "command"
        assert len(_accepted([interrupted], "sendMessage")) == 1
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    unexpected_backend: list[tuple[str, str]] = []
    _patch_backend_must_not_repeat(monkeypatch, restarted, unexpected_backend)
    healed = _Telegram()
    try:
        assert await _run_pending(restarted, healed) is True
        assert _row(_opened(restarted)) is None
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert backend_calls == 1
    assert unexpected_backend == []
    texts = [str(item.get("text") or "") for item in _accepted([healed], "sendMessage")]
    assert texts == [commands_module._UPDATE_EFFECT_UNCERTAINTY_NOTICE]  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact", ["voice", "file"])
async def test_artifact_503_is_ambiguous_and_never_replayed_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: ArtifactKind,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    response = _artifact_response(artifact)
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(_message_update("Верни дополнительный результат")) is True
    inbox.cache_backend_response(UPDATE_ID, response)
    unexpected_backend: list[tuple[str, str]] = []
    _patch_backend_must_not_repeat(monkeypatch, first, unexpected_backend)
    endpoint = "sendVoice" if artifact == "voice" else "sendDocument"
    ambiguous = _Telegram(target=endpoint, failure="503")
    try:
        assert await _run_pending(first, ambiguous) is True
        durable = _row(inbox)
        assert durable is not None
        assert (int(durable["chunks_sent"]), int(durable["delivery_uncertainty"])) == (2, 1)
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    _patch_backend_must_not_repeat(monkeypatch, restarted, unexpected_backend)
    healed = _Telegram()
    try:
        assert await _run_pending(restarted, healed) is True
        assert _row(_opened(restarted)) is None
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert unexpected_backend == []
    assert len(_accepted([ambiguous], endpoint)) == 1
    assert _accepted([healed], endpoint) == []
    texts = [str(item.get("text") or "") for item in _accepted([ambiguous, healed], "sendMessage")]
    assert texts.count(ANSWER) == 1
    assert texts.count(DELIVERY_UNKNOWN) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["hard-crash", "503"])
async def test_truncated_voice_notice_ambiguous_acceptance_is_never_replayed_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: DeliveryFailure,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    response = _artifact_response("voice", truncated=True)
    first = _bridge(path)
    inbox = _opened(first)
    assert inbox.store(_message_update("Верни длинный голосовой ответ")) is True
    inbox.cache_backend_response(UPDATE_ID, response)
    unexpected_backend: list[tuple[str, str]] = []
    _patch_backend_must_not_repeat(monkeypatch, first, unexpected_backend)
    ambiguous = _Telegram(target="sendMessage", failure=failure, target_ordinal=2)
    try:
        if failure == "hard-crash":
            with pytest.raises(_HardCrash, match="possible Telegram acceptance"):
                await _run_pending(first, ambiguous)
        else:
            assert await _run_pending(first, ambiguous) is True
        durable = _row(inbox)
        assert durable is not None
        assert (int(durable["chunks_sent"]), int(durable["delivery_uncertainty"])) == (3, 1)
    finally:
        first._inbox.close()  # noqa: SLF001

    restarted = _bridge(path)
    _patch_backend_must_not_repeat(monkeypatch, restarted, unexpected_backend)
    healed = _Telegram()
    try:
        assert await _run_pending(restarted, healed) is True
        assert _row(_opened(restarted)) is None
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert unexpected_backend == []
    assert len(_accepted([ambiguous, healed], "sendVoice")) == 1
    texts = [str(item.get("text") or "") for item in _accepted([ambiguous, healed], "sendMessage")]
    truncation_notice = callbacks_module._VOICE_TRUNCATION_NOTICE  # noqa: SLF001
    assert texts.count(ANSWER) == 1
    assert texts.count(truncation_notice) == 1
    assert texts.count(DELIVERY_UNKNOWN) == 1
    healed_texts = [str(item.get("text") or "") for item in _accepted([healed], "sendMessage")]
    assert truncation_notice not in healed_texts
