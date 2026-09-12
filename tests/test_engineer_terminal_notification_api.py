from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from friday.api.notifications import notification_artifact, notifications_pending
from friday.organs.engineer import EngineerOrgan
from friday.organs.engineer.command_tools import provision_engineer_command_store
from friday.organs.engineer.publication import exact_generated_file_batch
from friday.organs.engineer.terminal_delivery import (
    TERMINAL_NOTIFICATION_KIND,
    stage_terminal_archive,
)
from friday.permissions import LEGACY_OWNER_USER_ID, AuthorizationService
from friday.security import sign_bridge_request
from friday.server import create_app


def _engineer_http_settings(settings: Any):
    from pathlib import Path

    key = Path(settings.engineer_command_key_file)
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(b"K" * 32)
    key.chmod(0o600)
    Path(settings.engineer_command_store_dir).mkdir(parents=True, exist_ok=True)
    enabled = replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True)
    provision_engineer_command_store(enabled)
    return enabled


def _bridge(client: TestClient, settings: Any, method: str, path: str, body: bytes = b""):
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    signer = "5001"
    headers = {
        "X-Friday-Timestamp": str(timestamp),
        "X-Friday-User": signer,
        "X-Friday-Chat": signer,
        "X-Friday-Nonce": nonce,
        "X-Friday-Signature": sign_bridge_request(
            settings.telegram_bridge_secret,
            timestamp=timestamp,
            method=method,
            path=path,
            external_user_id=signer,
            chat_id=signer,
            nonce=nonce,
            body=body,
        ),
    }
    if method == "GET":
        return client.get(path, headers=headers)
    headers["Content-Type"] = "application/json"
    return client.post(path, content=body, headers=headers)


def _stage(storage: Any, settings: Any, *, chat_id: str = "5001"):
    storage.ensure_user(
        LEGACY_OWNER_USER_ID,
        source="api-token",
        preset_key="owner",
        metadata={"chat_id": chat_id},
    )
    storage.link_identity(
        "telegram",
        chat_id,
        LEGACY_OWNER_USER_ID,
        linked_by=LEGACY_OWNER_USER_ID,
    )
    conversation = storage.create_conversation(LEGACY_OWNER_USER_ID, "Engineer")
    source = storage.store_message(
        str(conversation["id"]),
        LEGACY_OWNER_USER_ID,
        "user",
        "Запусти команду",
        metadata={"telegram_update_id": "100"},
    )
    payload = b"PK\x03\x04exact-terminal-archive"
    attachment = {
        "kind": "document",
        "filename": f"engineer-command-{'1' * 32}.zip",
        "mime_type": "application/zip",
        "content_base64": base64.b64encode(payload).decode("ascii"),
    }
    batch = exact_generated_file_batch([attachment], max_bytes=settings.max_upload_bytes)
    staged = stage_terminal_archive(
        storage,
        settings.files_dir,
        actor_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=str(conversation["id"]),
        source_message_id=str(source["id"]),
        delivery_chat_id=chat_id,
        job_id="1" * 32,
        status="completed",
        receipt_mac="2" * 64,
        attachment=attachment,
        batch=batch,
        max_bytes=settings.max_upload_bytes,
    )
    return staged, payload


def _request(storage: Any, settings: Any, authorization: AuthorizationService) -> Request:
    app = SimpleNamespace(
        state=SimpleNamespace(
            storage=storage,
            settings=settings,
            auth_service=authorization,
        )
    )
    request = Request({"type": "http", "method": "GET", "path": "/", "app": app})
    request.state.actor = SimpleNamespace(source="telegram-bridge")
    return request


def _authority(storage: Any) -> AuthorizationService:
    authorization = AuthorizationService(storage)
    for capability in EngineerOrgan().capabilities():
        authorization.register_capability(capability)
    return authorization


