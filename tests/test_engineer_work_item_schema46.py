from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from friday.account_deletion import _mark_account_deletion_history_clean, preflight_account_deletion
from friday.interaction_control_plane.engineer_work_item import (
    EngineerWorkItemAnchorError,
    EngineerWorkItemChannel,
    EngineerWorkItemConflictError,
    EngineerWorkItemContractError,
    EngineerWorkItemState,
    bind_engineer_command_receipts_in_transaction,
    close_engineer_work_item_in_transaction,
    create_engineer_work_item_in_transaction,
    delete_engineer_work_item_in_transaction,
    engineer_job_receipt_sha256,
    engineer_source_binding_sha256,
    expire_due_engineer_work_items_in_transaction,
    get_current_engineer_work_item_in_transaction,
    get_engineer_work_item_in_transaction,
    mark_engineer_command_unknown_in_transaction,
    mark_engineer_work_item_ready_to_answer_in_transaction,
    prune_engineer_work_items_in_transaction,
    settle_engineer_terminal_receipt_in_transaction,
    start_next_engineer_step_in_transaction,
)
from friday.interaction_control_plane.engineer_work_item_schema import (
    ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256,
    ENGINEER_WORK_ITEM_SCHEMA,
    validate_engineer_work_item_schema,
)
from friday.organs.engineer.command.contracts import CommandLane, CommandOrigin, CommandRequest
from friday.organs.engineer.command.store import CommandJobStore
from friday.organs.engineer.command_tools import _idempotency_key as runtime_command_idempotency_key
from friday.storage import SCHEMA_VERSION, FridayStorage, UnsupportedSchemaVersionError

OWNER = "engineer-owner"
TENANT = "engineer-tenant"
NOW = "2026-08-27T10:00:00+00:00"
LATER = "2026-08-27T10:00:01+00:00"
EXPIRY = "2026-08-27T22:00:00+00:00"
SOURCE = "a" * 64
COMPLETION = ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256
COMMAND = "c" * 64
JOB_ID = "d" * 32
TERMINAL = "e" * 64


def _scope(storage: FridayStorage) -> tuple[str, dict[str, object]]:
    storage.ensure_user(OWNER)
    storage.ensure_user(TENANT)
    conversation = storage.create_conversation(OWNER, title="synthetic")
    conversation_id = str(conversation["id"])
    return conversation_id, {
        "owner_id": OWNER,
        "tenant_id": TENANT,
        "conversation_id": conversation_id,
        "channel": EngineerWorkItemChannel.TELEGRAM,
    }


def _create(storage: FridayStorage) -> tuple[dict[str, object], object]:
    _conversation_id, scope = _scope(storage)
    with storage.transaction() as conn:
        item = create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=SOURCE,
            completion_contract_sha256=COMPLETION,
            idempotency_key="ecmd-" + "1" * 64,
            command_digest=COMMAND,
            now=NOW,
            expires_at=EXPIRY,
        )
    return scope, item


def _settled(storage: FridayStorage) -> tuple[dict[str, object], object]:
    scope, item = _create(storage)
    with storage.transaction() as conn:
        admitted = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            command_digest=COMMAND,
            job_id=JOB_ID,
            now=NOW,
        )
        settled = settle_engineer_terminal_receipt_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=admitted.revision,
            verified_terminal_receipt_sha256=TERMINAL,
            now=LATER,
        )
    return scope, settled


def _unpack_schema_45(tmp_path: Path) -> Path:
    source = Path(__file__).parent / "fixtures" / "schemas" / "schema-45.sqlite3.gz"
    target = tmp_path / "schema-45.sqlite3"
    with gzip.open(source, "rb") as packed, target.open("wb") as raw:
        shutil.copyfileobj(packed, raw)
    return target


def test_schema_45_migrates_atomically_to_body_free_schema_46(settings, tmp_path) -> None:
    database = _unpack_schema_45(tmp_path)
    migrated = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        assert SCHEMA_VERSION == 46
        assert (
            migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "46"
        )
        validate_engineer_work_item_schema(migrated.conn)
        columns = {str(row[1]) for row in migrated.execute("PRAGMA table_info(engineer_work_items)")}
        forbidden = {
            "prompt",
            "goal",
            "plan",
            "reasoning",
            "argv",
            "command",
            "stdout",
            "stderr",
            "path",
            "filename",
            "message_body",
            "receipt_json",
        }
        assert columns.isdisjoint(forbidden)
        assert migrated.kv_get("fixture:marker") == "schema-45"
    finally:
        migrated.close()


