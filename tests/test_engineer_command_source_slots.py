"""Shared immutable source authority for command jobs and work-item fences."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from friday.engineer_source_binding import (
    canonical_engineer_source_binding_sha256,
    legacy_engineer_source_binding_sha256,
)
from friday.organs.engineer.command.contracts import CommandError, sha256_bytes
from friday.organs.engineer.command.store import CommandJobStore

ACTOR = "owner"
_KEY = b"engineer-source-slot-test-key!!!"


def _id(value: str) -> str:
    return "ecmd-" + sha256_bytes(value.encode("ascii"))


def _job(
    *,
    key: str,
    job_id: str,
    source_step_id: str = "ecstep-" + "1" * 32,
) -> dict[str, object]:
    source = {
        "owner_id": ACTOR,
        "tenant_id": "tenant",
        "conversation_id": "conversation",
        "channel": "telegram",
        "source_row_id": "message-row",
        "source_hash": "3" * 64,
        "telegram_update_id": "telegram-update",
        "delivery_chat_id": "123",
    }
    binding = (
        canonical_engineer_source_binding_sha256(
            **source,
            source_step_id=source_step_id,
        )
        if source_step_id
        else legacy_engineer_source_binding_sha256(**source)
    )
    return {
        "job_id": job_id,
        "actor_id": ACTOR,
        "tenant_id": source["tenant_id"],
        "conversation_id": source["conversation_id"],
        "channel": source["channel"],
        "source_row_id": source["source_row_id"],
        "source_step_id": source_step_id,
        "source_binding_sha256": binding,
        "source_hash": source["source_hash"],
        "telegram_update_id": source["telegram_update_id"],
        "isolation_profile": "host_user",
        "host_user_authorized": True,
        "idempotency_key": key,
        "command_digest": sha256_bytes(("command:" + key).encode("ascii")),
        "input_manifest_sha256": "",
        "argv_sha256": "4" * 64,
        "lane": "argv",
        "origin": "model",
        "status": "admitted",
        "grant_nonce": "grant-" + job_id,
        "timeout_sec": 30,
        "max_stdout_bytes": 1024,
        "max_stderr_bytes": 1024,
        "created_at": time.time(),
        "executable_json": "{}",
        "delivery_chat_id": source["delivery_chat_id"],
    }


def _legacy_binding() -> str:
    return legacy_engineer_source_binding_sha256(
        owner_id=ACTOR,
        tenant_id="tenant",
        conversation_id="conversation",
        channel="telegram",
        source_row_id="message-row",
        source_hash="3" * 64,
        telegram_update_id="telegram-update",
        delivery_chat_id="123",
    )


def test_job_claim_is_atomic_and_source_and_key_are_permanent(tmp_path: Path) -> None:
    store = CommandJobStore(tmp_path / "store")
    first = _job(key=_id("first"), job_id="1" * 32)
    try:
        with store.transaction():
            store.insert_job(first)
        by_source = store.lookup_engineer_command_source_slot(
            ACTOR,
            str(first["source_binding_sha256"]),
            legacy_source_binding_sha256=_legacy_binding(),
        )
        assert by_source == store.lookup_engineer_command_source_slot_by_key(
            ACTOR,
            str(first["idempotency_key"]),
        )
        assert by_source is not None
        assert by_source["target_kind"] == "job"
        assert by_source["job_id"] == first["job_id"]
        assert by_source["legacy_source_binding_sha256"] is None

        same_source = _job(key=_id("rekey"), job_id="2" * 32)
        with pytest.raises(CommandError, match="engineer_command_source_slot_conflict"), store.transaction():
            store.insert_job(same_source)

        same_key = _job(
            key=str(first["idempotency_key"]),
            job_id="3" * 32,
            source_step_id="ecstep-" + "2" * 32,
        )
        with pytest.raises(CommandError, match="engineer_command_source_slot_conflict"), store.transaction():
            store.insert_job(same_key)
        assert store.lookup_idempotency(ACTOR, _id("rekey")) is None
    finally:
        store.close()


def test_job_and_fence_are_mutually_exclusive_by_source_and_key(tmp_path: Path) -> None:
    fenced_store = CommandJobStore(tmp_path / "fenced")
    candidate = _job(key=_id("candidate"), job_id="4" * 32)
    try:
        fenced_store.create_engineer_work_item_fence(
            actor_id=ACTOR,
            idempotency_key=_id("fence"),
            work_item_id="ewi_" + "5" * 32,
            expected_revision=1,
            step_ordinal=1,
            source_binding_sha256=str(candidate["source_binding_sha256"]),
            legacy_source_binding_sha256=_legacy_binding(),
            command_digest="6" * 64,
        )
        with pytest.raises(CommandError, match="idempotency_fenced"), fenced_store.transaction():
            fenced_store.insert_job(candidate)
    finally:
        fenced_store.close()

    admitted_store = CommandJobStore(tmp_path / "admitted")
    admitted = _job(key=_id("admitted"), job_id="7" * 32)
    try:
        with admitted_store.transaction():
            admitted_store.insert_job(admitted)
        with pytest.raises(CommandError, match="idempotency_fence_conflict"):
            admitted_store.create_engineer_work_item_fence(
                actor_id=ACTOR,
                idempotency_key=_id("other-key"),
                work_item_id="ewi_" + "8" * 32,
                expected_revision=1,
                step_ordinal=1,
                source_binding_sha256=str(admitted["source_binding_sha256"]),
                legacy_source_binding_sha256=_legacy_binding(),
                command_digest="9" * 64,
            )
        with pytest.raises(CommandError, match="idempotency_conflict"):
            admitted_store.create_engineer_work_item_fence(
                actor_id=ACTOR,
                idempotency_key=str(admitted["idempotency_key"]),
                work_item_id="ewi_" + "a" * 32,
                expected_revision=1,
                step_ordinal=1,
                source_binding_sha256="b" * 64,
                legacy_source_binding_sha256="c" * 64,
                command_digest="d" * 64,
            )
    finally:
        admitted_store.close()


def test_raw_source_or_target_mutation_fails_closed(tmp_path: Path) -> None:
    store = CommandJobStore(tmp_path / "store")
    payload = _job(key=_id("raw"), job_id="a" * 32)
    try:
        with store.transaction():
            store.insert_job(payload)
        with (
            pytest.raises(sqlite3.IntegrityError, match="engineer_command_source_slot_unauthorized"),
            store.transaction() as conn,
        ):
            conn.execute(
                """INSERT INTO engineer_command_source_slots(
                       actor_id,source_binding_sha256,idempotency_key,command_digest,
                       target_kind,job_id,created_at)
                   VALUES(?,?,?,?, 'job',?,?)""",
                (ACTOR, "b" * 64, _id("rogue"), "c" * 64, "d" * 32, 123.5),
            )

        for statement in (
            "UPDATE engineer_command_source_slots SET command_digest=command_digest",
            "DELETE FROM engineer_command_source_slots",
            "INSERT OR REPLACE INTO engineer_command_source_slots SELECT * FROM engineer_command_source_slots",
        ):
            with pytest.raises(sqlite3.IntegrityError), store.transaction() as conn:
                conn.execute(statement)

        with (
            pytest.raises(sqlite3.IntegrityError, match="engineer_command_source_slot_missing"),
            store.transaction() as conn,
        ):
            conn.execute(
                "CREATE TEMP TABLE rogue_job AS SELECT * FROM jobs WHERE job_id=?",
                (payload["job_id"],),
            )
            conn.execute(
                """UPDATE rogue_job SET job_id=?,idempotency_key=?,source_step_id=?,
                       source_binding_sha256=?,command_digest=?""",
                ("e" * 32, _id("rogue-job"), "ecstep-" + "f" * 32, "1" * 64, "2" * 64),
            )
            conn.execute("INSERT INTO jobs SELECT * FROM rogue_job")
    finally:
        store.close()


def test_legacy_blank_source_job_conservatively_blocks_v2_rekey(tmp_path: Path) -> None:
    store = CommandJobStore(tmp_path / "store")
    legacy = _job(key=_id("legacy"), job_id="3" * 32, source_step_id="")
    try:
        with store.transaction():
            store.insert_job(legacy)
        legacy_slot = store.lookup_engineer_command_source_slot(
            ACTOR,
            _legacy_binding(),
        )
        assert legacy_slot is not None
        assert legacy_slot["legacy_source_binding_sha256"] == _legacy_binding()

        rekey = _job(key=_id("v2-rekey"), job_id="4" * 32)
        with pytest.raises(CommandError, match="engineer_command_source_slot_conflict"), store.transaction():
            store.insert_job(rekey)
        with pytest.raises(CommandError, match="idempotency_fence_conflict"):
            store.create_engineer_work_item_fence(
                actor_id=ACTOR,
                idempotency_key=_id("v2-fence-rekey"),
                work_item_id="ewi_" + "5" * 32,
                expected_revision=1,
                step_ordinal=1,
                source_binding_sha256=str(rekey["source_binding_sha256"]),
                legacy_source_binding_sha256=_legacy_binding(),
                command_digest="6" * 64,
            )
    finally:
        store.close()


def test_runtime_requires_exact_source_slot_schema_and_legacy_fence_lookup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    state = tmp_path / "non-restored-anchor"
    provisioned = CommandJobStore.provision(
        root,
        lifecycle_key=_KEY,
        lifecycle_state_dir=state,
    )
    provisioned.close()
    runtime = CommandJobStore.open_runtime(
        root,
        lifecycle_key=_KEY,
        lifecycle_state_dir=state,
    )
    try:
        with pytest.raises(CommandError, match="idempotency_fence_source_invalid"):
            runtime.create_engineer_work_item_fence(
                actor_id=ACTOR,
                idempotency_key=_id("strict-fence"),
                work_item_id="ewi_" + "7" * 32,
                expected_revision=1,
                step_ordinal=1,
                source_binding_sha256="8" * 64,
                command_digest="9" * 64,
            )
    finally:
        runtime.close()

    with sqlite3.connect(root / "kernel.sqlite") as connection:
        connection.execute("DROP TRIGGER trg_engineer_command_source_slot_insert_authority")
    with pytest.raises(CommandError, match="engineer_command_source_slot_schema_invalid"):
        CommandJobStore.open_runtime(
            root,
            lifecycle_key=_KEY,
            lifecycle_state_dir=state,
        )
