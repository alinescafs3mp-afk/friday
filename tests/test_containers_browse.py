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
import json

import pytest
from fastapi.testclient import TestClient

from friday.knowledge_graph import CONTAINER_ENTITY_TYPES, KnowledgeGraph
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import EntityType, KnowledgeObject, RawObject, new_id


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
        first = _tagged_ko(storage, LEGACY_OWNER_USER_ID, "Первая", ["Идеи", "python"])
        second = _tagged_ko(storage, LEGACY_OWNER_USER_ID, "Вторая", ["идеи"])

        # /api/knowledge/tags is a real route, not a knowledge id.
        tags = client.get("/api/knowledge/tags", headers=owner)
        assert tags.status_code == 200
        assert len(tags.json()["items"]) == 2, "browse_tag_membership"
        assert {item["tag"].casefold(): item["count"] for item in tags.json()["items"]} == {
            "идеи": 2,
            "python": 1,
        }

        filtered = client.get("/api/knowledge", params={"tag": "ИДЕИ"}, headers=owner)
        assert filtered.status_code == 200
        assert filtered.json()["count"] == 2
        assert sorted((item["id"], item["title"]) for item in filtered.json()["items"]) == sorted(
            [(first["id"], "Первая"), (second["id"], "Вторая")]
        ), "browse_knowledge_membership"

        audit_before = [
            dict(row) for row in storage.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()
        ]

        created = client.post("/api/kg/containers", json={"name": "Дом", "kind": "project"}, headers=owner)
        assert created.status_code == 200
        root_id = created.json()["container"]["id"]
        child = client.post(
            "/api/kg/containers",
            json={"name": "Ремонт", "kind": "collection", "parent_id": root_id},
            headers=owner,
        )
        assert child.status_code == 200
        child_id = child.json()["container"]["id"]
        for response, entity_id, name, kind in (
            (created, root_id, "Дом", "project"),
            (child, child_id, "Ремонт", "collection"),
        ):
            expected = {"id": entity_id, "name": name, "entity_type": kind, "description": ""}
            assert {key: response.json()["container"][key] for key in expected} == expected, (
                "container_http_create"
            )
            persisted = storage.get_entity(entity_id, LEGACY_OWNER_USER_ID)
            assert persisted is not None
            assert {key: persisted[key] for key in expected} == expected, "container_http_persistence"
            assert not persisted["deleted_at"] and persisted["user_id"] == LEGACY_OWNER_USER_ID
        edges = storage.list_part_of_relations(LEGACY_OWNER_USER_ID)
        assert [(edge["source_entity_id"], edge["target_entity_id"]) for edge in edges] == [
            (child_id, root_id)
        ], "container_http_parent_persistence"
        assert (
            client.post("/api/kg/containers", json={"name": "Z", "kind": "person"}, headers=owner).status_code
            == 400
        )

        listed = client.get("/api/kg/containers", headers=owner)
        assert listed.status_code == 200
        listing = listed.json()
        assert (listing["count"], listing["matched_at_least"], listing["truncated"]) == (2, 2, False)
        assert sorted(
            (item["id"], item["name"], item["entity_type"], item["parent_id"], item["knowledge_count"])
            for item in listing["items"]
        ) == sorted(
            [
                (root_id, "Дом", "project", None, 0),
                (child_id, "Ремонт", "collection", root_id, 0),
            ]
        ), "container_http_membership"

        # Entity name lookup for browse surfaces.
        found = client.get("/api/kg/entities", params={"q": "Дом"}, headers=owner)
        assert found.status_code == 200
        assert (found.json()["count"], found.json()["matched_at_least"], found.json()["truncated"]) == (
            1,
            1,
            False,
        )
        assert [(item["id"], item["name"], item["entity_type"]) for item in found.json()["items"]] == [
            (root_id, "Дом", "project")
        ], "entity_search_membership"
        stats = client.get("/api/kg/stats", headers=owner)
        assert stats.status_code == 200
        expected_stats = {
            "entity_count": 2,
            "relation_count": 1,
            "knowledge_object_count": 2,
            "raw_object_count": 2,
            "file_count": 0,
            "entities_by_type": {"project": 1, "collection": 1},
        }
        assert {key: stats.json()[key] for key in expected_stats} == expected_stats, "graph_http_stats"
        audit_after = [
            dict(row) for row in storage.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()
        ]
        assert audit_after[: len(audit_before)] == audit_before, "container_http_audit_prefix"
        delta = audit_after[len(audit_before) :]
        assert [(row["action"], row["user_id"], row["target_type"], row["target_id"]) for row in delta] == [
            ("container.create", LEGACY_OWNER_USER_ID, "entity", root_id),
            ("container.create", LEGACY_OWNER_USER_ID, "entity", child_id),
        ], "container_http_audit_delta"
        assert all(
            word not in json.dumps(audit_after, ensure_ascii=False)
            for word in ("Первая", "Вторая", "Дом", "Ремонт")
        )


