"""Transaction-time relation snapshots are complete, tenant-scoped and exact.

``as_of`` asks when a fact was valid. ``known_at`` asks which version of that
fact Friday had recorded. The order matters: first choose the latest revision at
the transaction boundary, then apply endpoint, delete and valid-time predicates.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from friday.storage.models import (
    Entity,
    EntityType,
    KnowledgeObject,
    RawObject,
    Relation,
    RelationHistorySnapshotError,
    RelationType,
    new_id,
)


def _entity(storage, user_id: str, name: str, entity_type: EntityType = EntityType.PERSON) -> str:
    storage.ensure_user(user_id)
    entity = Entity(new_id("ent"), user_id, name, entity_type)
    storage.create_entity(entity)
    return entity.id


def _relation(
    storage,
    user_id: str,
    source: str,
    target: str,
    *,
    valid_from: str = "2020-01-01",
    metadata: dict | None = None,
) -> Relation:
    relation = Relation(
        new_id("rel"),
        user_id,
        source,
        target,
        RelationType.MEMBER_OF,
        weight=0.8,
        metadata_json=metadata or {"evidence": "original"},
        valid_from=valid_from,
    )
    storage.create_relation(relation)
    return relation


def _revision_rows(storage, relation_id: str) -> list[dict]:
    return [
        dict(row)
        for row in storage.execute(
            "SELECT * FROM relation_revisions WHERE relation_id=? ORDER BY event_seq",
            (relation_id,),
        ).fetchall()
    ]


def _relation_with_dated_evidence(storage, user_id: str, suffix: str, document_date: str) -> Relation:
    source = _entity(storage, user_id, f"Иванов {suffix}")
    target = _entity(storage, user_id, f"в/ч {suffix}", EntityType.ORGANIZATION)
    raw = RawObject(new_id("raw"), user_id, "test", new_id("ref"), f"Рапорт {suffix}", "text")
    storage.store_raw_object(raw)
    document = KnowledgeObject(
        new_id("ko"),
        user_id,
        raw.id,
        content=raw.raw_content,
        title=f"Рапорт {suffix}",
        metadata_json={"document_date": document_date},
    )
    storage.store_knowledge_object(document)
    return _relation(
        storage,
        user_id,
        source,
        target,
        valid_from="",
        metadata={"evidence": {"knowledge_object_id": document.id}},
    )


def test_known_at_truth_table_chooses_revision_before_valid_time(storage):
    person = _entity(storage, "alice", "Иванов")
    unit = _entity(storage, "alice", "в/ч 12345", EntityType.ORGANIZATION)
    floor = storage.relation_history_status("alice")["known_at_floor"]

    relation = _relation(storage, "alice", person, unit, valid_from="2020-01-01")
    created_at = _revision_rows(storage, relation.id)[-1]["recorded_at"]
    storage.invalidate_relation("alice", relation.id, valid_to="2023-01-01", reason="перевод")
    invalidated_at = _revision_rows(storage, relation.id)[-1]["recorded_at"]

    # До T2 строки не было, но и выбранная сущность появилась после completeness
    # floor. Выдумывать её существование в таком snapshot нельзя: direct lookup
    # честно отказывает, а на самой границе T2 первая relation-версия уже действует.
    with pytest.raises(RelationHistorySnapshotError, match="historical identity is unavailable"):
        storage.get_entity_relations(person, "alice", as_of="2024-01-01", known_at=floor)
    before_late_knowledge = storage.get_entity_relations(
        person, "alice", as_of="2024-01-01", known_at=created_at
    )
    assert [row["id"] for row in before_late_knowledge] == [relation.id]

    # Ровно на T4 действует новая версия, без gap и без двойной строки.
    still_valid_then = storage.get_entity_relations(
        person, "alice", as_of="2022-12-31", known_at=invalidated_at
    )
    already_ended_then = storage.get_entity_relations(
        person, "alice", as_of="2023-01-01", known_at=invalidated_at
    )
    assert [row["id"] for row in still_valid_then] == [relation.id]
    assert already_ended_then == []

    # Without valid-time input, the default is what was believed active in that
    # transaction snapshot, not date(known_at). Explicit archive access still
    # exposes the ended row.
    assert len(storage.get_entity_relations(person, "alice", known_at=created_at)) == 1
    assert storage.get_entity_relations(person, "alice", known_at=invalidated_at) == []
    assert (
        len(storage.get_entity_relations(person, "alice", known_at=invalidated_at, include_invalidated=True))
        == 1
    )


def test_latest_revision_precedes_endpoint_delete_and_tombstone_filters(storage):
    person = _entity(storage, "alice", "Иванов")
    first_unit = _entity(storage, "alice", "в/ч 1", EntityType.ORGANIZATION)
    second_unit = _entity(storage, "alice", "в/ч 2", EntityType.ORGANIZATION)
    moved = _relation(storage, "alice", person, first_unit)
    before_move = _revision_rows(storage, moved.id)[-1]["recorded_at"]

    with storage.transaction() as conn:
        conn.execute(
            "UPDATE relations SET target_entity_id=? WHERE id=? AND user_id=?",
            (second_unit, moved.id, "alice"),
        )
    after_move = _revision_rows(storage, moved.id)[-1]["recorded_at"]

    assert [
        row["target_entity_id"]
        for row in storage.get_entity_relations(first_unit, "alice", known_at=before_move)
    ] == [first_unit]
    assert storage.get_entity_relations(first_unit, "alice", known_at=after_move) == []
    assert [
        row["target_entity_id"]
        for row in storage.get_entity_relations(second_unit, "alice", known_at=after_move)
    ] == [second_unit]

    with storage.transaction() as conn:
        conn.execute("UPDATE relations SET deleted_at=? WHERE id=?", (after_move, moved.id))
    after_soft_delete = _revision_rows(storage, moved.id)[-1]["recorded_at"]
    assert storage.get_entity_relations(second_unit, "alice", known_at=after_soft_delete) == []

    physical = _relation(storage, "alice", person, first_unit)
    before_delete = _revision_rows(storage, physical.id)[-1]["recorded_at"]
    with storage.transaction() as conn:
        conn.execute("DELETE FROM relations WHERE id=? AND user_id=?", (physical.id, "alice"))
    tombstone = _revision_rows(storage, physical.id)[-1]
    assert tombstone["operation"] == "delete" and tombstone["present"] == 0
    assert len(storage.get_entity_relations(first_unit, "alice", known_at=before_delete)) == 1
    assert storage.get_entity_relations(first_unit, "alice", known_at=tombstone["recorded_at"]) == []


def test_same_batch_uses_event_sequence_and_preserves_metadata_snapshot(storage):
    person = _entity(storage, "alice", "Иванов")
    unit = _entity(storage, "alice", "в/ч 1", EntityType.ORGANIZATION)
    relation = _relation(storage, "alice", person, unit)
    original_at = _revision_rows(storage, relation.id)[-1]["recorded_at"]

    with storage.transaction() as conn:
        conn.execute(
            "UPDATE relations SET weight=?, metadata_json=? WHERE id=?",
            (0.4, '{"evidence":"intermediate"}', relation.id),
        )
        conn.execute(
            "UPDATE relations SET weight=?, metadata_json=? WHERE id=?",
            (0.9, '{"evidence":"final"}', relation.id),
        )
    revisions = _revision_rows(storage, relation.id)
    assert revisions[-1]["recorded_at"] == revisions[-2]["recorded_at"]
    assert revisions[-1]["batch_id"] == revisions[-2]["batch_id"]
    assert revisions[-1]["event_seq"] > revisions[-2]["event_seq"]

    original = storage.get_entity_relations(person, "alice", known_at=original_at)[0]
    final = storage.get_entity_relations(person, "alice", known_at=revisions[-1]["recorded_at"])[0]
    assert json.loads(original["metadata_json"]) == {"evidence": "original"}
    assert final["weight"] == pytest.approx(0.9)
    assert json.loads(final["metadata_json"]) == {"evidence": "final"}


def test_known_at_validation_floor_tenant_and_current_fast_path(storage, monkeypatch):
    alice = _entity(storage, "alice", "Иванов")
    alice_unit = _entity(storage, "alice", "в/ч 1", EntityType.ORGANIZATION)
    relation = _relation(storage, "alice", alice, alice_unit)
    boundary = _revision_rows(storage, relation.id)[-1]["recorded_at"]
    _entity(storage, "bob", "Петров")

    assert storage.get_entity_relations(alice, "bob", known_at=boundary) == []
    with pytest.raises(ValueError, match="user_id"):
        storage.get_entity_relations(alice, known_at=boundary)

    observed_before_invalid = storage.execute(
        "SELECT observed_at FROM relation_revision_context WHERE singleton=1"
    ).fetchone()[0]
    floor = storage.relation_history_status("alice")["known_at_floor"]
    before_floor = (
        datetime.fromisoformat(floor.replace("Z", "+00:00")) - timedelta(microseconds=1)
    ).isoformat()
    with pytest.raises(ValueError, match="earliest boundary"):
        storage.relation_history_status("alice", before_floor)
    for invalid in ("2026-08-05", "2026-08-05T12:00:00", "not-a-time"):
        with pytest.raises(ValueError, match="RFC3339"):
            storage.relation_history_status("alice", invalid)
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="future"):
        storage.relation_history_status("alice", future)
    assert (
        storage.execute("SELECT observed_at FROM relation_revision_context WHERE singleton=1").fetchone()[0]
        == observed_before_invalid
    )

    # Offset spelling is normalized once and echoed in UTC.
    as_offset = datetime.fromisoformat(boundary.replace("Z", "+00:00")).astimezone(UTC)
    assert storage.relation_history_status("alice", as_offset.isoformat())["known_at"] == boundary

    seen_sql: list[str] = []
    original_execute = storage.execute

    def recording_execute(sql, params=None):
        seen_sql.append(sql)
        return original_execute(sql, params)

    monkeypatch.setattr(storage, "execute", recording_execute)
    assert storage.get_entity_relations(alice, "alice")
    assert all("relation_revisions" not in sql for sql in seen_sql)

    # Ownership is immutable, which makes the indexed tenant prefilter safe: no
    # later revision can move this lineage to Bob and resurrect Alice's older row.
    before_rewrite = _revision_rows(storage, relation.id)
    with (
        pytest.raises(sqlite3.IntegrityError, match="id and user_id are immutable"),
        storage.transaction() as conn,
    ):
        conn.execute("UPDATE relations SET user_id='bob' WHERE id=?", (relation.id,))
    assert _revision_rows(storage, relation.id) == before_rewrite
    assert len(storage.get_entity_relations(alice, "alice", known_at=boundary)) == 1
    assert storage.get_entity_relations(alice, "bob", known_at=boundary) == []


def test_served_empty_cutoff_survives_a_later_rewound_managed_write(storage, monkeypatch):
    """A cutoff after the event tail is a durable promise, not an empty guess."""

    person = _entity(storage, "alice", "Иванов")
    unit = _entity(storage, "alice", "в/ч 1", EntityType.ORGANIZATION)
    relation = _relation(storage, "alice", person, unit)
    tail = str(_revision_rows(storage, relation.id)[-1]["recorded_at"])
    tail_time = datetime.fromisoformat(tail.replace("Z", "+00:00"))
    cutoff_time = tail_time + timedelta(milliseconds=1)
    while datetime.now(UTC) < cutoff_time:
        pass
    cutoff = cutoff_time.isoformat(timespec="microseconds").replace("+00:00", "Z")

    assert storage.relation_history_status("alice", cutoff)["known_at"] == cutoff
    observed = storage.execute(
        "SELECT observed_at FROM relation_revision_context WHERE singleton=1"
    ).fetchone()[0]
    assert observed == cutoff
    assert storage.get_entity_relations(person, "alice", known_at=cutoff)[0]["weight"] == pytest.approx(0.8)

    class RewoundDateTime:
        @classmethod
        def now(cls, tz=None):
            return tail_time.astimezone(tz) if tz is not None else tail_time.replace(tzinfo=None)

    monkeypatch.setattr("friday.storage._core.datetime", RewoundDateTime)
    with storage.transaction() as conn:
        conn.execute("UPDATE relations SET weight=0.25 WHERE id=?", (relation.id,))

    later = str(_revision_rows(storage, relation.id)[-1]["recorded_at"])
    assert later > cutoff
    assert storage.get_entity_relations(person, "alice", known_at=cutoff)[0]["weight"] == pytest.approx(0.8)
    assert storage.get_entity_relations(person, "alice", known_at=later)[0]["weight"] == pytest.approx(0.25)


def test_uncommitted_transaction_clock_is_never_published_as_an_observed_cutoff(storage):
    before = str(
        storage.execute("SELECT observed_at FROM relation_revision_context WHERE singleton=1").fetchone()[0]
    )

    with (
        pytest.raises(RelationHistorySnapshotError, match="active transaction"),
        storage.transaction() as conn,
    ):
        active = str(
            conn.execute("SELECT observed_at FROM relation_revision_context WHERE singleton=1").fetchone()[0]
        )
        assert active > before
        storage.relation_history_status("alice", active)

    assert (
        storage.execute("SELECT observed_at FROM relation_revision_context WHERE singleton=1").fetchone()[0]
        == before
    )


@pytest.mark.parametrize("operation", ["insert", "update", "delete"])
def test_external_relation_fallback_is_strictly_after_observed_clock(storage, operation):
    person = _entity(storage, "alice", "Иванов")
    unit = _entity(storage, "alice", "в/ч 1", EntityType.ORGANIZATION)
    existing = _relation(storage, "alice", person, unit)
    synthetic_observed = (
        (datetime.now(UTC) + timedelta(days=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    storage._observe_relation_history_boundary(synthetic_observed)

    affected_id = existing.id
    if operation == "insert":
        inserted = Relation(
            new_id("rel"),
            "alice",
            person,
            unit,
            RelationType.WORKS_ON,
            weight=0.6,
            valid_from="2020-01-01",
        )
        storage.execute(
            """INSERT INTO relations(
                   id, user_id, source_entity_id, target_entity_id, relation_type,
                   weight, metadata_json, created_at, deleted_at, valid_from,
                   valid_to, invalidated_at, superseded_by
               ) VALUES(
                   :id, :user_id, :source_entity_id, :target_entity_id, :relation_type,
                   :weight, :metadata_json, :created_at, :deleted_at, :valid_from,
                   :valid_to, :invalidated_at, :superseded_by
               )""",
            inserted.to_row(),
        )
        affected_id = inserted.id
    elif operation == "update":
        storage.execute("UPDATE relations SET weight=0.4 WHERE id=?", (existing.id,))
    else:
        storage.execute("DELETE FROM relations WHERE id=?", (existing.id,))
    storage.commit()

    recorded_at = str(_revision_rows(storage, affected_id)[-1]["recorded_at"])
    assert recorded_at > synthetic_observed
    context = dict(storage.execute("SELECT * FROM relation_revision_context").fetchone())
    assert context["observed_at"] == recorded_at
    assert context["batch_id"] == context["recorded_at"] == ""


def test_graph_reads_one_historical_snapshot_and_disables_timeless_edges(storage):
    person = _entity(storage, "alice", "Иванов")
    unit = _entity(storage, "alice", "в/ч 1", EntityType.ORGANIZATION)
    raw = RawObject(new_id("raw"), "alice", "test", new_id("ref"), "Иванов, в/ч 1", "text")
    storage.store_raw_object(raw)
    document = KnowledgeObject(new_id("ko"), "alice", raw.id, content=raw.raw_content, title="Рапорт")
    storage.store_knowledge_object(document)
    storage.link_knowledge_entity("alice", document.id, person, status="accepted")
    storage.link_knowledge_entity("alice", document.id, unit, status="accepted")
    relation = _relation(storage, "alice", person, unit)
    known_before_end = _revision_rows(storage, relation.id)[-1]["recorded_at"]
    storage.invalidate_relation("alice", relation.id, valid_to="2023-01-01")
    known_after_end = _revision_rows(storage, relation.id)[-1]["recorded_at"]

    neighbourhood = storage.get_entity_graph(
        "alice", person, 1, as_of="2024-01-01", known_at=known_before_end
    )
    assert [edge["id"] for edge in neighbourhood["edges"]] == [relation.id]
    assert neighbourhood["known_at"] == known_before_end
    assert neighbourhood["history_complete"] is True
    assert neighbourhood["identity_basis"] == "current_names"

    overview = storage.graph_overview("alice", as_of="2024-01-01", known_at=known_before_end)
    assert [edge["kind"] for edge in overview["edges"]] == ["relation"]
    assert overview["known_at"] == known_before_end

    # The overview has its own bounded SQL lane, so protect the same ordering
    # independently: latest revision first, valid-time and tenant predicates next.
    assert len(storage.graph_overview("alice", as_of="2022-12-31", known_at=known_after_end)["edges"]) == 1
    assert storage.graph_overview("alice", as_of="2023-01-01", known_at=known_after_end)["edges"] == []

    # A physical DELETE tombstone is likewise filtered only after it wins the
    # window; putting `present=1` inside the window resurrects the insert revision.
    physical = _relation(storage, "alice", person, unit)
    with storage.transaction() as conn:
        conn.execute("DELETE FROM relations WHERE id=? AND user_id='alice'", (physical.id,))
    deleted_at = _revision_rows(storage, physical.id)[-1]["recorded_at"]
    assert storage.graph_overview("alice", known_at=deleted_at)["edges"] == []


def test_merge_and_unmerge_share_the_relation_batch_boundary(storage):
    source = _entity(storage, "alice", "Иванов Иван")
    target = _entity(storage, "alice", "Иванов И.")
    unit = _entity(storage, "alice", "в/ч 1", EntityType.ORGANIZATION)
    relation = _relation(storage, "alice", source, unit)
    before_merge = _revision_rows(storage, relation.id)[-1]["recorded_at"]

    merged = storage.merge_entities("alice", source, target, merged_by="alice")
    merge_row = storage.execute(
        "SELECT created_at FROM entity_merge_history WHERE id=?", (merged["_merge_id"],)
    ).fetchone()
    after_merge = _revision_rows(storage, relation.id)[-1]["recorded_at"]
    assert merge_row["created_at"] == after_merge
    with pytest.raises(ValueError, match="merge or unmerge"):
        storage.relation_history_status("alice", before_merge)
    assert storage.relation_history_status("alice", after_merge)["known_at"] == after_merge

    storage.unmerge_entities("alice", merged["_merge_id"], undone_by="alice")
    unmerge_row = storage.execute(
        "SELECT undone_at FROM entity_merge_history WHERE id=?", (merged["_merge_id"],)
    ).fetchone()
    after_unmerge = _revision_rows(storage, relation.id)[-1]["recorded_at"]
    assert unmerge_row["undone_at"] == after_unmerge
    with pytest.raises(ValueError, match="merge or unmerge"):
        storage.relation_history_status("alice", after_merge)
    assert storage.relation_history_status("alice", after_unmerge)["known_at"] == after_unmerge


@pytest.mark.parametrize("reader", ["relations", "neighbourhood", "overview"])
def test_historical_reads_fail_if_identity_changes_between_preflight_and_return(storage, monkeypatch, reader):
    person = _entity(storage, "alice", "Иванов Иван")
    duplicate = _entity(storage, "alice", "Иванов И.")
    unit = _entity(storage, "alice", "в/ч 1", EntityType.ORGANIZATION)
    relation = _relation(storage, "alice", person, unit)
    boundary = _revision_rows(storage, relation.id)[-1]["recorded_at"]

    raw = RawObject(new_id("raw"), "alice", "test", new_id("ref"), "Иванов, в/ч 1", "text")
    storage.store_raw_object(raw)
    document = KnowledgeObject(new_id("ko"), "alice", raw.id, content=raw.raw_content, title="Рапорт")
    storage.store_knowledge_object(document)
    storage.link_knowledge_entity("alice", document.id, person, status="accepted")
    storage.link_knowledge_entity("alice", document.id, unit, status="accepted")

    original_status = storage.relation_history_status
    checks = 0

    def status_with_merge(user_id: str, known_at: str = ""):
        nonlocal checks
        checks += 1
        if checks == 2:
            storage.merge_entities("alice", person, duplicate, merged_by="race-test")
        return original_status(user_id, known_at)

    monkeypatch.setattr(storage, "relation_history_status", status_with_merge)
    with pytest.raises(ValueError, match="merge or unmerge"):
        if reader == "relations":
            storage.get_entity_relations(person, "alice", known_at=boundary)
        elif reader == "neighbourhood":
            storage.get_entity_graph("alice", person, 1, known_at=boundary)
        else:
            storage.graph_overview("alice", known_at=boundary)
    assert checks == 2, "historical read returned without a final identity postflight"


def test_user_export_contains_only_that_tenants_relation_revisions(storage):
    alice = _entity(storage, "alice", "Иванов")
    alice_unit = _entity(storage, "alice", "в/ч 1", EntityType.ORGANIZATION)
    alice_relation = _relation(storage, "alice", alice, alice_unit)
    storage.invalidate_relation("alice", alice_relation.id, valid_to="2023-01-01")

    bob = _entity(storage, "bob", "Петров")
    bob_unit = _entity(storage, "bob", "в/ч 2", EntityType.ORGANIZATION)
    bob_relation = _relation(storage, "bob", bob, bob_unit)

    export = storage.export_user("alice")
    payload = json.loads(Path(export["path"]).read_text(encoding="utf-8"))
    exported_ids = [row["relation_id"] for row in payload["relation_revisions"]]
    assert exported_ids == [alice_relation.id, alice_relation.id]
    assert bob_relation.id not in exported_ids


def test_relation_date_backfill_is_one_exact_relation_history_batch(storage, settings, monkeypatch):
    import argparse

    from friday.cli import _backfill_relation_dates

    first = _relation_with_dated_evidence(storage, "alice", "1", "2024-04-01")
    second = _relation_with_dated_evidence(storage, "alice", "2", "2024-04-02")
    monkeypatch.setattr("friday.config.load_settings", lambda: settings)
    monkeypatch.setattr("friday.storage.init_storage", lambda _settings: storage)
    monkeypatch.setattr(storage, "close", lambda *args, **kwargs: None)

    assert _backfill_relation_dates(argparse.Namespace(user="alice", apply=True)) == 0

    updates = [_revision_rows(storage, relation.id)[-1] for relation in (first, second)]
    assert {row["batch_id"] for row in updates} == {updates[0]["batch_id"]}
    assert {row["recorded_at"] for row in updates} == {updates[0]["recorded_at"]}
    assert updates[0]["batch_id"].startswith("relation_batch_")
    assert [
        storage.execute("SELECT valid_from FROM relations WHERE id=?", (relation.id,)).fetchone()[0]
        for relation in (first, second)
    ] == ["2024-04-01", "2024-04-02"]


def test_relation_date_backfill_failure_rolls_back_every_row_and_revision(storage, settings, monkeypatch):
    import argparse

    from friday.cli import _backfill_relation_dates

    first = _relation_with_dated_evidence(storage, "alice", "1", "2024-04-01")
    second = _relation_with_dated_evidence(storage, "alice", "2", "2024-04-02")
    before = {relation.id: len(_revision_rows(storage, relation.id)) for relation in (first, second)}
    storage.execute("CREATE TABLE synthetic_backfill_counter(value INTEGER NOT NULL)")
    storage.execute("INSERT INTO synthetic_backfill_counter(value) VALUES(0)")
    storage.execute(
        """CREATE TRIGGER synthetic_fail_second_relation_date
           BEFORE UPDATE OF valid_from ON relations
           WHEN OLD.valid_from='' AND NEW.valid_from<>''
           BEGIN
               UPDATE synthetic_backfill_counter SET value=value+1;
               SELECT CASE WHEN (SELECT value FROM synthetic_backfill_counter)=2
                           THEN RAISE(ABORT, 'synthetic second update failure') END;
           END"""
    )
    storage.commit()
    monkeypatch.setattr("friday.config.load_settings", lambda: settings)
    monkeypatch.setattr("friday.storage.init_storage", lambda _settings: storage)
    monkeypatch.setattr(storage, "close", lambda *args, **kwargs: None)

    with pytest.raises(sqlite3.IntegrityError, match="synthetic second update failure"):
        _backfill_relation_dates(argparse.Namespace(user="alice", apply=True))

    assert [
        storage.execute("SELECT valid_from FROM relations WHERE id=?", (relation.id,)).fetchone()[0]
        for relation in (first, second)
    ] == ["", ""]
    assert {relation.id: len(_revision_rows(storage, relation.id)) for relation in (first, second)} == before
    assert storage.execute("SELECT value FROM synthetic_backfill_counter").fetchone()[0] == 0
    assert storage.list_events(event_type="graph.relation_dates_backfilled") == []