def test_partial_schema_46_is_rejected_without_advancing_the_marker(settings, tmp_path) -> None:
    database = _unpack_schema_45(tmp_path)
    with sqlite3.connect(database) as forged:
        forged.execute("CREATE TABLE engineer_work_items(id TEXT PRIMARY KEY)")
        forged.commit()
    broken = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    with pytest.raises(sqlite3.DatabaseError, match="incomplete or altered"):
        broken.execute("SELECT 1").fetchone()
    broken.close()
    with sqlite3.connect(database) as probe:
        assert probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone() == ("45",)
        assert {row[1] for row in probe.execute("PRAGMA table_info(engineer_work_items)")} == {"id"}


def test_max_schema_45_binary_is_not_a_fallback_after_schema_46(settings, tmp_path, monkeypatch) -> None:
    database = tmp_path / "schema46.sqlite3"
    current = FridayStorage(replace(settings, database_path=database))
    current.ensure_user(OWNER)
    current.close()

    import friday.storage._core as storage_core

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        assert storage_core._required_database_has_friday_schema(probe) is True
    monkeypatch.setattr(storage_core, "SCHEMA_VERSION", 45)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        assert storage_core._required_database_has_friday_schema(probe) is False


def test_newer_or_tampered_schema_fails_closed(settings, tmp_path) -> None:
    database = tmp_path / "future.sqlite3"
    initial = FridayStorage(replace(settings, database_path=database))
    initial.ensure_user(OWNER)
    initial.close()
    with sqlite3.connect(database) as forged:
        forged.execute("UPDATE schema_meta SET value='47' WHERE key='schema_version'")
        forged.commit()
    future = FridayStorage(replace(settings, database_path=database, database_must_exist=False))
    with pytest.raises(UnsupportedSchemaVersionError):
        future.execute("SELECT 1").fetchone()
    future.close()
    with sqlite3.connect(database) as probe:
        assert probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone() == ("47",)


def test_closed_source_binding_never_persists_raw_carrier_values(storage) -> None:
    conversation_id, scope = _scope(storage)
    source_row_id = "private-inbox-row-777"
    update_id = "telegram-update-888"
    binding = engineer_source_binding_sha256(
        **scope,
        source_row_id=source_row_id,
        source_hash="f" * 64,
        telegram_update_id=update_id,
    )
    with storage.transaction() as conn:
        create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=binding,
            completion_contract_sha256=COMPLETION,
            idempotency_key="ecmd-" + "2" * 64,
            command_digest=COMMAND,
            now=NOW,
            expires_at=EXPIRY,
        )
    assert storage.execute("SELECT source_binding_sha256 FROM engineer_work_items").fetchone()[0] == binding
    database_blob = b"".join(
        path.read_bytes()
        for path in (
            Path(storage.settings.database_path),
            Path(str(storage.settings.database_path) + "-wal"),
        )
        if path.exists()
    )
    assert source_row_id.encode() not in database_blob
    assert update_id.encode() not in database_blob
    assert conversation_id.encode() in database_blob


