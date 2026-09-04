from __future__ import annotations

import base64
import hashlib
import io
import threading
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from friday.api.notifications import _claim_strict_notification, notifications_pending
from friday.interaction_control_plane.engineer_work_item import (
    EngineerWorkItemChannel,
    EngineerWorkItemConflictError,
    EngineerWorkItemState,
    EngineerWorkItemStepState,
    get_current_engineer_work_item_in_transaction,
    get_engineer_work_item_in_transaction,
)
from friday.orchestration.engineer_work_item_coordinator import (
    EngineerCommandReservation,
    EngineerCommandSourceSlot,
    EngineerWorkItemRuntimeCoordinator,
)
from friday.organs.engineer import EngineerOrgan
from friday.organs.engineer.command import (
    CommandGrantAuthority,
    CommandKernel,
    CommandLane,
    CommandOrigin,
    CommandReceipt,
    CommandStatus,
    GeneratedFile,
    IsolationProfile,
    OwnerConfirmationAuthority,
    OwnerSourceAuthority,
)
from friday.organs.engineer.command.contracts import sha256_bytes
from friday.organs.engineer.command.progress import (
    PROGRESS_NOTIFICATION_KIND,
    stage_progress_notification,
)
from friday.organs.engineer.command.store import CommandJobStore
from friday.organs.engineer.command.store_lifecycle import command_store_backup_is_quiescent
from friday.organs.engineer.command_tools import EngineerCommandService
from friday.organs.engineer.publication import ExactGeneratedFileBatch, exact_generated_file_batch
from friday.organs.engineer.terminal_delivery import (
    TERMINAL_NOTIFICATION_KIND,
    TERMINAL_TEXT_NOTIFICATION_KIND,
    UNKNOWN_NOTIFICATION_KIND,
    TerminalDeliveryError,
    parse_terminal_envelope,
    parse_terminal_text_envelope,
    parse_unknown_envelope,
    read_terminal_notification_artifact,
    stage_terminal_archive,
    stage_terminal_text,
    stage_unknown_notification,
    terminal_notification_projection,
    terminal_notification_status,
    terminal_text_notification_projection,
    unknown_notification_projection,
)
from friday.permissions import LEGACY_OWNER_USER_ID, AuthorizationService


def _main_scope(storage, *, chat_id: str = "5001") -> tuple[str, str]:
    storage.ensure_user(
        LEGACY_OWNER_USER_ID,
        source="api-token",
        preset_key="owner",
        metadata={"chat_id": chat_id},
    )
    storage.link_identity("telegram", chat_id, LEGACY_OWNER_USER_ID, linked_by=LEGACY_OWNER_USER_ID)
    conversation = storage.create_conversation(LEGACY_OWNER_USER_ID, "Engineer")
    source = storage.store_message(
        str(conversation["id"]),
        LEGACY_OWNER_USER_ID,
        "user",
        "Собери результат",
        metadata={"telegram_update_id": "100"},
    )
    return str(conversation["id"]), str(source["id"])


def _archive_attachment(
    payload: bytes = b"PK\x03\x04sealed",
) -> tuple[dict[str, str], ExactGeneratedFileBatch]:
    attachment = {
        "kind": "document",
        "filename": "engineer-command-" + "1" * 32 + ".zip",
        "mime_type": "application/zip",
        "content_base64": base64.b64encode(payload).decode("ascii"),
    }
    return attachment, exact_generated_file_batch([attachment], max_bytes=1024 * 1024)


def test_archive_stage_is_atomic_content_free_and_replay_exact(storage, tmp_path: Path) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    attachment, batch = _archive_attachment()
    staged = stage_terminal_archive(
        storage,
        tmp_path / "files",
        actor_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        delivery_chat_id="5001",
        job_id="1" * 32,
        status="completed",
        receipt_mac="2" * 64,
        attachment=attachment,
        batch=batch,
        max_bytes=1024 * 1024,
    )
    repeated = stage_terminal_archive(
        storage,
        tmp_path / "files",
        actor_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        delivery_chat_id="5001",
        job_id="1" * 32,
        status="completed",
        receipt_mac="2" * 64,
        attachment=attachment,
        batch=batch,
        max_bytes=1024 * 1024,
    )
    assert repeated == staged
    changed_attachment, changed_batch = _archive_attachment(b"PK\x03\x04different")
    with pytest.raises(TerminalDeliveryError, match="terminal_artifact_replay_changed"):
        stage_terminal_archive(
            storage,
            tmp_path / "files",
            actor_id=LEGACY_OWNER_USER_ID,
            tenant_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            delivery_chat_id="5001",
            job_id="1" * 32,
            status="completed",
            receipt_mac="2" * 64,
            attachment=changed_attachment,
            batch=changed_batch,
            max_bytes=1024 * 1024,
        )
    row = storage.execute(
        "SELECT id,user_id,chat_id,kind,dedup_key,body,status FROM outbound_notifications WHERE id=?",
        (staged.notification_id,),
    ).fetchone()
    assert row is not None and row["kind"] == TERMINAL_NOTIFICATION_KIND
    assert "content_base64" not in str(row["body"])
    envelope = parse_terminal_envelope(row["body"])
    assert envelope["job_id"] == "1" * 32
    projection = terminal_notification_projection(
        storage,
        dict(row),
        tenant_id=LEGACY_OWNER_USER_ID,
        actor_id=LEGACY_OWNER_USER_ID,
    )
    assert projection["artifact"]["path"] == f"/api/notifications/{staged.notification_id}/artifact"
    stored = read_terminal_notification_artifact(
        storage,
        tmp_path / "files",
        dict(row),
        tenant_id=LEGACY_OWNER_USER_ID,
        actor_id=LEGACY_OWNER_USER_ID,
        max_bytes=1024 * 1024,
    )
    assert stored.content == b"PK\x03\x04sealed"
    assert (
        terminal_notification_status(
            storage,
            staged.notification_id,
            staged.dedup_key,
            staged.envelope_sha256,
        )
        == "pending"
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE outbound_notifications SET dedup_key='tampered' WHERE id=?",
            (staged.notification_id,),
        )
    assert (
        terminal_notification_status(
            storage,
            staged.notification_id,
            staged.dedup_key,
            staged.envelope_sha256,
        )
        == "invalid"
    )


