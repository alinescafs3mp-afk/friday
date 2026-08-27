from __future__ import annotations

import json
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
    CommandGrantAuthority,
    CommandKernel,
    CommandLane,
    CommandOrigin,
    CommandStatus,
    IsolationProfile,
    OwnerConfirmationAuthority,
    OwnerSourceAuthority,
)
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
from friday.organs.engineer.command.store import CommandJobStore, atomic_write
from friday.organs.engineer.command.workspace import JobWorkspace
from friday.organs.engineer.command_tools import EngineerCommandService
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


def _running_service(
    storage: Any,
    tmp_path: Path,
    *,
    started_at: float,
) -> tuple[EngineerCommandService, str, AuthorizationService]:
    conversation_id = _scope(storage)
    authority = CommandGrantAuthority(
        b"g" * 32,
        OwnerSourceAuthority(b"s" * 32),
        OwnerConfirmationAuthority(b"c" * 32),
    )
    kernel = CommandKernel(tmp_path / "commands", authority)
    workspace = JobWorkspace(kernel.store.job_dir("1" * 32))
    workspace.materialize()
    atomic_write(workspace.stdout_path, b"working\n")
    atomic_write(workspace.stderr_path, b"")
    with kernel.store.transaction():
        kernel.store.insert_job(
            {
                "job_id": "1" * 32,
                "actor_id": LEGACY_OWNER_USER_ID,
                "tenant_id": LEGACY_OWNER_USER_ID,
                "conversation_id": conversation_id,
                "channel": "telegram",
                "source_row_id": "msg_" + "2" * 16,
                "source_hash": "3" * 64,
                "telegram_update_id": "100",
                "isolation_profile": IsolationProfile.ISOLATED_WORKSPACE.value,
                "host_user_authorized": False,
                "idempotency_key": "progress-worker-test",
                "command_digest": "4" * 64,
                "argv_sha256": "5" * 64,
                "lane": CommandLane.ARGV.value,
                "origin": CommandOrigin.OWNER_TURN.value,
                "status": CommandStatus.RUNNING.value,
                "grant_nonce": "progress-nonce",
                "timeout_sec": 3600,
                "max_stdout_bytes": 1024,
                "max_stderr_bytes": 1024,
                "created_at": started_at - 1.0,
                "executable_json": None,
                "delivery_chat_id": "5001",
            }
        )
        kernel.store.update_job("1" * 32, {"started_at": started_at})
    with kernel._lock:  # noqa: SLF001 - faithful in-memory RUNNING fixture
        kernel._live["1" * 32] = SimpleNamespace(  # noqa: SLF001
            started_at=started_at,
            stdout_bytes=8,
            stderr_bytes=0,
            output_activity=True,
            isolation=IsolationProfile.ISOLATED_WORKSPACE,
        )
    authorization = _authority(storage)
    service = EngineerCommandService.__new__(EngineerCommandService)
    service.kernel = kernel
    service.storage = storage
    service.settings = SimpleNamespace(
        engineer_mode_enabled=True,
        engineer_command_enabled=True,
        telegram_effective_allowed_chat_ids={5001},
        telegram_open_registration=False,
    )
    service.authorization = authorization
    service._progress_lock = threading.Lock()
    return service, conversation_id, authorization


def _close_running_service(service: EngineerCommandService) -> None:
    with service.kernel._lock:  # noqa: SLF001 - fixture teardown
        service.kernel._live.pop("1" * 32, None)  # noqa: SLF001
    service.kernel.close()


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
            f"⏳ Engineer-задача `{'1' * 32}` выполняется 1 мин 0 с. "
            "Этап: выполняется команда. Получено вывода: stdout 17 Б, stderr 3 Б. "
            "Жёсткий тайм-аут не задан."
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

    request = _request(storage, enabled, authorization)
    legacy = await notifications_pending(request, limit=20)
    assert "status_update" not in legacy["items"][0]
    pending = await notifications_pending(request, limit=20, status_messages=True)
    assert pending["count"] == 1
    assert pending["items"] == [
        {
            "id": staged.notification_id,
            "chat_id": "5001",
            "body": (
                f"⏳ Engineer-задача `{'1' * 32}` выполняется 1 мин 0 с. "
                "Этап: выполняется команда. Получено вывода: stdout 17 Б, stderr 3 Б. "
                "Жёсткий тайм-аут не задан."
            ),
                "kind": PROGRESS_NOTIFICATION_KIND,
                "dedup_key": staged.dedup_key,
                "status_update": {
                    "schema": "friday.telegram-status.v1",
                    "operation_id": f"engineer:{'1' * 32}",
                    "revision": 60,
                    "terminal": False,
                    "stage": "command_running",
                    "elapsed_sec": 60,
                    "timeout_sec": 0,
                    "remaining_sec": None,
                    "stdout_bytes": 17,
                    "stderr_bytes": 3,
                    "output_activity": True,
                },
            }
    ]


