"""Graph-edge provenance — §26: every relation carries a mandatory origin stamp.

Edges previously stored only a clamped weight; manually POSTed relations had no
origin record, and review-materialized edges lost the extractor's raw
confidence. Provenance now lives in metadata_json and cannot be spoofed by an
API body.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import EntityType


def _edge_metadata(storage, user_id: str) -> dict:
    row = storage.execute(
        "SELECT metadata_json FROM relations WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return json.loads(row["metadata_json"])


def test_api_relations_carry_origin_and_actor_despite_spoof_attempt(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        kg = app.state.kg
        a = kg.create_entity(LEGACY_OWNER_USER_ID, "Orion", EntityType.PROJECT)
        b = kg.create_entity(LEGACY_OWNER_USER_ID, "PostgreSQL", EntityType.CONCEPT)

        response = client.post(
            "/api/kg/relations",
            json={
                "source_entity_id": a["id"],
                "target_entity_id": b["id"],
                "relation_type": "uses",
                "valid_from": "2024/3/5",
                "metadata": {"origin": "forged", "created_by": "mallory", "note": "kept"},
            },
            headers=owner,
        )
        assert response.status_code == 200
        assert response.json()["relation"]["valid_from"] == "2024-03-05"
        invalid_graph = client.get(
            f"/api/kg/graph/{a['id']}", params={"as_of": "not-a-date"}, headers=owner
        )
        assert invalid_graph.status_code == 400
        metadata = _edge_metadata(app.state.storage, LEGACY_OWNER_USER_ID)
        assert metadata["origin"] == "api"
        assert metadata["created_by"] == LEGACY_OWNER_USER_ID
        assert metadata["note"] == "kept"  # honest caller metadata survives


def test_reviewed_candidate_edge_preserves_confidence_and_origin(storage):
    graph = KnowledgeGraph(storage)
    a = graph.create_entity("alice", "Orion", EntityType.PROJECT)
    b = graph.create_entity("alice", "Backend", EntityType.CONCEPT)
    candidate = storage.store_relation_candidate(
        "alice",
        source_entity_id=a["id"],
        target_entity_id=b["id"],
        relation_type="part_of",
        confidence=0.66,
        evidence={"span": "Backend является частью Orion"},
    )
    storage.review_relation_candidate("alice", candidate["id"], "accepted", reviewed_by="alice")

    metadata = _edge_metadata(storage, "alice")
    assert metadata["origin"] == "review"
    assert metadata["candidate_id"] == candidate["id"]
    assert metadata["reviewed_by"] == "alice"
    assert metadata["confidence"] == 0.66
    assert metadata["evidence"]["span"].startswith("Backend")


def test_container_hierarchy_edges_are_stamped(storage):
    graph = KnowledgeGraph(storage)
    root = graph.create_container("alice", "Дом", kind="project")
    graph.create_container("alice", "Кухня", kind="collection", parent_id=root["id"])
    metadata = _edge_metadata(storage, "alice")
    assert metadata["origin"] == "container"
