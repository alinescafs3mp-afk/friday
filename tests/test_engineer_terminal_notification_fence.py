from __future__ import annotations

import asyncio
import hashlib
import json
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

    def _apply_ack(self, payload: dict[str, Any]) -> dict[str, list[str]]:
        requested = [
            str(value) for field in ("sent", "failed", "uncertain") for value in (payload.get(field) or [])
        ]
        notification_id = str(self.item["id"])
        if notification_id in (payload.get("sent") or []) and self.status == "pending":
            self.status = "sent"
        if notification_id in (payload.get("uncertain") or []) and self.status == "pending":
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
            items = [self.item] if self.status == "pending" else []
            body: dict[str, Any] = {"items": items, "count": len(items)}
            if self.retired:
                body["retired"] = list(self.retired)
            return httpx.Response(200, json=body, request=request)
        if url.endswith(str(self.item["artifact"]["path"])):
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

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        assert url.endswith("/sendDocument"), url
        if self.failure == "connect":
            raise httpx.ConnectError("not connected", request=request)
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
        assert bridge._inbox.notification_delivery_ids() == set()  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_lost_retirement_response_still_bounds_orphan_fence_cleanup(tmp_path):
    payload, item = b"PK\x03\x04archive", _terminal_item(b"PK\x03\x04archive")
    bridge, backend = _bridge(tmp_path), _Backend(item, payload)
    bridge._inbox.begin_notification_part_delivery(item["id"], "document:seed")  # noqa: SLF001
    # The backend already retired the row, but the response carrying `retired`
    # was lost. Exact durable ACK state is still sufficient cleanup proof.
    backend.status = "failed"
    try:
        await bridge._drain_outbound(_Telegram(), backend)  # noqa: SLF001
        assert bridge._inbox.notification_delivery_ids() == set()  # noqa: SLF001
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
    first.close()
    restarted = _UpdateInbox(path)
    try:
        assert restarted.notification_delivery_outcomes() == {"notif_1": "sent"}
        restarted.forget_notification_delivery_parts(["notif_1"])
        assert restarted.notification_delivery_ids() == set()
    finally:
        restarted.close()