def test_terminal_stage_atomically_retires_pending_progress(storage, tmp_path: Path) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    progress = stage_progress_notification(
        storage,
        actor_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        delivery_chat_id="5001",
        job_id="1" * 32,
        checkpoint_sec=60,
        stdout_bytes=7,
        stderr_bytes=0,
        output_activity=True,
    )
    attachment, batch = _archive_attachment()
    terminal = stage_terminal_archive(
        storage,
        tmp_path / "files",
        actor_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        delivery_chat_id="5001",
        job_id="1" * 32,
        status="completed",
        receipt_mac="2" * 64,
        attachment=attachment,
        batch=batch,
        max_bytes=1024 * 1024,
    )
    rows = {
        str(row["id"]): dict(row)
        for row in storage.execute("SELECT id,kind,status,dedup_key FROM outbound_notifications").fetchall()
    }
    assert rows[progress.notification_id] == {
        "id": progress.notification_id,
        "kind": PROGRESS_NOTIFICATION_KIND,
        "status": "failed",
        "dedup_key": progress.dedup_key,
    }
    assert rows[terminal.notification_id]["status"] == "pending"


def test_corrupt_progress_never_blocks_terminal_stage(storage, tmp_path: Path) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    progress = stage_progress_notification(
        storage,
        actor_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        delivery_chat_id="5001",
        job_id="1" * 32,
        checkpoint_sec=60,
        stdout_bytes=7,
        stderr_bytes=0,
        output_activity=True,
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE outbound_notifications SET body='{}' WHERE id=?",
            (progress.notification_id,),
        )
    attachment, batch = _archive_attachment()
    terminal = stage_terminal_archive(
        storage,
        tmp_path / "files",
        actor_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        delivery_chat_id="5001",
        job_id="1" * 32,
        status="completed",
        receipt_mac="2" * 64,
        attachment=attachment,
        batch=batch,
        max_bytes=1024 * 1024,
    )
    assert terminal.status == "pending"
    rows = {
        str(row["id"]): dict(row)
        for row in storage.execute("SELECT id,kind,status FROM outbound_notifications").fetchall()
    }
    assert rows[progress.notification_id]["status"] == "pending"
    assert rows[terminal.notification_id]["status"] == "pending"


def _receipt(
    payload: bytes,
    *,
    generated: bool = True,
    stdout: bytes = b"",
    status: CommandStatus = CommandStatus.COMPLETED,
) -> CommandReceipt:
    files = (
        (
            GeneratedFile(
                relative_path="result.bin",
                size_bytes=len(payload),
                sha256=sha256_bytes(payload),
                mode=0o600,
            ),
        )
        if generated
        else ()
    )
    return CommandReceipt(
        job_id="3" * 32,
        status=status,
        lane=CommandLane.ARGV,
        origin=CommandOrigin.OWNER_TURN,
        isolation_profile=IsolationProfile.ISOLATED_WORKSPACE,
        command_digest="4" * 64,
        argv_sha256="5" * 64,
        source_hash="6" * 64,
        exit_code=0,
        signal=None,
        timed_out=False,
        cancelled=False,
        truncated_stdout=False,
        truncated_stderr=False,
        started_at=10.0,
        finished_at=11.0,
        executable=None,
        stdout_sha256=sha256_bytes(stdout),
        stderr_sha256=sha256_bytes(b""),
        stdout=stdout,
        stderr=b"",
        generated_files=files,
        error_code="",
        effect_boundary_crossed=True,
        receipt_mac="7" * 64,
    )


def _insert_terminal_job(
    store: CommandJobStore,
    *,
    conversation_id: str,
    source_message_id: str,
    status: CommandStatus = CommandStatus.COMPLETED,
    source_step_id: str = "",
    idempotency_key: str = "terminal-worker-test",
) -> None:
    with store.transaction():
        store.insert_job(
            {
                "job_id": "3" * 32,
                "actor_id": LEGACY_OWNER_USER_ID,
                "tenant_id": LEGACY_OWNER_USER_ID,
                "conversation_id": conversation_id,
                "channel": "telegram",
                "source_row_id": source_message_id,
                "source_step_id": source_step_id,
                "source_hash": "6" * 64,
                "telegram_update_id": "100",
                "isolation_profile": IsolationProfile.ISOLATED_WORKSPACE.value,
                "host_user_authorized": False,
                "idempotency_key": idempotency_key,
                "command_digest": "4" * 64,
                "argv_sha256": "5" * 64,
                "lane": CommandLane.ARGV.value,
                "origin": CommandOrigin.OWNER_TURN.value,
                "status": status.value,
                "grant_nonce": "nonce",
                "timeout_sec": 30,
                "max_stdout_bytes": 1024,
                "max_stderr_bytes": 1024,
                "created_at": 10.0,
                "executable_json": None,
                "delivery_chat_id": "5001",
            }
        )


def _reserve_matching_terminal_work_item(
    storage: Any,
    store: CommandJobStore,
    *,
    conversation_id: str,
    source_message_id: str,
    status: CommandStatus = CommandStatus.COMPLETED,
) -> tuple[str, EngineerWorkItemRuntimeCoordinator]:
    source_step_id = (
        "ecstep-"
        + hashlib.sha256((source_message_id + "\x00terminal-worker").encode("utf-8")).hexdigest()[:32]
    )
    idempotency_key = "ecmd-" + "a" * 64
    source = EngineerCommandSourceSlot(
        owner_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        channel=EngineerWorkItemChannel.TELEGRAM,
        source_row_id=source_message_id,
        source_step_id=source_step_id,
        source_hash="6" * 64,
        telegram_update_id="100",
        delivery_chat_id="5001",
    )
    coordinator = EngineerWorkItemRuntimeCoordinator(store)
    with storage.transaction() as conn:
        outcome = coordinator.reserve_initial_in_transaction(
            conn,
            reservation=EngineerCommandReservation(
                source=source,
                idempotency_key=idempotency_key,
                command_digest="4" * 64,
            ),
        )
    assert outcome.can_submit and outcome.continuation is not None
    _insert_terminal_job(
        store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        status=status,
        source_step_id=source_step_id,
        idempotency_key=idempotency_key,
    )
    return outcome.continuation.work_item_id, coordinator


