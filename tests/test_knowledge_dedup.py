"""Near-duplicate Knowledge Object detection — §6.

Entities dedup; knowledge did not, so bulk imports accumulate near-identical
records. Pins the pure pairwise-cosine detector, the store-as-conflict flow
(reusing the near_duplicate type + §5 resolve), the resolve→deprecate loop
end-to-end, exclusion of near-duplicates from the agent's reasoning context,
and the on-demand admin endpoint.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from jericho.dedup import NEAR_DUPLICATE_TYPE, detect_near_duplicates, find_near_duplicate_pairs
from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.retrieval import pack_vector
from jericho.server import create_app
from jericho.storage.models import KnowledgeObject, RawObject, new_id

# --- pure detector --------------------------------------------------------


def test_find_near_duplicate_pairs_thresholds_and_canonicalises():
    vectors = [
        ("b", [1.0, 0.0, 0.0]),
        ("a", [0.99, 0.14, 0.0]),  # ~0.99 cosine with b
        ("c", [0.0, 1.0, 0.0]),  # orthogonal to b
        ("z", [0.0, 0.0, 0.0]),  # zero vector -> skipped
    ]
    pairs = find_near_duplicate_pairs(vectors, threshold=0.9)
    assert len(pairs) == 1
    id_a, id_b, score = pairs[0]
    assert (id_a, id_b) == ("a", "b")  # canonical order, lexicographic
    assert score >= 0.9
    # A high threshold rejects the near-but-not-identical pair.
    assert find_near_duplicate_pairs(vectors, threshold=0.999) == []


# --- storage integration --------------------------------------------------


def _store(storage, user_id: str, content: str, title: str) -> str:
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
        title=title,
        summary=content,
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _index(storage, user_id: str, ko_id: str, vector: list[float], model: str) -> None:
    storage.upsert_knowledge_embeddings(
        [
            {
                "knowledge_object_id": ko_id,
                "user_id": user_id,
                "model": model,
                "dim": len(vector),
                "source_version": 1,
                "content_hash": "h",
                "vector": pack_vector(vector),
            }
        ]
    )


def _dedup_settings(settings):
    from dataclasses import replace

    return replace(settings, embeddings_model="test-embed", dedup_threshold=0.9)


def test_detect_stores_near_duplicate_conflicts(settings, storage):
    cfg = _dedup_settings(settings)
    storage.ensure_user("alice")
    dup_a = _store(storage, "alice", "Купить молоко и хлеб.", "Список A")
    dup_b = _store(storage, "alice", "Купить молоко, хлеб.", "Список B")
    other = _store(storage, "alice", "Позвонить врачу в среду.", "Врач")
    _index(storage, "alice", dup_a, [1.0, 0.0, 0.0], "test-embed")
    _index(storage, "alice", dup_b, [0.98, 0.2, 0.0], "test-embed")
    _index(storage, "alice", other, [0.0, 0.0, 1.0], "test-embed")

    result = detect_near_duplicates(storage, cfg, "alice")
    assert result["detected"] == 1
    conflicts = storage.list_knowledge_conflicts("alice", status="suggested")
    assert len(conflicts) == 1
    assert conflicts[0]["conflict_type"] == NEAR_DUPLICATE_TYPE
    assert {conflicts[0]["knowledge_a_id"], conflicts[0]["knowledge_b_id"]} == {dup_a, dup_b}
    assert other not in {conflicts[0]["knowledge_a_id"], conflicts[0]["knowledge_b_id"]}

    # Re-running is idempotent (ON CONFLICT upsert on the pair), not duplicating.
    detect_near_duplicates(storage, cfg, "alice")
    assert len(storage.list_knowledge_conflicts("alice", status="suggested")) == 1

    # And the §5 resolve action deduplicates: keep A, deprecate the near-copy.
    resolved = storage.resolve_conflict("alice", conflicts[0]["id"], dup_a, reviewed_by="alice")
    assert resolved["deprecated_id"] == dup_b
    assert storage.get_knowledge_object(dup_b, "alice")["lifecycle_stage"] == "deprecated"


def test_detect_no_model_is_a_clean_noop(settings, storage):
    storage.ensure_user("alice")
    result = detect_near_duplicates(storage, settings, "alice")  # embeddings_model = ""
    assert result == {"detected": 0, "reason": "embeddings model not configured"}


@pytest.mark.asyncio
async def test_near_duplicates_excluded_from_agent_context(settings, storage):
    import json

    from jericho.agent_runtime import AgentRuntime
    from jericho.permissions import ActorContext

    storage.ensure_user("alice")
    a = _store(storage, "alice", "Купить молоко и хлеб.", "Список A")
    b = _store(storage, "alice", "Купить молоко, хлеб.", "Список B")
    storage.store_knowledge_conflict("alice", a, b, conflict_type=NEAR_DUPLICATE_TYPE, confidence=0.97)

    class _Searcher:
        async def search(self, *a, **k):
            return {
                "results": [
                    {**storage.get_knowledge_object(a, "alice"), "_score": 0.9, "_entities": []},
                    {**storage.get_knowledge_object(b, "alice"), "_score": 0.85, "_entities": []},
                ],
                "entity_matches": [],
            }

    class _LLM:
        enabled = True
        model = "x"

        def __init__(self):
            self.payload = None

        async def chat(self, messages, **kwargs):
            for item in messages:
                content = str(item.get("content") or "")
                if "JERICHO_CONTEXT_DATA" in content and "{" in content:
                    self.payload = json.loads(content[content.index("{") :])
            return {"content": "ок"}

    llm = _LLM()
    await AgentRuntime(settings, storage, llm=llm).chat(
        "alice",
        "что купить?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        enable_tools=False,
        hybrid_searcher=_Searcher(),
    )
    # No knowledge object in the prompt carries a near_duplicate "conflict".
    for obj in llm.payload["knowledge_objects"]:
        assert "conflict" not in obj


def test_detect_duplicates_endpoint(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        # Default settings have no embeddings model -> clean, gated no-op.
        response = client.post(
            "/api/admin/knowledge/detect-duplicates",
            json={"user_id": LEGACY_OWNER_USER_ID},
            headers=owner,
        )
        assert response.status_code == 200, response.text
        assert response.json()["reason"] == "embeddings model not configured"
        actions = [row["action"] for row in app.state.storage.list_audit_log(None, limit=20)]
        assert "admin.knowledge.detect_duplicates" in actions
        assert client.post("/api/admin/knowledge/detect-duplicates", json={}).status_code == 401
