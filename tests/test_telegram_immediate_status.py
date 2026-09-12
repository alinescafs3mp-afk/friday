"""Immediate status uses real Telegram methods and durable restart fences."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from test_telegram_chat_progress import _bridge, _client_stub, _never_typing, _update

from friday.telegram_bridge import _commands as commands


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_at", ["backend", "final"])
async def test_cancel_turn_drains_status_edit_owned_outside_the_notifier(
    tmp_path, monkeypatch, cancel_at
) -> None:
    bridge = _bridge(tmp_path)
    blocked = asyncio.Event()
    work_entered = asyncio.Event()
    status_drained = asyncio.Event()
    work_drained = asyncio.Event()
    status_task = None

    async def prepare(*_args, **_kwargs):
        return {
            "filename": "source.txt",
            "mime_type": "text/plain",
            "content_base64": "eA==",
            "source_ref": "telegram-file:fixture",
        }

    async def backend(*_args, **_kwargs):
        if cancel_at == "backend":
            work_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                work_drained.set()
        return {"message": "Итог", "message_format": "plain"}

    async def transport(request: httpx.Request) -> httpx.Response:
        nonlocal status_task
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        if method == "editMessageText":
            status_task = asyncio.current_task()
            blocked.set()
            try:
                await asyncio.Event().wait()
            finally:
                status_drained.set()
        elif method == "sendMessage" and payload["text"] == "Итог":
            assert cancel_at == "final"
            work_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                work_drained.set()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7848}})

    monkeypatch.setattr(bridge, "_prepare_document", prepare)
    monkeypatch.setattr(bridge, "_backend_json", backend)
    monkeypatch.setattr(bridge, "_typing_loop", _never_typing)
    update = _update("Прочитай файл", update_id=8848)
    update["message"]["document"] = {"file_id": "fixture", "file_name": "source.txt"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as telegram:
        task = None
        try:
            bridge._inbox.store(update)
            task = asyncio.create_task(
                bridge._run_update(telegram, _client_stub(), bridge._inbox.pending()[0])
            )
            await asyncio.wait_for(work_entered.wait(), 1)
            await asyncio.wait_for(blocked.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert work_drained.is_set()
            assert status_drained.is_set()
            assert status_task is not None and status_task.done()
            assert bridge._inbox.pending(now=time.time() + 3600)
        finally:
            for pending in (task, status_task):
                if pending is not None:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            bridge._inbox.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_while_blocked", [False, True])
async def test_progress_burst_keeps_one_latest_snapshot_and_isolates_operations(
    tmp_path, finish_while_blocked
) -> None:
    bridge = _bridge(tmp_path)
    first_edit = asyncio.Event()
    release = asyncio.Event()
    calls = []
    state = commands._ChatProgressState("opaque", operation_id="chat:8845", item_total=33)
    other = commands._ChatProgressState("opaque", operation_id="chat:8846")
    a_edits = []
    sends = 0

    async def transport(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        calls.append((method, payload))
        if method == "sendMessage":
            sends += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 100 + sends}})
        assert method == "editMessageText"
        if payload["message_id"] == 101:
            a_edits.append(payload)
            if len(a_edits) == 1:
                first_edit.set()
                await release.wait()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": payload["message_id"]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as telegram:
        finish = None
        try:
            await commands._start_chat_progress(bridge, telegram, 5001, 91, state)
            state.received_items = 1
            commands._refresh_chat_progress(bridge, telegram, 5001, 91, state)
            await asyncio.wait_for(first_edit.wait(), 1)
            for count in range(2, 34):
                state.received_items = count
                commands._refresh_chat_progress(bridge, telegram, 5001, 91, state)
            pending_count = len(state.pending)
            await commands._start_chat_progress(bridge, telegram, 5001, 92, other)
            await commands._finish_chat_progress(
                bridge, telegram, 5001, 92, other, commands.TelegramStatusStage.COMPLETE
            )
            assert bridge._status_messages.snapshot(5001, other.operation_id)["terminal"]
            assert not release.is_set() and len(a_edits) == 1
            if finish_while_blocked:
                finish = asyncio.create_task(
                    commands._finish_chat_progress(
                        bridge, telegram, 5001, 91, state, commands.TelegramStatusStage.COMPLETE
                    )
                )
                # Let the terminal producer replace the queued refresh before
                # the already-started HTTP edit can finish.
                await asyncio.sleep(0)
                commands._refresh_chat_progress(bridge, telegram, 5001, 91, state)
                commands._set_chat_progress_stage(
                    bridge, telegram, 5001, 91, state, commands.TelegramStatusStage.BACKEND_WAIT
                )
                assert state.revision == 35 and state.stage is commands.TelegramStatusStage.COMPLETE
            release.set()
            if finish is None:
                await asyncio.wait_for(asyncio.gather(*state.pending), 1)
                assert bridge._status_messages.snapshot(5001, state.operation_id)["revision"] == 34
                await commands._finish_chat_progress(
                    bridge, telegram, 5001, 91, state, commands.TelegramStatusStage.COMPLETE
                )
            else:
                await asyncio.wait_for(finish, 1)
            assert pending_count == 1
            assert len(a_edits) == (2 if finish_while_blocked else 3)
            assert bridge._status_messages.snapshot(5001, state.operation_id) == {
                "message_id": 101,
                "revision": 35,
                "terminal": True,
            }
            assert sends == 2 and not state.pending
        finally:
            release.set()
            await asyncio.gather(*state.pending, *other.pending, return_exceptions=True)
            if finish is not None:
                await asyncio.gather(finish, return_exceptions=True)
            bridge._inbox.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stall", ["edit", "replacement"])
async def test_confirmed_final_is_removed_despite_stalled_status_and_reopen(
    tmp_path, monkeypatch, stall
) -> None:
    bridge = _bridge(tmp_path)
    final_sent = asyncio.Event()
    status_blocked = asyncio.Event()
    drained = asyncio.Event()
    backend_calls = 0
    final_calls = 0
    status_sends = 0

    async def backend(*_args, **_kwargs):
        nonlocal backend_calls
        backend_calls += 1
        return {"message": "Итог", "message_format": "plain"}

    async def transport(request: httpx.Request) -> httpx.Response:
        nonlocal final_calls, status_sends
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        if method == "sendMessage" and payload["text"] == "Итог":
            final_calls += 1
            final_sent.set()
        elif method == "sendMessage":
            status_sends += 1
            if status_sends == 1:
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 7847}})
        elif method == "editMessageText":
            await final_sent.wait()
            if stall == "replacement":
                return httpx.Response(
                    400,
                    json={
                        "ok": False,
                        "error_code": 400,
                        "description": "Bad Request: message to edit not found",
                    },
                )
        else:
            assert method == "sendChatAction"
            return httpx.Response(200, json={"ok": True})
        if method == "editMessageText" or method == "sendMessage" and payload["text"] != "Итог":
            status_blocked.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7947}})

    monkeypatch.setattr(bridge, "_backend_json", backend)
    monkeypatch.setattr(bridge, "_typing_loop", _never_typing)
    update = _update("Короткий запрос", update_id=8847)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as telegram:
        task = None
        try:
            bridge._inbox.store(update)
            task = asyncio.create_task(
                bridge._run_update(telegram, _client_stub(), bridge._inbox.pending()[0])
            )
            await asyncio.wait_for(final_sent.wait(), 1)
            await asyncio.wait_for(status_blocked.wait(), 1)
            await asyncio.wait_for(task, 2)
            assert drained.is_set()
            assert bridge._inbox.pending(now=time.time() + 3600) == []
            fence = bridge._inbox.telegram_status_send_fence(5001, "chat:8847")
            assert (fence is not None) == (stall == "replacement")
            assert bridge._status_messages.snapshot(5001, "chat:8847")["terminal"] is False
            bridge._inbox.close()
            bridge = _bridge(tmp_path)
            assert bridge._inbox.pending(now=time.time() + 3600) == []
            assert bridge._inbox.telegram_status_send_fence(5001, "chat:8847") == fence
            assert backend_calls == final_calls == 1
            assert status_sends == (2 if stall == "replacement" else 1)
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            bridge._inbox.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial", ["ok", "connect", "read_timeout", "blocked", "rate_limit", "rate_limit_unproved"]
)
@pytest.mark.parametrize("restart", [False, True])
async def test_fast_status_failure_and_final_reconnect_do_not_repeat_backend(
    tmp_path, monkeypatch, initial, restart
) -> None:
    bridge = _bridge(tmp_path)
    update = _update("Короткий запрос", update_id=8840)
    calls: list[tuple[str, dict]] = []
    accepted: list[dict] = []
    backend_calls = 0
    blocked_drained = False
    reconnected = False

    async def backend_json(*_args, **_kwargs) -> dict:
        nonlocal backend_calls
        backend_calls += 1
        # No accelerated timer or held model: the initial transport attempt
        # precedes an otherwise synchronous backend reply.
        assert calls and calls[0][0] == "sendMessage"
        if initial == "blocked":
            assert blocked_drained
        return {"message": "Итог", "message_format": "plain"}

    async def telegram_transport(request: httpx.Request) -> httpx.Response:
        nonlocal blocked_drained
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        calls.append((method, payload))
        if method == "sendMessage":
            final = payload["text"] == "Итог"
            if final and restart and not reconnected:
                raise httpx.ConnectError("final not accepted", request=request)
            if not final and initial == "connect" and not reconnected:
                raise httpx.ConnectError("status not accepted", request=request)
            if not final and initial.startswith("rate_limit") and not reconnected:
                return httpx.Response(
                    429,
                    json={
                        "ok": initial != "rate_limit",
                        "error_code": 429,
                        "description": "Too Many Requests",
                        "parameters": {"retry_after": 5},
                    },
                )
            accepted.append(payload)
            if not final and initial == "read_timeout":
                raise httpx.ReadTimeout("status response lost", request=request)
            if not final and initial == "blocked":
                try:
                    await asyncio.Event().wait()
                finally:
                    blocked_drained = True
        assert method in {"sendMessage", "editMessageText", "sendChatAction"}
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7840}})

    def bind() -> None:
        monkeypatch.setattr(bridge, "_backend_json", backend_json)
        monkeypatch.setattr(bridge, "_typing_loop", _never_typing)

    bind()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(telegram_transport)) as telegram:
            assert bridge._inbox.store(update)
            row = bridge._inbox.pending()[0]
            await asyncio.wait_for(bridge._run_update(telegram, _client_stub(), row), 3)
            if restart:
                assert bridge._inbox.pending(now=time.time() + 3600)
                bridge._inbox.close()
                bridge = _bridge(tmp_path)
                bind()
                reconnected = True
                row = bridge._inbox.pending(now=time.time() + 3600)[0]
                await asyncio.wait_for(bridge._run_update(telegram, _client_stub(), row), 3)
            assert bridge._inbox.pending(now=time.time() + 3600) == []
            fence = bridge._inbox.telegram_status_send_fence(5001, "chat:8840")
            snapshot = bridge._inbox.telegram_status_message(5001, "chat:8840")
    finally:
        bridge._inbox.close()

    assert backend_calls == 1
    assert [item["text"] for item in accepted if item["text"] == "Итог"] == ["Итог"]
    assert len([item for item in accepted if item["text"] != "Итог"]) == (
        0 if (initial in {"connect", "rate_limit"} and not restart) or initial == "rate_limit_unproved" else 1
    )
    if initial in {"blocked", "read_timeout", "rate_limit_unproved"}:
        assert fence == {"revision": 1}
        assert snapshot is None
        assert not any(method == "editMessageText" for method, _ in calls)
    elif initial == "rate_limit" and not restart:
        assert fence is None
        assert snapshot is None
    elif initial == "ok" or restart:
        assert fence is None
        assert snapshot is not None and snapshot["terminal"]
        assert {payload["message_id"] for method, payload in calls if method == "editMessageText"} == {7840}


@pytest.mark.asyncio
async def test_cancel_initial_status_drains_send_and_restart_preserves_unknown_fence(
    tmp_path, monkeypatch
) -> None:
    bridge = _bridge(tmp_path)
    update = _update("Короткий запрос", update_id=8841)
    entered = asyncio.Event()
    drained = asyncio.Event()
    status_attempts = 0
    backend_calls = 0

    async def backend_json(*_args, **_kwargs) -> dict:
        nonlocal backend_calls
        backend_calls += 1
        return {"message": "Итог", "message_format": "plain"}

    async def telegram_transport(request: httpx.Request) -> httpx.Response:
        nonlocal status_attempts
        payload = json.loads(request.content)
        if payload.get("text") != "Итог":
            status_attempts += 1
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7841}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram_transport)) as telegram:
        try:
            monkeypatch.setattr(bridge, "_backend_json", backend_json)
            monkeypatch.setattr(bridge, "_typing_loop", _never_typing)
            task = asyncio.create_task(
                bridge._process_update(telegram, _client_stub(), update, cached_response=None)
            )
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert drained.is_set() and backend_calls == 0
            bridge._inbox.close()
            bridge = _bridge(tmp_path)
            monkeypatch.setattr(bridge, "_backend_json", backend_json)
            monkeypatch.setattr(bridge, "_typing_loop", _never_typing)
            await asyncio.wait_for(
                bridge._process_update(telegram, _client_stub(), update, cached_response=None), 2
            )
            assert bridge._inbox.telegram_status_send_fence(5001, "chat:8841") == {"revision": 1}
            assert backend_calls == 1 and status_attempts == 1
        finally:
            bridge._inbox.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["ok_true", "wrong_code", "empty_description"])
async def test_unproved_status_rate_limit_cannot_retry_possible_acceptance(tmp_path, defect) -> None:
    bridge = _bridge(tmp_path)
    attempts = 0

    async def telegram_transport(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                json={
                    "ok": defect == "ok_true",
                    "error_code": 400 if defect == "wrong_code" else 429,
                    "description": "" if defect == "empty_description" else "Too Many Requests",
                    "parameters": {"retry_after": 0.001},
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7843}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram_transport)) as telegram:
        try:
            outcome = await bridge._status_messages.publish(
                telegram, 5001, "chat:8843", 1, "Обрабатываю запрос"
            )
            assert outcome == "uncertain"
            assert attempts == 1
            assert bridge._inbox.telegram_status_send_fence(5001, "chat:8843") == {"revision": 1}
        finally:
            bridge._inbox.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_at", ["backoff", "retry"])
async def test_cancel_rate_limited_status_preserves_only_in_flight_ambiguity(
    tmp_path, monkeypatch, cancel_at
) -> None:
    from friday.telegram_bridge import _status

    bridge = _bridge(tmp_path)
    entered = asyncio.Event()
    status_attempts = 0
    reopened = False

    async def backoff(delay):
        assert delay == 5
        if cancel_at == "backoff":
            entered.set()
            await asyncio.Event().wait()

    async def telegram_transport(request: httpx.Request) -> httpx.Response:
        nonlocal status_attempts
        status_attempts += 1
        if status_attempts == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 5},
                },
            )
        if not reopened:
            entered.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7842}})

    monkeypatch.setattr(_status, "_status_sleep", backoff)
    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram_transport)) as telegram:
        try:
            task = asyncio.create_task(
                bridge._status_messages.publish(telegram, 5001, "chat:8842", 1, "Обрабатываю запрос")
            )
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
            assert status_attempts == (1 if cancel_at == "backoff" else 2)
            assert bridge._inbox.telegram_status_send_fence(5001, "chat:8842") == (
                None if cancel_at == "backoff" else {"revision": 1}
            )
            bridge._inbox.close()
            bridge = _bridge(tmp_path)
            reopened = True
            outcome = await bridge._status_messages.publish(
                telegram, 5001, "chat:8842", 2, "Завершено", terminal=True
            )
            assert outcome == ("sent" if cancel_at == "backoff" else "uncertain")
            assert status_attempts == 2
        finally:
            bridge._inbox.close()