def _worker_service(
    storage: Any,
    tmp_path: Path,
    command_store: CommandJobStore,
    receipt: CommandReceipt,
    outputs: tuple[tuple[GeneratedFile, bytes], ...],
) -> Any:
    class _Kernel:
        store = command_store

        @staticmethod
        def terminal_receipt(*_args, **_kwargs):
            return receipt, 2

        @staticmethod
        def terminal_result(*_args, **_kwargs):
            return receipt, outputs

    authorization = AuthorizationService(storage)
    for capability in EngineerOrgan().capabilities():
        authorization.register_capability(capability)
    service: Any = EngineerCommandService.__new__(EngineerCommandService)
    service.kernel = _Kernel()
    service.work_items = EngineerWorkItemRuntimeCoordinator(command_store)
    service.storage = storage
    service.settings = SimpleNamespace(
        engineer_mode_enabled=True,
        engineer_command_enabled=True,
        telegram_effective_allowed_chat_ids={5001},
        telegram_open_registration=False,
    )
    service.authorization = authorization
    service.files_root = tmp_path / "files"
    service.max_upload_bytes = 4 * 1024 * 1024
    service._archive_lock = threading.Lock()
    service._archive_cache = None
    service._publication_lock = threading.Lock()
    return service


@pytest.mark.asyncio
async def test_terminal_text_pending_does_not_require_file_read(settings, storage) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    receipt = _receipt(b"", generated=False, stdout=b"scan complete\n")
    staged = stage_terminal_text(
        storage,
        actor_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        delivery_chat_id="5001",
        receipt=receipt,
    )
    assert (
        terminal_notification_status(
            storage,
            staged.notification_id,
            staged.dedup_key,
            staged.envelope_sha256,
        )
        == "pending"
    )
    authorization = AuthorizationService(storage)
    for capability in EngineerOrgan().capabilities():
        authorization.register_capability(capability)
    authorization.deny_permission(LEGACY_OWNER_USER_ID, "files.read")
    app = SimpleNamespace(
        state=SimpleNamespace(
            storage=storage,
            settings=replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
            auth_service=authorization,
        )
    )
    request = Request({"type": "http", "method": "GET", "path": "/", "app": app})
    request.state.actor = SimpleNamespace(source="telegram-bridge")

    legacy = await notifications_pending(request, limit=20)
    assert "status_update" not in legacy["items"][0]
    pending = await notifications_pending(request, limit=20, status_messages=True)

    assert pending["items"] == [
        {
            "id": staged.notification_id,
            "chat_id": "5001",
            "kind": TERMINAL_TEXT_NOTIFICATION_KIND,
            "dedup_key": staged.dedup_key,
        }
    ]
    claimed = _claim_strict_notification(
        app.state,
        staged.notification_id,
        pending["items"][0],
        status_messages=True,
    )
    assert (
        claimed["body"]
        == terminal_text_notification_projection(
            storage,
            storage.list_pending_notifications()[0],
            tenant_id=LEGACY_OWNER_USER_ID,
            actor_id=LEGACY_OWNER_USER_ID,
        )["body"]
    )
    assert claimed["status_update"] == {
        "schema": "friday.telegram-status.v1",
        "operation_id": f"engineer:{'3' * 32}",
        "revision": (1 << 63) - 1,
        "terminal": True,
        "stage": "completed",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["identity", "capability", "account"])
async def test_terminal_text_claim_reauthorizes_after_pointer_listing(
    settings,
    storage,
    revocation: str,
) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    staged = stage_terminal_text(
        storage,
        actor_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        delivery_chat_id="5001",
        receipt=_receipt(b"", generated=False, stdout=b"done\n"),
    )
    authorization = AuthorizationService(storage)
    for capability in EngineerOrgan().capabilities():
        authorization.register_capability(capability)
    state = SimpleNamespace(
        storage=storage,
        settings=replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        auth_service=authorization,
    )
    request = Request({"type": "http", "method": "GET", "path": "/", "app": SimpleNamespace(state=state)})
    request.state.actor = SimpleNamespace(source="telegram-bridge")
    pointer = (await notifications_pending(request, limit=20))["items"][0]

    if revocation == "identity":
        assert storage.unlink_identity("telegram", "5001")
    elif revocation == "capability":
        authorization.deny_permission(LEGACY_OWNER_USER_ID, "engineer.command.manage")
    else:
        with storage.transaction() as conn:
            conn.execute("UPDATE users SET status='disabled' WHERE id=?", (LEGACY_OWNER_USER_ID,))

    with pytest.raises(TerminalDeliveryError, match="terminal_authorization_changed"):
        _claim_strict_notification(
            state,
            staged.notification_id,
            pointer,
            status_messages=True,
        )


@pytest.mark.asyncio
async def test_unknown_pending_is_code_owned_honest_and_needs_no_file_read(
    settings,
    storage,
) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    progress = stage_progress_notification(
        storage,
        actor_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        delivery_chat_id="5001",
        job_id="3" * 32,
        checkpoint_sec=60,
        stdout_bytes=1,
        stderr_bytes=0,
        output_activity=True,
    )
    staged = stage_unknown_notification(
        storage,
        actor_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        delivery_chat_id="5001",
        job_id="3" * 32,
        source_binding_sha256="a" * 64,
    )
    repeated = stage_unknown_notification(
        storage,
        actor_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        delivery_chat_id="5001",
        job_id="3" * 32,
        source_binding_sha256="a" * 64,
    )
    assert repeated == staged
    queued = storage.list_pending_notifications()
    assert len(queued) == 1 and queued[0]["kind"] == UNKNOWN_NOTIFICATION_KIND
    retired_progress = storage.execute(
        "SELECT status FROM outbound_notifications WHERE id=?",
        (progress.notification_id,),
    ).fetchone()
    assert retired_progress is not None and retired_progress["status"] == "failed"
    envelope = parse_unknown_envelope(queued[0]["body"])
    assert envelope["job_id"] == "3" * 32
    assert envelope["source_binding_sha256"] == "a" * 64
    projection = unknown_notification_projection(
        storage,
        queued[0],
        tenant_id=LEGACY_OWNER_USER_ID,
        actor_id=LEGACY_OWNER_USER_ID,
    )
    body = str(projection["body"])
    assert "неизвестно" in body
    assert "ни успех, ни ошибку" in body
    assert "автоматически не запускалась повторно" in body
    assert "завершена успешно" not in body

    authorization = AuthorizationService(storage)
    for capability in EngineerOrgan().capabilities():
        authorization.register_capability(capability)
    authorization.deny_permission(LEGACY_OWNER_USER_ID, "files.read")
    app = SimpleNamespace(
        state=SimpleNamespace(
            storage=storage,
            settings=replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
            auth_service=authorization,
        )
    )
    request = Request({"type": "http", "method": "GET", "path": "/", "app": app})
    request.state.actor = SimpleNamespace(source="telegram-bridge")
    pending = await notifications_pending(request, limit=20, status_messages=True)
    assert pending["items"] == [
        {
            "id": staged.notification_id,
            "chat_id": "5001",
            "kind": UNKNOWN_NOTIFICATION_KIND,
            "dedup_key": staged.dedup_key,
        }
    ]
    claimed_unknown = _claim_strict_notification(
        app.state,
        staged.notification_id,
        pending["items"][0],
        status_messages=True,
    )
    assert claimed_unknown["body"] == body
    assert claimed_unknown["status_update"] == {
        "schema": "friday.telegram-status.v1",
        "operation_id": f"engineer:{'3' * 32}",
        "revision": (1 << 63) - 1,
        "terminal": True,
        "stage": "unknown",
    }

    assistant = storage.execute(
        "SELECT role,content,metadata_json,reply_to FROM messages WHERE id=?",
        (envelope["assistant_message_id"],),
    ).fetchone()
    assert assistant is not None
    assert assistant["role"] == "assistant" and assistant["content"] == body
    assert assistant["reply_to"] == source_message_id
    assert "engineer_command_unknown" in str(assistant["metadata_json"])


