"""Слияние сущностей обязано уметь откатываться — §0.4 / #51.

Снимки «до/после» писались всегда, а хода назад не было. Главная ловушка: при
merge связи переносятся INSERT OR IGNORE, и документ, уже связанный с целью,
теряет следы источника. Честный откат требует transfer set, записанного в момент
слияния.

Мутация, которую тест обязан ловить: не писать links_suppressed (или обнулить
transfer_json). На непересекающихся документах сломанный код ещё зелёный — поэтому
пересечение обязательно.
"""

from __future__ import annotations

import json

import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import EntityType, KnowledgeObject, RawObject, RelationType, new_id


def _knowledge(storage, user_id: str, title: str) -> str:
    raw = RawObject(new_id("raw"), user_id, "test", new_id("ref"), title, "text")
    storage.store_raw_object(raw)
    ko = KnowledgeObject(new_id("ko"), user_id, raw.id, content=title, title=title)
    storage.store_knowledge_object(ko)
    return ko.id


def _link_ids(storage, user_id: str, entity_id: str) -> set[str]:
    return {
        str(link["knowledge_object_id"])
        for link in storage.list_knowledge_entity_links(user_id, entity_id=entity_id, status=None)
    }


def test_unmerge_restores_overlapping_and_exclusive_documents(storage):
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Alpha Project", EntityType.PROJECT, aliases=["A Project"])
    target = graph.create_entity("alice", "Project Alpha", EntityType.PROJECT, aliases=["Alpha"])
    person = graph.create_entity("alice", "Ivan Petrov", EntityType.PERSON)

    only_source = _knowledge(storage, "alice", "only on source")
    only_target = _knowledge(storage, "alice", "only on target")
    shared = _knowledge(storage, "alice", "on both sides")

    graph.link_knowledge_to_entity(only_source, source["id"], "alice")
    graph.link_knowledge_to_entity(shared, source["id"], "alice")
    graph.link_knowledge_to_entity(only_target, target["id"], "alice")
    graph.link_knowledge_to_entity(shared, target["id"], "alice")
    graph.create_relation("alice", source["id"], person["id"])

    before_source_links = _link_ids(storage, "alice", source["id"])
    before_target_links = _link_ids(storage, "alice", target["id"])
    assert before_source_links == {only_source, shared}
    assert before_target_links == {only_target, shared}

    merged = storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")
    merge_id = merged["_merge_id"]
    history = storage.get_merge_history(merge_id, "alice")
    assert history is not None
    transfer = json.loads(history["transfer_json"])
    assert transfer["links_moved"], "exclusive source docs must be recorded as moved"
    assert transfer["links_suppressed"], "shared docs must be recorded as suppressed"
    assert {item["knowledge_object_id"] for item in transfer["links_suppressed"]} == {shared}

    after_target = _link_ids(storage, "alice", target["id"])
    assert after_target == {only_source, only_target, shared}
    assert _link_ids(storage, "alice", source["id"]) == set()
    source_row = storage.get_entity(source["id"], "alice")
    assert source_row and source_row["merged_into_id"] == target["id"] and int(source_row["canonical"]) == 0

    undone = storage.unmerge_entities("alice", merge_id, undone_by="owner")
    assert undone["merge_id"] == merge_id

    source_after = storage.get_entity(source["id"], "alice")
    target_after = storage.get_entity(target["id"], "alice")
    assert source_after and source_after.get("merged_into_id") in (None, "")
    assert int(source_after["canonical"]) == 1
    assert source_after.get("deleted_at") in (None, "")
    assert "A Project" in (source_after.get("aliases_json") or "")
    # Target must not keep the source's name as an alias after undo.
    target_aliases = target_after.get("aliases_json") or ""
    assert "Alpha Project" not in target_aliases

    assert _link_ids(storage, "alice", source["id"]) == {only_source, shared}
    assert _link_ids(storage, "alice", target["id"]) == {only_target, shared}

    relations = storage.get_entity_relations(source["id"], "alice")
    assert any(rel["target_entity_id"] == person["id"] for rel in relations)

    history_after = storage.get_merge_history(merge_id, "alice")
    assert history_after and history_after.get("undone_at")
    assert history_after.get("undone_by") == "owner"