def test_progress_reports_elapsed_and_deadline_without_inventing_eta(storage) -> None:
    conversation_id = _scope(storage)
    staged = stage_progress_notification(
        storage,
        actor_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        delivery_chat_id="5001",
        job_id="1" * 32,
        checkpoint_sec=60,
        stdout_bytes=22,
        stderr_bytes=0,
        output_activity=True,
        elapsed_sec=75,
        timeout_sec=300,
    )
    row = storage.execute(
        "SELECT id,user_id,chat_id,kind,dedup_key,body,status FROM outbound_notifications WHERE id=?",
        (staged.notification_id,),
    ).fetchone()
    assert row is not None
    projection = progress_notification_projection(
        storage,
        dict(row),
        tenant_id=LEGACY_OWNER_USER_ID,
        actor_id=LEGACY_OWNER_USER_ID,
    )
    assert "выполняется 1 мин 15 с" in projection["body"]
    assert "До заданного тайм-аута: около 3 мин 45 с" in projection["body"]
    assert "готовност" not in projection["body"].lower()
    assert "eta" not in projection["body"].lower()


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


def test_progress_worker_emits_only_highest_newly_due_checkpoint(
    storage,
    tmp_path: Path,
) -> None:
    service, _conversation_id, authorization = _running_service(
        storage,
        tmp_path,
        started_at=1_000.0,
    )
    authorization.deny_permission(LEGACY_OWNER_USER_ID, "files.read")
    assert service.publish_progress_jobs(now=2_000.0) == {
        "staged": 1,
        "retired": 0,
        "failed": 0,
    }
    assert service.publish_progress_jobs(now=2_000.0) == {
        "staged": 0,
        "retired": 0,
        "failed": 0,
    }
    assert service.publish_progress_jobs(now=2_800.0) == {
        "staged": 1,
        "retired": 0,
        "failed": 0,
    }
    rows = storage.execute(
        "SELECT body FROM outbound_notifications WHERE kind=? ORDER BY created_at",
        (PROGRESS_NOTIFICATION_KIND,),
    ).fetchall()
    envelopes = [parse_progress_envelope(row["body"]) for row in rows]
    assert [item["checkpoint_sec"] for item in envelopes] == [900, 1800]
    assert all(item["stdout_bytes"] == 8 and item["output_activity"] is True for item in envelopes)
    state = service.kernel.store._conn.execute(  # noqa: SLF001 - exact checkpoint assertion
        "SELECT checkpoint_sec,retired_at FROM command_job_progress WHERE job_id=?",
        ("1" * 32,),
    ).fetchone()
    assert state is not None and dict(state) == {"checkpoint_sec": 1800, "retired_at": None}
    _close_running_service(service)


def test_progress_worker_recovers_enqueue_before_private_cas(
    storage,
    tmp_path: Path,
) -> None:
    service, conversation_id, _authorization = _running_service(
        storage,
        tmp_path,
        started_at=1_000.0,
    )
    frozen = _stage(
        storage,
        conversation_id,
        checkpoint_sec=60,
        stdout_bytes=1,
        stderr_bytes=2,
    )
    assert service.publish_progress_jobs(now=1_061.0) == {
        "staged": 1,
        "retired": 0,
        "failed": 0,
    }
    row = storage.execute(
        "SELECT body FROM outbound_notifications WHERE id=?",
        (frozen.notification_id,),
    ).fetchone()
    assert row is not None
    envelope = parse_progress_envelope(row["body"])
    assert (envelope["stdout_bytes"], envelope["stderr_bytes"]) == (1, 2)
    state = service.kernel.store._conn.execute(  # noqa: SLF001 - crash-window assertion
        "SELECT checkpoint_sec FROM command_job_progress WHERE job_id=?",
        ("1" * 32,),
    ).fetchone()
    assert state is not None and state["checkpoint_sec"] == 60
    _close_running_service(service)


