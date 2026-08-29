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
from fastapi.testclient import TestClient

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
    discard_unsubmitted_engineer_work_item_in_transaction,
    engineer_job_receipt_sha256,
    engineer_source_binding_sha256,
    expire_due_engineer_work_items_in_transaction,
    get_current_engineer_work_item_in_transaction,
    get_engineer_work_item_in_transaction,
    mark_engineer_command_unknown_in_transaction,
    mark_engineer_work_item_ready_to_answer_in_transaction,
    prune_engineer_work_items_in_transaction,
    rollback_fenced_unsubmitted_engineer_step_in_transaction,
    settle_engineer_terminal_receipt_in_transaction,
    start_next_engineer_step_in_transaction,
)
from friday.interaction_control_plane.engineer_work_item_schema import (
    ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256,
    ENGINEER_WORK_ITEM_SCHEMA,
    register_engineer_work_item_connection_functions,
    validate_engineer_work_item_schema,
)
from friday.organs.engineer.command.contracts import CommandLane, CommandOrigin, CommandRequest
from friday.organs.engineer.command.store import CommandJobStore
from friday.organs.engineer.command_tools import _idempotency_key as runtime_command_idempotency_key
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage import SCHEMA_VERSION, FridayStorage, UnsupportedSchemaVersionError

OWNER = "engineer-owner"
TENANT = "engineer-tenant"
NOW = "2026-08-27T10:00:00+00:00"
LATER = "2026-08-27T10:00:01+00:00"
EXPIRY = "2026-08-27T22:00:00+00:00"
SOURCE = "a" * 64
SOURCE_ROW_ID = "msg_engineer_source"
SOURCE_STEP_ID = "ecstep-" + "1" * 32
TELEGRAM_UPDATE_ID = "4242"
DELIVERY_CHAT_ID = "123456789"
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


def _source_binding(
    scope: dict[str, object],
    *,
    source_row_id: str = SOURCE_ROW_ID,
    source_step_id: str = SOURCE_STEP_ID,
    source_hash: str = SOURCE,
    telegram_update_id: str = TELEGRAM_UPDATE_ID,
) -> str:
    return engineer_source_binding_sha256(
        **scope,
        source_row_id=source_row_id,
        source_step_id=source_step_id,
        source_hash=source_hash,
        telegram_update_id=telegram_update_id,
        delivery_chat_id=DELIVERY_CHAT_ID,
    )


def _ledger_binding(
    scope: dict[str, object],
    item: object,
    *,
    command_digest: str = COMMAND,
    job_id: str = JOB_ID,
    source_row_id: str = SOURCE_ROW_ID,
    source_step_id: str = SOURCE_STEP_ID,
    source_hash: str = SOURCE,
    telegram_update_id: str = TELEGRAM_UPDATE_ID,
) -> dict[str, str]:
    return {
        "job_id": job_id,
        "actor_id": str(scope["owner_id"]),
        "tenant_id": str(scope["tenant_id"]),
        "conversation_id": str(scope["conversation_id"]),
        "channel": str(scope["channel"].value),
        "source_row_id": source_row_id,
        "source_step_id": source_step_id,
        "source_hash": source_hash,
        "telegram_update_id": telegram_update_id,
        "idempotency_key": str(item.current_step.idempotency_key),
        "command_digest": command_digest,
        "delivery_chat_id": DELIVERY_CHAT_ID,
    }


def _fence_binding(item: object) -> dict[str, object]:
    return {
        "actor_id": str(item.owner_id),
        "work_item_id": str(item.id),
        "expected_revision": int(item.revision),
        "step_ordinal": int(item.step_ordinal),
        "source_binding_sha256": str(item.current_step.source_binding_sha256),
        "idempotency_key": str(item.current_step.idempotency_key),
        "command_digest": str(item.current_step.command_digest),
    }


def _create(storage: FridayStorage) -> tuple[dict[str, object], object]:
    _conversation_id, scope = _scope(storage)
    with storage.transaction() as conn:
        item = create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=_source_binding(scope),
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
            ledger_binding=_ledger_binding(scope, item),
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


