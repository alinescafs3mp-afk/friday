"""Schema-45 ingress identity migration and graph CAS foundation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from friday.interaction_control_plane.compare_current_file_web_work_graph import (
    COMPARE_CURRENT_FILE_WEB_UNBOUND_SCHEMA44_REQUEST_SHA256,
    COMPARE_CURRENT_FILE_WEB_WORK_GRAPH_SCHEMA,
    FILE_READ_STEP_ID,
    CompareCurrentFileWebStepKind,
    CompareCurrentFileWebStepState,
    CompareCurrentFileWebWorkGraph,
)
from friday.interaction_control_plane.compare_current_file_web_work_graph_store import (
    CompareCurrentFileWebGraphConflictError,
    CompareCurrentFileWebRestartRebind,
    claim_compare_current_file_web_step_in_transaction,
    create_compare_current_file_web_work_graph_in_transaction,
    get_compare_current_file_web_work_graph_in_transaction,
    rebind_compare_current_file_web_work_graph_after_restart_in_transaction,
)
from friday.interaction_control_plane.engineer_work_item import (
    EngineerWorkItemChannel,
    create_engineer_work_item_in_transaction,
)
from friday.interaction_control_plane.engineer_work_item_schema import (
    ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256,
)
from friday.interaction_control_plane.work_item_schema import (
    _WORK_ITEM_SCHEMA_42,
    _WORK_ITEM_SCHEMA_44_EXTENSION,
    _canonical_schema_42_objects,
    _canonical_schema_44_objects,
    _canonical_work_item_schema_objects,
    _drop_legacy_schema_objects,
    _execute_schema,
    _schema_objects,
    register_work_item_connection_functions,
    validate_work_item_schema,
)
from friday.storage import SCHEMA_VERSION, FridayStorage
from friday.storage.models import RawObject, new_id


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build_bound_graph(storage: FridayStorage, label: str) -> CompareCurrentFileWebWorkGraph:
    owner = "local:schema45-owner"
    storage.ensure_user(owner, source="schema45-test")
    conversation = storage.create_conversation(owner, f"schema45 {label}")
    anchor = storage.store_message(
        str(conversation["id"]),
        owner,
        "user",
        f"synthetic schema45 request {label}",
    )
    raw_id = new_id("raw")
    storage.store_raw_object(
        RawObject(
            id=raw_id,
            user_id=owner,
            source="upload",
            source_ref=f"sha256:{_sha256(f'source:{label}')}",
            raw_content=f"synthetic schema45 file {label}",
            content_type="text/plain",
            content_hash=_sha256(f"content:{label}"),
        )
    )
    kinds = tuple(CompareCurrentFileWebStepKind)
    graph = CompareCurrentFileWebWorkGraph.admitted(
        user_id=owner,
        conversation_id=str(conversation["id"]),
        anchor_user_message_id=str(anchor["id"]),
        anchor_request_binding_sha256=_sha256(f"request-binding:{label}"),
        current_file_raw_object_id=raw_id,
        proposal_sha256=_sha256(f"proposal:{label}"),
        accepted_plan_sha256=_sha256(f"plan:{label}"),
        manifest_sha256=_sha256(f"manifest:{label}"),
        policy_sha256=_sha256(f"policy:{label}"),
        runtime_profile_sha256=_sha256(f"runtime:{label}"),
        adapter_registry_sha256=_sha256(f"adapters:{label}"),
        actor_binding_sha256=_sha256(f"actor:{owner}"),
        conversation_binding_sha256=_sha256(f"conversation:{conversation['id']}"),
        current_file_source_identity_sha256=_sha256(f"source-identity:{raw_id}"),
        current_file_content_sha256=_sha256(f"file-content:{label}"),
        step_input_identities={kind: _sha256(f"input:{label}:{kind.value}") for kind in kinds},
        step_idempotency_keys={kind: _sha256(f"idempotency:{label}:{kind.value}") for kind in kinds},
        now="2026-08-26T10:00:00+00:00",
        expires_at="2026-08-26T22:00:00+00:00",
    )
    return graph


def _seed_bound_graph(storage: FridayStorage, label: str) -> CompareCurrentFileWebWorkGraph:
    graph = _build_bound_graph(storage, label)
    with storage.transaction() as conn:
        return create_compare_current_file_web_work_graph_in_transaction(conn, graph)


def _copy_matching_columns(
    conn: sqlite3.Connection,
    *,
    destination: str,
    source: str,
) -> None:
    columns = tuple(
        str(row[1])
        for row in conn.execute(f'PRAGMA table_info("{destination}")')  # nosec B608
    )
    projected = ",".join(f'"{column}"' for column in columns)
    conn.execute(
        f'INSERT INTO "{destination}"({projected}) SELECT {projected} FROM "{source}"'  # nosec B608
    )


def _drop_document_passage_schema(conn: sqlite3.Connection) -> None:
    conversation_triggers = tuple(
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
                WHERE type='trigger' AND name LIKE 'conversation_passage_%'
                ORDER BY name"""
        )
    )
    for name in conversation_triggers:
        conn.execute(f'DROP TRIGGER "{name}"')  # nosec B608 - SQLite-owned names
    conn.execute("DROP TABLE IF EXISTS conversation_passages_fts")
    conn.execute("DROP VIEW IF EXISTS conversation_passage_search_content")
    conn.execute("DROP TABLE IF EXISTS conversation_passages")
    conn.execute("DROP TABLE IF EXISTS conversation_passage_projections")
    trigger_names = tuple(
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
                WHERE type='trigger' AND name LIKE 'document_passage_%'
                ORDER BY name"""
        )
    )
    for name in trigger_names:
        conn.execute(f'DROP TRIGGER "{name}"')  # nosec B608 - SQLite-owned names
    conn.execute("DROP TABLE IF EXISTS document_passages")
    conn.execute("DROP TABLE IF EXISTS document_passage_projections")


def _downgrade_graph_projection_to_released_schema44(database: Path) -> None:
    """Test-only inverse rebuild whose result must equal the released DDL."""

    conn = sqlite3.connect(database)
    try:
        register_work_item_connection_functions(conn)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        _drop_document_passage_schema(conn)
        # Build an actual released schema-44 image; the independent schema-46
        # EngineerWorkItem package must not leak through this test-only inverse.
        engineer_triggers = tuple(
            str(row[0])
            for row in conn.execute(
                """SELECT name FROM sqlite_master
                    WHERE type='trigger' AND name LIKE 'trg_engineer_work_item_%'
                    ORDER BY name"""
            )
        )
        for trigger in engineer_triggers:
            conn.execute(f'DROP TRIGGER "{trigger}"')  # nosec B608 - schema-owned test names
        conn.execute("DROP TABLE engineer_work_item_command_fences")
        conn.execute("DROP TABLE engineer_work_item_steps")
        conn.execute("DROP TABLE engineer_work_items")
        schema42 = _canonical_schema_42_objects()
        current = _canonical_work_item_schema_objects()
        current_extension = {key: value for key, value in current.items() if key not in schema42}
        changed_schema42_objects = {
            key: value for key, value in current.items() if key in schema42 and value != schema42[key]
        }
        _drop_legacy_schema_objects(conn, current_extension)
        _drop_legacy_schema_objects(conn, changed_schema42_objects)
        _execute_schema(conn, _WORK_ITEM_SCHEMA_42)
        conn.execute("DROP TABLE work_item_compare_current_file_web_restart_rebind_steps")
        conn.execute("DROP TABLE work_item_compare_current_file_web_restart_rebinds")
        conn.execute(
            "ALTER TABLE work_item_compare_current_file_web_steps "
            "RENAME TO work_item_compare_current_file_web_steps_schema45"
        )
        conn.execute(
            "ALTER TABLE work_item_compare_current_file_web_graphs "
            "RENAME TO work_item_compare_current_file_web_graphs_schema45"
        )
        _execute_schema(conn, _WORK_ITEM_SCHEMA_44_EXTENSION)
        _copy_matching_columns(
            conn,
            destination="work_item_compare_current_file_web_graphs",
            source="work_item_compare_current_file_web_graphs_schema45",
        )
        _copy_matching_columns(
            conn,
            destination="work_item_compare_current_file_web_steps",
            source="work_item_compare_current_file_web_steps_schema45",
        )
        conn.execute("DROP TABLE work_item_compare_current_file_web_steps_schema45")
        conn.execute("DROP TABLE work_item_compare_current_file_web_graphs_schema45")
        conn.execute("UPDATE schema_meta SET value='44' WHERE key='schema_version'")
        conn.execute("UPDATE schema_meta SET value='44' WHERE key='fts_build'")
        assert _schema_objects(conn, current=True) == _canonical_schema_44_objects()
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def test_schema45_exact_binding_is_durable_immutable_and_revision_cas(storage) -> None:
    graph = _seed_bound_graph(storage, "exact")

    assert SCHEMA_VERSION == 49
    assert COMPARE_CURRENT_FILE_WEB_WORK_GRAPH_SCHEMA.endswith(".v3")
    assert graph.has_exact_request_binding is True
    assert graph.payload()["anchor_request_binding_sha256"] == graph.anchor_request_binding_sha256
    validate_work_item_schema(storage.conn)

    with (
        pytest.raises(sqlite3.IntegrityError, match="current-file/web WorkGraph"),
        storage.transaction() as conn,
    ):
        conn.execute(
            "UPDATE work_item_compare_current_file_web_graphs SET anchor_request_binding_sha256=? WHERE id=?",
            (_sha256("different-request"), graph.id),
        )

    with storage.transaction() as conn:
        claimed = claim_compare_current_file_web_step_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            step_id=FILE_READ_STEP_ID,
            now="2026-08-26T10:00:01+00:00",
        )
    assert claimed.revision == graph.revision + 1
    with (
        pytest.raises(CompareCurrentFileWebGraphConflictError, match="revision"),
        storage.transaction() as conn,
    ):
        claim_compare_current_file_web_step_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            step_id=FILE_READ_STEP_ID,
            now="2026-08-26T10:00:02+00:00",
        )


def test_engineer_open_scope_reciprocally_blocks_work_graph_admission(storage) -> None:
    graph = _build_bound_graph(storage, "engineer-exclusive")
    with storage.transaction() as conn:
        create_engineer_work_item_in_transaction(
            conn,
            owner_id=graph.user_id,
            tenant_id=graph.user_id,
            conversation_id=graph.conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
            source_binding_sha256="a" * 64,
            completion_contract_sha256=ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256,
            idempotency_key="ecmd-" + "b" * 64,
            command_digest="c" * 64,
            now="2026-08-26T10:00:00+00:00",
            expires_at="2026-08-26T22:00:00+00:00",
        )
    with pytest.raises(CompareCurrentFileWebGraphConflictError), storage.transaction() as conn:
        create_compare_current_file_web_work_graph_in_transaction(conn, graph)


def test_schema45_restart_rebind_is_one_shot_atomic_and_predecessor_audited(storage) -> None:
    graph = _seed_bound_graph(storage, "restart-rebind")
    with storage.transaction() as conn:
        graph = claim_compare_current_file_web_step_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            step_id=FILE_READ_STEP_ID,
            now="2026-08-26T10:00:01+00:00",
        )
    predecessor_plan = graph.accepted_plan_sha256
    predecessor_file = graph.step(FILE_READ_STEP_ID)
    kinds = tuple(CompareCurrentFileWebStepKind)
    rebind = CompareCurrentFileWebRestartRebind(
        proposal_sha256=_sha256("restart:proposal"),
        accepted_plan_sha256=_sha256("restart:plan"),
        manifest_sha256=_sha256("restart:manifest"),
        policy_sha256=_sha256("restart:policy"),
        runtime_profile_sha256=_sha256("restart:runtime"),
        adapter_registry_sha256=_sha256("restart:registry"),
        actor_binding_sha256=_sha256("restart:actor"),
        conversation_binding_sha256=_sha256("restart:conversation"),
        step_input_identities={kind: _sha256(f"restart:input:{kind.value}") for kind in kinds},
        step_idempotency_keys={kind: _sha256(f"restart:key:{kind.value}") for kind in kinds},
    )
    with storage.transaction() as conn:
        rebound = rebind_compare_current_file_web_work_graph_after_restart_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            rebind=rebind,
            now="2026-08-26T10:00:02+00:00",
        )

    assert rebound.revision == graph.revision + 1
    assert rebound.accepted_plan_sha256 == rebind.accepted_plan_sha256
    assert all(step.state is CompareCurrentFileWebStepState.PENDING for step in rebound.steps)
    assert rebound.step(FILE_READ_STEP_ID).attempt == predecessor_file.attempt == 1
    assert rebound.step(FILE_READ_STEP_ID).started_at is None
    assert all(
        step.input_identity_sha256 == rebind.step_input_identities[step.kind]
        and step.idempotency_key_sha256 == rebind.step_idempotency_keys[step.kind]
        for step in rebound.steps
    )
    audit = storage.execute(
        "SELECT * FROM work_item_compare_current_file_web_restart_rebinds WHERE graph_id=?",
        (graph.id,),
    ).fetchone()
    assert audit is not None
    assert audit["from_revision"] == graph.revision
    assert audit["to_revision"] == rebound.revision
    assert audit["predecessor_accepted_plan_sha256"] == predecessor_plan
    assert audit["successor_accepted_plan_sha256"] == rebind.accepted_plan_sha256
    histories = storage.execute(
        "SELECT * FROM work_item_compare_current_file_web_restart_rebind_steps "
        "WHERE graph_id=? ORDER BY step_id",
        (graph.id,),
    ).fetchall()
    assert len(histories) == 3
    file_history = next(row for row in histories if row["step_id"] == FILE_READ_STEP_ID)
    assert file_history["predecessor_state"] == "running"
    assert file_history["predecessor_attempt"] == 1
    assert file_history["predecessor_started_at"] == predecessor_file.started_at
    assert (
        file_history["successor_input_identity_sha256"]
        == (rebind.step_input_identities[CompareCurrentFileWebStepKind.FILE_READ])
    )
    validate_work_item_schema(storage.conn)
    exported = storage.export_user(graph.user_id)
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    assert len(payload["work_item_compare_current_file_web_restart_rebinds"]) == 1
    assert len(payload["work_item_compare_current_file_web_restart_rebind_steps"]) == 3
    assert "synthetic schema45 file" not in json.dumps(
        payload["work_item_compare_current_file_web_restart_rebind_steps"],
        sort_keys=True,
    )

    with (
        pytest.raises(CompareCurrentFileWebGraphConflictError, match="one-shot|CAS"),
        storage.transaction() as conn,
    ):
        rebind_compare_current_file_web_work_graph_after_restart_in_transaction(
            conn,
            graph_id=rebound.id,
            user_id=rebound.user_id,
            conversation_id=rebound.conversation_id,
            expected_revision=rebound.revision,
            rebind=replace(
                rebind,
                actor_binding_sha256=_sha256("restart:actor:second"),
                conversation_binding_sha256=_sha256("restart:conversation:second"),
            ),
            now="2026-08-26T10:00:03+00:00",
        )

    with pytest.raises(sqlite3.IntegrityError, match="immutable"), storage.transaction() as conn:
        conn.execute(
            "UPDATE work_item_compare_current_file_web_restart_rebinds "
            "SET predecessor_accepted_plan_sha256=? WHERE graph_id=?",
            (_sha256("forged-predecessor"), graph.id),
        )


def test_released_schema44_graph_migrates_to_explicit_unbound_sentinel_and_reads(
    settings,
    tmp_path: Path,
) -> None:
    database = tmp_path / "released-schema44.sqlite3"
    configured = replace(settings, database_path=database)
    storage = FridayStorage(configured)
    try:
        original = _seed_bound_graph(storage, "released-schema44")
    finally:
        storage.close()
    _downgrade_graph_projection_to_released_schema44(database)

    migrated = FridayStorage(replace(configured, database_must_exist=True))
    try:
        assert (
            migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "49"
        )
        with migrated.transaction() as conn:
            graph = get_compare_current_file_web_work_graph_in_transaction(
                conn,
                graph_id=original.id,
                user_id=original.user_id,
                conversation_id=original.conversation_id,
            )
        assert graph is not None
        assert graph.anchor_request_binding_sha256 == (
            COMPARE_CURRENT_FILE_WEB_UNBOUND_SCHEMA44_REQUEST_SHA256
        )
        assert graph.has_exact_request_binding is False
        assert tuple(step.step_id for step in graph.steps) == tuple(step.step_id for step in original.steps)

        with migrated.transaction() as conn:
            claimed = claim_compare_current_file_web_step_in_transaction(
                conn,
                graph_id=graph.id,
                user_id=graph.user_id,
                conversation_id=graph.conversation_id,
                expected_revision=graph.revision,
                step_id=FILE_READ_STEP_ID,
                now="2026-08-26T10:00:01+00:00",
            )
        assert claimed.revision == graph.revision + 1
        assert claimed.has_exact_request_binding is False
        validate_work_item_schema(migrated.conn)
        assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migrated.close()


def test_schema44_upgrade_rejects_counterfeit_graph_ddl(settings, tmp_path: Path) -> None:
    database = tmp_path / "counterfeit-schema44.sqlite3"
    configured = replace(settings, database_path=database)
    storage = FridayStorage(configured)
    try:
        _seed_bound_graph(storage, "counterfeit")
    finally:
        storage.close()
    _downgrade_graph_projection_to_released_schema44(database)
    conn = sqlite3.connect(database)
    try:
        conn.execute("DROP TRIGGER trg_work_item_compare_current_file_web_graphs_identity_immutable")
        conn.commit()
    finally:
        conn.close()

    counterfeit = FridayStorage(replace(configured, database_must_exist=True))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="incomplete or altered"):
            _ = counterfeit.conn
    finally:
        counterfeit.close()
