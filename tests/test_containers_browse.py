"""Containers and browse-by-tag/entity — the §13 organization layer.

"Deeply organize" previously had no UX: tags were write-only (never listable
or filterable), knowledge could not be browsed by entity, and PART_OF
hierarchies never arose. These tests pin the tag aggregation/filter (Unicode
case-insensitive — SQLite's lower() folds ASCII only), accepted-links-only
entity browse, container entities with PART_OF hierarchies, the HTTP surface
(including /api/knowledge/tags not being shadowed by the {id} route), and the
signed-bridge regression for query-bearing GET paths.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from jericho.knowledge_graph import CONTAINER_ENTITY_TYPES, KnowledgeGraph
from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.server import create_app
from jericho.storage.models import EntityType, KnowledgeObject, RawObject, new_id


def _tagged_ko(storage, user_id: str, content: str, tags: list[str]) -> dict:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title=content[:40],
        summary=content,
        tags_json=tags,
    )
    storage.store_knowledge_object(ko)
    return storage.get_knowledge_object(ko.id, user_id) or {}


# --- storage: tags --------------------------------------------------------


def test_list_knowledge_tags_counts_casefold_and_excludes_deleted(storage):
    first = _tagged_ko(storage, "alice", "Заметка о питоне", ["Python", "Идеи"])
    _tagged_ko(storage, "alice", "Вторая заметка", ["python", "дом"])
    removed = _tagged_ko(storage, "alice", "Старая заметка", ["идеи", "python"])
    storage.soft_delete_knowledge_object(removed["id"], "alice")
    _tagged_ko(storage, "bob", "Чужая заметка", ["python"])

    tags = storage.list_knowledge_tags("alice")
    by_name = {item["tag"].casefold(): item["count"] for item in tags}
    # "Python"/"python" fold together; the deleted object and other users don't count.
    assert by_name == {"python": 2, "идеи": 1, "дом": 1}
    assert tags[0]["tag"].casefold() == "python"  # count DESC ordering

    assert first["id"] in {item["id"] for item in storage.list_knowledge_objects("alice", tag="ИДЕИ")}
    assert storage.list_knowledge_objects("alice", tag="нет-такого") == []


# --- storage: browse-by-entity -------------------------------------------


def test_list_knowledge_objects_by_entity_counts_accepted_links_only(storage):
    graph = KnowledgeGraph(storage)
    project = graph.create_entity("alice", "Ремонт", EntityType.PROJECT)
    accepted = _tagged_ko(storage, "alice", "Смета на ремонт", ["дом"])
    suggested = _tagged_ko(storage, "alice", "Черновик", [])
    graph.link_knowledge_to_entity(
        accepted["id"], project["id"], "alice", status="accepted", reviewed_by="alice"
    )
    graph.link_knowledge_to_entity(suggested["id"], project["id"], "alice", status="suggested")

    ids = {item["id"] for item in storage.list_knowledge_objects("alice", entity_id=project["id"])}
    assert ids == {accepted["id"]}


# --- KG: containers -------------------------------------------------------


def test_create_container_validates_kind_parent_and_builds_part_of(storage):
    graph = KnowledgeGraph(storage)
    root = graph.create_container("alice", "Дом", kind="project")
    child = graph.create_container("alice", "Ремонт кухни", kind="collection", parent_id=root["id"])
    person = graph.create_entity("alice", "Ivan", EntityType.PERSON)

    assert root["entity_type"] in CONTAINER_ENTITY_TYPES
    with pytest.raises(ValueError, match="kind"):
        graph.create_container("alice", "X", kind="person")
    with pytest.raises(ValueError, match="Parent"):
        graph.create_container("alice", "Y", parent_id=person["id"])
    with pytest.raises(ValueError, match="itself"):
        # Same name+kind dedups to the existing entity, making parent==self.
        graph.create_container("alice", "Дом", kind="project", parent_id=root["id"])

    containers = graph.list_containers("alice")
    by_id = {item["id"]: item for item in containers}
    assert by_id[root["id"]]["parent_id"] is None
    assert by_id[child["id"]]["parent_id"] == root["id"]
    # The hierarchy exists as a real PART_OF relation in the graph.
    edges = storage.list_part_of_relations("alice")
    assert {(edge["source_entity_id"], edge["target_entity_id"]) for edge in edges} == {
        (child["id"], root["id"])
    }


def test_container_knowledge_count_reflects_accepted_members(storage):
    graph = KnowledgeGraph(storage)
    box = graph.create_container("alice", "Идеи", kind="collection")
    one = _tagged_ko(storage, "alice", "Идея один", [])
    two = _tagged_ko(storage, "alice", "Идея два", [])
    draft = _tagged_ko(storage, "alice", "Черновик", [])
    graph.link_knowledge_to_entity(one["id"], box["id"], "alice", status="accepted", reviewed_by="alice")
    graph.link_knowledge_to_entity(two["id"], box["id"], "alice", status="accepted", reviewed_by="alice")
    graph.link_knowledge_to_entity(draft["id"], box["id"], "alice", status="suggested")

    containers = {item["id"]: item for item in graph.list_containers("alice")}
    assert containers[box["id"]]["knowledge_count"] == 2


# --- HTTP surface ---------------------------------------------------------


def test_http_tags_containers_and_filters(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        storage = app.state.storage
        _tagged_ko(storage, LEGACY_OWNER_USER_ID, "Первая", ["Идеи", "python"])
        _tagged_ko(storage, LEGACY_OWNER_USER_ID, "Вторая", ["идеи"])

        # /api/knowledge/tags is a real route, not a knowledge id.
        tags = client.get("/api/knowledge/tags", headers=owner)
        assert tags.status_code == 200
        assert {item["tag"].casefold(): item["count"] for item in tags.json()["items"]} == {
            "идеи": 2,
            "python": 1,
        }

        filtered = client.get("/api/knowledge", params={"tag": "ИДЕИ"}, headers=owner)
        assert filtered.status_code == 200
        assert filtered.json()["count"] == 2

        created = client.post("/api/kg/containers", json={"name": "Дом", "kind": "project"}, headers=owner)
        assert created.status_code == 200
        root_id = created.json()["container"]["id"]
        child = client.post(
            "/api/kg/containers",
            json={"name": "Ремонт", "kind": "collection", "parent_id": root_id},
            headers=owner,
        )
        assert child.status_code == 200
        assert (
            client.post("/api/kg/containers", json={"name": "Z", "kind": "person"}, headers=owner).status_code
            == 400
        )

        listed = client.get("/api/kg/containers", headers=owner)
        assert listed.status_code == 200
        parents = {item["name"]: item["parent_id"] for item in listed.json()["items"]}
        assert parents["Дом"] is None
        assert parents["Ремонт"] == root_id

        # Entity name lookup for browse surfaces.
        found = client.get("/api/kg/entities", params={"q": "Дом"}, headers=owner)
        assert found.status_code == 200
        assert any(item["id"] == root_id for item in found.json()["items"])


def test_admin_tags_containers_and_entity_filter(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        storage = app.state.storage
        storage.ensure_user("local:kate", source="test", display_name="Kate")
        ko = _tagged_ko(storage, "local:kate", "Запись Кати", ["сад"])
        box = app.state.kg.create_container("local:kate", "Дача", kind="project")
        app.state.kg.link_knowledge_to_entity(
            ko["id"], box["id"], "local:kate", status="accepted", reviewed_by="local:kate"
        )

        tags = client.get("/api/admin/knowledge/tags", params={"user_id": "local:kate"}, headers=owner)
        assert tags.status_code == 200
        assert tags.json()["items"] == [{"tag": "сад", "count": 1}]

        containers = client.get("/api/admin/containers", params={"user_id": "local:kate"}, headers=owner)
        assert containers.status_code == 200
        assert containers.json()["items"][0]["knowledge_count"] == 1

        by_entity = client.get(
            "/api/admin/knowledge",
            params={"user_id": "local:kate", "entity_id": box["id"]},
            headers=owner,
        )
        assert by_entity.status_code == 200
        assert [item["id"] for item in by_entity.json()["items"]] == [ko["id"]]

        made = client.post(
            "/api/admin/containers",
            json={"user_id": "local:kate", "name": "Урожай", "kind": "collection"},
            headers=owner,
        )
        assert made.status_code == 200
        assert made.json()["container"]["entity_type"] == "collection"


# --- signed bridge + query strings ---------------------------------------


def test_bridge_signature_covers_query_string(settings):
    """The bridge signs path?query verbatim; verification must accept it."""
    import time
    import uuid

    from jericho.security import sign_bridge_request

    app = create_app(settings)
    with TestClient(app) as client:

        def signed_get(path: str):
            timestamp = int(time.time())
            nonce = uuid.uuid4().hex
            return client.get(
                path,
                headers={
                    "X-Jericho-Timestamp": str(timestamp),
                    "X-Jericho-User": "1001",
                    "X-Jericho-Chat": "5001",
                    "X-Jericho-Nonce": nonce,
                    "X-Jericho-Signature": sign_bridge_request(
                        settings.telegram_bridge_secret,
                        timestamp=timestamp,
                        method="GET",
                        path=path,
                        external_user_id="1001",
                        chat_id="5001",
                        nonce=nonce,
                        body=b"",
                    ),
                },
            )

        assert signed_get("/api/knowledge/tags").status_code == 200
        # Query-bearing paths previously could never authenticate: the bridge
        # signed "/x?limit=25" while the server verified only "/x".
        assert signed_get("/api/knowledge/tags?limit=25").status_code == 200
        assert signed_get("/api/knowledge?tag=%D0%B8%D0%B4%D0%B5%D0%B8&limit=8").status_code == 200


# --- the graph layer stops loading rows it never reads --------------------


def _make_knowledge(storage, user_id: str, text: str):
    from jericho.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user(user_id)
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        title=text[:50],
        summary=text[:120],
    )
    storage.store_knowledge_object(ko)
    return raw, ko


def test_entity_counts_do_not_materialise_the_rows_they_count(storage, monkeypatch):
    """`search_entities` produced two numbers by loading everything behind them.

    `_knowledge_count` came from `len(get_entity_knowledge(..., limit=1000))` — up to
    a thousand full Knowledge Objects, bodies included — and `_relation_count` from
    every relation with both endpoint names joined in. Per returned entity, per
    query.
    """
    from jericho.knowledge_graph import KnowledgeGraph
    from jericho.storage.models import EntityType

    graph = KnowledgeGraph(storage)
    storage.ensure_user("owner")
    entity = graph.create_entity("owner", "Проект Орион", EntityType.PROJECT)
    for index in range(30):
        raw, ko = _make_knowledge(storage, "owner", f"заметка {index} про Орион")
        graph.link_knowledge_to_entity(ko.id, entity["id"], "owner")
        del raw

    heavy: list[str] = []
    original = storage.get_entity_knowledge

    def watched(*args, **kwargs):
        heavy.append("get_entity_knowledge")
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "get_entity_knowledge", watched)

    found = graph.search_entities("owner", "Орион", limit=5)
    assert found and found[0]["_knowledge_count"] == 30
    assert found[0]["_relation_count"] == 0
    assert heavy == [], "counting still went through the full-row query"


def test_graph_context_reads_a_projection_not_document_bodies(storage):
    """The BFS uses the id, the link confidence and two scores. Nothing else.

    It loaded up to 1000 full rows per entity — for every entity the traversal
    dequeued, neighbours included — and the document text was read from disk and
    discarded. `list_entity_knowledge_refs` returns the four columns that are
    actually consulted.
    """
    from jericho.knowledge_graph import KnowledgeGraph
    from jericho.storage.models import EntityType

    graph = KnowledgeGraph(storage)
    storage.ensure_user("owner")
    entity = graph.create_entity("owner", "Проект Орион", EntityType.PROJECT)
    raw, ko = _make_knowledge(storage, "owner", "Орион " + "тело документа " * 400)
    graph.link_knowledge_to_entity(ko.id, entity["id"], "owner")
    del raw

    refs = storage.list_entity_knowledge_refs("owner", entity["id"], limit=10)
    assert refs and set(refs[0]) == {"id", "importance", "quality_score", "_link_confidence"}
    assert "content" not in refs[0]

    context = graph.context_for_query("owner", "Орион")
    assert any(item["knowledge_object_id"] == ko.id for item in context["knowledge_candidates"])