def _route_archive_state(
    storage: FridayStorage,
    *,
    target: str,
) -> tuple[dict[str, object], object]:
    owner_id = LEGACY_OWNER_USER_ID
    storage.ensure_user(owner_id, source="api-token", preset_key="owner")
    conversation = storage.create_conversation(owner_id, title=f"archive-{target}")
    scope: dict[str, object] = {
        "owner_id": owner_id,
        "tenant_id": owner_id,
        "conversation_id": str(conversation["id"]),
        "channel": EngineerWorkItemChannel.TELEGRAM,
    }
    token = hashlib.sha256(f"{target}:{conversation['id']}".encode()).hexdigest()
    with storage.transaction() as conn:
        item = create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=_source_binding(scope),
            completion_contract_sha256=COMPLETION,
            idempotency_key="ecmd-" + token,
            command_digest=COMMAND,
            now=NOW,
            expires_at=EXPIRY,
        )
        if target == "prepared":
            return scope, item
        item = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            ledger_binding=_ledger_binding(scope, item, job_id=token[:32]),
            now=NOW,
        )
        if target == "admitted":
            return scope, item
        if target == "unknown":
            item = mark_engineer_command_unknown_in_transaction(
                conn,
                **scope,
                work_item_id=item.id,
                expected_revision=item.revision,
                now=LATER,
            )
            return scope, item
        item = settle_engineer_terminal_receipt_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            verified_terminal_receipt_sha256=TERMINAL,
            now=LATER,
        )
        if target == "settled":
            return scope, item
        item = mark_engineer_work_item_ready_to_answer_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            now=LATER,
        )
    if target != "ready":
        raise AssertionError(f"unsupported route archive state: {target}")
    return scope, item


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
        assert SCHEMA_VERSION == 47
        assert (
            migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "47"
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


def test_integer_authority_columns_reject_real_values() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        register_engineer_work_item_connection_functions(conn)
        conn.executescript(
            """CREATE TABLE users(id TEXT PRIMARY KEY,status TEXT NOT NULL);
               CREATE TABLE conversations(
                   id TEXT PRIMARY KEY,user_id TEXT NOT NULL,is_archived INTEGER NOT NULL
               );
               CREATE TABLE work_items(
                   id TEXT PRIMARY KEY,user_id TEXT,conversation_id TEXT,state TEXT
               );
               CREATE TABLE work_item_compare_current_file_web_graphs(
                   id TEXT PRIMARY KEY,user_id TEXT,conversation_id TEXT,state TEXT
               );"""
        )
        conn.executescript(ENGINEER_WORK_ITEM_SCHEMA)
        conn.executemany(
            "INSERT INTO users(id,status) VALUES(?,'active')",
            ((OWNER,), (TENANT,)),
        )
        conversation_id = "conv_0123456789abcdef"
        conn.execute(
            "INSERT INTO conversations(id,user_id,is_archived) VALUES(?,?,0)",
            (conversation_id, OWNER),
        )
        parent_sql = """INSERT INTO engineer_work_items(
               id,owner_id,tenant_id,conversation_id,channel,source_binding_sha256,
               state,revision,step_ordinal,transition,completion_contract_sha256,
               created_at,updated_at,expires_at,completed_at,closed_at
           ) VALUES(?,?,?,?,?,?,'active',?,?,'created',?,?,?,?,NULL,NULL)"""
        parent_base = (
            "ewi_" + "a" * 32,
            OWNER,
            TENANT,
            conversation_id,
            "telegram",
            SOURCE,
        )
        parent_tail = (COMPLETION, NOW, NOW, EXPIRY)
        for revision, ordinal in ((1.5, 1), (1, 1.5)):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                conn.execute(parent_sql, (*parent_base, revision, ordinal, *parent_tail))

        conn.execute(parent_sql, (*parent_base, 1, 1, *parent_tail))
        conn.execute("DROP TRIGGER trg_engineer_work_item_step_insert_guard")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute(
                """INSERT INTO engineer_work_item_steps(
                       work_item_id,owner_id,ordinal,source_binding_sha256,state,
                       idempotency_key,command_digest,created_at,updated_at
                   ) VALUES(?,?,?,'b' || substr(?,2),'prepared',?,?,?,?)""",
                (
                    parent_base[0],
                    OWNER,
                    1.5,
                    SOURCE,
                    "ecmd-" + "b" * 64,
                    COMMAND,
                    NOW,
                    NOW,
                ),
            )

        conn.execute("DROP TRIGGER trg_engineer_work_item_command_fence_insert_guard")
        fence_sql = """INSERT INTO engineer_work_item_command_fences(
               owner_id,idempotency_key,work_item_id,expected_revision,step_ordinal,
               source_binding_sha256,command_digest,retired_at
           ) VALUES(?,?,?,?,?,?,?,?)"""
        for expected_revision, ordinal in ((1.5, 1), (1, 1.5)):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                conn.execute(
                    fence_sql,
                    (
                        OWNER,
                        "ecmd-" + "c" * 64,
                        "ewi_" + "c" * 32,
                        expected_revision,
                        ordinal,
                        "c" * 64,
                        COMMAND,
                        NOW,
                    ),
                )
    finally:
        conn.close()


def test_newer_or_tampered_schema_fails_closed(settings, tmp_path) -> None:
    database = tmp_path / "future.sqlite3"
    initial = FridayStorage(replace(settings, database_path=database))
    initial.ensure_user(OWNER)
    initial.close()
    with sqlite3.connect(database) as forged:
        forged.execute("UPDATE schema_meta SET value='48' WHERE key='schema_version'")
        forged.commit()
    future = FridayStorage(replace(settings, database_path=database, database_must_exist=False))
    with pytest.raises(UnsupportedSchemaVersionError):
        future.execute("SELECT 1").fetchone()
    future.close()
    with sqlite3.connect(database) as probe:
        assert probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone() == ("48",)


def test_closed_source_binding_never_persists_raw_carrier_values(storage) -> None:
    conversation_id, scope = _scope(storage)
    source_row_id = "private-inbox-row-777"
    update_id = "telegram-update-888"
    binding = engineer_source_binding_sha256(
        **scope,
        source_row_id=source_row_id,
        source_step_id=SOURCE_STEP_ID,
        source_hash="f" * 64,
        telegram_update_id=update_id,
        delivery_chat_id=DELIVERY_CHAT_ID,
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


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("owner_id", "o" * 129),
        ("tenant_id", "t" * 129),
        ("source_row_id", "r" * 129),
        ("telegram_update_id", "7" * 129),
    ),
)
def test_source_binding_limits_match_command_authority(field: str, value: str) -> None:
    scope: dict[str, object] = {
        "owner_id": OWNER,
        "tenant_id": TENANT,
        "conversation_id": "conv_0123456789abcdef",
        "channel": EngineerWorkItemChannel.TELEGRAM,
        "source_row_id": SOURCE_ROW_ID,
        "source_step_id": SOURCE_STEP_ID,
        "source_hash": SOURCE,
        "telegram_update_id": TELEGRAM_UPDATE_ID,
        "delivery_chat_id": DELIVERY_CHAT_ID,
    }
    scope[field] = value
    with pytest.raises(EngineerWorkItemAnchorError):
        engineer_source_binding_sha256(**scope)  # type: ignore[arg-type]