def test_terminal_text_reports_timeout_and_bounded_output(storage) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    receipt = replace(
        _receipt(b"", generated=False, stdout=(b"x" * 8_000)),
        status=CommandStatus.TIMEOUT,
        exit_code=None,
        signal=15,
        timed_out=True,
        finished_at=310.0,
    )
    staged = stage_terminal_text(
        storage,
        actor_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        delivery_chat_id="5001",
        receipt=receipt,
    )
    row = storage.execute(
        "SELECT id,user_id,chat_id,kind,dedup_key,body,status FROM outbound_notifications WHERE id=?",
        (staged.notification_id,),
    ).fetchone()
    assert row is not None
    body = terminal_text_notification_projection(
        storage,
        dict(row),
        tenant_id=LEGACY_OWNER_USER_ID,
        actor_id=LEGACY_OWNER_USER_ID,
    )["body"]
    assert "остановлена по тайм-ауту" in body
    assert "за 300 с" in body
    assert "вывод сокращён" in body
    assert len(body) <= 3_800


def test_worker_stages_without_model_and_reconciles_sent(storage, tmp_path: Path) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    _insert_terminal_job(
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )
    payload = b"compiled binary"
    receipt = _receipt(payload)
    service = _worker_service(
        storage,
        tmp_path,
        command_store,
        receipt,
        ((receipt.generated_files[0], payload),),
    )

    result = service.publish_terminal_jobs()
    assert result == {"staged": 1, "reconciled": 0, "failed": 0}
    queued = storage.list_pending_notifications()
    assert len(queued) == 1 and queued[0]["kind"] == TERMINAL_NOTIFICATION_KIND
    envelope = parse_terminal_envelope(queued[0]["body"])
    assert envelope["caption"] == (f"Engineer-задание {receipt.job_id} завершено. Файл результата приложен.")
    assert envelope["artifact"]["filename"] == "result.bin"
    assert envelope["artifact"]["mime_type"] == "application/octet-stream"
    row = storage.execute(
        "SELECT id,user_id,chat_id,kind,dedup_key,body,status FROM outbound_notifications WHERE id=?",
        (queued[0]["id"],),
    ).fetchone()
    stored = read_terminal_notification_artifact(
        storage,
        tmp_path / "files",
        dict(row),
        tenant_id=LEGACY_OWNER_USER_ID,
        actor_id=LEGACY_OWNER_USER_ID,
        max_bytes=4 * 1024 * 1024,
    )
    assert stored.content == payload
    assert not stored.content.startswith(b"PK")
    publication = command_store.list_staged_publications()
    assert len(publication) == 1
    storage.mark_notifications(sent_ids=[queued[0]["id"]])
    result = service.publish_terminal_jobs()
    assert result["reconciled"] == 1
    state = command_store._conn.execute(  # noqa: SLF001 - exact durable assertion
        "SELECT state FROM command_job_publications WHERE job_id=?",
        (receipt.job_id,),
    ).fetchone()
    assert state is not None and state["state"] == "sent"
    command_store.close()


def test_worker_publishes_two_user_files_as_zip_without_receipt(storage, tmp_path: Path) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    _insert_terminal_job(
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )
    alpha = b"alpha\n"
    nested = b"nested"
    files = (
        GeneratedFile(
            relative_path="a.txt",
            size_bytes=len(alpha),
            sha256=sha256_bytes(alpha),
            mode=0o600,
        ),
        GeneratedFile(
            relative_path="reports/z.bin",
            size_bytes=len(nested),
            sha256=sha256_bytes(nested),
            mode=0o600,
        ),
    )
    receipt = replace(_receipt(b"unused", generated=False), generated_files=files)
    service = _worker_service(
        storage,
        tmp_path,
        command_store,
        receipt,
        ((files[0], alpha), (files[1], nested)),
    )

    assert service.publish_terminal_jobs() == {"staged": 1, "reconciled": 0, "failed": 0}
    queued = storage.list_pending_notifications()
    envelope = parse_terminal_envelope(queued[0]["body"])
    assert envelope["caption"] == (
        f"Engineer-задание {receipt.job_id} завершено. Проверенный архив результата приложен."
    )
    assert envelope["artifact"]["filename"] == f"engineer-command-{receipt.job_id}.zip"
    row = storage.execute(
        "SELECT id,user_id,chat_id,kind,dedup_key,body,status FROM outbound_notifications WHERE id=?",
        (queued[0]["id"],),
    ).fetchone()
    stored = read_terminal_notification_artifact(
        storage,
        tmp_path / "files",
        dict(row),
        tenant_id=LEGACY_OWNER_USER_ID,
        actor_id=LEGACY_OWNER_USER_ID,
        max_bytes=4 * 1024 * 1024,
    )
    with zipfile.ZipFile(io.BytesIO(stored.content)) as archive:
        assert archive.namelist() == ["a.txt", "reports/z.bin"]
        assert "RECEIPT.json" not in archive.namelist()
        assert archive.read("a.txt") == alpha
    command_store.close()


