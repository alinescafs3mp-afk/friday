from __future__ import annotations

import base64
import io
import json
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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
from friday.organs.engineer.command.store import CommandJobStore
from friday.organs.engineer.command_tools import EngineerCommandService
from friday.organs.engineer.publication import ExactGeneratedFileBatch, exact_generated_file_batch
from friday.organs.engineer.terminal_delivery import (
    TERMINAL_NOTIFICATION_KIND,
    TerminalDeliveryError,
    parse_terminal_envelope,
    read_terminal_notification_artifact,
    stage_terminal_archive,
    terminal_notification_projection,
    terminal_notification_status,
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
    assert terminal_notification_status(
        storage,
        staged.notification_id,
        staged.dedup_key,
        staged.envelope_sha256,
    ) == "pending"
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE outbound_notifications SET dedup_key='tampered' WHERE id=?",
            (staged.notification_id,),
        )
    assert terminal_notification_status(
        storage,
        staged.notification_id,
        staged.dedup_key,
        staged.envelope_sha256,
    ) == "invalid"


def _receipt(
    payload: bytes,
    *,
    generated: bool = True,
    stdout: bytes = b"",
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
        status=CommandStatus.COMPLETED,
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
                "status": CommandStatus.COMPLETED.value,
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


def test_worker_delivers_zero_generated_outputs_as_strict_receipt_archive(
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
    output = b"console result\n"
    receipt = _receipt(b"", generated=False, stdout=output)
    service = _worker_service(storage, tmp_path, command_store, receipt, ())

    assert service.publish_terminal_jobs() == {"staged": 1, "reconciled": 0, "failed": 0}
    row = storage.list_pending_notifications()[0]
    stored = read_terminal_notification_artifact(
        storage,
        tmp_path / "files",
        {**row, "status": "pending"},
        tenant_id=LEGACY_OWNER_USER_ID,
        actor_id=LEGACY_OWNER_USER_ID,
        max_bytes=4 * 1024 * 1024,
    )
    with zipfile.ZipFile(io.BytesIO(stored.content)) as archive:
        assert archive.read("stdout.bin") == output
        assert json.loads(archive.read("MANIFEST.json"))["output_count"] == 0
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