def test_crud_cas_idempotency_unknown_reconcile_and_completion(storage) -> None:
    scope, created = _create(storage)
    assert created.state is EngineerWorkItemState.ACTIVE
    assert created.revision == 1 and created.step_ordinal == 1
    with storage.transaction() as conn:
        replay = create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=created.source_binding_sha256,
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
                source_binding_sha256=created.source_binding_sha256,
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
            ledger_binding=_ledger_binding(scope, created),
            now=NOW,
        )
        assert admitted.state is EngineerWorkItemState.WAITING_FOR_CAPABILITY
        assert (
            bind_engineer_command_receipts_in_transaction(
                conn,
                **scope,
                work_item_id=created.id,
                expected_revision=created.revision,
                ledger_binding=_ledger_binding(scope, created),
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
                source_binding_sha256=_source_binding(
                    scope,
                    source_row_id="msg_premature_followup",
                    source_hash="3" * 64,
                    telegram_update_id="4344",
                ),
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
    followup_source = _source_binding(
        scope,
        source_row_id="msg_followup_source",
        source_hash="4" * 64,
        telegram_update_id="4343",
    )
    with storage.transaction() as conn:
        second = start_next_engineer_step_in_transaction(
            conn,
            **scope,
            work_item_id=settled.id,
            expected_revision=settled.revision,
            source_binding_sha256=followup_source,
            idempotency_key=second_key,
            command_digest=COMMAND,
            now=LATER,
        )
        assert second.step_ordinal == 2
        assert [step.ordinal for step in second.steps] == [1, 2]
        assert second.current_step.source_binding_sha256 == followup_source
        assert second.current_step.source_binding_sha256 != second.source_binding_sha256
        replay = start_next_engineer_step_in_transaction(
            conn,
            **scope,
            work_item_id=settled.id,
            expected_revision=settled.revision,
            source_binding_sha256=followup_source,
            idempotency_key=second_key,
            command_digest=COMMAND,
            now=LATER,
        )
        assert replay == second
        admitted_second = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=second.id,
            expected_revision=second.revision,
            ledger_binding=_ledger_binding(
                scope,
                second,
                source_row_id="msg_followup_source",
                source_hash="4" * 64,
                telegram_update_id="4343",
            ),
            now=LATER,
        )
        assert admitted_second.current_step.source_binding_sha256 == followup_source
        with pytest.raises(EngineerWorkItemConflictError):
            bind_engineer_command_receipts_in_transaction(
                conn,
                **scope,
                work_item_id=settled.id,
                expected_revision=settled.revision,
                ledger_binding=_ledger_binding(scope, settled),
                now=LATER,
            )


def test_fenced_later_prepared_step_rolls_back_without_erasing_receipts(storage) -> None:
    scope, settled = _settled(storage)
    followup_source = _source_binding(
        scope,
        source_row_id="msg_rollback_followup",
        source_hash="5" * 64,
        telegram_update_id="4545",
    )
    with storage.transaction() as conn:
        second = start_next_engineer_step_in_transaction(
            conn,
            **scope,
            work_item_id=settled.id,
            expected_revision=settled.revision,
            source_binding_sha256=followup_source,
            idempotency_key="ecmd-" + "5" * 64,
            command_digest="5" * 64,
            now=LATER,
        )
    first_receipts = (
        second.steps[0].job_receipt_sha256,
        second.steps[0].terminal_receipt_sha256,
    )
    fence = _fence_binding(second)

    with (
        pytest.raises(
            sqlite3.IntegrityError,
            match="transition_invalid",
        ),
        storage.transaction() as conn,
    ):
        conn.execute(
            """UPDATE engineer_work_items
                  SET state='cancelled',transition='cancelled',revision=revision+1,
                      step_ordinal=step_ordinal-1,updated_at=?,closed_at=?
                WHERE id=?""",
            (LATER, LATER, second.id),
        )

    with storage.transaction() as conn:
        conn.create_function(
            "friday_engineer_prepared_discard_authorized",
            4,
            lambda *_args: 1,
        )
        try:
            with pytest.raises(sqlite3.IntegrityError, match="transition_invalid"):
                conn.execute(
                    """UPDATE engineer_work_items
                          SET state='waiting_for_input',transition='prepared_step_discarded',
                              revision=revision+1,step_ordinal=step_ordinal-1,updated_at=?
                        WHERE id=?""",
                    (LATER, second.id),
                )
        finally:
            register_engineer_work_item_connection_functions(conn)

    with pytest.raises(EngineerWorkItemConflictError), storage.transaction() as conn:
        discard_unsubmitted_engineer_work_item_in_transaction(
            conn,
            **scope,
            work_item_id=second.id,
            fence_binding=fence,
        )

    with storage.transaction() as conn:
        rolled_back = rollback_fenced_unsubmitted_engineer_step_in_transaction(
            conn,
            **scope,
            work_item_id=second.id,
            fence_binding=fence,
            now=LATER,
        )
        assert (
            rollback_fenced_unsubmitted_engineer_step_in_transaction(
                conn,
                **scope,
                work_item_id=second.id,
                fence_binding=fence,
                now=LATER,
            )
            == rolled_back
        )
    assert rolled_back.state is EngineerWorkItemState.WAITING_FOR_INPUT
    assert rolled_back.transition.value == "prepared_step_discarded"
    assert rolled_back.step_ordinal == 1
    assert len(rolled_back.steps) == 1
    assert (
        rolled_back.current_step.job_receipt_sha256,
        rolled_back.current_step.terminal_receipt_sha256,
    ) == first_receipts
    with (
        pytest.raises(
            EngineerWorkItemConflictError,
            match="permanently fenced",
        ),
        storage.transaction() as conn,
    ):
        start_next_engineer_step_in_transaction(
            conn,
            **scope,
            work_item_id=rolled_back.id,
            expected_revision=rolled_back.revision,
            source_binding_sha256=str(fence["source_binding_sha256"]),
            idempotency_key=str(fence["idempotency_key"]),
            command_digest=str(fence["command_digest"]),
            now=LATER,
        )
    with (
        pytest.raises(
            EngineerWorkItemConflictError,
            match="permanently fenced",
        ),
        storage.transaction() as conn,
    ):
        start_next_engineer_step_in_transaction(
            conn,
            **scope,
            work_item_id=rolled_back.id,
            expected_revision=rolled_back.revision,
            source_binding_sha256=str(fence["source_binding_sha256"]),
            idempotency_key="ecmd-" + "7" * 64,
            command_digest="7" * 64,
            now=LATER,
        )


def test_fenced_later_step_in_inactive_scope_closes_without_losing_receipts(storage) -> None:
    scope, settled = _settled(storage)
    with storage.transaction() as conn:
        second = start_next_engineer_step_in_transaction(
            conn,
            **scope,
            work_item_id=settled.id,
            expected_revision=settled.revision,
            source_binding_sha256=_source_binding(
                scope,
                source_row_id="msg_inactive_followup",
                source_hash="6" * 64,
                telegram_update_id="4646",
            ),
            idempotency_key="ecmd-" + "6" * 64,
            command_digest="6" * 64,
            now=LATER,
        )
    assert storage.archive_conversation(str(scope["conversation_id"]), OWNER)
    with storage.transaction() as conn:
        cancelled = rollback_fenced_unsubmitted_engineer_step_in_transaction(
            conn,
            **scope,
            work_item_id=second.id,
            fence_binding=_fence_binding(second),
            now=LATER,
        )
    assert cancelled.state is EngineerWorkItemState.CANCELLED
    assert cancelled.step_ordinal == 1
    assert cancelled.current_step.terminal_receipt_sha256 == TERMINAL


def test_current_step_source_replay_with_a_new_key_is_not_idempotent(storage) -> None:
    scope, settled = _settled(storage)
    source = _source_binding(
        scope,
        source_row_id="msg_current_source_rekey",
        source_hash="6" * 64,
        telegram_update_id="4647",
    )
    with storage.transaction() as conn:
        second = start_next_engineer_step_in_transaction(
            conn,
            **scope,
            work_item_id=settled.id,
            expected_revision=settled.revision,
            source_binding_sha256=source,
            idempotency_key="ecmd-" + "6" * 64,
            command_digest="6" * 64,
            now=LATER,
        )
        with pytest.raises(EngineerWorkItemConflictError, match="source or idempotency"):
            start_next_engineer_step_in_transaction(
                conn,
                **scope,
                work_item_id=second.id,
                expected_revision=settled.revision,
                source_binding_sha256=source,
                idempotency_key="ecmd-" + "7" * 64,
                command_digest="6" * 64,
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
            ledger_binding=_ledger_binding(scope, item),
            now=NOW,
        )
        with pytest.raises(EngineerWorkItemConflictError):
            bind_engineer_command_receipts_in_transaction(
                conn,
                **scope,
                work_item_id=item.id,
                expected_revision=item.revision,
                ledger_binding=_ledger_binding(scope, item, command_digest="8" * 64),
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


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("actor_id", "engineer-owner-other"),
        ("tenant_id", "engineer-tenant-other"),
        ("conversation_id", "conv_ffffffffffffffff"),
        ("source_row_id", "msg_other_source"),
        ("source_step_id", "ecstep-" + "2" * 32),
        ("source_hash", "b" * 64),
        ("telegram_update_id", "9999"),
        ("idempotency_key", "ecmd-" + "b" * 64),
        ("command_digest", "b" * 64),
        ("delivery_chat_id", "987654321"),
    ),
)
def test_command_ledger_scope_drift_cannot_bind_prepared_work(
    storage,
    field: str,
    replacement: str,
) -> None:
    scope, item = _create(storage)
    observed = _ledger_binding(scope, item)
    observed[field] = replacement
    with pytest.raises(EngineerWorkItemConflictError), storage.transaction() as conn:
        bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            ledger_binding=observed,
            now=NOW,
        )
    assert (
        get_engineer_work_item_in_transaction(
            storage.conn,
            **scope,
            work_item_id=item.id,
        )
        == item
    )