def test_worker_hides_internal_only_outputs_as_text(storage, tmp_path: Path) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    _insert_terminal_job(
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )
    payload = b"{}"
    files = (
        GeneratedFile(
            relative_path="RECEIPT.json",
            size_bytes=len(payload),
            sha256=sha256_bytes(payload),
            mode=0o600,
        ),
    )
    receipt = replace(
        _receipt(b"", generated=False, stdout=b"done\n"),
        generated_files=files,
    )
    service = _worker_service(
        storage,
        tmp_path,
        command_store,
        receipt,
        ((files[0], payload),),
    )

    assert service.publish_terminal_jobs() == {"staged": 1, "reconciled": 0, "failed": 0}
    queued = storage.list_pending_notifications()
    assert len(queued) == 1 and queued[0]["kind"] == TERMINAL_TEXT_NOTIFICATION_KIND
    command_store.close()


def test_unknown_worker_marks_exact_work_item_and_never_resubmits_or_reads_result(
    storage,
    tmp_path: Path,
) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    work_item_id, _coordinator = _reserve_matching_terminal_work_item(
        storage,
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        status=CommandStatus.UNKNOWN,
    )
    service = _worker_service(storage, tmp_path, command_store, _receipt(b""), ())

    def forbidden(*_args, **_kwargs):  # noqa: ANN002, ANN003
        pytest.fail("UNKNOWN publication must not execute, wait for, or rebuild a command result")

    service.kernel.submit = forbidden
    service.kernel.terminal_receipt = forbidden
    service.kernel.terminal_result = forbidden

    assert command_store_backup_is_quiescent(command_store._conn) is False  # noqa: SLF001
    assert service.publish_terminal_jobs() == {"staged": 1, "reconciled": 0, "failed": 0}
    queued = storage.list_pending_notifications()
    assert len(queued) == 1 and queued[0]["kind"] == UNKNOWN_NOTIFICATION_KIND
    envelope = parse_unknown_envelope(queued[0]["body"])
    job = command_store.read_job("3" * 32)
    assert envelope["source_binding_sha256"] == job["source_binding_sha256"]
    with storage.transaction() as conn:
        unknown = get_engineer_work_item_in_transaction(
            conn,
            work_item_id=work_item_id,
            owner_id=LEGACY_OWNER_USER_ID,
            tenant_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
        )
    assert unknown is not None and unknown.state is EngineerWorkItemState.UNCERTAIN
    assert unknown.current_step.state is EngineerWorkItemStepState.UNKNOWN
    revision = unknown.revision

    # Staged publication is the durable dedup boundary, not another command turn.
    assert service.publish_terminal_jobs() == {"staged": 0, "reconciled": 0, "failed": 0}
    assert len(storage.list_pending_notifications()) == 1
    assert command_store_backup_is_quiescent(command_store._conn) is False  # noqa: SLF001
    with storage.transaction() as conn:
        replay = get_engineer_work_item_in_transaction(
            conn,
            work_item_id=work_item_id,
            owner_id=LEGACY_OWNER_USER_ID,
            tenant_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
        )
    assert replay is not None and replay.revision == revision

    storage.mark_notifications(sent_ids=[str(queued[0]["id"])])
    # Main ACK without external reconciliation is still a restore-unsafe crash window.
    assert command_store_backup_is_quiescent(command_store._conn) is False  # noqa: SLF001
    assert service.publish_terminal_jobs() == {"staged": 0, "reconciled": 1, "failed": 0}
    publication = command_store._conn.execute(  # noqa: SLF001 - exact durable assertion
        "SELECT state FROM command_job_publications WHERE job_id=?",
        ("3" * 32,),
    ).fetchone()
    assert publication is not None and publication["state"] == "sent"
    assert command_store_backup_is_quiescent(command_store._conn) is True  # noqa: SLF001
    command_store.close()


def test_unknown_worker_restart_after_main_reconciliation_stages_same_notice(
    storage,
    tmp_path: Path,
) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    work_item_id, _coordinator = _reserve_matching_terminal_work_item(
        storage,
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        status=CommandStatus.UNKNOWN,
    )
    before_crash = _worker_service(storage, tmp_path, command_store, _receipt(b""), ())
    candidate = command_store.list_terminal_publication_candidates()[0]

    # Crash window: exact EWI reconciliation commits before either carrier ledger.
    before_crash._mark_unknown_work_item_for_publication(candidate)  # noqa: SLF001
    assert storage.list_pending_notifications() == []
    with storage.transaction() as conn:
        marked = get_engineer_work_item_in_transaction(
            conn,
            work_item_id=work_item_id,
            owner_id=LEGACY_OWNER_USER_ID,
            tenant_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
        )
    assert marked is not None and marked.current_step.state is EngineerWorkItemStepState.UNKNOWN
    revision = marked.revision

    restarted = _worker_service(storage, tmp_path, command_store, _receipt(b""), ())
    assert restarted.publish_terminal_jobs() == {"staged": 1, "reconciled": 0, "failed": 0}
    assert len(storage.list_pending_notifications()) == 1
    with storage.transaction() as conn:
        replay = get_engineer_work_item_in_transaction(
            conn,
            work_item_id=work_item_id,
            owner_id=LEGACY_OWNER_USER_ID,
            tenant_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
        )
    assert replay is not None and replay.revision == revision
    command_store.close()


def test_real_kernel_restart_unknown_is_proactively_published_without_reexecution(
    storage,
    tmp_path: Path,
) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_root = tmp_path / "commands"
    command_store = CommandJobStore(command_root)
    work_item_id, _coordinator = _reserve_matching_terminal_work_item(
        storage,
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        status=CommandStatus.RUNNING,
    )
    command_store.close()

    restarted = CommandKernel(
        command_root,
        CommandGrantAuthority(
            b"g" * 48,
            OwnerSourceAuthority(b"s" * 48),
            OwnerConfirmationAuthority(b"c" * 48),
        ),
    )
    try:
        assert restarted.store.read_job("3" * 32)["status"] == CommandStatus.UNKNOWN.value
        service = _worker_service(storage, tmp_path, restarted.store, _receipt(b""), ())
        service.kernel = restarted
        assert service.publish_terminal_jobs() == {"staged": 1, "reconciled": 0, "failed": 0}
        queued = storage.list_pending_notifications()
        assert len(queued) == 1 and queued[0]["kind"] == UNKNOWN_NOTIFICATION_KIND
        with storage.transaction() as conn:
            item = get_engineer_work_item_in_transaction(
                conn,
                work_item_id=work_item_id,
                owner_id=LEGACY_OWNER_USER_ID,
                tenant_id=LEGACY_OWNER_USER_ID,
                conversation_id=conversation_id,
                channel=EngineerWorkItemChannel.TELEGRAM,
            )
        assert item is not None and item.state is EngineerWorkItemState.UNCERTAIN
        assert item.current_step.state is EngineerWorkItemStepState.UNKNOWN
    finally:
        restarted.close()