def test_unmerge_preserves_later_target_edits_and_reverses_only_the_merge_delta(storage):
    graph = KnowledgeGraph(storage)
    source = graph.create_entity(
        "alice", "Source Alpha", EntityType.PROJECT, aliases=["Source Alias"]
    )
    target = graph.create_entity(
        "alice",
        "Canonical Alpha",
        EntityType.PROJECT,
        aliases=["Target Before"],
        description="before description",
        metadata={"phase": "before"},
    )

    merged = storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")
    after_merge = storage.get_entity(target["id"], "alice")
    assert after_merge is not None
    merge_aliases = json.loads(after_merge["aliases_json"])
    assert {"Source Alpha", "Source Alias", "Target Before"} <= set(merge_aliases)

    # A normal PATCH sends the whole alias list.  The human removes an old target
    # alias, leaves the merge-produced aliases visible, and adds one of their own.
    edited_aliases = [alias for alias in merge_aliases if alias != "Target Before"]
    edited_aliases.append("Added Later")
    edited = graph.update_entity(
        "alice",
        target["id"],
        aliases=edited_aliases,
        description="edited after merge",
        metadata={"phase": "after", "reviewed": True},
    )
    assert edited is not None

    storage.unmerge_entities("alice", merged["_merge_id"], undone_by="owner")

    restored = storage.get_entity(target["id"], "alice")
    assert restored is not None
    aliases = json.loads(restored["aliases_json"])
    assert aliases == ["Added Later"]
    assert restored["description"] == "edited after merge"
    assert json.loads(restored["metadata_json"]) == {"phase": "after", "reviewed": True}
    assert int(restored["version"]) == int(edited["version"]) + 1

    latest_version = storage.list_entity_versions(target["id"], "alice")[0]
    snapshot = json.loads(latest_version["snapshot_json"])
    assert json.loads(snapshot["aliases_json"]) == ["Added Later"]
    assert snapshot["description"] == "edited after merge"
    assert json.loads(snapshot["metadata_json"]) == {"phase": "after", "reviewed": True}


def test_independent_merges_can_be_undone_out_of_order_without_losing_the_other_aliases(storage):
    graph = KnowledgeGraph(storage)
    first = graph.create_entity("alice", "First Source", EntityType.PROJECT, aliases=["First Bridge"])
    second = graph.create_entity(
        "alice", "Second Source", EntityType.PROJECT, aliases=["Second Bridge"]
    )
    target = graph.create_entity("alice", "Canonical", EntityType.PROJECT, aliases=["Original"])

    first_merge = storage.merge_entities("alice", first["id"], target["id"], merged_by="owner")
    second_merge = storage.merge_entities("alice", second["id"], target["id"], merged_by="owner")

    storage.unmerge_entities("alice", first_merge["_merge_id"], undone_by="owner")
    after_first_undo = json.loads(storage.get_entity(target["id"], "alice")["aliases_json"])
    assert set(after_first_undo) == {"Original", "Second Source", "Second Bridge"}

    storage.unmerge_entities("alice", second_merge["_merge_id"], undone_by="owner")
    after_both = json.loads(storage.get_entity(target["id"], "alice")["aliases_json"])
    assert after_both == ["Original"]


def test_unmerge_refuses_to_remove_an_alias_borrowed_by_another_live_merge(storage):
    graph = KnowledgeGraph(storage)
    first = graph.create_entity("alice", "First Source", EntityType.PROJECT, aliases=["Shared Bridge"])
    second = graph.create_entity(
        "alice", "Second Source", EntityType.PROJECT, aliases=["Shared Bridge"]
    )
    target = graph.create_entity("alice", "Canonical", EntityType.PROJECT)

    first_merge = storage.merge_entities("alice", first["id"], target["id"], merged_by="owner")
    second_merge = storage.merge_entities("alice", second["id"], target["id"], merged_by="owner")
    before = storage.get_entity(target["id"], "alice")
    assert before is not None

    with pytest.raises(ValueError, match="dependent merge"):
        storage.unmerge_entities("alice", first_merge["_merge_id"], undone_by="owner")

    after_refusal = storage.get_entity(target["id"], "alice")
    assert after_refusal == before, "the failed dependency check must roll back every earlier undo write"
    history = storage.get_merge_history(first_merge["_merge_id"], "alice")
    assert history and history["undone_at"] is None
    assert storage.get_entity(first["id"], "alice")["merged_into_id"] == target["id"]

    storage.unmerge_entities("alice", second_merge["_merge_id"], undone_by="owner")
    storage.unmerge_entities("alice", first_merge["_merge_id"], undone_by="owner")
    assert json.loads(storage.get_entity(target["id"], "alice")["aliases_json"]) == []


