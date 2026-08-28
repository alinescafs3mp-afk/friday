from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.requests import Request

from friday.api import notifications as notifications_api
from friday.telegram_bridge import TelegramBridge, TelegramConfig, _UpdateInbox
from friday.telegram_bridge import _transport as bridge_transport


class _HardCrash(BaseException):
    pass


def _terminal_item(payload: bytes = b"PK\x03\x04sealed-output") -> dict[str, Any]:
    notification_id = "notif_terminal_1"
    return {
        "id": notification_id,
        "chat_id": "5001",
        "kind": "engineer_command_terminal",
        "dedup_key": "engineer-command-terminal:job_1:receipt_1",
        "caption": "Engineer job job_1 завершён; результат приложен.",
        "artifact": {
            "filename": "job_1.zip",
            "mime_type": "application/zip",
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "path": f"/api/notifications/{notification_id}/artifact",
        },
    }


def _terminal_text_item(body: str = "Engineer command completed") -> dict[str, Any]:
    job_id = "a" * 32
    return {
        "id": "notif_terminal_text_1",
        "chat_id": "5001",
        "body": body,
        "kind": "engineer_command_terminal_text",
        "dedup_key": f"engineer-terminal:text:{job_id}:{'7' * 64}",
    }


def _unknown_item() -> dict[str, Any]:
    job_id = "b" * 32
    source_binding = "8" * 64
    return {
        "id": "notif_engineer_unknown_1",
        "chat_id": "5001",
        "body": (
            f"⚠️ Состояние Engineer-задачи `{job_id}` неизвестно. "
            "Нельзя честно подтвердить ни успех, ни ошибку: после попытки запуска потеряно "
            "достоверное подтверждение завершения. Команда автоматически не запускалась "
            "повторно. Проверь её эффекты вручную перед любым повтором."
        ),
        "kind": "engineer_command_unknown",
        "dedup_key": f"engineer-unknown:v1:{job_id}:{source_binding}",
    }


def _progress_item() -> dict[str, Any]:
    job_id = "c" * 32
    return {
        "id": "notif_engineer_progress_1",
        "chat_id": "5001",
        "body": "fact-only progress",
        "kind": "engineer_command_progress",
        "dedup_key": f"engineer-progress:v1:{job_id}:60",
        "status_update": {
            "schema": "friday.telegram-status.v1",
            "operation_id": f"engineer:{job_id}",
            "revision": 60,
            "terminal": False,
            "stage": "command_running",
            "elapsed_sec": 60,
            "timeout_sec": 0,
            "remaining_sec": None,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "output_activity": False,
        },
    }


def _empty_states() -> dict[str, list[str]]:
    return {
        "sent": [],
        "failed": [],
        "uncertain": [],
        "pending": [],
        "dismissed": [],
        "missing": [],
        "unconfirmed": [],
    }


class _Backend:
    def __init__(
        self,
        item: dict[str, Any],
        payload: bytes,
        *,
        ack_failures: int = 0,
        ack_lost_after_apply: int = 0,
    ) -> None:
        self.item = item
        self.payload = payload
        self.status = "pending"
        self.failed_attempts = 0
        self.ack_failures = ack_failures
        self.ack_lost_after_apply = ack_lost_after_apply
        self.ack_attempts = 0
        self.acks: list[dict[str, Any]] = []
        self.artifact_reads = 0
        self.artifact_status = 200
        self.retired: list[str] = []
        self.state_override: dict[str, list[str]] | None = None
        self.claim_hook: Any | None = None
        self.claim_status = 200
        self.claim_calls = 0
        self.omit_pending = False

    def _apply_ack(self, payload: dict[str, Any]) -> dict[str, list[str]]:
        requested = [
            str(value) for field in ("sent", "failed", "uncertain") for value in (payload.get(field) or [])
        ]
        notification_id = str(self.item["id"])
        if notification_id in (payload.get("sent") or []) and self.status in {"pending", "failed"}:
            self.status = "sent"
        if notification_id in (payload.get("uncertain") or []) and self.status in {
            "pending",
            "failed",
        }:
            self.status = "uncertain"
        if notification_id in (payload.get("failed") or []) and self.status == "pending":
            self.failed_attempts += 1
            if self.failed_attempts >= 5:
                self.status = "failed"
        states = _empty_states()
        for value in dict.fromkeys(requested):
            if value != notification_id:
                states["missing"].append(value)
            else:
                states[self.status].append(value)
        return self.state_override or states

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, url)
        if "/api/notifications/pending" in url:
            listed = self.item
            if self.item.get("kind") in {
                "engineer_command_terminal_text",
                "engineer_command_unknown",
                "engineer_command_progress",
            }:
                listed = {key: self.item[key] for key in ("id", "chat_id", "kind", "dedup_key")}
            items = [listed] if self.status == "pending" and not self.omit_pending else []
            body: dict[str, Any] = {"items": items, "count": len(items)}
            if self.retired:
                body["retired"] = list(self.retired)
            return httpx.Response(200, json=body, request=request)
        if url.endswith(f"/api/notifications/{self.item['id']}/claim"):
            self.claim_calls += 1
            if self.claim_hook is not None:
                self.claim_hook()
            if self.claim_status != 200:
                return httpx.Response(
                    self.claim_status,
                    json={"detail": "authority revoked"},
                    request=request,
                )
            return httpx.Response(200, json={"item": self.item}, request=request)
        artifact = self.item.get("artifact")
        if isinstance(artifact, dict) and url.endswith(str(artifact["path"])):
            self.artifact_reads += 1
            return httpx.Response(self.artifact_status, content=self.payload, request=request)
        if "/api/notifications/ack" in url:
            self.ack_attempts += 1
            payload = json.loads(kwargs.get("content") or b"{}")
            if self.ack_attempts <= self.ack_failures:
                return httpx.Response(503, json={"detail": "restart"}, request=request)
            states = self._apply_ack(payload)
            self.acks.append(payload)
            if self.ack_lost_after_apply > 0:
                self.ack_lost_after_apply -= 1
                raise httpx.ReadTimeout("committed response lost", request=request)
            return httpx.Response(200, json={"state_ids": states}, request=request)
        return httpx.Response(404, request=request)