def test_unknown_old_source_notifies_owner_without_mutating_new_current_step(
    storage,
    tmp_path: Path,
) -> None:
    conversation_id, old_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    _insert_terminal_job(
        command_store,
        conversation_id=conversation_id,
        source_message_id=old_message_id,
        status=CommandStatus.UNKNOWN,
        source_step_id="ecstep-" + "b" * 32,
        idempotency_key="ecmd-" + "b" * 64,
    )
    new_message = storage.store_message(
        conversation_id,
        LEGACY_OWNER_USER_ID,
        "user",
        "Новая независимая команда",
        metadata={"telegram_update_id": "101"},
    )
    new_source = EngineerCommandSourceSlot(
        owner_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        channel=EngineerWorkItemChannel.TELEGRAM,
        source_row_id=str(new_message["id"]),
        source_step_id="ecstep-" + "c" * 32,
        source_hash="8" * 64,
        telegram_update_id="101",
        delivery_chat_id="5001",
    )
    coordinator = EngineerWorkItemRuntimeCoordinator(command_store)
    with storage.transaction() as conn:
        current = coordinator.reserve_initial_in_transaction(
            conn,
            reservation=EngineerCommandReservation(
                source=new_source,
                idempotency_key="ecmd-" + "c" * 64,
                command_digest="9" * 64,
            ),
        )
    assert current.can_submit and current.continuation is not None
    work_item_id = current.continuation.work_item_id
    revision = current.continuation.revision

    service = _worker_service(storage, tmp_path, command_store, _receipt(b""), ())
    assert service.publish_terminal_jobs() == {"staged": 1, "reconciled": 0, "failed": 0}
    queued = storage.list_pending_notifications()
    assert len(queued) == 1
    assert parse_unknown_envelope(queued[0]["body"])["source_message_id"] == old_message_id
    with storage.transaction() as conn:
        after = get_engineer_work_item_in_transaction(
            conn,
            work_item_id=work_item_id,
            owner_id=LEGACY_OWNER_USER_ID,
            tenant_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
        )
    assert after is not None and after.revision == revision
    assert after.state is EngineerWorkItemState.ACTIVE
    assert after.current_step.state is EngineerWorkItemStepState.PREPARED
    command_store.close()


def test_archived_conversation_still_receives_owed_unknown_notice(
    storage,
    tmp_path: Path,
) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    work_item_id, _coordinator = _reserve_matching_terminal_work_item(
        storage,
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        status=CommandStatus.UNKNOWN,
    )
    service = _worker_service(storage, tmp_path, command_store, _receipt(b""), ())
    assert storage.archive_conversation(conversation_id, LEGACY_OWNER_USER_ID)

    assert service.publish_terminal_jobs() == {"staged": 1, "reconciled": 0, "failed": 0}
    queued = storage.list_pending_notifications()
    assert len(queued) == 1 and queued[0]["kind"] == UNKNOWN_NOTIFICATION_KIND
    with storage.transaction() as conn:
        item = get_engineer_work_item_in_transaction(
            conn,
            work_item_id=work_item_id,
            owner_id=LEGACY_OWNER_USER_ID,
            tenant_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
        )
    assert item is not None and item.state is EngineerWorkItemState.UNCERTAIN
    assert item.current_step.state is EngineerWorkItemStepState.UNKNOWN
    command_store.close()


def test_worker_restart_after_work_item_settlement_stages_exact_carrier(
    storage,
    tmp_path: Path,
) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    work_item_id, _coordinator = _reserve_matching_terminal_work_item(
        storage,
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )
    payload = b"restart-safe"
    receipt = _receipt(payload)
    service = _worker_service(
        storage,
        tmp_path,
        command_store,
        receipt,
        ((receipt.generated_files[0], payload),),
    )
    candidate = command_store.list_terminal_publication_candidates()[0]

    # Crash window: main EWI commits first, no external carrier exists yet.
    service._settle_terminal_work_item_for_publication(candidate, receipt, 2)  # noqa: SLF001
    assert storage.list_pending_notifications() == []
    with storage.transaction() as conn:
        settled = get_engineer_work_item_in_transaction(
            conn,
            work_item_id=work_item_id,
            owner_id=LEGACY_OWNER_USER_ID,
            tenant_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
        )
    assert settled is not None
    assert settled.state is EngineerWorkItemState.WAITING_FOR_INPUT
    assert settled.current_step.state is EngineerWorkItemStepState.SETTLED
    settled_revision = settled.revision
    settled_digest = settled.current_step.terminal_receipt_sha256

    restarted = _worker_service(
        storage,
        tmp_path,
        command_store,
        receipt,
        ((receipt.generated_files[0], payload),),
    )
    assert restarted.publish_terminal_jobs() == {"staged": 1, "reconciled": 0, "failed": 0}
    with storage.transaction() as conn:
        replay = get_engineer_work_item_in_transaction(
            conn,
            work_item_id=work_item_id,
            owner_id=LEGACY_OWNER_USER_ID,
            tenant_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
        )
    assert replay is not None
    assert (replay.revision, replay.current_step.terminal_receipt_sha256) == (
        settled_revision,
        settled_digest,
    )
    command_store.close()


def test_worker_lost_cas_race_replays_exact_settlement_and_still_delivers(
    storage,
    tmp_path: Path,
) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    _work_item_id, coordinator = _reserve_matching_terminal_work_item(
        storage,
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )
    payload = b"race-safe"
    receipt = _receipt(payload)
    service = _worker_service(
        storage,
        tmp_path,
        command_store,
        receipt,
        ((receipt.generated_files[0], payload),),
    )
    candidate = command_store.list_terminal_publication_candidates()[0]
    service._settle_terminal_work_item_for_publication(candidate, receipt, 2)  # noqa: SLF001
    with storage.transaction() as conn:
        winner = coordinator.current_structural_state_in_transaction(
            conn,
            owner_id=LEGACY_OWNER_USER_ID,
            tenant_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
        )
    assert winner is not None and winner.step_state is EngineerWorkItemStepState.SETTLED

    class _LostCasCoordinator:
        reads = 0

        def current_structural_state_in_transaction(self, conn, **scope):
            self.reads += 1
            if self.reads == 1:
                return replace(
                    winner,
                    revision=max(1, winner.revision - 1),
                    step_state=EngineerWorkItemStepState.ADMITTED,
                    terminal_receipt_sha256="",
                )
            return coordinator.current_structural_state_in_transaction(conn, **scope)

        @staticmethod
        def settle_verified_terminal_in_transaction(*_args, **_kwargs):
            raise EngineerWorkItemConflictError("lost CAS")

    service.work_items = _LostCasCoordinator()
    assert service.publish_terminal_jobs() == {"staged": 1, "reconciled": 0, "failed": 0}
    assert len(storage.list_pending_notifications()) == 1
    command_store.close()


