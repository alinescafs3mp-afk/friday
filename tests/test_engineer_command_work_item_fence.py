"""Durable cross-database admission fence for Engineer Work Item recovery."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from friday.organs.engineer.command.contracts import (
    CommandError,
    CommandLane,
    CommandOrigin,
    CommandRequest,
    CommandStatus,
    IsolationProfile,
)
from friday.organs.engineer.command.store import CommandJobStore

ACTOR = "owner"
KEY = "ecmd-" + "1" * 64
WORK_ITEM_ID = "ewi_" + "2" * 32
SOURCE_BINDING = "3" * 64
COMMAND_DIGEST = "4" * 64


def _fence(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "actor_id": ACTOR,
        "idempotency_key": KEY,
        "work_item_id": WORK_ITEM_ID,
        "expected_revision": 7,
        "step_ordinal": 2,
        "source_binding_sha256": SOURCE_BINDING,
        "command_digest": COMMAND_DIGEST,
    }
    values.update(changes)
    return values


def _projection() -> dict[str, str | int]:
    return {
        "actor_id": ACTOR,
        "work_item_id": WORK_ITEM_ID,
        "expected_revision": 7,
        "step_ordinal": 2,
        "source_binding_sha256": SOURCE_BINDING,
        "idempotency_key": KEY,
        "command_digest": COMMAND_DIGEST,
    }


def _job_payload(
    *,
    key: str,
    job_id: str = "5" * 32,
    delivery_chat_id: str = "",
) -> dict[str, object]:
    request = CommandRequest(
        lane=CommandLane.ARGV,
        origin=CommandOrigin.OWNER_TURN,
        argv=("/usr/bin/true",),
        idempotency_key=key,
    )
    return {
        "job_id": job_id,
        "actor_id": ACTOR,
        "tenant_id": "tenant",
        "conversation_id": "conversation",
        "channel": "cli_test",
        "source_row_id": "source-row",
        "source_hash": "6" * 64,
        "telegram_update_id": "source-update",
        "isolation_profile": IsolationProfile.ISOLATED_WORKSPACE.value,
        "host_user_authorized": False,
        "idempotency_key": key,
        "command_digest": request.digest,
        "argv_sha256": request.argv_sha256,
        "lane": request.lane.value,
        "origin": request.origin.value,
        "status": CommandStatus.ADMITTED.value,
        "grant_nonce": "grant",
        "timeout_sec": 30,
        "max_stdout_bytes": 1_024,
        "max_stderr_bytes": 1_024,
        "created_at": time.time(),
        "executable_json": "{}",
        "delivery_chat_id": delivery_chat_id,
    }


def test_fence_commit_readback_replay_conflict_and_failed_commit(tmp_path: Path) -> None:
    store = CommandJobStore(tmp_path / "store")
    try:
        assert store.lookup_engineer_work_item_fence(ACTOR, KEY) is None
        assert store.create_engineer_work_item_fence(**_fence(), created_at=123.5) == _projection()
        assert store.lookup_engineer_work_item_fence(ACTOR, KEY) == _projection()
        assert (
            store.lookup_engineer_work_item_fence_by_source(ACTOR, SOURCE_BINDING)
            == _projection()
        )
        assert store.lookup_engineer_work_item_fence_by_source("other-owner", SOURCE_BINDING) is None

        with sqlite3.connect(store.db_path) as connection:
            raw = connection.execute(
                """SELECT created_at,typeof(created_at)
                     FROM engineer_work_item_idempotency_fences
                    WHERE actor_id=? AND idempotency_key=?""",
                (ACTOR, KEY),
            ).fetchone()
        assert raw == (123.5, "real")

        # A retry never changes the first durable audit timestamp.
        assert store.create_engineer_work_item_fence(**_fence(), created_at=456.5) == _projection()
        with sqlite3.connect(store.db_path) as connection:
            audit_row = connection.execute(
                """SELECT created_at FROM engineer_work_item_idempotency_fences
                    WHERE actor_id=? AND idempotency_key=?""",
                (ACTOR, KEY),
            ).fetchone()
        assert audit_row == (123.5,)

        with pytest.raises(CommandError, match="idempotency_fence_conflict"):
            store.create_engineer_work_item_fence(
                **_fence(command_digest="7" * 64),
                created_at=123.5,
            )
        with pytest.raises(CommandError, match="idempotency_fence_conflict"):
            store.create_engineer_work_item_fence(
                **_fence(idempotency_key="ecmd-" + "7" * 64),
                created_at=123.5,
            )
        with pytest.raises(CommandError, match="idempotency_fence_conflict"):
            store.create_engineer_work_item_fence(
                **_fence(
                    idempotency_key="ecmd-" + "6" * 64,
                    work_item_id="ewi_" + "6" * 32,
                    expected_revision=1,
                    step_ordinal=1,
                ),
                created_at=123.5,
            )

        second_key = "ecmd-" + "8" * 64
        store.fail_next_commit = 1
        with pytest.raises(CommandError, match="durable_write_failed"):
            store.create_engineer_work_item_fence(
                **_fence(
                    idempotency_key=second_key,
                    work_item_id="ewi_" + "8" * 32,
                    source_binding_sha256="8" * 64,
                ),
                created_at=123.5,
            )
        assert store.lookup_engineer_work_item_fence(ACTOR, second_key) is None
    finally:
        store.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor_id", " owner"),
        ("idempotency_key", "ecmd-" + "A" * 64),
        ("work_item_id", "ewi_bad"),
        ("expected_revision", True),
        ("expected_revision", 2_147_483_647),
        ("step_ordinal", 4_097),
        ("source_binding_sha256", "A" * 64),
        ("command_digest", "short"),
    ],
)
def test_fence_input_contract_is_closed(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    store = CommandJobStore(tmp_path / f"store-{field}-{str(value)[:4]}")
    try:
        with pytest.raises(CommandError, match="idempotency_fence_.*_invalid"):
            store.create_engineer_work_item_fence(**_fence(**{field: value}))
    finally:
        store.close()


def test_fence_ddl_and_projection_never_coerce_fractional_revisions(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = CommandJobStore(root)
    fractional_key = "ecmd-" + "d" * 64
    values = (
        ACTOR,
        fractional_key,
        "ewi_" + "e" * 32,
        1.5,
        1,
        SOURCE_BINDING,
        COMMAND_DIGEST,
        123.5,
    )
    statement = """INSERT INTO engineer_work_item_idempotency_fences(
                       actor_id,idempotency_key,work_item_id,expected_revision,
                       step_ordinal,source_binding_sha256,command_digest,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)"""
    try:
        with pytest.raises(sqlite3.IntegrityError), store.transaction() as conn:
            conn.execute(statement, values)
        assert store.lookup_engineer_work_item_fence(ACTOR, fractional_key) is None

        store._conn.execute("PRAGMA ignore_check_constraints=ON")
        with store.transaction() as conn:
            conn.execute(statement, values)
        store._conn.execute("PRAGMA ignore_check_constraints=OFF")
        with pytest.raises(CommandError, match="idempotency_fence_corrupt"):
            store.lookup_engineer_work_item_fence(ACTOR, fractional_key)
    finally:
        store.close()
    with pytest.raises(CommandError, match="idempotency_fence_corrupt"):
        CommandJobStore(root)


def test_source_lookup_contract_is_exact(tmp_path: Path) -> None:
    store = CommandJobStore(tmp_path / "store")
    try:
        for actor_id, source_binding in (
            (" owner", SOURCE_BINDING),
            (ACTOR, "A" * 64),
            (ACTOR, "short"),
        ):
            with pytest.raises(CommandError, match="idempotency_fence_.*_invalid"):
                store.lookup_engineer_work_item_fence_by_source(actor_id, source_binding)
    finally:
        store.close()


def test_fence_and_job_identities_are_immutable_and_reciprocal(tmp_path: Path) -> None:
    store = CommandJobStore(tmp_path / "store")
    second_key = "ecmd-" + "9" * 64
    try:
        store.create_engineer_work_item_fence(**_fence(), created_at=123.5)
        assert store._conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        for statement in (
            """UPDATE engineer_work_item_idempotency_fences
                   SET command_digest=command_digest WHERE actor_id=? AND idempotency_key=?""",
            """DELETE FROM engineer_work_item_idempotency_fences
                  WHERE actor_id=? AND idempotency_key=?""",
        ):
            with (
                pytest.raises(sqlite3.IntegrityError, match="engineer_work_item_fence_immutable"),
                store.transaction() as conn,
            ):
                conn.execute(statement, (ACTOR, KEY))

        with (
            pytest.raises(sqlite3.IntegrityError, match="engineer_work_item_fence_collision"),
            store.transaction() as conn,
        ):
            conn.execute(
                """INSERT OR REPLACE INTO engineer_work_item_idempotency_fences(
                       actor_id,idempotency_key,work_item_id,expected_revision,
                       step_ordinal,source_binding_sha256,command_digest,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    ACTOR,
                    KEY,
                    WORK_ITEM_ID,
                    7,
                    2,
                    SOURCE_BINDING,
                    "f" * 64,
                    999.5,
                ),
            )
        with (
            pytest.raises(sqlite3.IntegrityError, match="engineer_work_item_fence_collision"),
            store.transaction() as conn,
        ):
            conn.execute(
                """INSERT OR REPLACE INTO engineer_work_item_idempotency_fences(
                       actor_id,idempotency_key,work_item_id,expected_revision,
                       step_ordinal,source_binding_sha256,command_digest,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    ACTOR,
                    "ecmd-" + "e" * 64,
                    "ewi_" + "e" * 32,
                    1,
                    1,
                    SOURCE_BINDING,
                    COMMAND_DIGEST,
                    999.5,
                ),
            )
        assert store.lookup_engineer_work_item_fence(ACTOR, KEY) == _projection()
        with (
            pytest.raises(sqlite3.IntegrityError, match="engineer_work_item_fence_collision"),
            store.transaction() as conn,
        ):
            conn.execute(
                """INSERT OR REPLACE INTO engineer_work_item_idempotency_fences(
                       actor_id,idempotency_key,work_item_id,expected_revision,
                       step_ordinal,source_binding_sha256,command_digest,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    ACTOR,
                    "ecmd-" + "f" * 64,
                    WORK_ITEM_ID,
                    7,
                    2,
                    SOURCE_BINDING,
                    COMMAND_DIGEST,
                    999.5,
                ),
            )

        with pytest.raises(CommandError, match="idempotency_fenced"), store.transaction():
            store.insert_job(_job_payload(key=KEY))

        with store.transaction():
            store.insert_job(_job_payload(key=second_key, job_id="a" * 32))
        with (
            pytest.raises(sqlite3.IntegrityError, match="command_job_identity_collision"),
            store.transaction() as conn,
        ):
            conn.execute(
                "CREATE TEMP TABLE attempted_job AS SELECT * FROM jobs WHERE job_id=?",
                ("a" * 32,),
            )
            conn.execute("UPDATE attempted_job SET job_id=?", ("b" * 32,))
            conn.execute("INSERT OR REPLACE INTO jobs SELECT * FROM attempted_job")
        with (
            pytest.raises(sqlite3.IntegrityError, match="command_job_identity_collision"),
            store.transaction() as conn,
        ):
            conn.execute(
                "CREATE TEMP TABLE attempted_job AS SELECT * FROM jobs WHERE job_id=?",
                ("a" * 32,),
            )
            conn.execute(
                "UPDATE attempted_job SET actor_id=?,idempotency_key=?",
                ("different-owner", "ecmd-" + "b" * 64),
            )
            conn.execute("INSERT OR REPLACE INTO jobs SELECT * FROM attempted_job")
        assert store.lookup_idempotency(ACTOR, second_key) is not None
        with pytest.raises(CommandError, match="idempotency_conflict"):
            store.create_engineer_work_item_fence(
                **_fence(idempotency_key=second_key),
                created_at=123.5,
        )
        for column, value in (("idempotency_key", "different"), ("source_hash", "b" * 64)):
            with (
                pytest.raises(sqlite3.IntegrityError, match="command_job_identity_immutable"),
                store.transaction() as conn,
            ):
                conn.execute(
                    f"UPDATE jobs SET {column}=? WHERE job_id=?",  # nosec B608 - fixed test tuple
                    (value, "a" * 32),
                )
        with (
            pytest.raises(sqlite3.IntegrityError, match="command_job_immutable"),
            store.transaction() as conn,
        ):
            conn.execute("DELETE FROM command_job_focus WHERE job_id=?", ("a" * 32,))
            conn.execute("DELETE FROM jobs WHERE job_id=?", ("a" * 32,))

        publication_key = "ecmd-" + "c" * 64
        with store.transaction():
            store.insert_job(
                _job_payload(
                    key=publication_key,
                    job_id="c" * 32,
                    delivery_chat_id="123456789",
                )
            )
        with store.transaction() as conn:
            conn.execute(
                "UPDATE command_job_publications SET attempts=attempts+1 WHERE job_id=?",
                ("c" * 32,),
            )
        for statement in (
            """UPDATE command_job_publications SET delivery_chat_id='987654321'
                  WHERE job_id=?""",
            "DELETE FROM command_job_publications WHERE job_id=?",
        ):
            with (
                pytest.raises(sqlite3.IntegrityError, match="command_job_publication_.*immutable"),
                store.transaction() as conn,
            ):
                conn.execute(statement, ("c" * 32,))
        with (
            pytest.raises(
                sqlite3.IntegrityError,
                match="command_job_publication_identity_collision",
            ),
            store.transaction() as conn,
        ):
            conn.execute(
                """CREATE TEMP TABLE attempted_publication AS
                   SELECT * FROM command_job_publications WHERE job_id=?""",
                ("c" * 32,),
            )
            conn.execute(
                "UPDATE attempted_publication SET delivery_chat_id='987654321'",
            )
            conn.execute(
                "INSERT OR REPLACE INTO command_job_publications SELECT * FROM attempted_publication"
            )
        publication = store._conn.execute(
            "SELECT delivery_chat_id,attempts FROM command_job_publications WHERE job_id=?",
            ("c" * 32,),
        ).fetchone()
        assert tuple(publication) == ("123456789", 1)
    finally:
        store.close()


