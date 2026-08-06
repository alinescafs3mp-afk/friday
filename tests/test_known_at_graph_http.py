"""Proposal 28: direct graph HTTP keeps one honest transaction-time boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json

from fastapi.testclient import TestClient

from friday.http_errors import relation_history_http_detail
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage import normalize_entity_name
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

REQUESTED = "2025-03-04T06:07:08+03:00"
NORMALIZED = "2025-03-04T03:07:08.000000Z"
FLOOR = "2025-01-01T00:00:00.000000Z"
STATUS = {
    "known_at": NORMALIZED,
    "known_at_floor": FLOOR,
    "history_complete": True,
    "identity_basis": "current_names",
}
_PUBLIC_RELATION_KEYS = {
    "id",
    "source_entity_id",
    "target_entity_id",
    "source_name",
    "target_name",
    "relation_type",
    "weight",
    "valid_from",
    "valid_to",
    "created_at",
    "invalidated_at",
    "superseded_by",
    "provenance",
}


def test_relation_history_http_error_translation_never_echoes_arbitrary_detail() -> None:
    secret = "SYNTHETIC_HISTORY_ERROR_SENTINEL_" + "x" * 10_000
    detail = relation_history_http_detail(RelationHistorySnapshotError(secret))
    assert detail == "Исторический снимок графа недоступен или неполон"
    assert secret not in detail

    earliest = "2025-01-01T00:00:00.000000Z"
    detail = relation_history_http_detail(
        RelationHistorySnapshotError(
            f"known_at precedes complete relation history; earliest boundary is {earliest}"
        )
    )
    assert earliest in detail


def _snapshot(root: str = "ent-root") -> dict:
    return {
        "root": root,
        "nodes": [{"id": root, "name": "Альфа", "entity_type": "project"}],
        "edges": [],
        "as_of": "2024-01-01",
        "temporal_basis": "bitemporal",
        **STATUS,
    }


def _assert_metadata(body: dict) -> None:
    assert {key: body[key] for key in STATUS} == STATUS
    assert body["temporal_basis"] == "bitemporal"


def _oversized_private_relation_metadata(secret: str, version: str) -> dict:
    return {
        "origin": "manual",
        "source": "explicit_test_relation",
        "private_candidate_id": f"candidate-{version}-" + "c" * 20_000,
        "private_reviewer": f"reviewer-{version}-" + "r" * 20_000,
        "created_by": f"creator-{version}-" + "u" * 20_000,
        "confidence": 0.81,
        "evidence": {
            "knowledge_object_id": f"ko-{version}-" + "k" * 20_000,
            "private_excerpt": secret,
        },
        "private": secret,
        "unbounded_payload": "x" * 250_000,
    }


def test_current_and_historical_neighbourhoods_publish_one_bounded_relation_shape(
    settings,
) -> None:
    """Current rows and revision rows cannot smuggle arbitrary metadata over HTTP."""

    historical_secret = "SYNTHETIC_HISTORICAL_RELATION_SECRET"
    current_secret = "SYNTHETIC_CURRENT_RELATION_SECRET"
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        kg = app.state.kg
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        source = kg.create_entity(LEGACY_OWNER_USER_ID, "Альфа", EntityType.PROJECT)
        target = kg.create_entity(LEGACY_OWNER_USER_ID, "Бета", EntityType.ORGANIZATION)
        relation = kg.create_relation(
            LEGACY_OWNER_USER_ID,
            str(source["id"]),
            str(target["id"]),
            RelationType.RELATED_TO,
            metadata=_oversized_private_relation_metadata(historical_secret, "historical"),
            origin="manual",
            valid_from="2024-01-01",
        )
        first_revision = storage.execute(
            """SELECT recorded_at FROM relation_revisions
               WHERE relation_id=? ORDER BY event_seq LIMIT 1""",
            (relation.id,),
        ).fetchone()
        assert first_revision is not None
        historical_known_at = str(first_revision["recorded_at"])

        with storage.transaction() as connection:
            connection.execute(
                "UPDATE relations SET metadata_json=? WHERE id=? AND user_id=?",
                (
                    json.dumps(
                        _oversized_private_relation_metadata(current_secret, "current"),
                        ensure_ascii=False,
                    ),
                    relation.id,
                    LEGACY_OWNER_USER_ID,
                ),
            )

        requests = (
            (f"/api/kg/graph/{source['id']}", {}, "edges"),
            (
                f"/api/admin/graph/{source['id']}",
                {"user_id": LEGACY_OWNER_USER_ID},
                "edges",
            ),
            (
                f"/api/kg/graph/{source['id']}",
                {"known_at": historical_known_at},
                "edges",
            ),
            (
                f"/api/admin/graph/{source['id']}",
                {"user_id": LEGACY_OWNER_USER_ID, "known_at": historical_known_at},
                "edges",
            ),
            (f"/api/kg/entities/{source['id']}", {}, "relations"),
            (
                f"/api/kg/entities/{source['id']}",
                {"known_at": historical_known_at},
                "relations",
            ),
            ("/api/kg/entity-profile", {"name": "Альфа"}, "relations"),
        )
        for path, params, collection in requests:
            response = client.get(path, params=params, headers=headers)
            assert response.status_code == 200, response.text
            edges = response.json()[collection]
            assert len(edges) == 1
            edge = edges[0]
            assert edge["id"] == relation.id
            assert set(edge) <= _PUBLIC_RELATION_KEYS
            assert "metadata_json" not in edge
            # Oversized metadata is dropped before Python materialization. Its
            # provenance is unavailable rather than making graph-read memory
            # depend on a tenant-controlled JSON blob.
            assert "provenance" not in edge
            assert all(len(value) <= 240 for value in edge.values() if isinstance(value, str))
            encoded = json.dumps(edge, ensure_ascii=False)
            assert historical_secret not in encoded
            assert current_secret not in encoded
            assert len(encoded) < 2_500


def test_public_and_admin_graph_routes_forward_the_same_known_at(settings, monkeypatch) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        local_calls: list[dict] = []
        overview_calls: list[dict] = []

        def local_graph(
            user_id: str,
            entity_id: str,
            depth: int = 2,
            **kwargs,
        ) -> dict:
            try:
                asyncio.get_running_loop()
                on_event_loop = True
            except RuntimeError:
                on_event_loop = False
            local_calls.append(
                {
                    "user_id": user_id,
                    "entity_id": entity_id,
                    "depth": depth,
                    "on_event_loop": on_event_loop,
                    **kwargs,
                }
            )
            return _snapshot(entity_id)

        def overview(user_id: str, **kwargs) -> dict:
            try:
                asyncio.get_running_loop()
                on_event_loop = True
            except RuntimeError:
                on_event_loop = False
            overview_calls.append({"user_id": user_id, "on_event_loop": on_event_loop, **kwargs})
            return {
                "nodes": _snapshot()["nodes"],
                "edges": [],
                "shown": 1,
                "total": 1,
                "as_of": "2024-01-01",
                "temporal_basis": "bitemporal",
                **STATUS,
            }

        monkeypatch.setattr(app.state.kg, "get_entity_graph", local_graph)
        monkeypatch.setattr(app.state.storage, "graph_overview", overview)
        common = {"as_of": "2024-01-01", "known_at": REQUESTED}

        public = client.get("/api/kg/graph/ent-root", params=common, headers=headers)
        admin_local = client.get(
            "/api/admin/graph/ent-root",
            params={"user_id": LEGACY_OWNER_USER_ID, **common},
            headers=headers,
        )
        admin_overview = client.get(
            "/api/admin/graph",
            params={"user_id": LEGACY_OWNER_USER_ID, **common},
            headers=headers,
        )

        for response in (public, admin_local, admin_overview):
            assert response.status_code == 200, response.text
            _assert_metadata(response.json())

        assert len(local_calls) == 2
        assert {call["known_at"] for call in local_calls} == {NORMALIZED}
        assert {call["as_of"] for call in local_calls} == {"2024-01-01"}
        assert not any(call["on_event_loop"] for call in local_calls)
        assert overview_calls[0]["known_at"] == NORMALIZED
        assert overview_calls[0]["as_of"] == "2024-01-01"
        assert overview_calls[0]["on_event_loop"] is False


def test_entity_endpoint_uses_normalized_known_at_and_echoes_status(settings, monkeypatch) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        entity = app.state.kg.create_entity(
            LEGACY_OWNER_USER_ID,
            "Альфа",
            EntityType.PROJECT,
        )
        status_calls: list[tuple[str, str]] = []
        relation_calls: list[tuple[str, str, str]] = []

        def status(user_id: str, *, known_at: str = "") -> dict:
            status_calls.append((user_id, known_at))
            return dict(STATUS)

        def relations(
            entity_id: str,
            user_id: str,
            *,
            as_of: str = "",
            known_at: str = "",
        ) -> list[dict]:
            relation_calls.append((entity_id, user_id, known_at))
            return [{"id": "rel-historical", "source_entity_id": entity_id}]

        monkeypatch.setattr(app.state.storage, "relation_history_status", status)
        monkeypatch.setattr(app.state.kg, "get_entity_relations", relations)
        response = client.get(
            f"/api/kg/entities/{entity['id']}",
            params={"known_at": REQUESTED},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        _assert_metadata(body)
        assert [item["id"] for item in body["relations"]] == ["rel-historical"]
        assert status_calls == [
            (LEGACY_OWNER_USER_ID, NORMALIZED),
            (LEGACY_OWNER_USER_ID, NORMALIZED),
        ]
        assert relation_calls == [(str(entity["id"]), LEGACY_OWNER_USER_ID, NORMALIZED)]


def test_direct_routes_do_not_mask_a_merge_crossing_refusal(settings, monkeypatch) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        root_lookups: list[str] = []

        def refuse(*_args, **_kwargs):
            raise RelationHistorySnapshotError("known_at пересекает merge; снимок неполон")

        def entity_must_not_run(*_args, **_kwargs):
            root_lookups.append("entity")
            raise AssertionError("entity lookup ran after a known_at refusal")

        monkeypatch.setattr(app.state.kg, "get_entity_graph", refuse)
        monkeypatch.setattr(app.state.storage, "graph_overview", refuse)
        monkeypatch.setattr(app.state.storage, "relation_history_status", refuse)
        monkeypatch.setattr(app.state.kg, "get_entity", entity_must_not_run)

        responses = (
            client.get(
                "/api/kg/graph/ent-root",
                params={"known_at": REQUESTED},
                headers=headers,
            ),
            client.get(
                "/api/admin/graph/ent-root",
                params={"user_id": LEGACY_OWNER_USER_ID, "known_at": REQUESTED},
                headers=headers,
            ),
            client.get(
                "/api/admin/graph",
                params={"user_id": LEGACY_OWNER_USER_ID, "known_at": REQUESTED},
                headers=headers,
            ),
            client.get(
                "/api/kg/entities/ent-missing",
                params={"known_at": REQUESTED},
                headers=headers,
            ),
        )

        for response in responses:
            assert response.status_code == 400, response.text
            assert "слиян" in response.json()["detail"]
        assert root_lookups == []


def test_entity_endpoint_rechecks_identity_after_its_last_current_read(settings, monkeypatch) -> None:
    """A merge racing the card/name reads refuses the mixed response."""

    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        entity = app.state.kg.create_entity(
            LEGACY_OWNER_USER_ID,
            "Альфа",
            EntityType.PROJECT,
        )
        calls: list[str] = []

        def status(_user_id: str, *, known_at: str = "") -> dict:
            calls.append(known_at)
            if len(calls) == 2:
                raise RelationHistorySnapshotError("known_at пересекает concurrent merge")
            return dict(STATUS)

        monkeypatch.setattr(app.state.storage, "relation_history_status", status)
        monkeypatch.setattr(app.state.kg, "get_entity_relations", lambda *_args, **_kwargs: [])
        response = client.get(
            f"/api/kg/entities/{entity['id']}",
            params={"known_at": REQUESTED},
            headers=headers,
        )

        assert response.status_code == 400, response.text
        assert "слиян" in response.json()["detail"]
    assert calls == [NORMALIZED, NORMALIZED]


def test_entity_endpoint_rejects_incomplete_history_metadata_before_entity_reads(
    settings,
    monkeypatch,
) -> None:
    app = create_app(settings)
    reads: list[str] = []

    def incomplete_status(*_args, **_kwargs):
        return {
            "known_at": NORMALIZED,
            # Missing known_at_floor is a refusal, not a truthy fallback.
            "history_complete": True,
            "identity_basis": "current_names",
        }

    def entity_must_not_run(*_args, **_kwargs):
        reads.append("entity")
        raise AssertionError("entity endpoint read before validating history metadata")

    with TestClient(app) as client:
        monkeypatch.setattr(app.state.storage, "relation_history_status", incomplete_status)
        monkeypatch.setattr(app.state.kg, "get_entity", entity_must_not_run)
        response = client.get(
            "/api/kg/entities/ent-missing",
            params={"known_at": REQUESTED},
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )

    assert response.status_code == 400, response.text
    assert "непол" in response.json()["detail"]
    assert reads == []


def test_http_refuses_a_current_entity_created_after_known_at(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        storage = app.state.storage
        storage.ensure_user(LEGACY_OWNER_USER_ID)
        floor = str(storage.relation_history_status(LEGACY_OWNER_USER_ID)["known_at_floor"])
        late = app.state.kg.create_entity(
            LEGACY_OWNER_USER_ID,
            "Появился после границы",
            EntityType.PROJECT,
        )
        responses = (
            client.get(
                f"/api/kg/graph/{late['id']}",
                params={"known_at": floor},
                headers=headers,
            ),
            client.get(
                f"/api/kg/entities/{late['id']}",
                params={"known_at": floor},
                headers=headers,
            ),
        )

        assert all(response.status_code == 400 for response in responses)
        assert all("сущност" in response.json()["detail"] for response in responses)
        assert all(str(late["id"]) not in response.text for response in responses)


def test_soft_deleted_entity_is_absent_from_direct_public_routes(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        entity = app.state.kg.create_entity(
            LEGACY_OWNER_USER_ID,
            "Удалённый объект",
            EntityType.PROJECT,
        )
        assert app.state.kg.delete_entity(LEGACY_OWNER_USER_ID, str(entity["id"])) is True

        card = client.get(f"/api/kg/entities/{entity['id']}", headers=headers)
        graph = client.get(f"/api/kg/graph/{entity['id']}", headers=headers)
        assert card.status_code == 404, card.text
        assert graph.status_code == 404, graph.text


def test_direct_entity_surfaces_bound_relations_and_versions_before_serialization(settings) -> None:
    secret = "SYNTHETIC_DIRECT_VERSION_SENTINEL_" + "v" * 250_000
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        storage.ensure_user(LEGACY_OWNER_USER_ID)
        root = Entity(
            id=new_id("ent"),
            user_id=LEGACY_OWNER_USER_ID,
            name="Широкая сущность",
            entity_type=EntityType.PROJECT,
            metadata_json={"private": secret},
        )
        targets = [
            Entity(
                id=new_id("ent"),
                user_id=LEGACY_OWNER_USER_ID,
                name=f"Сосед {index:03d}",
                entity_type=EntityType.OTHER,
            )
            for index in range(201)
        ]
        entity_rows = []
        for entity in [root, *targets]:
            row = entity.to_row()
            row["normalized_name"] = normalize_entity_name(entity.name)
            entity_rows.append(row)

        with storage.transaction() as connection:
            version_time = connection.execute(
                "SELECT recorded_at FROM relation_revision_context WHERE singleton=1"
            ).fetchone()
            assert version_time is not None
            connection.executemany(
                """INSERT INTO entities(id, user_id, name, normalized_name, entity_type,
                   aliases_json, description, metadata_json, canonical, merged_into_id, version,
                   created_at, updated_at, deleted_at)
                   VALUES(:id, :user_id, :name, :normalized_name, :entity_type,
                   :aliases_json, :description, :metadata_json, :canonical, :merged_into_id, :version,
                   :created_at, :updated_at, :deleted_at)""",
                entity_rows,
            )
            connection.executemany(
                """INSERT INTO entity_versions
                   (id, user_id, entity_id, version, snapshot_json, created_at)
                   VALUES(?, ?, ?, ?, ?, ?)""",
                [
                    (
                        new_id("entv"),
                        LEGACY_OWNER_USER_ID,
                        str(row["id"]),
                        1,
                        json.dumps(row, ensure_ascii=False),
                        version_time["recorded_at"],
                    )
                    for row in entity_rows
                ],
            )
            connection.executemany(
                """INSERT INTO relations(id, user_id, source_entity_id, target_entity_id,
                   relation_type, weight, metadata_json, created_at, deleted_at,
                   valid_from, valid_to, invalidated_at, superseded_by)
                   VALUES(:id, :user_id, :source_entity_id, :target_entity_id,
                   :relation_type, :weight, :metadata_json, :created_at, :deleted_at,
                   :valid_from, :valid_to, :invalidated_at, :superseded_by)""",
                [
                    Relation(
                        id=new_id("rel"),
                        user_id=LEGACY_OWNER_USER_ID,
                        source_entity_id=root.id,
                        target_entity_id=target.id,
                        relation_type=RelationType.RELATED_TO,
                    ).to_row()
                    for target in targets
                ],
            )
            boundary_row = connection.execute(
                """SELECT recorded_at FROM relation_revisions
                     WHERE user_id=? ORDER BY event_seq DESC LIMIT 1""",
                (LEGACY_OWNER_USER_ID,),
            ).fetchone()
            assert boundary_row is not None
        boundary = str(boundary_row["recorded_at"])

        snapshot = dict(entity_rows[0])
        with storage.transaction() as connection:
            version_time = connection.execute(
                "SELECT recorded_at FROM relation_revision_context WHERE singleton=1"
            ).fetchone()
            assert version_time is not None
            versions = []
            for version in range(2, 103):
                version_snapshot = {**snapshot, "version": version}
                versions.append(
                    (
                        f"entv-wide-{version:03d}",
                        LEGACY_OWNER_USER_ID,
                        root.id,
                        version,
                        json.dumps(version_snapshot, ensure_ascii=False),
                        version_time["recorded_at"],
                    )
                )
            connection.executemany(
                """INSERT INTO entity_versions
                   (id, user_id, entity_id, version, snapshot_json, created_at)
                   VALUES(?, ?, ?, ?, ?, ?)""",
                versions,
            )

        responses = (
            client.get(f"/api/kg/entities/{root.id}", headers=headers),
            client.get(
                f"/api/kg/entities/{root.id}",
                params={"known_at": boundary},
                headers=headers,
            ),
        )
        for response in responses:
            assert response.status_code == 200, response.text
            body = response.json()
            assert len(body["relations"]) == 200
            assert body["relations_matched_at_least"] == 201
            assert body["relations_truncated"] is True
            assert len(body["versions"]) == 100
            assert body["versions_matched_at_least"] == 101
            assert body["versions_truncated"] is True
            encoded = json.dumps(body, ensure_ascii=False)
            assert secret not in encoded
            assert "snapshot_json" not in encoded
            assert "metadata_json" not in encoded

        profile = client.get(
            "/api/kg/entity-profile",
            params={"name": "Широкая сущность"},
            headers=headers,
        )
        assert profile.status_code == 200, profile.text
        assert len(profile.json()["relations"]) == 200
        assert profile.json()["relations_matched_at_least"] == 201
        assert profile.json()["relations_truncated"] is True


def test_every_direct_graph_route_rejects_unparsed_known_at_before_reads(settings, monkeypatch) -> None:
    app = create_app(settings)
    reads: list[str] = []

    def must_not_read(*_args, **_kwargs):
        reads.append("read")
        raise AssertionError("invalid known_at reached graph storage")

    with TestClient(app) as client:
        monkeypatch.setattr(app.state.kg, "get_entity_graph", must_not_read)
        monkeypatch.setattr(app.state.storage, "graph_overview", must_not_read)
        monkeypatch.setattr(app.state.storage, "relation_history_status", must_not_read)
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        invalid = "2025-03-04"
        responses = (
            client.get("/api/kg/graph/ent-root", params={"known_at": invalid}, headers=headers),
            client.get(
                "/api/admin/graph/ent-root",
                params={"user_id": LEGACY_OWNER_USER_ID, "known_at": invalid},
                headers=headers,
            ),
            client.get(
                "/api/admin/graph",
                params={"user_id": LEGACY_OWNER_USER_ID, "known_at": invalid},
                headers=headers,
            ),
            client.get("/api/kg/entities/ent-root", params={"known_at": invalid}, headers=headers),
        )

        assert all(response.status_code == 400 for response in responses)
        assert reads == []


def test_direct_graph_routes_leave_unrelated_value_errors_as_server_errors(settings, monkeypatch) -> None:
    """Only validation/snapshot refusals are client errors, not arbitrary bugs."""

    app = create_app(settings)

    def internal_bug(*_args, **_kwargs):
        raise ValueError("synthetic internal graph bug")

    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(app.state.kg, "get_entity_graph", internal_bug)
        monkeypatch.setattr(app.state.storage, "graph_overview", internal_bug)
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        responses = (
            client.get("/api/kg/graph/ent-root", headers=headers),
            client.get(
                "/api/admin/graph/ent-root",
                params={"user_id": LEGACY_OWNER_USER_ID},
                headers=headers,
            ),
            client.get(
                "/api/admin/graph",
                params={"user_id": LEGACY_OWNER_USER_ID},
                headers=headers,
            ),
        )

        assert [response.status_code for response in responses] == [500, 500, 500]


def test_real_admin_temporal_overview_echoes_normalized_date_and_relation_only_nodes(settings) -> None:
    """No-link relation endpoints survive the real admin route and empty results name their date."""

    secret = "SYNTHETIC_PRIVATE_ADMIN_OVERVIEW_" + "o" * 250_000
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        storage = app.state.storage
        source = Entity(
            new_id("ent"),
            LEGACY_OWNER_USER_ID,
            "Альфа" + "А" * 2_000,
            EntityType.PROJECT,
            description=secret,
            metadata_json={"private": secret},
        )
        target = Entity(new_id("ent"), LEGACY_OWNER_USER_ID, "Бета", EntityType.ORGANIZATION)
        storage.create_entity(source)
        storage.create_entity(target)
        storage.create_relation(
            Relation(
                new_id("rel"),
                LEGACY_OWNER_USER_ID,
                source.id,
                target.id,
                RelationType.RELATED_TO,
                valid_from="2024-01-01",
            )
        )

        response = client.get(
            "/api/admin/graph",
            params={"user_id": LEGACY_OWNER_USER_ID, "as_of": "2024/6"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["as_of"] == "2024-06-01"
        assert body["temporal_basis"] == "valid_time"
        assert body["identity_basis"] == "current_names"
        assert {node["id"] for node in body["nodes"]} == {source.id, target.id}
        assert all(set(node) == {"id", "name", "entity_type", "knowledge_count"} for node in body["nodes"])
        assert all(len(node["name"]) <= 240 for node in body["nodes"])
        assert [edge["kind"] for edge in body["edges"]] == ["relation"]
        encoded = json.dumps(body, ensure_ascii=False)
        assert secret not in encoded
        assert "description" not in encoded
        assert "metadata_json" not in encoded

        empty = client.get(
            "/api/admin/graph",
            params={"user_id": LEGACY_OWNER_USER_ID, "as_of": "2023"},
            headers=headers,
        )
        assert empty.status_code == 200, empty.text
        assert empty.json()["nodes"] == []
        assert empty.json()["as_of"] == "2023-01-01"
        assert empty.json()["temporal_basis"] == "valid_time"


def test_admin_overview_projects_untrusted_storage_shapes_and_rejects_bad_status(
    settings,
    monkeypatch,
) -> None:
    secret = "SYNTHETIC_RAW_ADMIN_GRAPH_SENTINEL_" + "r" * 250_000
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        raw = {
            "nodes": [
                {
                    "id": "ent-root",
                    "name": "Узел" + "Н" * 2_000,
                    "entity_type": "thing",
                    "description": secret,
                    "metadata_json": {"private": secret},
                    "user_id": "private-owner",
                }
            ],
            "edges": [],
            "shown": 1,
            "total": 1,
            "as_of": "",
            "known_at": "",
            "temporal_basis": "valid_time",
        }
        monkeypatch.setattr(app.state.storage, "graph_overview", lambda *_args, **_kwargs: raw)
        response = client.get(
            "/api/admin/graph",
            params={"user_id": LEGACY_OWNER_USER_ID},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body["nodes"][0]) == {"id", "name", "entity_type", "knowledge_count"}
        assert len(body["nodes"][0]["name"]) == 240
        assert secret not in json.dumps(body, ensure_ascii=False)
        assert set(body) == {
            "nodes",
            "edges",
            "shown",
            "total",
            "nodes_matched_at_least",
            "nodes_truncated",
            "edges_matched_at_least",
            "edges_truncated",
            "as_of",
            "known_at",
            "identity_basis",
            "temporal_basis",
        }

        malformed = {
            **raw,
            "as_of": "2024-01-01",
            "known_at": NORMALIZED,
            "temporal_basis": "bitemporal",
            "history_complete": True,
            "identity_basis": "current_names",
            # known_at_floor deliberately absent.
        }
        monkeypatch.setattr(
            app.state.storage,
            "graph_overview",
            lambda *_args, **_kwargs: malformed,
        )
        refused = client.get(
            "/api/admin/graph",
            params={
                "user_id": LEGACY_OWNER_USER_ID,
                "as_of": "2024-01-01",
                "known_at": REQUESTED,
            },
            headers=headers,
        )
        assert refused.status_code == 400, refused.text
        assert "непол" in refused.json()["detail"]


def test_current_admin_overview_reports_node_and_edge_truncation_honestly(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        storage = app.state.storage
        content = "Синтетический общий документ"
        raw = RawObject(
            id="raw-admin-overview-wide",
            user_id=LEGACY_OWNER_USER_ID,
            source="test",
            source_ref="admin-overview-wide",
            raw_content=content,
            content_type="text",
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        knowledge = KnowledgeObject(
            id="ko-admin-overview-wide",
            user_id=LEGACY_OWNER_USER_ID,
            raw_object_id=raw.id,
            content=content,
            content_type="text",
            title=content,
        )
        storage.store_knowledge_object(knowledge)
        for index in range(42):
            entity = Entity(
                f"ent-admin-overview-{index:02d}",
                LEGACY_OWNER_USER_ID,
                f"Узел {index:02d}",
                EntityType.PROJECT,
            )
            storage.create_entity(entity)
            storage.link_knowledge_entity(
                LEGACY_OWNER_USER_ID,
                knowledge.id,
                entity.id,
                status="accepted",
            )

        node_limited = client.get(
            "/api/admin/graph",
            params={"user_id": LEGACY_OWNER_USER_ID, "limit": 10},
            headers=headers,
        )
        assert node_limited.status_code == 200, node_limited.text
        assert len(node_limited.json()["nodes"]) == 10
        assert node_limited.json()["nodes_matched_at_least"] == 42
        assert node_limited.json()["nodes_truncated"] is True

        edge_limited = client.get(
            "/api/admin/graph",
            params={"user_id": LEGACY_OWNER_USER_ID, "limit": 500},
            headers=headers,
        )
        assert edge_limited.status_code == 200, edge_limited.text
        body = edge_limited.json()
        assert len(body["edges"]) == 800
        assert body["edges_matched_at_least"] == 861
        assert body["edges_truncated"] is True


def test_wide_entity_graph_is_bounded_allowlisted_and_honest_on_public_and_admin_http(settings) -> None:
    """An 801-edge star kills silent LIMIT mutations and raw entity publication."""

    secret = "SYNTHETIC_PRIVATE_ENTITY_SENTINEL_" + "s" * 250_000
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        storage = app.state.storage
        root = Entity(
            "ent-wide-root",
            LEGACY_OWNER_USER_ID,
            "Корень " + "К" * 1_000,
            EntityType.PROJECT,
            aliases_json=[secret],
            description=secret,
            metadata_json={"private": secret},
        )
        with storage.transaction():
            storage.create_entity(root)
            for index in range(801):
                neighbour = Entity(
                    f"ent-wide-{index:04d}",
                    LEGACY_OWNER_USER_ID,
                    f"Узел {index:04d}" + ("Н" * 1_000 if index == 0 else ""),
                    EntityType.ORGANIZATION,
                    description=secret if index == 0 else "",
                    metadata_json={"private": secret} if index == 0 else {},
                )
                storage.create_entity(neighbour)
                storage.create_relation(
                    Relation(
                        f"rel-wide-{index:04d}",
                        LEGACY_OWNER_USER_ID,
                        root.id,
                        neighbour.id,
                        RelationType.RELATED_TO,
                        metadata_json={"private": secret} if index == 0 else {},
                    )
                )

        responses = (
            client.get(f"/api/kg/graph/{root.id}", params={"depth": 1}, headers=headers),
            client.get(
                f"/api/admin/graph/{root.id}",
                params={"user_id": LEGACY_OWNER_USER_ID, "depth": 1},
                headers=headers,
            ),
        )
        bodies = []
        for response in responses:
            assert response.status_code == 200, response.text
            body = response.json()
            bodies.append(body)
            assert body["edges_matched_at_least"] == 801
            assert body["edges_truncated"] is True
            assert len(body["edges"]) == 800
            assert body["nodes_matched_at_least"] == 802
            assert body["nodes_truncated"] is True
            assert len(body["nodes"]) == 801
            assert all(
                set(node) == {"id", "name", "entity_type", "knowledge_count"} for node in body["nodes"]
            )
            assert all(len(node["id"]) <= 160 for node in body["nodes"])
            assert all(len(node["name"]) <= 240 for node in body["nodes"])
            assert all(len(node["entity_type"]) <= 80 for node in body["nodes"])
            encoded = json.dumps(body, ensure_ascii=False)
            assert secret not in encoded
            assert "metadata_json" not in encoded
            assert "description" not in encoded
            assert "aliases_json" not in encoded
            assert "user_id" not in encoded
        assert [edge["id"] for edge in bodies[0]["edges"]] == [edge["id"] for edge in bodies[1]["edges"]]


def test_relation_create_response_and_audit_use_separate_bounded_allowlists(settings) -> None:
    secret = "SYNTHETIC_PRIVATE_RELATION_POST_SENTINEL_" + "z" * 250_000
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        source = app.state.kg.create_entity(
            LEGACY_OWNER_USER_ID,
            "Альфа",
            EntityType.PROJECT,
        )
        target = app.state.kg.create_entity(
            LEGACY_OWNER_USER_ID,
            "Бета",
            EntityType.ORGANIZATION,
        )
        response = client.post(
            "/api/kg/relations",
            headers=headers,
            json={
                "source_entity_id": source["id"],
                "target_entity_id": target["id"],
                "relation_type": "related_to",
                "metadata": {"private": secret, "unbounded": "x" * 250_000},
            },
        )
        assert response.status_code == 200, response.text
        relation = response.json()["relation"]
        assert set(relation) <= _PUBLIC_RELATION_KEYS
        assert "metadata_json" not in relation
        assert "user_id" not in relation
        assert secret not in json.dumps(relation, ensure_ascii=False)
        assert all(len(value) <= 240 for value in relation.values() if isinstance(value, str))

        audit_rows = [
            row
            for row in app.state.storage.list_audit_log(LEGACY_OWNER_USER_ID, limit=20)
            if row["action"] == "relation.create"
        ]
        assert len(audit_rows) == 1
        audit_after = json.loads(str(audit_rows[0]["after_json"]))
        assert set(audit_after) <= {
            "id",
            "source_entity_id",
            "target_entity_id",
            "relation_type",
            "weight",
            "valid_from",
            "valid_to",
            "created_at",
        }
        encoded_audit = json.dumps(audit_after, ensure_ascii=False)
        assert secret not in encoded_audit
        assert "metadata_json" not in audit_after
        assert "user_id" not in audit_after

        revision_count = app.state.storage.execute(
            "SELECT COUNT(*) AS count FROM relation_revisions WHERE relation_id=?",
            (relation["id"],),
        ).fetchone()["count"]
        replay = client.post(
            "/api/kg/relations",
            headers=headers,
            json={
                "source_entity_id": source["id"],
                "target_entity_id": target["id"],
                "relation_type": "related_to",
                "weight": 0.1,
                "metadata": {"private": "fabricated-caller-value", "origin": "forged"},
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["relation"] == relation
        assert (
            app.state.storage.execute(
                "SELECT COUNT(*) AS count FROM relation_revisions WHERE relation_id=?",
                (relation["id"],),
            ).fetchone()["count"]
            == revision_count
        )
        replay_audits = [
            row
            for row in app.state.storage.list_audit_log(LEGACY_OWNER_USER_ID, limit=20)
            if row["action"] == "relation.create.idempotent"
        ]
        assert len(replay_audits) == 1
        replay_after = json.loads(str(replay_audits[0]["after_json"]))
        assert replay_after["idempotent_replay"] is True
        assert replay_after["id"] == relation["id"]
        assert replay_after["weight"] == relation["weight"]
        assert "fabricated-caller-value" not in json.dumps(replay_after, ensure_ascii=False)