class _Telegram:
    def __init__(self, *, failure: str = "") -> None:
        self.failure = failure
        self.documents: list[tuple[str, str, bytes, str]] = []
        self.messages: list[str] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        if self.failure == "connect":
            raise httpx.ConnectError("not connected", request=request)
        if url.endswith("/sendMessage"):
            self.messages.append(str((kwargs.get("json") or {}).get("text") or ""))
        else:
            assert url.endswith("/sendDocument"), url
            filename, content, mime = (kwargs.get("files") or {})["document"]
            caption = str((kwargs.get("data") or {}).get("caption") or "")
            self.documents.append((caption, str(filename), bytes(content), str(mime)))
        if self.failure == "hard_crash":
            raise _HardCrash
        if self.failure == "read_timeout":
            raise httpx.ReadTimeout("accepted then lost", request=request)
        if self.failure == "valid_4xx":
            return httpx.Response(
                413,
                json={"ok": False, "error_code": 413, "description": "too large"},
                request=request,
            )
        if self.failure == "malformed_4xx":
            return httpx.Response(400, content=b"proxy page", request=request)
        if self.failure == "server_5xx":
            return httpx.Response(503, json={"ok": False, "error_code": 503}, request=request)
        if self.failure == "malformed_2xx":
            return httpx.Response(200, json={"ok": True, "result": {}}, request=request)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 77}},
            request=request,
        )


def _bridge(tmp_path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
            max_document_bytes=1024,
        )
    )


@pytest.mark.asyncio
async def test_terminal_notification_fetches_and_sends_one_exact_captioned_document(tmp_path):
    payload = b"PK\x03\x04\x00\xffexact archive bytes"
    item = _terminal_item(payload)
    bridge, telegram, backend = _bridge(tmp_path), _Telegram(), _Backend(item, payload)
    try:
        await bridge._drain_outbound(telegram, backend)  # noqa: SLF001
        assert telegram.documents == [(item["caption"], "job_1.zip", payload, "application/zip")]
        assert backend.artifact_reads == 1
        assert backend.status == "sent"
        assert bridge._inbox.notification_delivery_ids() == set()  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connect", "valid_4xx"])