def test_merge_never_removes_a_preexisting_target_alias_equal_to_its_name(storage):
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Source", EntityType.PROJECT)
    target = graph.create_entity(
        "alice", "Canonical Alpha", EntityType.PROJECT, aliases=["CANONICAL ALPHA"]
    )

    storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")

    aliases = json.loads(storage.get_entity(target["id"], "alice")["aliases_json"])
    assert "CANONICAL ALPHA" in aliases


def test_merge_and_unmerge_preserve_the_two_times_of_a_relation(storage):
    """An ended relation must not become current merely because its entity was merged.

    The transfer set already records the full original row.  The mutation guarded
    here is narrower: both INSERT statements used to restore only the pre-temporal
    columns, silently resetting ``valid_*``/``invalidated_at``/``superseded_by``.
    """
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Проект Альфа", EntityType.PROJECT)
    target = graph.create_entity("alice", "Альфа", EntityType.PROJECT)
    person = graph.create_entity("alice", "Иван Петров", EntityType.PERSON)
    successor = graph.create_entity("alice", "Проект Бета", EntityType.PROJECT)

    old = graph.create_relation(
        "alice",
        source["id"],
        person["id"],
        RelationType.MEMBER_OF,
        valid_from="2024-01-01",
    )
    replacement = graph.create_relation(
        "alice", source["id"], successor["id"], RelationType.WORKS_ON, valid_from="2025-01-10"
    )
    graph.invalidate_relation(
        "alice",
        old.id,
        valid_to="2025-01-10",
        superseded_by=replacement.id,
        reason="перешёл в другой проект",
    )
    before = dict(storage.execute("SELECT * FROM relations WHERE id=?", (old.id,)).fetchone())
    temporal = ("valid_from", "valid_to", "invalidated_at", "superseded_by")

    merged = storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")
    after_merge = dict(storage.execute("SELECT * FROM relations WHERE id=?", (old.id,)).fetchone())
    assert {field: after_merge[field] for field in temporal} == {
        field: before[field] for field in temporal
    }
    assert old.id not in {
        relation["id"] for relation in storage.get_entity_relations(target["id"], "alice")
    }, "ended relation was resurrected in the current graph by merge"

    storage.unmerge_entities("alice", merged["_merge_id"], undone_by="owner")
    after_unmerge = dict(storage.execute("SELECT * FROM relations WHERE id=?", (old.id,)).fetchone())
    assert {field: after_unmerge[field] for field in temporal} == {
        field: before[field] for field in temporal
    }


def test_unmerge_keeps_a_relation_ended_after_the_merge(storage):
    """Undo moves endpoints back; it must not undo a later human temporal decision."""
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Проект Гамма", EntityType.PROJECT)
    target = graph.create_entity("alice", "Гамма", EntityType.PROJECT)
    person = graph.create_entity("alice", "Пётр Иванов", EntityType.PERSON)
    relation = graph.create_relation(
        "alice", source["id"], person["id"], RelationType.MEMBER_OF, valid_from="2024-01-01"
    )

    merged = storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")
    graph.invalidate_relation(
        "alice", relation.id, valid_to="2026-01-01", reason="решение после слияния"
    )
    decided = dict(storage.execute("SELECT * FROM relations WHERE id=?", (relation.id,)).fetchone())

    storage.unmerge_entities("alice", merged["_merge_id"], undone_by="owner")
    restored = dict(storage.execute("SELECT * FROM relations WHERE id=?", (relation.id,)).fetchone())

    assert restored["source_entity_id"] == source["id"]
    for field in ("valid_from", "valid_to", "invalidated_at", "superseded_by", "metadata_json"):
        assert restored[field] == decided[field], f"unmerge undid the later decision in {field}"
    assert storage.get_entity_relations(source["id"], "alice") == []


