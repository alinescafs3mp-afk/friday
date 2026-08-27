from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from friday.organs.engineer import EngineerOrgan
from friday.organs.engineer.command import (
    CommandError,
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
from friday.organs.engineer.command.kernel import _terminal_receipt_fields
from friday.organs.engineer.command.store import CommandJobStore, atomic_write
from friday.organs.engineer.command.workspace import JobWorkspace
from friday.organs.engineer.command_tools import EngineerCommandService
from friday.organs.engineer.terminal_delivery import (
    TERMINAL_TEXT_NOTIFICATION_KIND,
    parse_terminal_envelope,
)
from friday.permissions import LEGACY_OWNER_USER_ID, AuthorizationService

_JOB_ID = "3" * 32
_CHAT_ID = "5001"


def _authority() -> CommandGrantAuthority:
    return CommandGrantAuthority(
        b"g" * 32,
        OwnerSourceAuthority(b"s" * 32),
        OwnerConfirmationAuthority(b"c" * 32),
    )


def _scope(storage) -> tuple[str, str]:
    storage.ensure_user(
        LEGACY_OWNER_USER_ID,
        source="api-token",
        preset_key="owner",
        metadata={"chat_id": _CHAT_ID},
    )
    storage.link_identity(
        "telegram",
        _CHAT_ID,
        LEGACY_OWNER_USER_ID,
        linked_by=LEGACY_OWNER_USER_ID,
    )
    conversation = storage.create_conversation(LEGACY_OWNER_USER_ID, "Retention")
    source = storage.store_message(
        str(conversation["id"]),
        LEGACY_OWNER_USER_ID,
        "user",
        "Собери результат",
        metadata={"telegram_update_id": "100"},
    )
    return str(conversation["id"]), str(source["id"])


def _service(
    storage,
    tmp_path: Path,
    *,
    generated_output: bool = True,
) -> tuple[EngineerCommandService, CommandReceipt]:
    conversation_id, source_message_id = _scope(storage)
    kernel = CommandKernel(tmp_path / "commands", _authority())
    workspace = JobWorkspace(kernel.store.job_dir(_JOB_ID))
    workspace.materialize()
    stdout = b"compiled\n"
    stderr = b"warning\n"
    output = b"binary-result"
    atomic_write(workspace.stdout_path, stdout)
    atomic_write(workspace.stderr_path, stderr)
    generated = GeneratedFile(
        relative_path="result.bin",
        size_bytes=len(output),
        sha256=sha256_bytes(output),
        mode=0o600,
    )
    if generated_output:
        atomic_write(workspace.sealed / generated.relative_path, output, mode=0o400)
    unsigned = CommandReceipt(
        job_id=_JOB_ID,
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
        stderr_sha256=sha256_bytes(stderr),
        stdout=stdout,
        stderr=stderr,
        generated_files=(generated,) if generated_output else (),
        error_code="",
        effect_boundary_crossed=True,
        receipt_mac="",
    )
    receipt = replace(
        unsigned,
        receipt_mac=kernel.authority.sign_receipt(unsigned.to_public_payload()),
    )
    with kernel.store.transaction():
        kernel.store.insert_job(
            {
                "job_id": _JOB_ID,
                "actor_id": LEGACY_OWNER_USER_ID,
                "tenant_id": LEGACY_OWNER_USER_ID,
                "conversation_id": conversation_id,
                "channel": "telegram",
                "source_row_id": source_message_id,
                "source_hash": receipt.source_hash,
                "telegram_update_id": "100",
                "isolation_profile": receipt.isolation_profile.value,
                "host_user_authorized": False,
                "idempotency_key": "retention-idempotency",
                "command_digest": receipt.command_digest,
                "argv_sha256": receipt.argv_sha256,
                "lane": receipt.lane.value,
                "origin": receipt.origin.value,
                "status": receipt.status.value,
                "grant_nonce": "retention-nonce",
                "timeout_sec": 30,
                "max_stdout_bytes": 1024,
                "max_stderr_bytes": 1024,
                "created_at": 10.0,
                "executable_json": None,
                "delivery_chat_id": _CHAT_ID,
            }
        )
        terminal_fields = _terminal_receipt_fields(receipt, cleanup_pending=False)
        terminal_fields["started_at"] = receipt.started_at
        kernel.store.update_job(_JOB_ID, terminal_fields)
    authorization = AuthorizationService(storage)
    for capability in EngineerOrgan().capabilities():
        authorization.register_capability(capability)
    service = EngineerCommandService.__new__(EngineerCommandService)
    service.kernel = kernel
    service.storage = storage
    service.settings = SimpleNamespace(
        engineer_mode_enabled=True,
        engineer_command_enabled=True,
        telegram_effective_allowed_chat_ids={int(_CHAT_ID)},
        telegram_open_registration=False,
    )
    service.authorization = authorization
    service.files_root = tmp_path / "files"
    service.max_upload_bytes = 4 * 1024 * 1024
    service._archive_lock = threading.Lock()
    service._archive_cache = None
    service._publication_lock = threading.Lock()
    service._retention_lock = threading.Lock()
    return service, receipt


def _age_sent_publication(service: EngineerCommandService, *, now: float) -> dict[str, object]:
    assert service.publish_terminal_jobs() == {"staged": 1, "reconciled": 0, "failed": 0}
    queued = service.storage.list_pending_notifications()[0]
    service.storage.mark_notifications(sent_ids=[queued["id"]])
    assert service.publish_terminal_jobs()["reconciled"] == 1
    old = now - 31 * 24 * 60 * 60
    sent_at = datetime.fromtimestamp(old, UTC).isoformat(timespec="seconds")
    with service.storage.transaction() as conn:
        conn.execute(
            "UPDATE outbound_notifications SET sent_at=? WHERE id=?",
            (sent_at, queued["id"]),
        )
    with service.kernel.store.transaction():
        service.kernel.store._conn.execute(  # noqa: SLF001 - exact private-ledger fixture
            "UPDATE command_job_publications SET updated_at=? WHERE job_id=?",
            (old, _JOB_ID),
        )
    row = service.storage.execute(
        "SELECT id,user_id,chat_id,kind,dedup_key,body,status,sent_at FROM outbound_notifications WHERE id=?",
        (queued["id"],),
    ).fetchone()
    assert row is not None
    return dict(row)


def test_retention_removes_only_workspace_and_exact_sent_carrier(storage, tmp_path: Path) -> None:
    service, receipt = _service(storage, tmp_path)
    now = time.time()
    carrier = _age_sent_publication(service, now=now)
    envelope = parse_terminal_envelope(carrier["body"])
    raw_id = str(envelope["artifact"]["raw_id"])
    assistant_id = str(envelope["assistant_message_id"])
    with service.kernel._lock:  # noqa: SLF001 - cache-retention invariant
        service.kernel._cache_receipt_locked(_JOB_ID, receipt)  # noqa: SLF001
    assert _JOB_ID in service.kernel._receipts  # noqa: SLF001 - cache-retention invariant

    assert service.retain_terminal_jobs(now=now) == {"retired": 1, "failed": 0, "ephemera": 0}
    job = service.kernel.store.read_job(_JOB_ID)
    assert job["workspace_retired_at"] is not None
    assert job["stdout_bytes"] == len(receipt.stdout)
    assert job["stderr_bytes"] == len(receipt.stderr)
    assert not service.kernel.store.job_dir(_JOB_ID).exists()
    assert _JOB_ID not in service.kernel._receipts  # noqa: SLF001
    publication = service.kernel.store._conn.execute(  # noqa: SLF001
        "SELECT state,carrier_retired_at FROM command_job_publications WHERE job_id=?",
        (_JOB_ID,),
    ).fetchone()
    assert publication is not None
    assert publication["state"] == "sent" and publication["carrier_retired_at"] is not None
    assert service.kernel.store.lookup_idempotency(
        LEGACY_OWNER_USER_ID,
        "retention-idempotency",
    ) == {
        "job_id": _JOB_ID,
        "digest": receipt.command_digest,
        "delivery_chat_id": _CHAT_ID,
    }
    assert (
        storage.execute(
            "SELECT 1 FROM outbound_notifications WHERE id=?",
            (carrier["id"],),
        ).fetchone()
        is None
    )
    assert storage.execute("SELECT 1 FROM raw_objects WHERE id=?", (raw_id,)).fetchone() is not None
    assert storage.get_message(assistant_id, LEGACY_OWNER_USER_ID) is not None
    progress = service.kernel.progress(
        _JOB_ID,
        actor_id=LEGACY_OWNER_USER_ID,
        conversation_id=str(envelope["conversation_id"]),
    )
    assert progress.status is CommandStatus.COMPLETED
    assert (progress.stdout_bytes, progress.stderr_bytes) == (
        len(receipt.stdout),
        len(receipt.stderr),
    )
    restored, version = service.kernel.terminal_receipt(
        _JOB_ID,
        actor_id=LEGACY_OWNER_USER_ID,
        conversation_id=str(envelope["conversation_id"]),
    )
    assert version == 2 and restored.status is CommandStatus.COMPLETED
    assert restored.stdout == restored.stderr == b""
    with pytest.raises(CommandError, match="job_output_retired"):
        service.kernel.terminal_result(
            _JOB_ID,
            actor_id=LEGACY_OWNER_USER_ID,
            conversation_id=str(envelope["conversation_id"]),
        )
    actor = service.authorization.actor_for_user(
        LEGACY_OWNER_USER_ID,
        source="retention-test",
        identity_id=_CHAT_ID,
    )
    status = service.status(
        actor=actor,
        job_id=_JOB_ID,
        _conversation_id=str(envelope["conversation_id"]),
    )
    assert status["ok"] is True and status["status"] == "completed"
    assert status["output_retired"] is True
    assert status["artifact_delivery"] == {
        "available": False,
        "error_code": "job_output_retired",
    }
    with service.kernel._lock:  # noqa: SLF001 - bounded-cache invariant
        for index in range(80):
            service.kernel._cache_receipt_locked(str(index), receipt)  # noqa: SLF001
        assert len(service.kernel._receipts) == 64  # noqa: SLF001
    service.kernel.close()


def test_retention_uses_historical_artifact_size_not_current_upload_limit(
    storage,
    tmp_path: Path,
) -> None:
    service, _receipt = _service(storage, tmp_path)
    now = time.time()
    _age_sent_publication(service, now=now)
    service.max_upload_bytes = 1
    assert service.retain_terminal_jobs(now=now) == {"retired": 1, "failed": 0, "ephemera": 0}
    assert not service.kernel.store.job_dir(_JOB_ID).exists()
    service.kernel.close()


def test_text_delivered_no_file_job_retires_workspace_without_an_archive(storage, tmp_path: Path) -> None:
    service, receipt = _service(storage, tmp_path, generated_output=False)
    now = time.time()
    old = now - 31 * 24 * 60 * 60

    assert service.publish_terminal_jobs() == {"staged": 1, "reconciled": 0, "failed": 0}
    queued = storage.list_pending_notifications()
    assert len(queued) == 1 and queued[0]["kind"] == TERMINAL_TEXT_NOTIFICATION_KIND
    storage.mark_notifications(sent_ids=[queued[0]["id"]])
    assert storage.list_pending_notifications() == []
    with service.kernel.store.transaction():
        service.kernel.store._conn.execute(  # noqa: SLF001 - exact aging fixture
            "UPDATE command_job_publications SET updated_at=? WHERE job_id=?",
            (old, _JOB_ID),
        )

    assert service.retain_terminal_jobs(now=now) == {"retired": 1, "failed": 0, "ephemera": 0}
    assert not service.kernel.store.job_dir(_JOB_ID).exists()
    job = service.kernel.store.read_job(_JOB_ID)
    assert job["workspace_retired_at"] is not None
    assert (job["stdout_bytes"], job["stderr_bytes"]) == (
        len(receipt.stdout),
        len(receipt.stderr),
    )
    publication = service.kernel.store._conn.execute(  # noqa: SLF001
        "SELECT state,last_error_code,carrier_retired_at FROM command_job_publications WHERE job_id=?",
        (_JOB_ID,),
    ).fetchone()
    assert publication is not None
    assert dict(publication) == {
        "carrier_retired_at": now,
        "last_error_code": "no_generated_files",
        "state": "blocked",
    }
    assert storage.list_pending_notifications() == []
    service.kernel.close()


def test_receipt_reader_converges_when_retention_marker_wins_race(
    storage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, receipt = _service(storage, tmp_path)

    def retired_during_read(_workspace, _name, **_kwargs):
        with service.kernel.store.transaction():
            service.kernel.store.update_job(
                _JOB_ID,
                {
                    "workspace_retired_at": time.time(),
                    "stdout_bytes": len(receipt.stdout),
                    "stderr_bytes": len(receipt.stderr),
                },
            )
        raise CommandError("workspace_unreadable")

    monkeypatch.setattr(JobWorkspace, "read_evidence_verified", retired_during_read)
    restored, version = service.kernel.terminal_receipt(
        _JOB_ID,
        actor_id=LEGACY_OWNER_USER_ID,
        conversation_id=str(service.kernel.store.read_job(_JOB_ID)["conversation_id"]),
    )
    assert version == 2
    assert restored.status is CommandStatus.COMPLETED
    assert restored.stdout == restored.stderr == b""
    assert service.kernel.store.read_job(_JOB_ID)["error_code"] == ""
    service.kernel.close()


def test_status_reports_retired_when_marker_wins_archive_race(
    storage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, receipt = _service(storage, tmp_path)
    job = service.kernel.store.read_job(_JOB_ID)
    conversation_id = str(job["conversation_id"])
    actor = service.authorization.actor_for_user(
        LEGACY_OWNER_USER_ID,
        source="retention-race-test",
        identity_id=_CHAT_ID,
    )

    def retired_before_archive(*_args, **_kwargs):
        with service.kernel.store.transaction():
            service.kernel.store.update_job(
                _JOB_ID,
                {
                    "workspace_retired_at": time.time(),
                    "stdout_bytes": len(receipt.stdout),
                    "stderr_bytes": len(receipt.stderr),
                },
            )
        raise CommandError("job_output_retired")

    monkeypatch.setattr(service, "_archive_for_receipt", retired_before_archive)
    result = service.status(
        actor=actor,
        job_id=_JOB_ID,
        _conversation_id=conversation_id,
    )
    assert result["ok"] is True
    assert result["output_retired"] is True
    assert result["artifact_delivery"] == {
        "available": False,
        "error_code": "job_output_retired",
    }
    assert result["stdout"] == result["stderr"] == ""
    assert "_attachment" not in result
    assert service.kernel.store.read_job(_JOB_ID)["status"] == CommandStatus.COMPLETED.value
    service.kernel.close()


def test_retention_fails_closed_on_sent_body_drift(storage, tmp_path: Path) -> None:
    service, _receipt = _service(storage, tmp_path)
    now = time.time()
    carrier = _age_sent_publication(service, now=now)
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE outbound_notifications SET body='{}' WHERE id=?",
            (carrier["id"],),
        )
    assert service.retain_terminal_jobs(now=now) == {"retired": 0, "failed": 1, "ephemera": 0}
    assert service.kernel.store.read_job(_JOB_ID)["workspace_retired_at"] is None
    assert service.kernel.store.job_dir(_JOB_ID).is_dir()
    assert (
        storage.execute(
            "SELECT 1 FROM outbound_notifications WHERE id=?",
            (carrier["id"],),
        ).fetchone()
        is not None
    )
    service.kernel.close()


def test_retention_resumes_after_marker_before_workspace_delete(
    storage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _receipt = _service(storage, tmp_path)
    now = time.time()
    carrier = _age_sent_publication(service, now=now)
    original = service.kernel.retire_workspace

    def interrupted(_job_id: str) -> None:
        raise CommandError("injected_retirement_failure")

    monkeypatch.setattr(service.kernel, "retire_workspace", interrupted)
    assert service.retain_terminal_jobs(now=now) == {"retired": 0, "failed": 1, "ephemera": 0}
    assert service.kernel.store.read_job(_JOB_ID)["workspace_retired_at"] is not None
    assert service.kernel.store.job_dir(_JOB_ID).is_dir()
    assert (
        storage.execute(
            "SELECT 1 FROM outbound_notifications WHERE id=?",
            (carrier["id"],),
        ).fetchone()
        is not None
    )

    monkeypatch.setattr(service.kernel, "retire_workspace", original)
    assert service.retain_terminal_jobs(now=now + 301.0) == {
        "retired": 1,
        "failed": 0,
        "ephemera": 0,
    }
    assert not service.kernel.store.job_dir(_JOB_ID).exists()
    assert (
        storage.execute(
            "SELECT 1 FROM outbound_notifications WHERE id=?",
            (carrier["id"],),
        ).fetchone()
        is None
    )
    service.kernel.close()


def test_workspace_retirement_never_follows_symlink(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_bytes(b"keep")
    job = jobs / _JOB_ID
    job.mkdir()
    (job / "escape").symlink_to(external, target_is_directory=True)
    workspace = JobWorkspace(job)
    with pytest.raises(CommandError, match="workspace_retirement_refused"):
        workspace.retire()
    assert sentinel.read_bytes() == b"keep"


def test_workspace_retirement_refuses_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    job = jobs / _JOB_ID
    job.mkdir()
    (job / "original").write_bytes(b"original")
    replacement = jobs / "replacement"
    replacement.mkdir()
    (replacement / "sentinel").write_bytes(b"keep")
    original_rename = os.rename

    def swapped_rename(source, target, *, src_dir_fd=None, dst_dir_fd=None):
        original_rename(source, "parked", src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        original_rename(
            "replacement",
            source,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        original_rename(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr("friday.organs.engineer.command.workspace.os.rename", swapped_rename)
    with pytest.raises(CommandError, match="workspace_retirement_changed"):
        JobWorkspace(job).retire()
    assert (jobs / f".retired-{_JOB_ID}" / "sentinel").read_bytes() == b"keep"
    assert (jobs / "parked" / "original").read_bytes() == b"original"


def test_retention_candidates_are_strict_and_hard_bounded(tmp_path: Path) -> None:
    store = CommandJobStore(tmp_path / "store")

    def insert(index: int, *, status: str, publication: str, updated_at: float) -> str:
        job_id = f"{index:032x}"
        with store.transaction():
            store.insert_job(
                {
                    "job_id": job_id,
                    "actor_id": f"actor-{index}",
                    "tenant_id": "tenant",
                    "conversation_id": "conversation",
                    "channel": "telegram",
                    "source_row_id": f"source-{index}",
                    "source_hash": "6" * 64,
                    "telegram_update_id": str(index),
                    "isolation_profile": IsolationProfile.ISOLATED_WORKSPACE.value,
                    "host_user_authorized": False,
                    "idempotency_key": f"key-{index}",
                    "command_digest": "4" * 64,
                    "argv_sha256": "5" * 64,
                    "lane": CommandLane.ARGV.value,
                    "origin": CommandOrigin.OWNER_TURN.value,
                    "status": status,
                    "grant_nonce": f"nonce-{index}",
                    "timeout_sec": 30,
                    "max_stdout_bytes": 1024,
                    "max_stderr_bytes": 1024,
                    "created_at": 1.0,
                    "executable_json": None,
                    "delivery_chat_id": str(index + 1),
                }
            )
            store._conn.execute(  # noqa: SLF001 - exact eligibility fixture
                "UPDATE command_job_publications SET state=?,updated_at=? WHERE job_id=?",
                (publication, updated_at, job_id),
            )
        return job_id

    eligible = {
        insert(index, status="completed", publication="sent", updated_at=1.0) for index in range(1, 26)
    }
    insert(30, status="completed", publication="pending", updated_at=1.0)
    insert(31, status="unknown", publication="sent", updated_at=1.0)
    insert(32, status="completed", publication="sent", updated_at=3.0)
    candidates = store.list_workspace_retention_candidates(cutoff=2.0, now=2.0, limit=1000)
    assert len(candidates) == 20
    assert {str(candidate["job_id"]) for candidate in candidates} <= eligible
    for candidate in candidates:
        with store.transaction():
            store.update_job(
                str(candidate["job_id"]),
                {"workspace_retired_at": 2.0},
            )
        store.record_workspace_retention_failure(
            str(candidate["job_id"]),
            error_code="terminal_artifact_changed",
            failed_at=2.0,
        )
    remainder = store.list_workspace_retention_candidates(cutoff=2.0, now=3.0, limit=1000)
    assert len(remainder) == 5
    assert {str(candidate["job_id"]) for candidate in remainder} == eligible - {
        str(candidate["job_id"]) for candidate in candidates
    }
    store.close()


def test_private_store_migrates_retention_columns_and_prunes_only_ephemera(
    tmp_path: Path,
) -> None:
    store = CommandJobStore(tmp_path / "store")
    with store.transaction():
        store._conn.execute(  # noqa: SLF001 - private migration fixture
            "INSERT INTO confirmation_source_ledger(source_key,handle) VALUES('source','handle')"
        )
        store._conn.execute(  # noqa: SLF001
            "INSERT INTO confirmation_events(handle,payload_json,mac,exp,consumed) "
            "VALUES('handle','{}','mac',1,0)"
        )
        store._conn.execute(  # noqa: SLF001
            "INSERT INTO grant_nonces(nonce,kind,exp) VALUES('nonce','used',1)"
        )
    db_path = store.db_path
    store.close()
    legacy = sqlite3.connect(str(db_path), isolation_level=None)
    for column in ("stdout_bytes", "stderr_bytes", "workspace_retired_at"):
        legacy.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
    for column in (
        "carrier_retired_at",
        "retention_attempts",
        "retention_next_attempt_at",
        "retention_error_code",
    ):
        legacy.execute(f"ALTER TABLE command_job_publications DROP COLUMN {column}")
    for column in (
        "stage_attempts",
        "stage_next_attempt_at",
        "stage_error_code",
        "retire_attempts",
        "retire_next_attempt_at",
        "retire_error_code",
    ):
        legacy.execute(f"ALTER TABLE command_job_progress DROP COLUMN {column}")
    legacy.close()
    store = CommandJobStore(tmp_path / "store")
    assert store.prune_expired_ephemera(now=2) == 2
    assert (
        store._conn.execute(  # noqa: SLF001
            "SELECT 1 FROM confirmation_source_ledger WHERE source_key='source'"
        ).fetchone()
        is not None
    )
    columns = {row[1] for row in store._conn.execute("PRAGMA table_info(jobs)")}  # noqa: SLF001
    assert {"stdout_bytes", "stderr_bytes", "workspace_retired_at"} <= columns
    publication_columns = {
        row[1]
        for row in store._conn.execute(  # noqa: SLF001
            "PRAGMA table_info(command_job_publications)"
        )
    }
    assert {
        "carrier_retired_at",
        "retention_attempts",
        "retention_next_attempt_at",
        "retention_error_code",
    } <= publication_columns
    progress_columns = {
        row[1]
        for row in store._conn.execute(  # noqa: SLF001
            "PRAGMA table_info(command_job_progress)"
        )
    }
    assert {
        "stage_attempts",
        "stage_next_attempt_at",
        "stage_error_code",
        "retire_attempts",
        "retire_next_attempt_at",
        "retire_error_code",
    } <= progress_columns
    store.close()