@pytest.mark.parametrize("mutation", ("missing", "extra", "empty_chat", "foreign_channel"))
def test_incomplete_or_unadmitted_command_ledger_projection_fails_closed(
    storage,
    mutation: str,
) -> None:
    scope, item = _create(storage)
    observed: dict[str, object] = _ledger_binding(scope, item)
    if mutation == "missing":
        observed.pop("source_hash")
    elif mutation == "extra":
        observed["shell_command"] = "must-not-cross-the-control-plane"
    elif mutation == "empty_chat":
        observed["delivery_chat_id"] = ""
    else:
        observed["channel"] = "web"
    with (
        pytest.raises((EngineerWorkItemContractError, EngineerWorkItemAnchorError)),
        storage.transaction() as conn,
    ):
        bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            ledger_binding=observed,
            now=NOW,
        )
    durable = get_engineer_work_item_in_transaction(
        storage.conn,
        **scope,
        work_item_id=item.id,
    )
    assert durable is not None and durable.current_step.job_receipt_sha256 == ""


def test_direct_parent_delete_cannot_erase_effect_bearing_receipts(storage) -> None:
    scope, item = _create(storage)
    with storage.transaction() as conn:
        admitted = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            ledger_binding=_ledger_binding(scope, item),
            now=NOW,
        )
        uncertain = mark_engineer_command_unknown_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=admitted.revision,
            now=LATER,
        )
    with (
        pytest.raises(
            sqlite3.IntegrityError,
            match="open_deletion_forbidden",
        ),
        storage.transaction() as conn,
    ):
        conn.execute("DELETE FROM engineer_work_items WHERE id=?", (item.id,))
    assert not storage.conn.in_transaction
    durable = get_engineer_work_item_in_transaction(
        storage.conn,
        **scope,
        work_item_id=item.id,
    )
    assert durable == uncertain


def test_raw_transition_chain_cannot_hide_then_delete_unknown_receipt(storage) -> None:
    scope, item = _create(storage)
    with storage.transaction() as conn:
        admitted = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            ledger_binding=_ledger_binding(scope, item),
            now=NOW,
        )
        with pytest.raises(sqlite3.IntegrityError, match="transition_invalid"):
            conn.execute(
                """UPDATE engineer_work_items
                      SET state='waiting_for_input',transition='terminal_observed',
                          revision=revision+1,updated_at=?
                    WHERE id=?""",
                (LATER, item.id),
            )
        unknown = mark_engineer_command_unknown_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=admitted.revision,
            now=LATER,
        )
        with pytest.raises(sqlite3.IntegrityError, match="transition_invalid"):
            conn.execute(
                """UPDATE engineer_work_items
                      SET state='waiting_for_input',transition='terminal_observed',
                          revision=revision+1,updated_at=?
                    WHERE id=?""",
                (LATER, item.id),
            )

    storage.execute("DROP TRIGGER trg_engineer_work_item_transition_guard")
    storage.execute(
        """UPDATE engineer_work_items
              SET state='waiting_for_input',transition='terminal_observed',
                  revision=revision+1,updated_at=?
            WHERE id=?""",
        (LATER, item.id),
    )
    storage.conn.executescript(ENGINEER_WORK_ITEM_SCHEMA)
    with (
        pytest.raises(
            sqlite3.IntegrityError,
            match="transition_invalid",
        ),
        storage.transaction() as conn,
    ):
        conn.execute(
            """UPDATE engineer_work_items
                  SET state='cancelled',transition='cancelled',revision=revision+1,
                      updated_at=?,closed_at=?
                WHERE id=?""",
            (LATER, LATER, item.id),
        )
    assert not storage.conn.in_transaction

    storage.execute("DROP TRIGGER trg_engineer_work_item_transition_guard")
    storage.execute(
        """UPDATE engineer_work_items
              SET state='cancelled',transition='cancelled',revision=revision+1,
                  updated_at=?,closed_at=?
            WHERE id=?""",
        (LATER, LATER, item.id),
    )
    storage.conn.executescript(ENGINEER_WORK_ITEM_SCHEMA)
    with (
        pytest.raises(
            sqlite3.IntegrityError,
            match="open_deletion_forbidden",
        ),
        storage.transaction() as conn,
    ):
        conn.execute("DELETE FROM engineer_work_items WHERE id=?", (unknown.id,))