def test_merge_retargets_a_superseded_relation_that_is_suppressed(storage):
    """A replacement collapsed into an existing target edge must not leave a dangling id."""
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Проект Дельта", EntityType.PROJECT)
    target = graph.create_entity("alice", "Дельта", EntityType.PROJECT)
    person = graph.create_entity("alice", "Анна Петрова", EntityType.PERSON)
    successor = graph.create_entity("alice", "Проект Эпсилон", EntityType.PROJECT)

    ended = graph.create_relation(
        "alice", source["id"], person["id"], RelationType.MEMBER_OF, valid_from="2024-01-01"
    )
    source_replacement = graph.create_relation(
        "alice", source["id"], successor["id"], RelationType.WORKS_ON, valid_from="2025-01-10"
    )
    kept_replacement = graph.create_relation(
        "alice", target["id"], successor["id"], RelationType.WORKS_ON, valid_from="2025-01-10"
    )
    graph.invalidate_relation(
        "alice",
        ended.id,
        valid_to="2025-01-10",
        superseded_by=source_replacement.id,
    )

    merged = storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")
    after_merge = dict(storage.execute("SELECT * FROM relations WHERE id=?", (ended.id,)).fetchone())
    assert after_merge["superseded_by"] == kept_replacement.id
    assert storage.execute(
        "SELECT 1 FROM relations WHERE id=? AND user_id=?",
        (after_merge["superseded_by"], "alice"),
    ).fetchone(), "merge left superseded_by dangling"

    storage.unmerge_entities("alice", merged["_merge_id"], undone_by="owner")
    after_unmerge = dict(storage.execute("SELECT * FROM relations WHERE id=?", (ended.id,)).fetchone())
    assert after_unmerge["superseded_by"] == source_replacement.id
    assert storage.execute(
        "SELECT 1 FROM relations WHERE id=? AND user_id=?",
        (source_replacement.id, "alice"),
    ).fetchone()


def test_merge_keeps_an_ended_and_a_current_interval_of_the_same_relation(storage):
    """Duplicate suppression must not collapse two different real-world epochs."""
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Проект Зета", EntityType.PROJECT)
    target = graph.create_entity("alice", "Зета", EntityType.PROJECT)
    person = graph.create_entity("alice", "Сергей Орлов", EntityType.PERSON)

    historical = graph.create_relation(
        "alice", target["id"], person["id"], RelationType.MEMBER_OF, valid_from="2019-01-01"
    )
    graph.invalidate_relation("alice", historical.id, valid_to="2020-01-01")
    current = graph.create_relation(
        "alice", source["id"], person["id"], RelationType.MEMBER_OF, valid_from="2024-01-01"
    )

    storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")

    assert [row["id"] for row in storage.get_entity_relations(target["id"], "alice")] == [current.id]
    assert {
        row["id"]
        for row in storage.get_entity_relations(target["id"], "alice", include_invalidated=True)
    } == {historical.id, current.id}


def test_merge_temporarily_clears_a_superseded_self_loop_without_dangling(storage):
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Проект Эта", EntityType.PROJECT)
    target = graph.create_entity("alice", "Эта", EntityType.PROJECT)
    person = graph.create_entity("alice", "Олег Серов", EntityType.PERSON)
    ended = graph.create_relation(
        "alice", source["id"], person["id"], RelationType.MEMBER_OF, valid_from="2024-01-01"
    )
    replacement = graph.create_relation(
        "alice", source["id"], target["id"], RelationType.RELATED_TO, valid_from="2025-01-01"
    )
    graph.invalidate_relation(
        "alice", ended.id, valid_to="2025-01-01", superseded_by=replacement.id
    )

    merged = storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")
    after_merge = storage.execute(
        "SELECT superseded_by FROM relations WHERE id=?", (ended.id,)
    ).fetchone()
    assert after_merge["superseded_by"] is None
    assert not storage.execute("SELECT 1 FROM relations WHERE id=?", (replacement.id,)).fetchone()

    storage.unmerge_entities("alice", merged["_merge_id"], undone_by="owner")
    after_unmerge = storage.execute(
        "SELECT superseded_by FROM relations WHERE id=?", (ended.id,)
    ).fetchone()
    assert after_unmerge["superseded_by"] == replacement.id
    assert storage.execute("SELECT 1 FROM relations WHERE id=?", (replacement.id,)).fetchone()


