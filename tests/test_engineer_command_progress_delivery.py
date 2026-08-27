from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from friday.api.notifications import notifications_pending
from friday.organs.engineer import EngineerOrgan
from friday.organs.engineer.command.progress import (
    PROGRESS_CHECKPOINTS_SEC,
    PROGRESS_NOTIFICATION_KIND,
    ProgressDeliveryError,
    parse_progress_envelope,
    progress_dedup_key,
    progress_notification_projection,
    retire_pending_progress_notifications,
    stage_progress_notification,
)
from friday.permissions import LEGACY_OWNER_USER_ID, AuthorizationService


def _scope(storage: Any, *, chat_id: str = "5001") -> str:
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
    return str(storage.create_conversation(LEGACY_OWNER_USER_ID, "Engineer")["id"])


def _stage(
    storage: Any,
    conversation_id: str,
    *,
    job_id: str = "1" * 32,
    checkpoint_sec: int = 60,
    stdout_bytes: int = 17,
    stderr_bytes: int = 3,
    output_activity: bool = True,
):
    return stage_progress_notification(
        storage,
        actor_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        delivery_chat_id="5001",
        job_id=job_id,
        checkpoint_sec=checkpoint_sec,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        output_activity=output_activity,
    )


def _authority(storage: Any) -> AuthorizationService:
    authorization = AuthorizationService(storage)
    for capability in EngineerOrgan().capabilities():
        authorization.register_capability(capability)
    return authorization


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


def test_closed_checkpoints_freeze_first_sample_and_project_only_facts(storage) -> None:
    assert PROGRESS_CHECKPOINTS_SEC == (60, 300, 900, 1800)
    conversation_id = _scope(storage)
    staged = _stage(storage, conversation_id)
    # Crash after queue commit but before producer CAS: later counters may grow,
    # while the already-durable first sample remains the exact replay authority.
    repeated = _stage(
        storage,
        conversation_id,
        stdout_bytes=999,
        stderr_bytes=55,
        output_activity=False,
    )
    assert repeated == staged
    assert staged.dedup_key == f"engineer-progress:v1:{'1' * 32}:60"

    row = storage.execute(
        "SELECT id,user_id,chat_id,kind,dedup_key,body,status FROM outbound_notifications WHERE id=?",
        (staged.notification_id,),
    ).fetchone()
    assert row is not None
    envelope = parse_progress_envelope(row["body"])
    assert (envelope["stdout_bytes"], envelope["stderr_bytes"], envelope["output_activity"]) == (
        17,
        3,
        True,
    )
    assert not {"percent", "eta_sec", "argv", "phase", "output"}.intersection(envelope)
    projection = progress_notification_projection(
        storage,
        dict(row),
        tenant_id=LEGACY_OWNER_USER_ID,
        actor_id=LEGACY_OWNER_USER_ID,
    )
    assert projection == {
        "body": (
            f"Engineer-задача {'1' * 32} на момент проверки выполнялась не менее 60 с. "
            "stdout: 17 байт; stderr: 3 байт; активность вывода: да."
        )
    }
    assert not any(token in projection["body"].lower() for token in ("%", "eta", "готовност", "argv"))


def test_progress_carrier_rejects_open_shape_checkpoint_and_scope_conflict(storage) -> None:
    conversation_id = _scope(storage)
    with pytest.raises(ProgressDeliveryError, match="progress_identity_invalid"):
        _stage(storage, conversation_id, checkpoint_sec=61)
    canonical = storage.execute("SELECT COUNT(*) FROM outbound_notifications").fetchone()[0]
    assert canonical == 0

    _stage(storage, conversation_id)
    other_conversation = str(storage.create_conversation(LEGACY_OWNER_USER_ID, "Other")["id"])
    with pytest.raises(ProgressDeliveryError, match="progress_dedup_conflict"):
        _stage(storage, other_conversation)

    row = storage.execute("SELECT body FROM outbound_notifications").fetchone()
    parsed = json.loads(str(row["body"]))
    parsed["percent"] = 50
    with pytest.raises(ProgressDeliveryError, match="progress_envelope_noncanonical"):
        parse_progress_envelope(json.dumps(parsed))