def test_crud_cas_idempotency_unknown_reconcile_and_completion(storage) -> None:
    scope, created = _create(storage)
    assert created.state is EngineerWorkItemState.ACTIVE
    assert created.revision == 1 and created.step_ordinal == 1
    with storage.transaction() as conn:
        replay = create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=SOURCE,
            completion_contract_sha256=COMPLETION,
            idempotency_key=created.current_step.idempotency_key,
            command_digest=COMMAND,
            now=NOW,
            expires_at=EXPIRY,
        )
        assert replay.id == created.id
        with pytest.raises(EngineerWorkItemConflictError, match="idempotency key"):
            create_engineer_work_item_in_transaction(
                conn,
                **scope,
                source_binding_sha256=SOURCE,
                completion_contract_sha256=COMPLETION,
                idempotency_key=created.current_step.idempotency_key,
                command_digest=COMMAND,
                work_item_id="ewi_" + "f" * 32,
                now=NOW,
                expires_at=EXPIRY,
            )
        admitted = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=created.id,
            expected_revision=created.revision,
            command_digest=COMMAND,
            job_id=JOB_ID,
            now=NOW,
        )
        assert admitted.state is EngineerWorkItemState.WAITING_FOR_CAPABILITY
        assert (
            bind_engineer_command_receipts_in_transaction(
                conn,
                **scope,
                work_item_id=created.id,
                expected_revision=created.revision,
                command_digest=COMMAND,
                job_id=JOB_ID,
                now=NOW,
            )
            == admitted
        )
        unknown = mark_engineer_command_unknown_in_transaction(
            conn,
            **scope,
            work_item_id=created.id,
            expected_revision=admitted.revision,
            now=LATER,
        )
        assert unknown.state is EngineerWorkItemState.UNCERTAIN
        with pytest.raises(EngineerWorkItemConflictError, match="observed terminal"):
            start_next_engineer_step_in_transaction(
                conn,
                **scope,
                work_item_id=created.id,
                expected_revision=unknown.revision,
                idempotency_key="ecmd-" + "3" * 64,
                command_digest=COMMAND,
                now=LATER,
            )
        settled = settle_engineer_terminal_receipt_in_transaction(
            conn,
            **scope,
            work_item_id=created.id,
            expected_revision=unknown.revision,
            verified_terminal_receipt_sha256=TERMINAL,
            now=LATER,
        )
        assert settled.state is EngineerWorkItemState.WAITING_FOR_INPUT
        ready = mark_engineer_work_item_ready_to_answer_in_transaction(
            conn,
            **scope,
            work_item_id=created.id,
            expected_revision=settled.revision,
            now=LATER,
        )
        completed = close_engineer_work_item_in_transaction(
            conn,
            **scope,
            work_item_id=created.id,
            expected_revision=ready.revision,
            terminal_state=EngineerWorkItemState.COMPLETED,
            now=LATER,
        )
    assert completed.state is EngineerWorkItemState.COMPLETED
    assert completed.completed_at == completed.closed_at == LATER
    assert get_current_engineer_work_item_in_transaction(storage.conn, **scope) is None


def test_multistep_ledger_is_contiguous_and_idempotent(storage) -> None:
    scope, settled = _settled(storage)
    second_key = "ecmd-" + "4" * 64
    with storage.transaction() as conn:
        second = start_next_engineer_step_in_transaction(
            conn,
            **scope,
            work_item_id=settled.id,
            expected_revision=settled.revision,
            idempotency_key=second_key,
            command_digest=COMMAND,
            now=LATER,
        )
        assert second.step_ordinal == 2
        assert [step.ordinal for step in second.steps] == [1, 2]
        replay = start_next_engineer_step_in_transaction(
            conn,
            **scope,
            work_item_id=settled.id,
            expected_revision=settled.revision,
            idempotency_key=second_key,
            command_digest=COMMAND,
            now=LATER,
        )
        assert replay == second
        with pytest.raises(EngineerWorkItemConflictError):
            bind_engineer_command_receipts_in_transaction(
                conn,
                **scope,
                work_item_id=settled.id,
                expected_revision=settled.revision,
                command_digest=COMMAND,
                job_id=JOB_ID,
                now=LATER,
            )


def test_one_open_item_per_exact_scope_and_receipt_drift_fail_closed(storage) -> None:
    scope, item = _create(storage)
    with storage.transaction() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="step_deletion_immutable"):
            conn.execute(
                "DELETE FROM engineer_work_item_steps WHERE work_item_id=?",
                (item.id,),
            )
        with pytest.raises(EngineerWorkItemConflictError):
            create_engineer_work_item_in_transaction(
                conn,
                **scope,
                source_binding_sha256="9" * 64,
                completion_contract_sha256=COMPLETION,
                idempotency_key="ecmd-" + "5" * 64,
                command_digest=COMMAND,
                now=NOW,
                expires_at=EXPIRY,
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="step_(identity_immutable|transition_invalid)",
        ):
            conn.execute(
                "UPDATE engineer_work_item_steps SET command_digest=? WHERE work_item_id=?",
                ("8" * 64, item.id),
            )
        admitted = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            command_digest=COMMAND,
            job_id=JOB_ID,
            now=NOW,
        )
        with pytest.raises(EngineerWorkItemConflictError):
            bind_engineer_command_receipts_in_transaction(
                conn,
                **scope,
                work_item_id=item.id,
                expected_revision=item.revision,
                command_digest="8" * 64,
                job_id=JOB_ID,
                now=NOW,
            )
        with pytest.raises(EngineerWorkItemConflictError):
            settle_engineer_terminal_receipt_in_transaction(
                conn,
                **scope,
                work_item_id=item.id,
                expected_revision=item.revision,
                verified_terminal_receipt_sha256=TERMINAL,
                now=LATER,
            )
        assert admitted.current_step.terminal_receipt_sha256 == ""