def test_admin_tags_containers_and_entity_filter(settings):
    import re
    import sqlite3
    from contextlib import closing
    from datetime import UTC, datetime

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

        # A same-name private container must neither be reused nor changed.
        private_tenant = "local:container-private-086"
        private_canary = "CONTAINER-PRIVATE-086"
        storage.ensure_user(private_tenant, source="test")
        app.state.kg.create_container(private_tenant, "Урожай", kind="collection", description=private_canary)

        def snapshot():
            with closing(sqlite3.connect(settings.database_path.as_uri() + "?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                business = {
                    table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in (
                        "entities",
                        "entity_versions",
                        "raw_objects",
                        "knowledge_objects",
                        "knowledge_entity_links",
                        "relations",
                        "relation_revisions",
                    )
                }
                audits = [dict(row) for row in conn.execute("SELECT rowid, * FROM audit_log ORDER BY rowid")]
                return business, audits

        def canonical(value):
            return json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            )

        def request_time(value, *, audit=False):
            assert type(value) is str
            fraction = r"\.000000" if audit else ""
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}" + fraction + r"\+00:00", value)
            parsed = datetime.fromisoformat(value)
            assert parsed.isoformat(timespec="microseconds" if audit else "seconds") == value
            assert started.replace(microsecond=0) <= parsed <= ended
            return value

        before, audit_before = snapshot()
        assert not [
            row for row in before["entities"] if row["user_id"] == "local:kate" and row["name"] == "Урожай"
        ]
        assert (
            len(
                [
                    row
                    for row in before["entities"]
                    if row["user_id"] == private_tenant
                    and row["name"] == "Урожай"
                    and row["description"] == private_canary
                ]
            )
            == 1
        )
        assert audit_before
        started = datetime.now(UTC)
        made = client.post(
            "/api/admin/containers",
            json={"user_id": "local:kate", "name": "Урожай", "kind": "collection"},
            headers=owner,
        )
        assert made.status_code == 200
        assert made.json()["container"]["entity_type"] == "collection"
        ended = datetime.now(UTC)
        after, audit_after = snapshot()
        assert len(after["entities"]) == len(before["entities"]) + 1
        assert after["entities"][:-1] == before["entities"]
        created = after["entities"][-1]
        entity_id = created["id"]
        assert type(entity_id) is str and re.fullmatch(r"ent_[0-9a-f]{16}", entity_id)
        assert entity_id not in {row["id"] for row in before["entities"]}
        created_at = request_time(created["created_at"])
        updated_at = request_time(created["updated_at"])
        expected_entity = {
            "id": entity_id,
            "user_id": "local:kate",
            "name": "Урожай",
            "normalized_name": "урож",
            "entity_type": "collection",
            "aliases_json": "[]",
            "description": "",
            "metadata_json": '{"container": true, "origin": "user"}',
            "canonical": 1,
            "merged_into_id": None,
            "version": 1,
            "created_at": created_at,
            "updated_at": updated_at,
            "deleted_at": None,
        }
        assert canonical(after["entities"]) == canonical(before["entities"] + [expected_entity])
        expected_card = {
            "id": entity_id,
            "name": "Урожай",
            "entity_type": "collection",
            "aliases": [],
            "aliases_json": "[]",
            "description": "",
            "version": 1,
            "created_at": created_at,
            "updated_at": updated_at,
            "parent_id": None,
            "knowledge_count": 0,
        }
        assert canonical(made.json()) == canonical({"container": expected_card})

        assert len(after["entity_versions"]) == len(before["entity_versions"]) + 1
        assert after["entity_versions"][:-1] == before["entity_versions"]
        version = after["entity_versions"][-1]
        version_id = version["id"]
        assert type(version_id) is str and re.fullmatch(r"entv_[0-9a-f]{16}", version_id)
        assert version_id not in {row["id"] for row in before["entity_versions"]}
        version_time = version["created_at"]
        assert type(version_time) is str
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", version_time)
        parsed_version_time = datetime.fromisoformat(version_time.replace("Z", "+00:00"))
        assert parsed_version_time.isoformat(timespec="microseconds").replace("+00:00", "Z") == version_time
        # Ordinary wall-clock fixture only; no rollback/future-authority claim.
        assert started <= parsed_version_time <= ended
        assert all(
            datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")) < parsed_version_time
            for row in before["entity_versions"]
        )
        expected_version = {
            "id": version_id,
            "user_id": "local:kate",
            "entity_id": entity_id,
            "version": 1,
            "snapshot_json": json.dumps(expected_entity, ensure_ascii=False, sort_keys=True),
            "created_at": version_time,
        }
        assert canonical(after["entity_versions"]) == canonical(
            before["entity_versions"] + [expected_version]
        )
        # ensure_user and managed transaction context have legitimate writes;
        # equality is limited to these selected raw business tables.
        for table in (
            "raw_objects",
            "knowledge_objects",
            "knowledge_entity_links",
            "relations",
            "relation_revisions",
        ):
            assert canonical(after[table]) == canonical(before[table]), table

        assert len(audit_after) == len(audit_before) + 1
        assert audit_after[:-1] == audit_before
        audit = audit_after[-1]
        audit_id = audit["id"]
        assert type(audit_id) is str and re.fullmatch(r"audit_[0-9a-f]{16}", audit_id)
        assert audit_id not in {row["id"] for row in audit_before}
        assert type(audit["rowid"]) is int and audit["rowid"] > audit_before[-1]["rowid"]
        request_id = made.headers["X-Request-ID"]
        assert re.fullmatch(r"[0-9a-f]{24}", request_id)
        expected_fingerprint = {
            "id": entity_id,
            "entity_type": "collection",
            "version": 1,
            "created_at": created_at,
            "updated_at": updated_at,
            "name_chars": 6,
            "description_chars": 0,
            "aliases_chars": 2,
            "private_fields_count": 5,
        }
        expected_audit = {
            "rowid": audit["rowid"],
            "id": audit_id,
            "user_id": LEGACY_OWNER_USER_ID,
            "action": "admin.container.create",
            "target_type": "entity",
            "target_id": entity_id,
            "before_json": None,
            "after_json": json.dumps(expected_fingerprint, ensure_ascii=False, sort_keys=True),
            "ip_address": "",
            "request_id": request_id,
            "created_at": request_time(audit["created_at"], audit=True),
        }
        assert canonical(audit_after) == canonical(audit_before + [expected_audit])
        assert all(
            private not in made.text + canonical([audit])
            for private in (private_tenant, private_canary, "Запись Кати", settings.api_token)
        )