def test_insert_or_replace_cannot_erase_parent_or_step_receipt(storage) -> None:
    scope, item = _create(storage)
    with storage.transaction() as conn:
        admitted = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            ledger_binding=_ledger_binding(scope, item),
            now=NOW,
        )
        unknown = mark_engineer_command_unknown_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=admitted.revision,
            now=LATER,
        )
    with (
        pytest.raises(
            sqlite3.IntegrityError,
            match="identity_collision",
        ),
        storage.transaction() as conn,
    ):
        conn.execute(
            """INSERT OR REPLACE INTO engineer_work_items
               SELECT * FROM engineer_work_items WHERE id=?""",
            (item.id,),
        )
    with (
        pytest.raises(
            sqlite3.IntegrityError,
            match="step_scope_invalid",
        ),
        storage.transaction() as conn,
    ):
        conn.execute(
            """INSERT OR REPLACE INTO engineer_work_item_steps
               SELECT * FROM engineer_work_item_steps
                WHERE work_item_id=? AND ordinal=1""",
            (item.id,),
        )
    assert (
        get_engineer_work_item_in_transaction(
            storage.conn,
            **scope,
            work_item_id=item.id,
        )
        == unknown
    )


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
            ledger_binding=_ledger_binding(scope, item),
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
    source_binding = engineer_source_binding_sha256(
        **scope,
        source_row_id=source_message_id,
        source_step_id=runtime_step_id,
        source_hash=SOURCE,
        telegram_update_id=TELEGRAM_UPDATE_ID,
        delivery_chat_id=DELIVERY_CHAT_ID,
    )
    with first.transaction() as conn:
        item = create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=source_binding,
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
            "source_step_id": runtime_step_id,
            "source_hash": SOURCE,
            "telegram_update_id": TELEGRAM_UPDATE_ID,
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
            "delivery_chat_id": DELIVERY_CHAT_ID,
        }
    )
    lookup = kernel_store.lookup_idempotency(OWNER, actual_key)
    assert lookup == {
        "job_id": job_id,
        "digest": request.digest,
        "delivery_chat_id": DELIVERY_CHAT_ID,
    }
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
    with reopened_kernel.transaction():
        recovered = reopened_kernel.lookup_idempotency_binding(
            durable.owner_id,
            durable.current_step.idempotency_key,
        )
        assert recovered is not None
        assert recovered["command_digest"] == durable.current_step.command_digest
        assert recovered["job_id"] == job_id
        with reopened.transaction() as conn:
            admitted = bind_engineer_command_receipts_in_transaction(
                conn,
                **scope,
                work_item_id=item.id,
                expected_revision=durable.revision,
                ledger_binding=recovered,
                now="2026-08-28T00:00:00+00:00",
            )
    assert admitted.current_step.idempotency_key == actual_key
    assert admitted.current_step.job_receipt_sha256 == engineer_job_receipt_sha256(
        **scope,
        source_binding_sha256=source_binding,
        delivery_chat_id=DELIVERY_CHAT_ID,
        idempotency_key=actual_key,
        command_digest=request.digest,
        job_id=job_id,
    )
    with reopened.transaction() as conn:
        cancelled = settle_engineer_terminal_receipt_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=admitted.revision,
            verified_terminal_receipt_sha256=TERMINAL,
            now="2026-08-28T00:00:01+00:00",
        )
        assert cancelled.state is EngineerWorkItemState.CANCELLED
        assert (
            settle_engineer_terminal_receipt_in_transaction(
                conn,
                **scope,
                work_item_id=item.id,
                expected_revision=admitted.revision,
                verified_terminal_receipt_sha256=TERMINAL,
                now="2026-08-28T00:00:01+00:00",
            )
            == cancelled
        )
        with pytest.raises(EngineerWorkItemConflictError):
            start_next_engineer_step_in_transaction(
                conn,
                **scope,
                work_item_id=item.id,
                expected_revision=cancelled.revision,
                source_binding_sha256=_source_binding(
                    scope,
                    source_row_id="msg_closed_followup",
                    source_hash="f" * 64,
                    telegram_update_id="4949",
                ),
                idempotency_key="ecmd-" + "f" * 64,
                command_digest=COMMAND,
                now="2026-08-28T00:00:02+00:00",
            )
    try:
        durable = get_engineer_work_item_in_transaction(
            reopened.conn,
            **scope,
            work_item_id=item.id,
        )
        assert durable is not None
        recovered = reopened_kernel.lookup_idempotency_binding(
            durable.owner_id,
            durable.current_step.idempotency_key,
        )
        assert recovered is not None
        assert recovered["command_digest"] == durable.current_step.command_digest
        assert recovered["job_id"] == job_id
        assert private_command.encode() not in database.read_bytes()
        assert job_id.encode() not in database.read_bytes()
    finally:
        reopened_kernel.close()
        reopened.close()


