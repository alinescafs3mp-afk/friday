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

from jericho.knowledge_graph import KnowledgeGraph
from jericho.storage.models import EntityType, KnowledgeObject, RawObject, new_id


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