async def test_only_proven_rejection_rolls_back_and_allows_retry(tmp_path, failure):
    payload, item = b"PK\x03\x04archive", _terminal_item(b"PK\x03\x04archive")
    bridge, backend = _bridge(tmp_path), _Backend(item, payload)
    try:
        first = _Telegram(failure=failure)
        await bridge._drain_outbound(first, backend)  # noqa: SLF001
        assert backend.status == "pending"
        assert bridge._inbox.notification_delivery_part_states(item["id"]) == {}  # noqa: SLF001
        second = _Telegram()
        await bridge._drain_outbound(second, backend)  # noqa: SLF001
        assert len(second.documents) == 1
        assert backend.status == "sent"
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["read_timeout", "malformed_4xx", "server_5xx", "malformed_2xx"],
)
async def test_every_ambiguous_telegram_result_is_terminal_uncertain(tmp_path, failure):
    payload, item = b"PK\x03\x04archive", _terminal_item(b"PK\x03\x04archive")
    bridge, backend = _bridge(tmp_path), _Backend(item, payload)
    try:
        telegram = _Telegram(failure=failure)
        await bridge._drain_outbound(telegram, backend)  # noqa: SLF001
        assert len(telegram.documents) == 1
        assert backend.status == "uncertain"
        assert bridge._inbox.notification_delivery_ids() == set()  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_hard_crash_after_telegram_acceptance_is_not_resent(tmp_path):
    payload, item = b"PK\x03\x04archive", _terminal_item(b"PK\x03\x04archive")
    backend = _Backend(item, payload)
    first = _bridge(tmp_path)
    with pytest.raises(_HardCrash):
        await first._drain_outbound(_Telegram(failure="hard_crash"), backend)  # noqa: SLF001
    first._inbox.close()  # noqa: SLF001

    restarted, telegram = _bridge(tmp_path), _Telegram()
    try:
        await restarted._drain_outbound(telegram, backend)  # noqa: SLF001
        assert telegram.documents == []
        assert backend.status == "uncertain"
    finally:
        restarted._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_terminal_text_hard_crash_after_acceptance_is_not_resent(tmp_path):
    item = _terminal_text_item()
    backend = _Backend(item, b"")
    first = _bridge(tmp_path)
    with pytest.raises(_HardCrash):
        await first._drain_outbound(_Telegram(failure="hard_crash"), backend)  # noqa: SLF001
    first._inbox.close()  # noqa: SLF001

    restarted, telegram = _bridge(tmp_path), _Telegram()
    try:
        await restarted._drain_outbound(telegram, backend)  # noqa: SLF001
        assert telegram.messages == []
        assert backend.status == "uncertain"
    finally:
        restarted._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["account", "capability", "identity"])