def test_pending_projects_no_raw_handle_and_artifact_is_exact(settings) -> None:
    enabled = _engineer_http_settings(settings)
    with TestClient(create_app(enabled)) as client:
        storage = client.app.state.storage
        staged, payload = _stage(storage, enabled)
        job_id = "1" * 32
        receipt_mac = "2" * 64
        chat_id = "5001"
        digest = hashlib.sha256(payload).hexdigest()
        # Frozen from the fixture, not from production projection.
        expected_dedup = f"engineer-terminal:archive:{job_id}:{receipt_mac}"
        expected_caption = f"Engineer-задание {job_id} завершено. Проверенный архив результата приложен."
        expected_filename = f"engineer-command-{job_id}.zip"
        expected_path = f"/api/notifications/{staged.notification_id}/artifact"
        expected_artifact = {
            "filename": expected_filename,
            "mime_type": "application/zip",
            "size_bytes": len(payload),
            "sha256": digest,
            "path": expected_path,
        }
        expected_legacy_item = {
            "id": staged.notification_id,
            "chat_id": chat_id,
            "kind": TERMINAL_NOTIFICATION_KIND,
            "dedup_key": expected_dedup,
            "caption": expected_caption,
            "artifact": expected_artifact,
        }
        expected_status = {
            "schema": "friday.telegram-status.v1",
            "operation_id": f"engineer:{job_id}",
            "revision": (1 << 63) - 1,
            "terminal": True,
            "stage": "completed",
        }
        assert staged.dedup_key == expected_dedup
        assert payload == b"PK\x03\x04exact-terminal-archive"

        legacy = _bridge(client, enabled, "GET", "/api/notifications/pending?limit=20")
        assert legacy.status_code == 200, legacy.text
        legacy_body = legacy.json()
        assert legacy_body["count"] == 1
        assert legacy_body["items"] == [expected_legacy_item]
        pending = _bridge(
            client,
            enabled,
            "GET",
            "/api/notifications/pending?limit=20&status_messages=true",
        )
        assert pending.status_code == 200, pending.text
        pending_body = pending.json()
        assert pending_body["count"] == 1
        item = pending_body["items"][0]
        assert pending_body["items"] == [{**expected_legacy_item, "status_update": expected_status}]
        dumped = json.dumps(item, sort_keys=True)
        assert "raw_" not in dumped
        assert "content_base64" not in dumped

        advertised = item["artifact"]["path"]
        assert advertised == expected_path
        artifact = _bridge(client, enabled, "GET", advertised)
        assert artifact.status_code == 200, artifact.text
        assert artifact.content == payload
        assert artifact.headers["x-friday-sha256"] == digest

        ack_payload = json.dumps({"sent": [staged.notification_id]}).encode()
        ack = _bridge(client, enabled, "POST", "/api/notifications/ack", ack_payload)
        assert ack.status_code == 200, ack.text
        state_ids = ack.json()["state_ids"]
        assert state_ids["sent"] == [staged.notification_id]
        row = storage.execute(
            "SELECT status FROM outbound_notifications WHERE id=?",
            (staged.notification_id,),
        ).fetchone()
        assert row is not None and row["status"] == "sent"
        after = _bridge(client, enabled, "GET", "/api/notifications/pending?limit=20")
        assert after.status_code == 200, after.text
        assert staged.notification_id not in {entry["id"] for entry in after.json()["items"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["identity", "capability", "account"])
async def test_revocation_after_stage_retires_without_exposing_bytes(
    settings,
    storage,
    revocation: str,
) -> None:
    enabled = replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True)
    staged, _payload = _stage(storage, enabled)
    authorization = _authority(storage)
    request = _request(storage, enabled, authorization)
    if revocation == "identity":
        assert storage.unlink_identity("telegram", "5001")
    elif revocation == "capability":
        authorization.deny_permission(LEGACY_OWNER_USER_ID, "files.read")
    else:
        with storage.transaction() as conn:
            conn.execute(
                "UPDATE users SET status='disabled' WHERE id=?",
                (LEGACY_OWNER_USER_ID,),
            )

    with pytest.raises(HTTPException) as error:
        await notification_artifact(staged.notification_id, request)
    assert error.value.status_code == 404
    pending = await notifications_pending(request, limit=20)
    assert pending["items"] == []
    assert staged.notification_id in pending["retired"]
    row = storage.execute(
        "SELECT status,kind,dedup_key FROM outbound_notifications WHERE id=?",
        (staged.notification_id,),
    ).fetchone()
    assert row is not None
    assert (row["status"], row["kind"], row["dedup_key"]) == (
        "failed",
        TERMINAL_NOTIFICATION_KIND,
        staged.dedup_key,
    )