@pytest.mark.asyncio
async def test_pending_reauthorizes_progress_and_files_read_is_not_required(settings, storage) -> None:
    enabled = replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True)
    conversation_id = _scope(storage)
    staged = _stage(storage, conversation_id)
    authorization = _authority(storage)
    authorization.deny_permission(LEGACY_OWNER_USER_ID, "files.read")

    pending = await notifications_pending(_request(storage, enabled, authorization), limit=20)
    assert pending["count"] == 1
    assert pending["items"] == [
        {
            "id": staged.notification_id,
            "chat_id": "5001",
            "body": (
                f"Engineer-задача {'1' * 32} на момент проверки выполнялась не менее 60 с. "
                "stdout: 17 байт; stderr: 3 байт; активность вывода: да."
            ),
            "kind": PROGRESS_NOTIFICATION_KIND,
            "dedup_key": staged.dedup_key,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["identity", "manage", "account"])
async def test_progress_revocation_retires_but_keeps_strict_identity(
    settings,
    storage,
    revocation: str,
) -> None:
    enabled = replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True)
    conversation_id = _scope(storage)
    staged = _stage(storage, conversation_id)
    authorization = _authority(storage)
    if revocation == "identity":
        assert storage.unlink_identity("telegram", "5001")
    elif revocation == "manage":
        authorization.deny_permission(LEGACY_OWNER_USER_ID, "engineer.command.manage")
    else:
        with storage.transaction() as conn:
            conn.execute(
                "UPDATE users SET status='disabled' WHERE id=?",
                (LEGACY_OWNER_USER_ID,),
            )

    pending = await notifications_pending(_request(storage, enabled, authorization), limit=20)
    assert pending["items"] == []
    assert pending["retired"] == [staged.notification_id]
    row = storage.execute(
        "SELECT status,kind,dedup_key FROM outbound_notifications WHERE id=?",
        (staged.notification_id,),
    ).fetchone()
    assert row is not None
    assert dict(row) == {
        "status": "failed",
        "kind": PROGRESS_NOTIFICATION_KIND,
        "dedup_key": staged.dedup_key,
    }


def test_progress_retry_cap_and_terminal_retirement_keep_dedup(storage) -> None:
    conversation_id = _scope(storage)
    first = _stage(storage, conversation_id, checkpoint_sec=60)
    second = _stage(storage, conversation_id, checkpoint_sec=300)
    other = _stage(storage, conversation_id, job_id="2" * 32, checkpoint_sec=60)

    for _ in range(5):
        state = storage.acknowledge_notifications(failed_ids=[first.notification_id])
    assert state["failed"] == [first.notification_id]
    failed = storage.execute(
        "SELECT status,kind,dedup_key FROM outbound_notifications WHERE id=?",
        (first.notification_id,),
    ).fetchone()
    assert failed is not None
    assert dict(failed) == {
        "status": "failed",
        "kind": PROGRESS_NOTIFICATION_KIND,
        "dedup_key": first.dedup_key,
    }

    with storage.transaction() as conn:
        retired = retire_pending_progress_notifications(
            conn,
            actor_id=LEGACY_OWNER_USER_ID,
            tenant_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            delivery_chat_id="5001",
            job_id="1" * 32,
        )
    assert retired == [second.notification_id]
    rows = {
        str(row["id"]): dict(row)
        for row in storage.execute("SELECT id,status,kind,dedup_key FROM outbound_notifications").fetchall()
    }
    assert rows[second.notification_id]["status"] == "failed"
    assert rows[second.notification_id]["dedup_key"] == second.dedup_key
    assert rows[other.notification_id]["status"] == "pending"


def test_ordinary_notification_retirement_semantics_are_unchanged(storage) -> None:
    storage.ensure_user("alice")
    assert storage.enqueue_notification(
        "alice",
        "5001",
        "ordinary",
        kind="monitor",
        dedup_key="monitor:ordinary",
    )
    row = storage.execute("SELECT id FROM outbound_notifications WHERE kind='monitor'").fetchone()
    assert row is not None
    notification_id = str(row["id"])
    for _ in range(5):
        state = storage.acknowledge_notifications(failed_ids=[notification_id])
    assert state["failed"] == [notification_id]
    retired = storage.execute(
        "SELECT status,kind,dedup_key FROM outbound_notifications WHERE id=?",
        (notification_id,),
    ).fetchone()
    assert retired is not None
    assert dict(retired) == {"status": "failed", "kind": "monitor", "dedup_key": ""}


def test_dedup_prefix_is_exact_and_bounded() -> None:
    assert progress_dedup_key("a" * 32, 1800) == f"engineer-progress:v1:{'a' * 32}:1800"
    with pytest.raises(ProgressDeliveryError):
        progress_dedup_key("a" * 31 + "%", 1800)
