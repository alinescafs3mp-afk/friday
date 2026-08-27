from __future__ import annotations

import base64
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from friday.api.notifications import notifications_pending
from friday.organs.engineer import EngineerOrgan
from friday.organs.engineer.command import (
    CommandLane,
    CommandOrigin,
    CommandReceipt,
    CommandStatus,
    GeneratedFile,
    IsolationProfile,
)
from friday.organs.engineer.command.contracts import sha256_bytes
from friday.organs.engineer.command.progress import (
    PROGRESS_NOTIFICATION_KIND,
    stage_progress_notification,
)
from friday.organs.engineer.command.store import CommandJobStore
from friday.organs.engineer.command_tools import EngineerCommandService
from friday.organs.engineer.publication import ExactGeneratedFileBatch, exact_generated_file_batch
from friday.organs.engineer.terminal_delivery import (
    TERMINAL_NOTIFICATION_KIND,
    TERMINAL_TEXT_NOTIFICATION_KIND,
    TerminalDeliveryError,
    parse_terminal_envelope,
    parse_terminal_text_envelope,
    read_terminal_notification_artifact,
    stage_terminal_archive,
    stage_terminal_text,
    terminal_notification_projection,
    terminal_notification_status,
    terminal_text_notification_projection,
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
                "source_hash": "6" * 64,
                "telegram_update_id": "100",
                "isolation_profile": IsolationProfile.ISOLATED_WORKSPACE.value,
                "host_user_authorized": False,
                "idempotency_key": "terminal-worker-test",
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
            "body": terminal_text_notification_projection(
                storage,
                storage.list_pending_notifications()[0],
                tenant_id=LEGACY_OWNER_USER_ID,
                actor_id=LEGACY_OWNER_USER_ID,
            )["body"],
                "kind": TERMINAL_TEXT_NOTIFICATION_KIND,
                "dedup_key": staged.dedup_key,
                "status_update": {
                    "schema": "friday.telegram-status.v1",
                    "operation_id": f"engineer:{'3' * 32}",
                    "revision": (1 << 63) - 1,
                    "terminal": True,
                    "stage": "completed",
                },
            }
    ]


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
        "dedup_key": "",
        "envelope_sha256": "",
        "last_error_code": "no_generated_files",
        "notification_id": "",
        "state": "blocked",
    }
    assert service.publish_terminal_jobs() == {"staged": 0, "reconciled": 0, "failed": 0}
    assert len(storage.list_pending_notifications()) == 1
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