def test_scope_requires_active_owner_tenant_and_unarchived_conversation(storage) -> None:
    conversation_id, scope = _scope(storage)
    storage.update_user(TENANT, status="disabled")
    with storage.transaction() as conn, pytest.raises(EngineerWorkItemAnchorError):
        create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=SOURCE,
            completion_contract_sha256=COMPLETION,
            idempotency_key="ecmd-" + "6" * 64,
            command_digest=COMMAND,
            now=NOW,
            expires_at=EXPIRY,
        )
    storage.update_user(TENANT, status="active")
    storage.archive_conversation(conversation_id, OWNER)
    with storage.transaction() as conn, pytest.raises(EngineerWorkItemAnchorError):
        create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=SOURCE,
            completion_contract_sha256=COMPLETION,
            idempotency_key="ecmd-" + "6" * 64,
            command_digest=COMMAND,
            now=NOW,
            expires_at=EXPIRY,
        )


def test_idempotency_key_namespace_is_exact_owner_scope(storage) -> None:
    shared_key = "ecmd-" + "7" * 64
    _first_conversation, first_scope = _scope(storage)
    with storage.transaction() as conn:
        first = create_engineer_work_item_in_transaction(
            conn,
            **first_scope,
            source_binding_sha256=SOURCE,
            completion_contract_sha256=COMPLETION,
            idempotency_key=shared_key,
            command_digest=COMMAND,
            now=NOW,
            expires_at=EXPIRY,
        )

    second_owner = "engineer-owner-two"
    second_tenant = "engineer-tenant-two"
    storage.ensure_user(second_owner)
    storage.ensure_user(second_tenant)
    second_conversation = storage.create_conversation(second_owner, title="synthetic-two")
    second_scope = {
        "owner_id": second_owner,
        "tenant_id": second_tenant,
        "conversation_id": str(second_conversation["id"]),
        "channel": EngineerWorkItemChannel.TELEGRAM,
    }
    with storage.transaction() as conn:
        second = create_engineer_work_item_in_transaction(
            conn,
            **second_scope,
            source_binding_sha256="8" * 64,
            completion_contract_sha256=COMPLETION,
            idempotency_key=shared_key,
            command_digest=COMMAND,
            now=NOW,
            expires_at=EXPIRY,
        )
    assert first.id != second.id
    assert first.current_step.owner_id == OWNER
    assert second.current_step.owner_id == second_owner


def test_restart_reader_preserves_unknown_without_replay(settings, tmp_path) -> None:
    database = tmp_path / "restart.sqlite3"
    first = FridayStorage(replace(settings, database_path=database))
    scope, item = _create(first)
    with first.transaction() as conn:
        admitted = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            command_digest=COMMAND,
            job_id=JOB_ID,
            now=NOW,
        )
        mark_engineer_command_unknown_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=admitted.revision,
            now=LATER,
        )
    first.close()

    reopened = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        current = get_current_engineer_work_item_in_transaction(reopened.conn, **scope)
        assert current is not None
        assert current.state is EngineerWorkItemState.UNCERTAIN
        assert current.current_step.state.value == "unknown"
        assert current.current_step.terminal_receipt_sha256 == ""
    finally:
        reopened.close()