def test_nonrunning_progress_is_retired_once_after_restart(storage, tmp_path: Path) -> None:
    service, _conversation_id, _authorization = _running_service(
        storage,
        tmp_path,
        started_at=1_000.0,
    )
    assert service.publish_progress_jobs(now=1_061.0)["staged"] == 1
    with service.kernel.store.transaction():
        service.kernel.store.update_job("1" * 32, {"status": CommandStatus.UNKNOWN.value})
    assert service.publish_progress_jobs(now=1_062.0) == {
        "staged": 0,
        "retired": 1,
        "failed": 0,
    }
    assert service.publish_progress_jobs(now=1_063.0) == {
        "staged": 0,
        "retired": 0,
        "failed": 0,
    }
    row = storage.execute(
        "SELECT status,dedup_key FROM outbound_notifications WHERE kind=?",
        (PROGRESS_NOTIFICATION_KIND,),
    ).fetchone()
    assert row is not None
    assert row["status"] == "failed" and row["dedup_key"].endswith(":60")
    marker = service.kernel.store._conn.execute(  # noqa: SLF001 - restart marker assertion
        "SELECT retired_at FROM command_job_progress WHERE job_id=?",
        ("1" * 32,),
    ).fetchone()
    assert marker is not None and marker["retired_at"] is not None
    _close_running_service(service)


def test_progress_candidate_batches_cannot_be_starved(tmp_path: Path) -> None:
    store = CommandJobStore(tmp_path / "fair-progress")
    job_ids: list[str] = []
    for index in range(1, 26):
        job_id = f"{index:032x}"
        job_ids.append(job_id)
        with store.transaction():
            store.insert_job(
                {
                    "job_id": job_id,
                    "actor_id": f"actor-{index}",
                    "tenant_id": "tenant",
                    "conversation_id": f"conversation-{index}",
                    "channel": "telegram",
                    "source_row_id": f"source-{index}",
                    "source_hash": "3" * 64,
                    "telegram_update_id": str(index),
                    "isolation_profile": IsolationProfile.ISOLATED_WORKSPACE.value,
                    "host_user_authorized": False,
                    "idempotency_key": f"progress-fair-{index}",
                    "command_digest": "4" * 64,
                    "argv_sha256": "5" * 64,
                    "lane": CommandLane.ARGV.value,
                    "origin": CommandOrigin.OWNER_TURN.value,
                    "status": CommandStatus.RUNNING.value,
                    "grant_nonce": f"nonce-{index}",
                    "timeout_sec": 3600,
                    "max_stdout_bytes": 1024,
                    "max_stderr_bytes": 1024,
                    "created_at": 1.0,
                    "executable_json": None,
                    "delivery_chat_id": str(index + 1),
                }
            )
            store.update_job(job_id, {"started_at": 1.0})
            if index <= 20:
                store._conn.execute(  # noqa: SLF001 - due-window fixture
                    "UPDATE command_job_progress SET checkpoint_sec=60 WHERE job_id=?",
                    (job_id,),
                )

    due_tail = store.list_progress_publication_candidates(now=100.0, limit=20)
    assert [str(row["job_id"]) for row in due_tail] == job_ids[20:]

    with store.transaction():
        store._conn.execute(  # noqa: SLF001 - poison-head fixture
            "UPDATE command_job_progress SET checkpoint_sec=0"
        )
    poison_stage = store.list_progress_publication_candidates(now=100.0, limit=20)
    assert [str(row["job_id"]) for row in poison_stage] == job_ids[:20]
    for row in poison_stage:
        store.record_progress_publication_failure(
            str(row["job_id"]),
            error_code="authorization_denied",
            failed_at=100.0,
        )
    stage_tail = store.list_progress_publication_candidates(now=101.0, limit=20)
    assert [str(row["job_id"]) for row in stage_tail] == job_ids[20:]

    with store.transaction():
        store._conn.execute("UPDATE jobs SET status='unknown'")  # noqa: SLF001
    poison_retire = store.list_progress_retirement_candidates(now=100.0, limit=20)
    assert [str(row["job_id"]) for row in poison_retire] == job_ids[:20]
    for row in poison_retire:
        store.record_progress_retirement_failure(
            str(row["job_id"]),
            error_code="progress_dedup_conflict",
            failed_at=100.0,
        )
    retire_tail = store.list_progress_retirement_candidates(now=101.0, limit=20)
    assert [str(row["job_id"]) for row in retire_tail] == job_ids[20:]
    store.close()


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