def test_unmerge_refuses_a_second_undo(storage):
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Left", EntityType.CONCEPT)
    target = graph.create_entity("alice", "Right", EntityType.CONCEPT)
    ko = _knowledge(storage, "alice", "doc")
    graph.link_knowledge_to_entity(ko, source["id"], "alice")
    merged = storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")
    storage.unmerge_entities("alice", merged["_merge_id"], undone_by="owner")
    try:
        storage.unmerge_entities("alice", merged["_merge_id"], undone_by="owner")
    except ValueError as exc:
        assert (
            "already" in str(exc).casefold()
            or "уже" in str(exc).casefold()
            or "undone" in str(exc).casefold()
        )
    else:
        raise AssertionError("second undo must fail")


def test_mutation_clearing_transfer_breaks_overlap_restore(storage):
    """Мутация: обнулить links_suppressed в истории → shared не вернётся на source.

    Это и есть доказательство, что запись пересечения обязательна: тест с одними
    exclusive-документами на такой мутации остался бы зелёным.
    """
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Src Name", EntityType.PROJECT)
    target = graph.create_entity("alice", "Tgt Name", EntityType.PROJECT)
    only_source = _knowledge(storage, "alice", "src only")
    shared = _knowledge(storage, "alice", "shared")
    graph.link_knowledge_to_entity(only_source, source["id"], "alice")
    graph.link_knowledge_to_entity(shared, source["id"], "alice")
    graph.link_knowledge_to_entity(shared, target["id"], "alice")

    merged = storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")
    merge_id = merged["_merge_id"]
    row = storage.get_merge_history(merge_id, "alice")
    transfer = json.loads(row["transfer_json"])
    transfer["links_suppressed"] = []
    storage.execute(
        "UPDATE entity_merge_history SET transfer_json=? WHERE id=?",
        (json.dumps(transfer, ensure_ascii=False), merge_id),
    )

    storage.unmerge_entities("alice", merge_id, undone_by="owner")
    restored = _link_ids(storage, "alice", source["id"])
    assert shared not in restored
    assert only_source in restored


def test_undo_returns_the_pair_to_the_review_queue(storage):
    """Откат возвращает пару на разбор, а не хоронит её.

    Слияние переводит все задетые кандидатуры в 'merged', а откат их не трогал —
    строка оставалась решённой навсегда. Повторно предложить ту же пару очередь
    не могла: `store_resolution_candidate` по правилу «решённое человеком
    durable» отдаёт существующую строку не изменяя её. А прямого «слей вот эти
    две» в системе нет вовсе — все поверхности (HTTP, админка, инструмент
    агента, кнопка в Telegram) идут через кандидатуру. То есть человек, нажавший
    «слить», а затем «откатить», терял возможность слить их когда-либо снова.

    Мутация: убрать возврат `closed_candidates` в `unmerge_entities` — тест
    обязан покраснеть.
    """
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Дубликат Один", EntityType.PROJECT)
    target = graph.create_entity("alice", "Дубликат Два", EntityType.PROJECT)
    document = _knowledge(storage, "alice", "общий документ")
    graph.link_knowledge_to_entity(document, source["id"], "alice")
    graph.link_knowledge_to_entity(document, target["id"], "alice")

    from friday.storage.models import EntityResolutionCandidate, new_id

    candidate = storage.store_resolution_candidate(
        EntityResolutionCandidate(
            id=new_id("res"),
            user_id="alice",
            entity_a_id=source["id"],
            entity_b_id=target["id"],
            confidence=0.9,
            resolution_method="test",
        )
    )

    merged = storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")
    after_merge = storage.get_resolution_candidate(candidate.id, "alice")
    assert str(after_merge["status"]) == "merged", "стенд не воспроизводит: пара не закрылась слиянием"

    storage.unmerge_entities("alice", merged["_merge_id"], undone_by="owner")

    after_undo = storage.get_resolution_candidate(candidate.id, "alice")
    assert str(after_undo["status"]) == "suggested", (
        "пара осталась решённой: слить её заново уже нечем — прямого пути к merge в системе нет"
    )
    assert after_undo.get("resolved_at") in (None, ""), "решение о паре не снято"