@pytest.mark.parametrize("item_factory", [_terminal_text_item, _unknown_item, _progress_item])
async def test_send_edge_claim_revocation_makes_zero_telegram_calls(
    tmp_path,
    revocation,
    item_factory,
):
    item = item_factory()
    backend = _Backend(item, b"")
    backend.claim_status = 404
    bridge, telegram = _bridge(tmp_path), _Telegram()
    try:
        await bridge._drain_outbound(telegram, backend)  # noqa: SLF001
        assert backend.claim_calls == 1, revocation
        assert telegram.messages == []
        assert telegram.documents == []
        assert backend.acks == []
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_two_chunk_text_restart_keeps_confirmed_emoji_prefix_and_retries_second(
    tmp_path,
) -> None:
    item = _terminal_text_item("🙂" * 3_000)
    backend = _Backend(item, b"")
    first = _bridge(tmp_path)
    envelope = bridge_transport._engineer_terminal_text_envelope(  # noqa: SLF001
        item,
        chat_id=5001,
    )
    assert envelope is not None and len(envelope["parts"]) == 2

    class SecondConnectFailure(_Telegram):
        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            if url.endswith("/sendMessage") and len(self.messages) == 1:
                raise httpx.ConnectError("second chunk not accepted", request=httpx.Request("POST", url))
            return await super().post(url, **kwargs)

    first_telegram = SecondConnectFailure()
    await first._drain_outbound(first_telegram, backend)  # noqa: SLF001
    assert first_telegram.messages == [envelope["parts"][0]["rendered_text"]]
    assert backend.status == "pending"
    assert first._inbox.notification_delivery_part_states(item["id"]) == {  # noqa: SLF001
        envelope["parts"][0]["fence_key"]: "confirmed"
    }
    first._inbox.close()  # noqa: SLF001

    restarted, replay = _bridge(tmp_path), _Telegram()
    try:
        await restarted._drain_outbound(replay, backend)  # noqa: SLF001
        assert replay.messages == [envelope["parts"][1]["rendered_text"]]
        assert backend.status == "sent"
    finally:
        restarted._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_terminal_text_refuses_any_fence_outside_full_expected_set(tmp_path) -> None:
    item = _terminal_text_item("🙂" * 3_000)
    backend = _Backend(item, b"")
    bridge, telegram = _bridge(tmp_path), _Telegram()
    bridge._inbox.begin_notification_part_delivery(item["id"], "document:foreign")  # noqa: SLF001
    try:
        await bridge._drain_outbound(telegram, backend)  # noqa: SLF001
        assert telegram.messages == []
        assert backend.status == "uncertain"
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_local_text_outcome_waits_for_visible_carrier_and_terminal_status_retry(
    tmp_path,
) -> None:
    item = _terminal_text_item()
    job_id = "a" * 32
    item["status_update"] = {
        "schema": "friday.telegram-status.v1",
        "operation_id": f"engineer:{job_id}",
        "revision": (1 << 63) - 1,
        "terminal": True,
        "stage": "completed",
    }
    backend = _Backend(item, b"")
    bridge = _bridge(tmp_path)

    class StatusFailsOnce(_Telegram):
        def __init__(self) -> None:
            super().__init__()
            self.fail_status = True

        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            text = str((kwargs.get("json") or {}).get("text") or "")
            if self.fail_status and text.startswith("✅"):
                raise httpx.ConnectError(
                    "terminal status not accepted",
                    request=httpx.Request("POST", url),
                )
            return await super().post(url, **kwargs)

    telegram = StatusFailsOnce()
    try:
        await bridge._drain_outbound(telegram, backend)  # noqa: SLF001
        assert telegram.messages == [item["body"]]
        assert backend.acks == []
        assert bridge._inbox.notification_delivery_outcomes() == {item["id"]: "sent"}  # noqa: SLF001

        backend.omit_pending = True
        await bridge._drain_outbound(telegram, backend)  # noqa: SLF001
        assert telegram.messages == [item["body"]]
        assert backend.acks == []
        assert bridge._inbox.notification_delivery_outcomes() == {item["id"]: "sent"}  # noqa: SLF001

        backend.omit_pending = False
        telegram.fail_status = False
        await bridge._drain_outbound(telegram, backend)  # noqa: SLF001
        assert telegram.messages == [
            item["body"],
            f"✅ Engineer-задача завершена. Результат отправлен.\nJob: {job_id}.",
        ]
        assert backend.status == "sent"
        assert bridge._inbox.notification_delivery_ids() == set()  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_unknown_hard_crash_after_acceptance_is_not_resent(tmp_path):
    item = _unknown_item()
    backend = _Backend(item, b"")
    first = _bridge(tmp_path)
    with pytest.raises(_HardCrash):
        await first._drain_outbound(_Telegram(failure="hard_crash"), backend)  # noqa: SLF001
    first._inbox.close()  # noqa: SLF001

    restarted, telegram = _bridge(tmp_path), _Telegram()
    try:
        await restarted._drain_outbound(telegram, backend)  # noqa: SLF001
        assert telegram.messages == []
        assert backend.status == "uncertain"
    finally:
        restarted._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_unknown_ack_committed_with_all_responses_lost_replays_without_resend(
    tmp_path,
    monkeypatch,
):
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("friday.telegram_bridge._transport.asyncio.sleep", no_sleep)
    item = _unknown_item()
    backend = _Backend(item, b"", ack_lost_after_apply=3)
    first, telegram = _bridge(tmp_path), _Telegram()
    await first._drain_outbound(telegram, backend)  # noqa: SLF001
    assert len(telegram.messages) == 1
    assert "Состояние Engineer-задачи" in telegram.messages[0]
    assert "неизвестно" in telegram.messages[0]
    assert backend.status == "sent"
    assert first._inbox.notification_delivery_outcomes() == {item["id"]: "sent"}  # noqa: SLF001
    first._inbox.close()  # noqa: SLF001

    restarted, replay = _bridge(tmp_path), _Telegram()
    try:
        await restarted._drain_outbound(replay, backend)  # noqa: SLF001
        assert replay.messages == []
        assert restarted._inbox.notification_delivery_ids() == set()  # noqa: SLF001
    finally:
        restarted._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_terminal_text_confirmed_before_outcome_crash_reconciles_without_resend(
    tmp_path,
    monkeypatch,
):
    item = _terminal_text_item()
    backend = _Backend(item, b"")
    first, telegram = _bridge(tmp_path), _Telegram()

    def crash_before_outcome(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise _HardCrash

    monkeypatch.setattr(first._inbox, "remember_notification_delivery_outcome", crash_before_outcome)
    with pytest.raises(_HardCrash):
        await first._drain_outbound(telegram, backend)  # noqa: SLF001
    assert telegram.messages == [item["body"]]
    first._inbox.close()  # noqa: SLF001

    restarted, replay = _bridge(tmp_path), _Telegram()
    try:
        await restarted._drain_outbound(replay, backend)  # noqa: SLF001
        assert replay.messages == []
        assert backend.status == "sent"
    finally:
        restarted._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_terminal_text_drift_after_lost_ack_downgrades_without_resend(tmp_path, monkeypatch):
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("friday.telegram_bridge._transport.asyncio.sleep", no_sleep)
    item = _terminal_text_item()
    backend = _Backend(item, b"", ack_failures=3)
    first, telegram = _bridge(tmp_path), _Telegram()
    await first._drain_outbound(telegram, backend)  # noqa: SLF001
    assert telegram.messages == [item["body"]]
    first._inbox.close()  # noqa: SLF001
    item["body"] = "changed terminal body"

    restarted, replay = _bridge(tmp_path), _Telegram()
    try:
        await restarted._drain_outbound(replay, backend)  # noqa: SLF001
        assert replay.messages == []
        assert backend.status == "uncertain"
    finally:
        restarted._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_racing_deliveries_have_one_atomic_prewrite_winner(tmp_path, monkeypatch):
    payload, item = b"PK\x03\x04archive", _terminal_item(b"PK\x03\x04archive")
    bridge = _bridge(tmp_path)
    envelope = bridge_transport._engineer_terminal_envelope(  # noqa: SLF001
        item,
        chat_id=5001,
        max_document_bytes=1024,
    )
    assert envelope is not None
    both_fetching = asyncio.Event()
    fetch_count = 0

    async def _barrier_fetch(*_args, **_kwargs):  # noqa: ANN002, ANN003
        nonlocal fetch_count
        fetch_count += 1
        if fetch_count == 2:
            both_fetching.set()
        await both_fetching.wait()
        return "ready", payload

    class _PausedTelegram(_Telegram):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            self.entered.set()
            await self.release.wait()
            return await super().post(url, **kwargs)

    telegram = _PausedTelegram()
    monkeypatch.setattr(bridge_transport, "_fetch_engineer_terminal_artifact", _barrier_fetch)
    first = asyncio.create_task(
        bridge_transport._deliver_engineer_terminal_document(  # noqa: SLF001
            bridge,
            telegram,
            object(),
            signer_chat="5001",
            envelope=envelope,
        )
    )
    second = asyncio.create_task(
        bridge_transport._deliver_engineer_terminal_document(  # noqa: SLF001
            bridge,
            telegram,
            object(),
            signer_chat="5001",
            envelope=envelope,
        )
    )
    try:
        await telegram.entered.wait()
        await asyncio.sleep(0)
        telegram.release.set()
        outcomes = await asyncio.gather(first, second)
        assert len(telegram.documents) == 1
        assert "sent" in outcomes
        assert set(outcomes) <= {"sent", "uncertain"}
    finally:
        first.cancel()
        second.cancel()
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_ack_committed_but_all_responses_lost_reconciles_after_restart(tmp_path, monkeypatch):
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("friday.telegram_bridge._transport.asyncio.sleep", _no_sleep)
    payload, item = b"PK\x03\x04archive", _terminal_item(b"PK\x03\x04archive")
    backend = _Backend(item, payload, ack_lost_after_apply=3)
    first, telegram = _bridge(tmp_path), _Telegram()
    await first._drain_outbound(telegram, backend)  # noqa: SLF001
    assert len(telegram.documents) == 1 and backend.status == "sent"
    assert first._inbox.notification_delivery_outcomes() == {item["id"]: "sent"}  # noqa: SLF001
    first._inbox.close()  # noqa: SLF001

    restarted, second = _bridge(tmp_path), _Telegram()
    try:
        await restarted._drain_outbound(second, backend)  # noqa: SLF001
        assert second.documents == []
        assert restarted._inbox.notification_delivery_ids() == set()  # noqa: SLF001
    finally:
        restarted._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_payload_or_chat_drift_after_lost_ack_downgrades_to_uncertain(tmp_path, monkeypatch):
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("friday.telegram_bridge._transport.asyncio.sleep", _no_sleep)
    payload, item = b"PK\x03\x04archive", _terminal_item(b"PK\x03\x04archive")
    backend = _Backend(item, payload, ack_failures=3)
    first = _bridge(tmp_path)
    await first._drain_outbound(_Telegram(), backend)  # noqa: SLF001
    first._inbox.close()  # noqa: SLF001
    item["caption"] = "подменённая подпись"

    restarted, telegram = _bridge(tmp_path), _Telegram()
    try:
        await restarted._drain_outbound(telegram, backend)  # noqa: SLF001
        assert telegram.documents == []
        assert backend.status == "uncertain"
    finally:
        restarted._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_retryable_failed_ack_preserves_parts_but_terminal_failed_cleans_them(tmp_path):
    payload, item = b"PK\x03\x04archive", _terminal_item(b"PK\x03\x04archive")
    bridge, backend = _bridge(tmp_path), _Backend(item, payload)
    bridge._inbox.begin_notification_part_delivery(item["id"], "document:seed")  # noqa: SLF001
    assert bridge._inbox.confirm_notification_part_delivery(item["id"], "document:seed")  # noqa: SLF001
    try:
        await bridge._ack_outbound(backend, "5001", [], [item["id"]])  # noqa: SLF001
        assert backend.status == "pending"
        assert bridge._inbox.notification_delivery_ids() == {item["id"]}  # noqa: SLF001
        backend.failed_attempts = 4
        await bridge._ack_outbound(backend, "5001", [], [item["id"]])  # noqa: SLF001
        assert backend.status == "failed"
        assert bridge._inbox.notification_delivery_ids() == set()  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_ack_noop_or_malformed_proof_never_cleans_a_strict_fence(tmp_path, monkeypatch):
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("friday.telegram_bridge._transport.asyncio.sleep", _no_sleep)
    payload, item = b"PK\x03\x04archive", _terminal_item(b"PK\x03\x04archive")
    bridge, backend = _bridge(tmp_path), _Backend(item, payload)
    bridge._inbox.begin_notification_part_delivery(item["id"], "document:seed")  # noqa: SLF001
    backend.state_override = _empty_states()
    backend.state_override["unconfirmed"] = [item["id"]]
    try:
        await bridge._ack_outbound(backend, "5001", [item["id"]], [])  # noqa: SLF001
        assert bridge._inbox.notification_delivery_ids() == {item["id"]}  # noqa: SLF001
        assert backend.ack_attempts == 3
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_verified_retirement_bounds_local_cleanup(tmp_path):
    payload, item = b"PK\x03\x04archive", _terminal_item(b"PK\x03\x04archive")
    bridge, backend = _bridge(tmp_path), _Backend(item, payload)
    bridge._inbox.begin_notification_part_delivery(item["id"], "document:seed")  # noqa: SLF001
    bridge._inbox.remember_notification_delivery_outcome(item["id"], "uncertain")  # noqa: SLF001
    backend.status = "failed"
    backend.retired = [item["id"]]
    try:
        await bridge._drain_outbound(_Telegram(), backend)  # noqa: SLF001
        assert backend.status == "uncertain"
        assert bridge._inbox.notification_delivery_ids() == set()  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_lost_sent_ack_survives_later_authority_retirement(tmp_path):
    payload, item = b"PK\x03\x04archive", _terminal_item(b"PK\x03\x04archive")
    bridge, backend = _bridge(tmp_path), _Backend(item, payload)
    fence_key = "document:exact-delivered-envelope"
    bridge._inbox.begin_notification_part_delivery(item["id"], fence_key)  # noqa: SLF001
    assert bridge._inbox.confirm_notification_part_delivery(item["id"], fence_key)  # noqa: SLF001
    bridge._inbox.remember_notification_delivery_outcome(item["id"], "sent")  # noqa: SLF001
    backend.status = "failed"
    backend.retired = [item["id"]]
    telegram = _Telegram()
    try:
        await bridge._drain_outbound(telegram, backend)  # noqa: SLF001
        assert telegram.documents == []
        assert backend.status == "sent"
        assert bridge._inbox.notification_delivery_ids() == set()  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_retired_fence_outside_bounded_orphan_scan_keeps_its_proof(tmp_path):
    payload, item = b"PK\x03\x04archive", _terminal_item(b"PK\x03\x04archive")
    bridge, backend = _bridge(tmp_path), _Backend(item, payload)
    for index in range(100):
        notification_id = f"notif_backlog_{index:03d}"
        bridge._inbox.begin_notification_part_delivery(notification_id, "document:old")  # noqa: SLF001
        assert bridge._inbox.confirm_notification_part_delivery(  # noqa: SLF001
            notification_id,
            "document:old",
        )
    bridge._inbox.begin_notification_part_delivery(item["id"], "document:newest")  # noqa: SLF001
    assert bridge._inbox.confirm_notification_part_delivery(  # noqa: SLF001
        item["id"],
        "document:newest",
    )
    backend.status = "failed"
    backend.retired = [item["id"]]
    try:
        await bridge._drain_outbound(_Telegram(), backend)  # noqa: SLF001
        assert backend.status == "uncertain"
        assert item["id"] not in bridge._inbox.notification_delivery_ids()  # noqa: SLF001
        assert bridge._inbox.notification_delivery_ids() == {  # noqa: SLF001
            f"notif_backlog_{index:03d}" for index in range(100)
        }
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_missing_retirement_tombstone_keeps_orphan_proof_for_later_reconciliation(tmp_path):
    payload, item = b"PK\x03\x04archive", _terminal_item(b"PK\x03\x04archive")
    bridge, backend = _bridge(tmp_path), _Backend(item, payload)
    bridge._inbox.begin_notification_part_delivery(item["id"], "document:seed")  # noqa: SLF001
    # Without a visible row or an explicit retirement tombstone the bridge must
    # not ACK a local outcome: the row may merely be outside the bounded page,
    # and a terminal status could still be owed.
    backend.status = "failed"
    try:
        await bridge._drain_outbound(_Telegram(), backend)  # noqa: SLF001
        assert bridge._inbox.notification_delivery_ids() == {item["id"]}  # noqa: SLF001
        assert backend.acks == []
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.update(files=[]),
        lambda item: item["artifact"].update(path="/api/files/raw_forbidden"),
        lambda item: item.update(caption="x" * 2000),
        lambda item: item["artifact"].update(size_bytes=9999),
        lambda item: item.update(chat_id="05001"),
    ],
)
async def test_invalid_or_inline_carrier_is_rejected_before_artifact_or_telegram_io(tmp_path, mutate):
    payload, item = b"PK\x03\x04archive", _terminal_item(b"PK\x03\x04archive")
    mutate(item)
    bridge, backend, telegram = _bridge(tmp_path), _Backend(item, payload), _Telegram()
    try:
        await bridge._drain_outbound(telegram, backend)  # noqa: SLF001
        assert telegram.documents == []
        assert backend.artifact_reads == 0
    finally:
        bridge._inbox.close()  # noqa: SLF001