def test_restart_reconciles_actual_runtime_key_and_digest_without_argv(settings, tmp_path) -> None:
    database = tmp_path / "main.sqlite3"
    command_root = tmp_path / "command-kernel"
    source_message_id = "msg-" + "a" * 32
    runtime_step_id = "ecstep-" + "b" * 32
    private_command = "printf schema46-private-command"
    preliminary = CommandRequest(
        lane=CommandLane.SHELL,
        origin=CommandOrigin.MODEL,
        shell_command=private_command,
        idempotency_key="pending",
    )
    actual_key = runtime_command_idempotency_key(
        source_message_id,
        runtime_step_id,
        preliminary,
    )
    request = replace(preliminary, idempotency_key=actual_key)
    assert request.digest == preliminary.digest

    first = FridayStorage(replace(settings, database_path=database))
    conversation_id, scope = _scope(first)
    with first.transaction() as conn:
        item = create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=SOURCE,
            completion_contract_sha256=COMPLETION,
            idempotency_key=actual_key,
            command_digest=request.digest,
            now=NOW,
            expires_at=EXPIRY,
        )
        assert item.current_step.command_digest == request.digest
        assert item.current_step.job_receipt_sha256 == ""

    kernel_store = CommandJobStore(command_root)
    job_id = "1" * 32
    kernel_store.insert_job(
        {
            "job_id": job_id,
            "actor_id": OWNER,
            "tenant_id": TENANT,
            "conversation_id": conversation_id,
            "channel": "telegram",
            "source_row_id": source_message_id,
            "source_hash": SOURCE,
            "telegram_update_id": "4242",
            "isolation_profile": "host_user",
            "host_user_authorized": True,
            "idempotency_key": actual_key,
            "command_digest": request.digest,
            "input_manifest_sha256": "",
            "argv_sha256": "f" * 64,
            "lane": "shell",
            "origin": "model",
            "status": "admitted",
            "error_code": "",
            "grant_nonce": "schema46-test-nonce",
            "timeout_sec": 300,
            "max_stdout_bytes": 1_024,
            "max_stderr_bytes": 1_024,
            "created_at": 1_777_000_000.0,
            "executable_json": None,
        }
    )
    lookup = kernel_store.lookup_idempotency(OWNER, actual_key)
    assert lookup == {"job_id": job_id, "digest": request.digest, "delivery_chat_id": ""}
    first.close()
    kernel_store.close()

    # Crash window: the command kernel admitted work but the main-DB item still
    # says prepared.  Expiry and conversation retirement must preserve its exact
    # key+digest until a healthy command-ledger lookup reconciles it.
    reopened = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    reopened_kernel = CommandJobStore(command_root)
    with reopened.transaction() as conn:
        assert expire_due_engineer_work_items_in_transaction(conn, now=EXPIRY) == 0
    retired = reopened.delete_conversation(conversation_id, OWNER)
    assert "engineer_work_items" not in retired["cancelled"]
    durable = get_engineer_work_item_in_transaction(
        reopened.conn,
        **scope,
        work_item_id=item.id,
    )
    assert durable is not None
    assert durable.state is EngineerWorkItemState.ACTIVE
    recovered = reopened_kernel.lookup_idempotency(
        durable.owner_id,
        durable.current_step.idempotency_key,
    )
    assert recovered is not None
    assert recovered["digest"] == durable.current_step.command_digest
    assert recovered["job_id"] == job_id
    with reopened.transaction() as conn:
        admitted = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=durable.revision,
            command_digest=request.digest,
            job_id=job_id,
            now="2026-08-28T00:00:00+00:00",
        )
    assert admitted.current_step.idempotency_key == actual_key
    assert admitted.current_step.job_receipt_sha256 == engineer_job_receipt_sha256(
        **scope,
        idempotency_key=actual_key,
        command_digest=request.digest,
        job_id=job_id,
    )
    try:
        durable = get_engineer_work_item_in_transaction(
            reopened.conn,
            **scope,
            work_item_id=item.id,
        )
        assert durable is not None
        recovered = reopened_kernel.lookup_idempotency(
            durable.owner_id,
            durable.current_step.idempotency_key,
        )
        assert recovered is not None
        assert recovered["digest"] == durable.current_step.command_digest
        assert recovered["job_id"] == job_id
        assert private_command.encode() not in database.read_bytes()
        assert job_id.encode() not in database.read_bytes()
    finally:
        reopened_kernel.close()
        reopened.close()


def test_expiry_retention_and_delete_preserve_effect_bearing_work(storage) -> None:
    scope, item = _create(storage)
    with storage.transaction() as conn:
        admitted = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            command_digest=COMMAND,
            job_id=JOB_ID,
            now=NOW,
        )
        unknown = mark_engineer_command_unknown_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=admitted.revision,
            now=LATER,
        )
        assert expire_due_engineer_work_items_in_transaction(conn, now=EXPIRY) == 0
        with pytest.raises(EngineerWorkItemConflictError, match="cannot be deleted"):
            delete_engineer_work_item_in_transaction(
                conn,
                **scope,
                work_item_id=item.id,
                expected_revision=unknown.revision,
            )
        assert prune_engineer_work_items_in_transaction(conn, before="2026-08-28T00:00:00+00:00") == 0