def test_same_owner_key_from_foreign_command_scope_cannot_bind(settings, tmp_path) -> None:
    storage = FridayStorage(replace(settings, database_path=tmp_path / "main.sqlite3"))
    scope, item = _create(storage)
    command_store = CommandJobStore(tmp_path / "command-kernel")
    command_store.insert_job(
        {
            "job_id": JOB_ID,
            "actor_id": OWNER,
            "tenant_id": TENANT,
            "conversation_id": "conv_ffffffffffffffff",
            "channel": "telegram",
            "source_row_id": SOURCE_ROW_ID,
            "source_step_id": SOURCE_STEP_ID,
            "source_hash": SOURCE,
            "telegram_update_id": TELEGRAM_UPDATE_ID,
            "isolation_profile": "host_user",
            "host_user_authorized": True,
            "idempotency_key": item.current_step.idempotency_key,
            "command_digest": COMMAND,
            "input_manifest_sha256": "",
            "argv_sha256": "f" * 64,
            "lane": "shell",
            "origin": "model",
            "status": "admitted",
            "error_code": "",
            "grant_nonce": "schema46-foreign-scope",
            "timeout_sec": 300,
            "max_stdout_bytes": 1_024,
            "max_stderr_bytes": 1_024,
            "created_at": 1_777_000_001.0,
            "executable_json": None,
            "delivery_chat_id": DELIVERY_CHAT_ID,
        }
    )
    try:
        with command_store.transaction():
            observed = command_store.lookup_idempotency_binding(
                OWNER,
                item.current_step.idempotency_key,
            )
            assert observed is not None
            assert observed["actor_id"] == item.owner_id
            assert observed["idempotency_key"] == item.current_step.idempotency_key
            with pytest.raises(EngineerWorkItemConflictError), storage.transaction() as conn:
                bind_engineer_command_receipts_in_transaction(
                    conn,
                    **scope,
                    work_item_id=item.id,
                    expected_revision=item.revision,
                    ledger_binding=observed,
                    now=NOW,
                )
        assert (
            get_engineer_work_item_in_transaction(
                storage.conn,
                **scope,
                work_item_id=item.id,
            )
            == item
        )
    finally:
        command_store.close()
        storage.close()


def test_prepared_negative_ledger_lookup_can_be_discarded_without_a_scope_wedge(
    settings,
    tmp_path,
) -> None:
    database = tmp_path / "main.sqlite3"
    command_root = tmp_path / "command-kernel"
    storage = FridayStorage(replace(settings, database_path=database))
    conversation_id, scope = _scope(storage)
    key = "ecmd-" + "8" * 64
    with storage.transaction() as conn:
        item = create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=SOURCE,
            completion_contract_sha256=COMPLETION,
            idempotency_key=key,
            command_digest=COMMAND,
            now=NOW,
            expires_at=EXPIRY,
        )

    with (
        pytest.raises(
            sqlite3.IntegrityError,
            match="open_deletion_forbidden",
        ),
        storage.transaction() as conn,
    ):
        conn.execute("DELETE FROM engineer_work_items WHERE id=?", (item.id,))
    assert not storage.conn.in_transaction

    command_store = CommandJobStore(command_root)
    try:
        assert command_store.lookup_idempotency(OWNER, key) is None
        fence = command_store.create_engineer_work_item_fence(**_fence_binding(item))
        assert command_store.lookup_engineer_work_item_fence(OWNER, key) == fence
    finally:
        command_store.close()

    # Crash window: the durable fence survives while main DB still says
    # prepared. Recovery reads it back before the independent main mutation.
    storage.close()
    storage = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    recovered_store = CommandJobStore(command_root)
    try:
        recovered_fence = recovered_store.lookup_engineer_work_item_fence(OWNER, key)
        assert recovered_fence is not None
        assert recovered_fence == fence
    finally:
        recovered_store.close()
    with storage.transaction() as conn:
        assert discard_unsubmitted_engineer_work_item_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            fence_binding=recovered_fence,
        )
    assert not storage.conn.in_transaction

    storage.close()
    storage = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    assert (
        get_engineer_work_item_in_transaction(
            storage.conn,
            **scope,
            work_item_id=item.id,
        )
        is None
    )
    assert (
        storage.conn.execute(
            """SELECT COUNT(*) FROM engineer_work_item_command_fences
            WHERE owner_id=? AND idempotency_key=?""",
            (OWNER, key),
        ).fetchone()[0]
        == 1
    )
    with (
        pytest.raises(
            sqlite3.IntegrityError,
            match="command_fence_invalid",
        ),
        storage.transaction() as conn,
    ):
        conn.execute(
            """INSERT OR REPLACE INTO engineer_work_item_command_fences
               SELECT * FROM engineer_work_item_command_fences
                WHERE owner_id=? AND idempotency_key=?""",
            (OWNER, key),
        )
    with (
        pytest.raises(
            EngineerWorkItemConflictError,
            match="permanently fenced",
        ),
        storage.transaction() as conn,
    ):
        create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=SOURCE,
            completion_contract_sha256=COMPLETION,
            idempotency_key=key,
            command_digest=COMMAND,
            now=NOW,
            expires_at=EXPIRY,
        )
    with (
        pytest.raises(
            EngineerWorkItemConflictError,
            match="permanently fenced",
        ),
        storage.transaction() as conn,
    ):
        create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=SOURCE,
            completion_contract_sha256=COMPLETION,
            idempotency_key="ecmd-" + "7" * 64,
            command_digest="7" * 64,
            now=NOW,
            expires_at=EXPIRY,
        )
    # The fenced key is permanently retired; only a fresh authenticated source
    # and deterministic key may reserve replacement work.
    with storage.transaction() as conn:
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
    assert replacement.id != item.id
    storage.close()


def test_restored_main_discards_recreated_initial_item_against_historical_fence(storage) -> None:
    _conversation_id, scope = _scope(storage)
    recreated_id = "ewi_" + "2" * 32
    historical_id = "ewi_" + "1" * 32
    key = "ecmd-" + "8" * 64
    with storage.transaction() as conn:
        recreated = create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=SOURCE,
            completion_contract_sha256=COMPLETION,
            idempotency_key=key,
            command_digest=COMMAND,
            work_item_id=recreated_id,
            now=NOW,
            expires_at=EXPIRY,
        )
    historical_fence = {**_fence_binding(recreated), "work_item_id": historical_id}

    with storage.transaction() as conn:
        assert discard_unsubmitted_engineer_work_item_in_transaction(
            conn,
            **scope,
            work_item_id=recreated.id,
            fence_binding=historical_fence,
        )
        assert not discard_unsubmitted_engineer_work_item_in_transaction(
            conn,
            **scope,
            work_item_id=recreated.id,
            fence_binding=historical_fence,
        )
    assert (
        get_engineer_work_item_in_transaction(
            storage.conn,
            **scope,
            work_item_id=recreated.id,
        )
        is None
    )
    retired = storage.execute(
        """SELECT work_item_id,expected_revision,step_ordinal
             FROM engineer_work_item_command_fences
            WHERE owner_id=? AND idempotency_key=?""",
        (OWNER, key),
    ).fetchone()
    assert tuple(retired) == (historical_id, 1, 1)
    validate_engineer_work_item_schema(storage.conn)

    # A historical external Work Item ID is permanently reserved even when a
    # fresh authenticated source and key would otherwise be admissible.
    with pytest.raises(EngineerWorkItemConflictError), storage.transaction() as conn:
        create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256="9" * 64,
            completion_contract_sha256=COMPLETION,
            idempotency_key="ecmd-" + "9" * 64,
            command_digest="9" * 64,
            work_item_id=historical_id,
            now=NOW,
            expires_at=EXPIRY,
        )