def test_storage_ack_reports_actual_retryable_terminal_and_missing_states(storage):
    storage.ensure_user("alice")
    assert storage.enqueue_notification(
        "alice",
        "5001",
        "{}",
        kind="engineer_command_terminal",
        dedup_key="engineer-terminal:job_1",
    )
    notification_id = storage.list_pending_notifications()[0]["id"]
    first = storage.acknowledge_notifications(failed_ids=[notification_id])
    assert first["pending"] == [notification_id]
    for _ in range(4):
        terminal = storage.acknowledge_notifications(failed_ids=[notification_id])
    assert terminal["failed"] == [notification_id]
    row = storage.execute(
        "SELECT kind,dedup_key FROM outbound_notifications WHERE id=?",
        (notification_id,),
    ).fetchone()
    assert row is not None
    assert row["kind"] == "engineer_command_terminal"
    assert row["dedup_key"] == "engineer-terminal:job_1"
    assert storage.acknowledge_notifications(sent_ids=[notification_id])["failed"] == [notification_id]
    assert storage.acknowledge_notifications(uncertain_ids=[notification_id])["failed"] == [notification_id]
    missing = storage.acknowledge_notifications(sent_ids=["notif_missing"])
    assert missing["missing"] == ["notif_missing"]


def test_verified_discard_keeps_strict_terminal_identity_for_reconciliation(storage):
    storage.ensure_user("alice")
    dedup_key = "engineer-terminal:archive:job_1:receipt_1"
    assert storage.enqueue_notification(
        "alice",
        "5001",
        "{}",
        kind="engineer_command_terminal",
        dedup_key=dedup_key,
    )
    notification_id = storage.list_pending_notifications()[0]["id"]
    assert storage.discard_notifications_verified(
        [notification_id],
        reason="chat_not_allowed",
    ) == [notification_id]
    row = storage.execute(
        "SELECT status,kind,dedup_key FROM outbound_notifications WHERE id=?",
        (notification_id,),
    ).fetchone()
    assert row is not None
    assert dict(row) == {
        "status": "failed",
        "kind": "engineer_command_terminal",
        "dedup_key": dedup_key,
    }