def test_restart_publishes_old_terminal_after_work_item_advances_to_next_source(
    storage,
    tmp_path: Path,
) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    work_item_id, coordinator = _reserve_matching_terminal_work_item(
        storage,
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )
    payload = b"old-step-result"
    receipt = _receipt(payload)
    before_crash = _worker_service(
        storage,
        tmp_path,
        command_store,
        receipt,
        ((receipt.generated_files[0], payload),),
    )
    candidate = command_store.list_terminal_publication_candidates()[0]
    before_crash._settle_terminal_work_item_for_publication(candidate, receipt, 2)  # noqa: SLF001

    with storage.transaction() as conn:
        settled = get_engineer_work_item_in_transaction(
            conn,
            work_item_id=work_item_id,
            owner_id=LEGACY_OWNER_USER_ID,
            tenant_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
        )
    assert settled is not None and settled.current_step.state is EngineerWorkItemStepState.SETTLED

    next_message = storage.store_message(
        conversation_id,
        LEGACY_OWNER_USER_ID,
        "user",
        "Следующий шаг",
        metadata={"telegram_update_id": "101"},
    )
    next_source = EngineerCommandSourceSlot(
        owner_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        channel=EngineerWorkItemChannel.TELEGRAM,
        source_row_id=str(next_message["id"]),
        source_step_id="ecstep-" + "c" * 32,
        source_hash="8" * 64,
        telegram_update_id="101",
        delivery_chat_id="5001",
    )
    with storage.transaction() as conn:
        advanced = coordinator.reserve_next_in_transaction(
            conn,
            work_item_id=work_item_id,
            expected_revision=settled.revision,
            reservation=EngineerCommandReservation(
                source=next_source,
                idempotency_key="ecmd-" + "d" * 64,
                command_digest="9" * 64,
            ),
        )
    assert advanced.can_submit and advanced.continuation is not None
    assert advanced.continuation.source_binding_sha256 != candidate["source_binding_sha256"]

    restarted = _worker_service(
        storage,
        tmp_path,
        command_store,
        receipt,
        ((receipt.generated_files[0], payload),),
    )
    assert restarted.publish_terminal_jobs() == {"staged": 1, "reconciled": 0, "failed": 0}
    assert len(storage.list_pending_notifications()) == 1
    command_store.close()


def test_archived_matching_work_item_auto_cancels_without_suppressing_delivery(
    storage,
    tmp_path: Path,
) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    work_item_id, _coordinator = _reserve_matching_terminal_work_item(
        storage,
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )
    payload = b"archived-result"
    receipt = _receipt(payload)
    service = _worker_service(
        storage,
        tmp_path,
        command_store,
        receipt,
        ((receipt.generated_files[0], payload),),
    )
    assert storage.archive_conversation(conversation_id, LEGACY_OWNER_USER_ID)

    assert service.publish_terminal_jobs() == {"staged": 1, "reconciled": 0, "failed": 0}
    with storage.transaction() as conn:
        current = get_current_engineer_work_item_in_transaction(
            conn,
            owner_id=LEGACY_OWNER_USER_ID,
            tenant_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
        )
        retired = get_engineer_work_item_in_transaction(
            conn,
            work_item_id=work_item_id,
            owner_id=LEGACY_OWNER_USER_ID,
            tenant_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
        )
    assert current is None
    assert retired is not None and retired.state is EngineerWorkItemState.CANCELLED
    assert retired.current_step.state is EngineerWorkItemStepState.SETTLED
    assert len(storage.list_pending_notifications()) == 1
    command_store.close()


@pytest.mark.parametrize("observation", ["none", "mismatch"])
def test_absent_or_mismatched_work_item_never_suppresses_terminal_delivery(
    storage,
    tmp_path: Path,
    observation: str,
) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    _insert_terminal_job(
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        source_step_id="ecstep-" + "b" * 32,
    )
    payload = b"independent-carrier"
    receipt = _receipt(payload)
    service = _worker_service(
        storage,
        tmp_path,
        command_store,
        receipt,
        ((receipt.generated_files[0], payload),),
    )
    if observation == "mismatch":

        class _MismatchCoordinator:
            @staticmethod
            def current_structural_state_in_transaction(*_args, **_kwargs):
                return SimpleNamespace(
                    command_job_id="9" * 32,
                    command_status=receipt.status,
                )

            @staticmethod
            def settle_verified_terminal_in_transaction(*_args, **_kwargs):
                pytest.fail("a mismatched Work Item must never be settled by this job")

        service.work_items = _MismatchCoordinator()

    assert service.publish_terminal_jobs() == {"staged": 1, "reconciled": 0, "failed": 0}
    assert len(storage.list_pending_notifications()) == 1
    command_store.close()


def test_worker_still_delivers_terminal_result_after_conversation_archive(
    storage,
    tmp_path: Path,
) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    _insert_terminal_job(
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )
    payload = b"late verified result"
    receipt = _receipt(payload)
    service = _worker_service(
        storage,
        tmp_path,
        command_store,
        receipt,
        ((receipt.generated_files[0], payload),),
    )
    assert storage.archive_conversation(conversation_id, LEGACY_OWNER_USER_ID)

    assert service.publish_terminal_jobs() == {
        "staged": 1,
        "reconciled": 0,
        "failed": 0,
    }
    queued = storage.list_pending_notifications()
    assert len(queued) == 1 and queued[0]["kind"] == TERMINAL_NOTIFICATION_KIND
    assert parse_terminal_envelope(queued[0]["body"])["conversation_id"] == conversation_id
    command_store.close()