def test_effect_free_expiry_delete_and_terminal_prune_are_bounded(storage) -> None:
    scope, item = _settled(storage)
    with storage.transaction() as conn:
        assert expire_due_engineer_work_items_in_transaction(conn, now=EXPIRY, limit=1) == 1
        expired = get_engineer_work_item_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
        )
        assert expired is not None and expired.state is EngineerWorkItemState.EXPIRED
        assert delete_engineer_work_item_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=expired.revision,
        )

        replacement = create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256="9" * 64,
            completion_contract_sha256=COMPLETION,
            idempotency_key="ecmd-" + "9" * 64,
            command_digest=COMMAND,
            now=NOW,
            expires_at=EXPIRY,
        )
        admitted = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=replacement.id,
            expected_revision=replacement.revision,
            command_digest=COMMAND,
            job_id=JOB_ID,
            now=NOW,
        )
        observed = settle_engineer_terminal_receipt_in_transaction(
            conn,
            **scope,
            work_item_id=replacement.id,
            expected_revision=admitted.revision,
            verified_terminal_receipt_sha256=TERMINAL,
            now=LATER,
        )
        cancelled = close_engineer_work_item_in_transaction(
            conn,
            **scope,
            work_item_id=replacement.id,
            expected_revision=observed.revision,
            terminal_state=EngineerWorkItemState.CANCELLED,
            now=LATER,
        )
        assert cancelled.state is EngineerWorkItemState.CANCELLED
        assert (
            prune_engineer_work_items_in_transaction(
                conn,
                before="2026-08-27T10:00:02+00:00",
                limit=1,
            )
            == 1
        )
    assert storage.execute("SELECT COUNT(*) FROM engineer_work_items").fetchone()[0] == 0


def test_conversation_archive_cancels_settled_work_but_preserves_unknown(storage) -> None:
    scope, item = _settled(storage)
    result = storage.delete_conversation(str(scope["conversation_id"]), OWNER)
    assert result["cancelled"]["engineer_work_items"] == 1
    cancelled = get_engineer_work_item_in_transaction(
        storage.conn,
        **scope,
        work_item_id=item.id,
    )
    assert cancelled is not None and cancelled.state is EngineerWorkItemState.CANCELLED


def test_conversation_archive_never_calls_unknown_work_cancelled(storage) -> None:
    scope, item = _create(storage)
    with storage.transaction() as conn:
        admitted = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            command_digest=COMMAND,
            job_id=JOB_ID,
            now=NOW,
        )
        unknown = mark_engineer_command_unknown_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=admitted.revision,
            now=LATER,
        )
    report = storage.delete_conversation(str(scope["conversation_id"]), OWNER)
    assert report["cancelled"].get("engineer_work_items", 0) == 0
    preserved = get_engineer_work_item_in_transaction(
        storage.conn,
        **scope,
        work_item_id=item.id,
    )
    assert preserved == unknown


def test_backup_export_and_account_preflight_are_schema46_closed(storage, tmp_path) -> None:
    scope, settled = _settled(storage)
    with storage.transaction() as conn:
        ready = mark_engineer_work_item_ready_to_answer_in_transaction(
            conn,
            **scope,
            work_item_id=settled.id,
            expected_revision=settled.revision,
            now=LATER,
        )
        completed = close_engineer_work_item_in_transaction(
            conn,
            **scope,
            work_item_id=settled.id,
            expected_revision=ready.revision,
            terminal_state=EngineerWorkItemState.COMPLETED,
            now=LATER,
        )
    backup = storage.create_backup(label="engineer-work-item")
    assert backup["schema_version"] == 46
    assert storage.verify_backup(str(backup["database"]))["ok"] is True

    exported = storage.export_user(OWNER)
    payload = json.loads(Path(str(exported["path"])).read_text(encoding="utf-8"))
    assert len(payload["engineer_work_items"]) == 1
    projected = payload["engineer_work_items"][0]
    assert projected["id"] == completed.id
    serialized = json.dumps(projected, ensure_ascii=False).casefold()
    for forbidden in ("stdout", "stderr", "argv", "shell_command", "workbench", "file_path"):
        assert forbidden not in serialized

    storage.execute("UPDATE users SET status='disabled' WHERE id=?", (OWNER,))
    report = preflight_account_deletion(storage, OWNER, quiescence_available=True)
    assert report["counts"]["engineer_work_items"] == 1
    assert report["counts"]["engineer_work_item_steps"] == 1
    assert "engineer_work_items.owner_id" not in report["unknown_scopes"]
    assert "engineer_work_items.tenant_id" not in report["unknown_scopes"]