def test_historical_fence_cannot_alias_an_existing_live_parent(storage) -> None:
    _historical_conversation, historical_scope = _scope(storage)
    historical_id = "ewi_" + "1" * 32
    with storage.transaction() as conn:
        create_engineer_work_item_in_transaction(
            conn,
            **historical_scope,
            source_binding_sha256="f" * 64,
            completion_contract_sha256=COMPLETION,
            idempotency_key="ecmd-" + "f" * 64,
            command_digest="f" * 64,
            work_item_id=historical_id,
            now=NOW,
            expires_at=EXPIRY,
        )
    _recreated_conversation, recreated_scope = _scope(storage)
    with storage.transaction() as conn:
        recreated = create_engineer_work_item_in_transaction(
            conn,
            **recreated_scope,
            source_binding_sha256=SOURCE,
            completion_contract_sha256=COMPLETION,
            idempotency_key="ecmd-" + "8" * 64,
            command_digest=COMMAND,
            work_item_id="ewi_" + "2" * 32,
            now=NOW,
            expires_at=EXPIRY,
        )
    historical_fence = {**_fence_binding(recreated), "work_item_id": historical_id}
    with (
        pytest.raises(
            EngineerWorkItemConflictError,
            match="could not be retired exactly",
        ),
        storage.transaction() as conn,
    ):
        discard_unsubmitted_engineer_work_item_in_transaction(
            conn,
            **recreated_scope,
            work_item_id=recreated.id,
            fence_binding=historical_fence,
        )
    assert (
        get_engineer_work_item_in_transaction(
            storage.conn,
            **recreated_scope,
            work_item_id=recreated.id,
        )
        == recreated
    )


def test_expiry_retention_and_delete_preserve_effect_bearing_work(storage) -> None:
    scope, item = _create(storage)
    with storage.transaction() as conn:
        admitted = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            ledger_binding=_ledger_binding(scope, item),
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
            source_binding_sha256=_source_binding(scope),
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
            ledger_binding=_ledger_binding(scope, replacement),
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
            ledger_binding=_ledger_binding(scope, item),
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


@pytest.mark.parametrize(
    ("target", "expected_state"),
    [
        ("prepared", EngineerWorkItemState.ACTIVE),
        ("admitted", EngineerWorkItemState.WAITING_FOR_CAPABILITY),
        ("unknown", EngineerWorkItemState.UNCERTAIN),
        ("settled", EngineerWorkItemState.CANCELLED),
        ("ready", EngineerWorkItemState.CANCELLED),
    ],
)
def test_public_and_admin_archive_routes_share_engineer_retirement_matrix(
    settings,
    target: str,
    expected_state: EngineerWorkItemState,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        for surface in ("public", "admin"):
            scope, before = _route_archive_state(app.state.storage, target=target)
            conversation_id = str(scope["conversation_id"])
            channel_id = f"archive-{surface}-{target}"
            app.state.storage.set_channel_conversation(
                LEGACY_OWNER_USER_ID,
                "telegram",
                channel_id,
                conversation_id,
            )
            path = (
                f"/api/conversations/{conversation_id}/archive"
                if surface == "public"
                else (f"/api/admin/conversations/{conversation_id}/archive?user_id={LEGACY_OWNER_USER_ID}")
            )
            response = client.post(path, json={"archived": True}, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["conversation"]["is_archived"] == 1
            assert (
                app.state.storage.get_channel_session(
                    LEGACY_OWNER_USER_ID,
                    "telegram",
                    channel_id,
                )
                is None
            )
            after = get_engineer_work_item_in_transaction(
                app.state.storage.conn,
                **scope,
                work_item_id=before.id,
            )
            assert after is not None and after.state is expected_state
            assert after.revision == before.revision + int(expected_state is EngineerWorkItemState.CANCELLED)


def test_archived_admitted_work_settles_directly_to_cancelled(storage) -> None:
    scope, item = _create(storage)
    with storage.transaction() as conn:
        admitted = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            ledger_binding=_ledger_binding(scope, item),
            now=NOW,
        )
    assert storage.archive_conversation(str(scope["conversation_id"]), OWNER)
    with storage.transaction() as conn:
        settled = settle_engineer_terminal_receipt_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=admitted.revision,
            verified_terminal_receipt_sha256=TERMINAL,
            now=LATER,
        )
    assert settled.state is EngineerWorkItemState.CANCELLED
    assert settled.revision == admitted.revision + 2
    assert settled.current_step.terminal_receipt_sha256 == TERMINAL


def test_inactive_scope_cannot_publish_a_prepared_answer(storage) -> None:
    scope, item = _settled(storage)
    with storage.transaction() as conn:
        ready = mark_engineer_work_item_ready_to_answer_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=item.revision,
            now=LATER,
        )
    assert storage.archive_conversation(str(scope["conversation_id"]), OWNER)
    cancelled = get_engineer_work_item_in_transaction(
        storage.conn,
        **scope,
        work_item_id=item.id,
    )
    assert cancelled is not None and cancelled.state is EngineerWorkItemState.CANCELLED
    with (
        storage.transaction() as conn,
        pytest.raises(
            EngineerWorkItemConflictError,
            match="revision|completed|terminal|current",
        ),
    ):
        close_engineer_work_item_in_transaction(
            conn,
            **scope,
            work_item_id=item.id,
            expected_revision=ready.revision,
            terminal_state=EngineerWorkItemState.COMPLETED,
            now=LATER,
        )


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
    assert backup["schema_version"] == 47
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
            source_binding_sha256=_source_binding(scope),
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
            ledger_binding=_ledger_binding(scope, item),
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
    assert {blocker["code"] for blocker in plan["blockers"]} == {
        "chat_history",
        "engineer_command_history",
    }
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