def test_terminal_text_keeps_identity_through_retry_and_retirement(storage):
    storage.ensure_user("alice")
    dedup_key = "engineer-terminal:text:" + "1" * 32 + ":" + "2" * 64
    assert storage.enqueue_notification(
        "alice",
        "5001",
        "{}",
        kind="engineer_command_terminal_text",
        dedup_key=dedup_key,
    )
    notification_id = storage.list_pending_notifications()[0]["id"]
    assert storage.acknowledge_notifications(failed_ids=[notification_id])["pending"] == [notification_id]
    assert storage.discard_notifications_verified(
        [notification_id],
        reason="chat_not_allowed",
    ) == [notification_id]
    row = storage.execute(
        "SELECT status,kind,dedup_key FROM outbound_notifications WHERE id=?",
        (notification_id,),
    ).fetchone()
    assert row is not None
    assert dict(row) == {
        "status": "failed",
        "kind": "engineer_command_terminal_text",
        "dedup_key": dedup_key,
    }


def test_terminal_text_uncertain_ack_is_terminal_and_keeps_identity(storage):
    storage.ensure_user("alice")
    dedup_key = "engineer-terminal:text:" + "1" * 32 + ":" + "2" * 64
    assert storage.enqueue_notification(
        "alice",
        "5001",
        "{}",
        kind="engineer_command_terminal_text",
        dedup_key=dedup_key,
    )
    notification_id = storage.list_pending_notifications()[0]["id"]
    states = storage.acknowledge_notifications(uncertain_ids=[notification_id])
    assert states["uncertain"] == [notification_id]
    row = storage.execute(
        "SELECT status,kind,dedup_key FROM outbound_notifications WHERE id=?",
        (notification_id,),
    ).fetchone()
    assert row is not None
    assert dict(row) == {
        "status": "uncertain",
        "kind": "engineer_command_terminal_text",
        "dedup_key": dedup_key,
    }