def test_transient_publication_failures_use_durable_bounded_backoff(
    storage,
    tmp_path: Path,
) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    _insert_terminal_job(
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )

    failure_time = 1_000.0
    for attempt in range(1, 9):
        command_store.record_publication_attempt(
            "3" * 32,
            "authorization_denied",
            failed_at=failure_time,
        )
        row = command_store._conn.execute(  # noqa: SLF001 - exact durable assertion
            "SELECT state,attempts,next_attempt_at FROM command_job_publications WHERE job_id=?",
            ("3" * 32,),
        ).fetchone()
        assert row is not None and row["state"] == "pending" and row["attempts"] == attempt
        delay = float(row["next_attempt_at"]) - failure_time
        assert 5 <= delay <= 30 * 60
        assert (
            command_store.list_terminal_publication_candidates(now=float(row["next_attempt_at"]) - 0.001)
            == []
        )
        assert len(command_store.list_terminal_publication_candidates(now=float(row["next_attempt_at"]))) == 1
        failure_time = float(row["next_attempt_at"])
    command_store.close()


def test_proven_permanent_publication_failure_blocks_immediately(storage, tmp_path: Path) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    _insert_terminal_job(
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )

    command_store.record_publication_attempt(
        "3" * 32,
        "terminal_receipt_unpublishable",
        failed_at=1_000.0,
        permanent=True,
    )
    row = command_store._conn.execute(  # noqa: SLF001 - exact durable assertion
        "SELECT state,attempts,last_error_code,next_attempt_at FROM command_job_publications",
    ).fetchone()
    assert row is not None
    assert dict(row) == {
        "attempts": 1,
        "last_error_code": "terminal_receipt_unpublishable",
        "next_attempt_at": None,
        "state": "blocked",
    }
    assert command_store.list_terminal_publication_candidates(now=10_000.0) == []
    command_store.close()


@pytest.mark.parametrize("status", [CommandStatus.COMPLETED, CommandStatus.FAILED])
def test_worker_delivers_zero_generated_outputs_as_text_without_empty_archive(
    storage,
    tmp_path: Path,
    status: CommandStatus,
) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    _insert_terminal_job(
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        status=status,
    )
    output = b"console result\n"
    receipt = _receipt(b"", generated=False, stdout=output, status=status)
    service = _worker_service(storage, tmp_path, command_store, receipt, ())
    service.kernel.terminal_result = lambda *_args, **_kwargs: pytest.fail(  # type: ignore[method-assign]
        "zero-output publication must not build or read an archive"
    )

    assert service.publish_terminal_jobs() == {"staged": 1, "reconciled": 0, "failed": 0}
    queued = storage.list_pending_notifications()
    assert len(queued) == 1 and queued[0]["kind"] == TERMINAL_TEXT_NOTIFICATION_KIND
    assert "console result" not in str(queued[0]["body"])
    envelope = parse_terminal_text_envelope(queued[0]["body"])
    assert envelope["job_id"] == receipt.job_id
    projection = terminal_text_notification_projection(
        storage,
        queued[0],
        tenant_id=LEGACY_OWNER_USER_ID,
        actor_id=LEGACY_OWNER_USER_ID,
    )
    assert "console result" in projection["body"]
    assert ("завершена" if status is CommandStatus.COMPLETED else "с ошибкой") in projection["body"]
    publication = command_store._conn.execute(  # noqa: SLF001 - exact durable assertion
        """SELECT state,last_error_code,attempts,notification_id,dedup_key,envelope_sha256
             FROM command_job_publications WHERE job_id=?""",
        (receipt.job_id,),
    ).fetchone()
    assert publication is not None
    assert dict(publication) == {
        "attempts": 0,
        "dedup_key": str(queued[0]["dedup_key"]),
        "envelope_sha256": hashlib.sha256(str(queued[0]["body"]).encode("ascii")).hexdigest(),
        "last_error_code": "",
        "notification_id": str(queued[0]["id"]),
        "state": "staged",
    }
    assert service.publish_terminal_jobs() == {"staged": 0, "reconciled": 0, "failed": 0}
    assert len(storage.list_pending_notifications()) == 1
    storage.mark_notifications(sent_ids=[str(queued[0]["id"])])
    assert service.publish_terminal_jobs() == {"staged": 0, "reconciled": 1, "failed": 0}
    final = command_store._conn.execute(  # noqa: SLF001 - exact durable assertion
        "SELECT state FROM command_job_publications WHERE job_id=?",
        (receipt.job_id,),
    ).fetchone()
    assert final is not None and final["state"] == "sent"
    assert not (tmp_path / "files").exists()
    command_store.close()


def test_notification_identity_drift_finishes_uncertain_without_requeue(
    storage,
    tmp_path: Path,
) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    command_store = CommandJobStore(tmp_path / "commands")
    _insert_terminal_job(
        command_store,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )
    payload = b"sealed"
    receipt = _receipt(payload)
    service = _worker_service(
        storage,
        tmp_path,
        command_store,
        receipt,
        ((receipt.generated_files[0], payload),),
    )
    assert service.publish_terminal_jobs()["staged"] == 1
    queued = storage.list_pending_notifications()[0]
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE outbound_notifications SET dedup_key='drifted' WHERE id=?",
            (queued["id"],),
        )

    result = service.publish_terminal_jobs()
    assert result == {"staged": 0, "reconciled": 1, "failed": 0}
    state = command_store._conn.execute(  # noqa: SLF001 - exact durable assertion
        "SELECT state FROM command_job_publications WHERE job_id=?",
        (receipt.job_id,),
    ).fetchone()
    assert state is not None and state["state"] == "uncertain"
    assert storage.execute("SELECT COUNT(*) FROM outbound_notifications").fetchone()[0] == 1
    command_store.close()


def test_authority_retirement_is_uncertain_but_proven_rejection_cap_is_failed(
    storage,
    tmp_path: Path,
) -> None:
    conversation_id, source_message_id = _main_scope(storage)
    attachment, batch = _archive_attachment()
    staged = stage_terminal_archive(
        storage,
        tmp_path / "files",
        actor_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        delivery_chat_id="5001",
        job_id="1" * 32,
        status="completed",
        receipt_mac="2" * 64,
        attachment=attachment,
        batch=batch,
        max_bytes=1024 * 1024,
    )
    storage.discard_notifications_verified(
        [staged.notification_id],
        reason="terminal_authorization_changed",
    )
    assert (
        terminal_notification_status(
            storage,
            staged.notification_id,
            staged.dedup_key,
            staged.envelope_sha256,
        )
        == "uncertain"
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE outbound_notifications SET attempts=5 WHERE id=?",
            (staged.notification_id,),
        )
    assert (
        terminal_notification_status(
            storage,
            staged.notification_id,
            staged.dedup_key,
            staged.envelope_sha256,
        )
        == "failed"
    )