def test_row_validator_rejects_orphan_step_even_when_foreign_keys_were_off(storage) -> None:
    _scope_data, item = _create(storage)
    with sqlite3.connect(storage.settings.database_path) as forged:
        register_engineer_work_item_connection_functions(forged)
        forged.execute("DROP TRIGGER trg_engineer_work_item_step_insert_guard")
        forged.execute(
            """INSERT INTO engineer_work_item_steps(
                   work_item_id,owner_id,ordinal,source_binding_sha256,state,
                   idempotency_key,command_digest,created_at,updated_at)
               VALUES(?,?,1,?,'prepared',?,?,?,?)""",
            (
                "ewi_" + "f" * 32,
                item.owner_id,
                "f" * 64,
                "ecmd-" + "e" * 64,
                "e" * 64,
                NOW,
                NOW,
            ),
        )
    storage.conn.executescript(ENGINEER_WORK_ITEM_SCHEMA)
    with pytest.raises(sqlite3.DatabaseError, match="step is orphaned"):
        validate_engineer_work_item_schema(storage.conn)


def test_row_validator_rejects_missing_parent_identity(storage) -> None:
    _scope_data, item = _create(storage)
    with sqlite3.connect(storage.settings.database_path) as forged:
        register_engineer_work_item_connection_functions(forged)
        forged.execute("DROP TRIGGER trg_engineer_work_item_identity_immutable")
        forged.execute("DROP TRIGGER trg_engineer_work_item_transition_guard")
        forged.execute(
            "UPDATE engineer_work_items SET tenant_id=? WHERE id=?",
            ("missing-engineer-tenant", item.id),
        )
    storage.conn.executescript(ENGINEER_WORK_ITEM_SCHEMA)
    with pytest.raises(sqlite3.DatabaseError, match="rows are inconsistent"):
        validate_engineer_work_item_schema(storage.conn)


def test_row_validator_rejects_noncontiguous_step_ordinals(storage) -> None:
    scope, settled = _settled(storage)
    with sqlite3.connect(storage.settings.database_path) as forged:
        register_engineer_work_item_connection_functions(forged)
        forged.execute("DROP TRIGGER trg_engineer_work_item_transition_guard")
        forged.execute("DROP TRIGGER trg_engineer_work_item_step_insert_guard")
        forged.execute(
            "UPDATE engineer_work_items SET step_ordinal=3 WHERE id=?",
            (settled.id,),
        )
        for ordinal, marker in ((3, "7"), (4, "8")):
            forged.execute(
                """INSERT INTO engineer_work_item_steps(
                       work_item_id,owner_id,ordinal,source_binding_sha256,state,
                       idempotency_key,command_digest,job_receipt_sha256,
                       terminal_receipt_sha256,created_at,updated_at,admitted_at,settled_at)
                   VALUES(?,?,?,?, 'settled',?,?,?,?,?,?,?,?)""",
                (
                    settled.id,
                    settled.owner_id,
                    ordinal,
                    marker * 64,
                    "ecmd-" + marker * 64,
                    marker * 64,
                    marker * 64,
                    marker * 64,
                    NOW,
                    LATER,
                    NOW,
                    LATER,
                ),
            )
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
            source_binding_sha256=_source_binding(
                scope,
                source_row_id="msg_validator_followup",
                source_hash="f" * 64,
                telegram_update_id="5050",
            ),
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
                        ledger_binding=_ledger_binding(scope, item, job_id=job_id),
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
            source_binding_sha256=item.source_binding_sha256,
            delivery_chat_id=DELIVERY_CHAT_ID,
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
            source_binding_sha256=_source_binding(scope),
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
            source_binding_sha256=_source_binding(scope),
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
                ledger_binding=_ledger_binding(scope, item),
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


def test_ready_replay_and_answer_commit_cannot_cross_expiry(storage) -> None:
    scope, settled = _settled(storage)
    with storage.transaction() as conn:
        ready = mark_engineer_work_item_ready_to_answer_in_transaction(
            conn,
            **scope,
            work_item_id=settled.id,
            expected_revision=settled.revision,
            now=LATER,
        )
    with storage.transaction() as conn:
        with pytest.raises(EngineerWorkItemConflictError, match="expired"):
            mark_engineer_work_item_ready_to_answer_in_transaction(
                conn,
                **scope,
                work_item_id=settled.id,
                expected_revision=settled.revision,
                now=EXPIRY,
            )
        with pytest.raises(EngineerWorkItemConflictError, match="expired"):
            close_engineer_work_item_in_transaction(
                conn,
                **scope,
                work_item_id=ready.id,
                expected_revision=ready.revision,
                terminal_state=EngineerWorkItemState.COMPLETED,
                now=EXPIRY,
            )
        assert expire_due_engineer_work_items_in_transaction(conn, now=EXPIRY) == 1


def test_raw_progress_and_completion_cannot_bypass_scope_revocation(storage) -> None:
    scope, settled = _settled(storage)
    storage.update_user(OWNER, status="disabled")
    for statement in (
        """UPDATE engineer_work_items
              SET state='active',revision=revision+1,step_ordinal=step_ordinal+1,
                  transition='next_step_started',updated_at='2026-08-27T10:00:02+00:00'
            WHERE id=?""",
        """UPDATE engineer_work_items
              SET state='ready_to_answer',revision=revision+1,
                  transition='answer_ready',updated_at='2026-08-27T10:00:02+00:00'
            WHERE id=?""",
    ):
        with (
            pytest.raises(
                sqlite3.IntegrityError,
                match="engineer_work_item_transition_invalid",
            ),
            storage.transaction() as conn,
        ):
            conn.execute(statement, (settled.id,))

    storage.update_user(OWNER, status="active")
    with storage.transaction() as conn:
        ready = mark_engineer_work_item_ready_to_answer_in_transaction(
            conn,
            **scope,
            work_item_id=settled.id,
            expected_revision=settled.revision,
            now="2026-08-27T10:00:02+00:00",
        )
    storage.update_user(OWNER, status="disabled")
    with (
        pytest.raises(
            sqlite3.IntegrityError,
            match="engineer_work_item_transition_invalid",
        ),
        storage.transaction() as conn,
    ):
        conn.execute(
            """UPDATE engineer_work_items
                  SET state='completed',revision=revision+1,transition='completed',
                      updated_at='2026-08-27T10:00:03+00:00',
                      completed_at='2026-08-27T10:00:03+00:00',
                      closed_at='2026-08-27T10:00:03+00:00'
                WHERE id=?""",
            (ready.id,),
        )
