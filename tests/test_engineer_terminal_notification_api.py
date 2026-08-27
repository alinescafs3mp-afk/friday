from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from friday.api.notifications import notification_artifact, notifications_pending
from friday.organs.engineer import EngineerOrgan
from friday.organs.engineer.publication import exact_generated_file_batch
from friday.organs.engineer.terminal_delivery import (
    TERMINAL_NOTIFICATION_KIND,
    stage_terminal_archive,
)
from friday.permissions import LEGACY_OWNER_USER_ID, AuthorizationService


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


@pytest.mark.asyncio
async def test_pending_projects_no_raw_handle_and_artifact_is_exact(settings, storage) -> None:
    enabled = replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True)
    staged, payload = _stage(storage, enabled)
    request = _request(storage, enabled, _authority(storage))

    pending = await notifications_pending(request, limit=20)
    assert pending["count"] == 1
    item = pending["items"][0]
    assert set(item) == {"id", "chat_id", "kind", "dedup_key", "caption", "artifact"}
    assert set(item["artifact"]) == {"filename", "mime_type", "size_bytes", "sha256", "path"}
    assert item["id"] == staged.notification_id
    assert item["kind"] == TERMINAL_NOTIFICATION_KIND
    assert item["artifact"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert "raw_" not in json.dumps(item, sort_keys=True)
    assert "content_base64" not in json.dumps(item, sort_keys=True)

    response = await notification_artifact(staged.notification_id, request)
    assert response.body == payload
    assert response.headers["x-friday-sha256"] == hashlib.sha256(payload).hexdigest()


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