# --- signed bridge + query strings ---------------------------------------


def test_bridge_signature_covers_query_string(settings):
    """The bridge signs path?query verbatim; verification must accept it."""
    import time
    import uuid

    from friday.security import sign_bridge_request

    app = create_app(settings)
    with TestClient(app) as client:

        def signed_get(path: str):
            timestamp = int(time.time())
            nonce = uuid.uuid4().hex
            return client.get(
                path,
                headers={
                    "X-Friday-Timestamp": str(timestamp),
                    "X-Friday-User": "1001",
                    "X-Friday-Chat": "5001",
                    "X-Friday-Nonce": nonce,
                    "X-Friday-Signature": sign_bridge_request(
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
    from friday.storage.models import KnowledgeObject, RawObject, new_id

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
    from friday.knowledge_graph import KnowledgeGraph
    from friday.storage.models import EntityType

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
    from friday.knowledge_graph import KnowledgeGraph
    from friday.storage.models import EntityType

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


def test_conversation_history_beyond_the_first_page_is_reachable(storage):
    """`count` was `len(items)` against a hard 1000-row cap, with no offset at all.

    A longer history reported itself as exactly 1000 and the rest could not be
    fetched by any parameter. Conversations hold the transient record of what was
    actually said, and the oldest are the ones a person goes looking for.
    """
    storage.ensure_user("owner")
    created = []
    for index in range(25):
        conversation_id = storage.create_conversation("owner", title=f"Диалог {index}")
        created.append(conversation_id if isinstance(conversation_id, str) else conversation_id["id"])

    assert storage.count_conversations("owner") == 25

    first = storage.list_conversations("owner", limit=10, offset=0)
    second = storage.list_conversations("owner", limit=10, offset=10)
    third = storage.list_conversations("owner", limit=10, offset=20)

    assert [len(first), len(second), len(third)] == [10, 10, 5]
    ids = [item["id"] for item in (*first, *second, *third)]
    assert len(set(ids)) == 25, "pages overlapped or skipped rows"
    assert set(ids) == set(created)