def test_pre_fence_store_is_upgraded_but_partial_or_tampered_schema_fails_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy"
    store = CommandJobStore(root)
    with store.transaction():
        store.insert_job(_job_payload(key="legacy-key"))
    store.close()

    connection = sqlite3.connect(root / "kernel.sqlite")
    try:
        rows = connection.execute(
            """SELECT name FROM sqlite_master
                WHERE type='trigger' AND name GLOB 'trg_engineer_work_item_fence_*'"""
        ).fetchall()
        for (name,) in rows:
            connection.execute(f'DROP TRIGGER "{name}"')  # nosec B608 - sqlite_master name
        connection.execute("DROP TABLE engineer_work_item_idempotency_fences")
        connection.commit()
    finally:
        connection.close()

    upgraded = CommandJobStore(root)
    try:
        assert upgraded.lookup_idempotency(ACTOR, "legacy-key") is not None
        assert upgraded.create_engineer_work_item_fence(**_fence(), created_at=123.5) == _projection()
    finally:
        upgraded.close()

    connection = sqlite3.connect(root / "kernel.sqlite")
    try:
        connection.execute(
            "DROP TRIGGER trg_engineer_work_item_fence_publication_collision_guard"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(CommandError, match="idempotency_fence_schema_invalid"):
        CommandJobStore(root)
    with sqlite3.connect(root / "kernel.sqlite") as connection:
        assert (
            connection.execute(
                """SELECT 1 FROM sqlite_master
                    WHERE name='trg_engineer_work_item_fence_publication_collision_guard'"""
            ).fetchone()
            is None
        )