def test_unknown_notice_keeps_identity_through_uncertain_ack_and_replay(storage):
    storage.ensure_user("alice")
    dedup_key = "engineer-unknown:v1:" + "1" * 32 + ":" + "2" * 64
    assert storage.enqueue_notification(
        "alice",
        "5001",
        "{}",
        kind="engineer_command_unknown",
        dedup_key=dedup_key,
    )
    notification_id = storage.list_pending_notifications()[0]["id"]
    first = storage.acknowledge_notifications(uncertain_ids=[notification_id])
    assert first["uncertain"] == [notification_id]
    replay = storage.acknowledge_notifications(uncertain_ids=[notification_id])
    assert replay["uncertain"] == [notification_id]
    row = storage.execute(
        "SELECT status,kind,dedup_key FROM outbound_notifications WHERE id=?",
        (notification_id,),
    ).fetchone()
    assert row is not None
    assert dict(row) == {
        "status": "uncertain",
        "kind": "engineer_command_unknown",
        "dedup_key": dedup_key,
    }


def _bridge_request(storage: Any, body: dict[str, Any] | None = None) -> Request:
    app = SimpleNamespace(state=SimpleNamespace(storage=storage, settings=object()))
    request = Request({"type": "http", "method": "POST", "path": "/", "app": app})
    request.state.actor = SimpleNamespace(source="telegram-bridge")
    if body is not None:
        request.state.json_body = body
    return request