def test_verify_backup_rejects_tampered_engineer_subschema(storage) -> None:
    backup = storage.create_backup(label="engineer-schema-tamper")
    database = Path(str(backup["path"]))
    manifest_path = Path(str(backup["manifest_path"]))
    with sqlite3.connect(database) as forged:
        forged.execute("DROP TRIGGER trg_engineer_work_item_step_delete_guard")
        forged.commit()
    blob = database.read_bytes()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["size_bytes"] = len(blob)
    manifest["sha256"] = hashlib.sha256(blob).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    verification = storage.verify_backup(database.name)
    assert verification["ok"] is False
    assert "Engineer Work Item DDL is incomplete or altered" in str(verification["database_error"])
    assert verification["hash_matches_manifest"] is True


def test_account_purge_inventory_is_closed_and_keeps_neighbour(storage) -> None:
    target = "local:engineer-delete-target"
    neighbour = "local:engineer-delete-neighbour"
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    assert _mark_account_deletion_history_clean(storage, target)
    conversation = storage.create_conversation(target, title="deletion target")
    scope = {
        "owner_id": target,
        "tenant_id": target,
        "conversation_id": str(conversation["id"]),
        "channel": EngineerWorkItemChannel.TELEGRAM,
    }
    with storage.transaction() as conn:
        item = create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=SOURCE,
            completion_contract_sha256=COMPLETION,
            idempotency_key="ecmd-" + "a" * 64,
            command_digest=COMMAND,
            now=NOW,
            expires_at=EXPIRY,
        )
        admitted = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            command_digest=COMMAND,
            job_id=JOB_ID,
            now=NOW,
        )
        observed = settle_engineer_terminal_receipt_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=admitted.revision,
            verified_terminal_receipt_sha256=TERMINAL,
            now=LATER,
        )
        close_engineer_work_item_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=observed.revision,
            terminal_state=EngineerWorkItemState.CANCELLED,
            now=LATER,
        )
    storage.update_user(target, status="disabled")
    plan = preflight_account_deletion(storage, target, quiescence_available=True)
    assert plan["ready"] is False
    assert plan["counts"]["engineer_work_item_steps"] == 1
    assert plan["counts"]["engineer_work_items"] == 1
    assert plan["unknown_scopes"] == []
    assert {blocker["code"] for blocker in plan["blockers"]} == {"chat_history"}
    assert storage.get_user(target) is not None
    assert storage.get_user(neighbour) is not None


def test_row_validator_rejects_cross_owner_step_tamper(storage) -> None:
    _scope_data, _item = _create(storage)
    storage.execute("DROP TRIGGER trg_engineer_work_item_step_identity_immutable")
    storage.execute("DROP TRIGGER trg_engineer_work_item_step_transition_guard")
    storage.execute("UPDATE engineer_work_item_steps SET owner_id=?", (TENANT,))
    storage.conn.executescript(ENGINEER_WORK_ITEM_SCHEMA)
    with pytest.raises(sqlite3.DatabaseError, match="rows are inconsistent"):
        validate_engineer_work_item_schema(storage.conn)


def test_row_validator_rejects_earlier_unresolved_step(storage) -> None:
    scope, settled = _settled(storage)
    with storage.transaction() as conn:
        start_next_engineer_step_in_transaction(
            conn,
            **scope,
            work_item_id=settled.id,
            expected_revision=settled.revision,
            idempotency_key="ecmd-" + "f" * 64,
            command_digest="f" * 64,
            now=LATER,
        )
    storage.execute("DROP TRIGGER trg_engineer_work_item_step_identity_immutable")
    storage.execute("DROP TRIGGER trg_engineer_work_item_step_transition_guard")
    storage.execute(
        """UPDATE engineer_work_item_steps
              SET state='unknown',terminal_receipt_sha256='',settled_at=NULL
            WHERE work_item_id=? AND ordinal=1""",
        (settled.id,),
    )
    storage.conn.executescript(ENGINEER_WORK_ITEM_SCHEMA)
    with pytest.raises(sqlite3.DatabaseError, match="rows are inconsistent"):
        validate_engineer_work_item_schema(storage.conn)


def test_two_writers_cannot_bind_different_job_receipts(settings, tmp_path) -> None:
    database = tmp_path / "race.sqlite3"
    initial = FridayStorage(replace(settings, database_path=database))
    scope, item = _create(initial)
    initial.close()

    barrier = threading.Barrier(2)
    lock = threading.Lock()
    successes: list[str] = []
    conflicts: list[str] = []

    def writer(job_id: str) -> None:
        local = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
        try:
            barrier.wait(timeout=5)
            try:
                with local.transaction() as conn:
                    admitted = bind_engineer_command_receipts_in_transaction(
                        conn,
                        **scope,
                        work_item_id=item.id,
                        expected_revision=item.revision,
                        command_digest=COMMAND,
                        job_id=job_id,
                        now=NOW,
                    )
                with lock:
                    successes.append(admitted.current_step.job_receipt_sha256)
            except EngineerWorkItemConflictError:
                with lock:
                    conflicts.append(job_id)
        finally:
            local.close()

    competing_job_ids = (JOB_ID, "8" * 32)
    expected_receipts = {
        engineer_job_receipt_sha256(
            **scope,
            idempotency_key=item.current_step.idempotency_key,
            command_digest=COMMAND,
            job_id=job_id,
        )
        for job_id in competing_job_ids
    }
    threads = [threading.Thread(target=writer, args=(job_id,), daemon=True) for job_id in competing_job_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert len(successes) == len(conflicts) == 1
    assert successes[0] in expected_receipts

    reopened = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        durable = get_engineer_work_item_in_transaction(
            reopened.conn,
            **scope,
            work_item_id=item.id,
        )
        assert durable is not None
        assert durable.revision == 2
        assert durable.current_step.command_digest == COMMAND
        assert durable.current_step.job_receipt_sha256 == successes[0]
    finally:
        reopened.close()


def test_synthetic_non_kernel_idempotency_key_is_rejected(storage) -> None:
    _conversation_id, scope = _scope(storage)
    with storage.transaction() as conn, pytest.raises(ValueError, match="opaque v1 key"):
        create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=SOURCE,
            completion_contract_sha256=COMPLETION,
            idempotency_key="ewik_" + "1" * 32,
            command_digest=COMMAND,
            now=NOW,
            expires_at=EXPIRY,
        )


def test_closed_contract_rejects_bad_digest_ttl_revision_and_channel(storage) -> None:
    _conversation_id, scope = _scope(storage)
    with storage.transaction() as conn:
        with pytest.raises(EngineerWorkItemContractError, match="lowercase SHA-256"):
            create_engineer_work_item_in_transaction(
                conn,
                **scope,
                source_binding_sha256="A" * 64,
                completion_contract_sha256=COMPLETION,
                idempotency_key="ecmd-" + "b" * 64,
                command_digest=COMMAND,
                now=NOW,
                expires_at=EXPIRY,
            )
        with pytest.raises(EngineerWorkItemContractError, match="TTL"):
            create_engineer_work_item_in_transaction(
                conn,
                **scope,
                source_binding_sha256=SOURCE,
                completion_contract_sha256=COMPLETION,
                idempotency_key="ecmd-" + "b" * 64,
                command_digest=COMMAND,
                now=NOW,
                expires_at="2026-08-27T22:00:01+00:00",
            )
        with pytest.raises(EngineerWorkItemContractError, match="completion contract"):
            create_engineer_work_item_in_transaction(
                conn,
                **scope,
                source_binding_sha256=SOURCE,
                completion_contract_sha256="b" * 64,
                idempotency_key="ecmd-" + "b" * 64,
                command_digest=COMMAND,
                now=NOW,
                expires_at=EXPIRY,
            )
        with pytest.raises(EngineerWorkItemAnchorError, match="channel"):
            create_engineer_work_item_in_transaction(
                conn,
                **{**scope, "channel": "telegram"},
                source_binding_sha256=SOURCE,
                completion_contract_sha256=COMPLETION,
                idempotency_key="ecmd-" + "b" * 64,
                command_digest=COMMAND,
                now=NOW,
                expires_at=EXPIRY,
            )
        item = create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=SOURCE,
            completion_contract_sha256=COMPLETION,
            idempotency_key="ecmd-" + "b" * 64,
            command_digest=COMMAND,
            now=NOW,
            expires_at=EXPIRY,
        )
        with pytest.raises(EngineerWorkItemContractError, match="expected_revision"):
            bind_engineer_command_receipts_in_transaction(
                conn,
                **scope,
                work_item_id=item.id,
                expected_revision=True,
                command_digest=COMMAND,
                job_id=JOB_ID,
                now=NOW,
            )


def test_expired_observed_result_cannot_pass_completion_gate(storage) -> None:
    scope, settled = _settled(storage)
    with storage.transaction() as conn:
        with pytest.raises(EngineerWorkItemConflictError, match="expired"):
            mark_engineer_work_item_ready_to_answer_in_transaction(
                conn,
                **scope,
                work_item_id=settled.id,
                expected_revision=settled.revision,
                now=EXPIRY,
            )
        assert expire_due_engineer_work_items_in_transaction(conn, now=EXPIRY) == 1