@pytest.mark.asyncio
async def test_ack_route_returns_storage_proof_not_echoed_ids():
    class _Storage:
        def acknowledge_notifications(self, sent, failed, uncertain):  # noqa: ANN001
            assert (sent, failed, uncertain) == (["sent"], ["failed"], ["uncertain"])
            states = _empty_states()
            states["sent"] = ["sent"]
            states["pending"] = ["failed"]
            states["unconfirmed"] = ["uncertain"]
            return states

    result = await notifications_api.notifications_ack(
        _bridge_request(
            _Storage(),
            {"sent": ["sent"], "failed": ["failed"], "uncertain": ["uncertain"]},
        )
    )
    assert result["state_ids"]["pending"] == ["failed"]
    assert result["state_ids"]["unconfirmed"] == ["uncertain"]


def test_outcome_rows_survive_restart_and_cleanup_is_atomic(tmp_path):
    path = tmp_path / "queue.sqlite3"
    first = _UpdateInbox(path)
    first.begin_notification_part_delivery("notif_1", "document:digest")
    first.remember_notification_delivery_outcome("notif_1", "sent")
    assert first.notification_delivery_reconciled_outcomes() == {}
    first.confirm_notification_status_reconciled("notif_1")
    assert first.notification_delivery_reconciled_outcomes() == {"notif_1": "sent"}
    first.close()
    restarted = _UpdateInbox(path)
    try:
        assert restarted.notification_delivery_outcomes() == {"notif_1": "sent"}
        restarted.forget_notification_delivery_parts(["notif_1"])
        assert restarted.notification_delivery_ids() == set()
    finally:
        restarted.close()


def test_uncertain_outcome_downgrade_requires_status_reconciliation_again(tmp_path):
    inbox = _UpdateInbox(tmp_path / "queue.sqlite3")
    try:
        inbox.remember_notification_delivery_outcome("notif_1", "sent")
        inbox.confirm_notification_status_reconciled("notif_1")
        assert inbox.notification_delivery_reconciled_outcomes() == {"notif_1": "sent"}
        inbox.remember_notification_delivery_outcome("notif_1", "uncertain")
        assert inbox.notification_delivery_outcomes() == {"notif_1": "uncertain"}
        assert inbox.notification_delivery_reconciled_outcomes() == {}
    finally:
        inbox.close()


def test_legacy_outcome_rows_migrate_to_unreconciled_status_proof(tmp_path):
    path = tmp_path / "queue.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute(
        """CREATE TABLE notification_delivery_outcomes (
               notification_id TEXT PRIMARY KEY,
               outcome TEXT NOT NULL CHECK(outcome IN ('sent', 'uncertain')),
               updated_at REAL NOT NULL
           )"""
    )
    legacy.execute("INSERT INTO notification_delivery_outcomes VALUES('notif_legacy', 'sent', 1.0)")
    legacy.commit()
    legacy.close()

    inbox = _UpdateInbox(path)
    try:
        columns = {
            str(row["name"])
            for row in inbox._conn.execute(  # noqa: SLF001 - migration assertion
                "PRAGMA table_info(notification_delivery_outcomes)"
            ).fetchall()
        }
        assert "status_reconciled" in columns
        assert inbox.notification_delivery_outcomes() == {"notif_legacy": "sent"}
        assert inbox.notification_delivery_reconciled_outcomes() == {}
    finally:
        inbox.close()
