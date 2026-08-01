"""Conflicts and merges are review queues; the owner must be able to clear them in chat.

G4: 200 suggested conflicts and 20 merge candidates on the live install, with
zero Telegram path for conflicts (merges already had /merges). The cheapest
path is the same as always: a capability-gated tool plus a bridge command that
shows a portion and never re-shows a decided row.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.server import create_app
from friday.storage.models import KnowledgeObject, RawObject, new_id
from friday.web_surfer import WebSurfer
from tests.conftest import run_with_approval


def _knowledge(storage, user_id: str, title: str, content: str) -> str:
    raw = storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id=user_id,
            source="test",
            source_ref=new_id("src"),
            raw_content=content,
            content_type="text",
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            received_at=datetime.now(UTC).isoformat(),
        )
    )
    return storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id=user_id,
            raw_object_id=raw.id,
            title=title,
            summary=content[:120],
            content=content,
            knowledge_kind="note",
            importance=0.5,
            created_at=datetime.now(UTC).isoformat(),
        )
    ).id


def _seed_conflict(storage, user_id: str) -> str:
    a = _knowledge(storage, user_id, "Первая редакция", "Текст A про объект.")
    b = _knowledge(storage, user_id, "Вторая редакция", "Текст B про объект.")
    row = storage.store_knowledge_conflict(
        user_id,
        a,
        b,
        conflict_type="near_duplicate",
        confidence=0.9,
        evidence={"method": "test"},
    )
    return str(row["id"])


def test_http_conflict_decide_dismisses_and_hides_from_suggested(settings):
    # Seed through the app's own storage: the bearer token authenticates as the
    # legacy owner, not an arbitrary fixture user.
    with TestClient(create_app(settings)) as client:
        from friday.permissions import LEGACY_OWNER_USER_ID

        owner = {"Authorization": f"Bearer {settings.api_token}"}
        app_storage = client.app.state.storage
        conflict_id = _seed_conflict(app_storage, LEGACY_OWNER_USER_ID)

        listed = client.get("/api/kg/conflicts?status=suggested&limit=5", headers=owner)
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["total"] >= 1
        assert any(item["id"] == conflict_id for item in body["items"])

        decided = client.post(
            f"/api/kg/conflicts/{conflict_id}/decide",
            json={"decision": "dismiss"},
            headers=owner,
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["status"] == "dismissed"

        again = client.get("/api/kg/conflicts?status=suggested&limit=20", headers=owner)
        assert again.status_code == 200
        assert all(item["id"] != conflict_id for item in again.json()["items"])


@pytest.mark.asyncio
async def test_conflict_tools_list_and_keep_a(settings, storage):
    storage.ensure_user("alice", preset_key="owner")
    conflict_id = _seed_conflict(storage, "alice")
    conflict = storage.get_knowledge_conflict("alice", conflict_id)
    winner = str(conflict["knowledge_a_id"])
    loser = str(conflict["knowledge_b_id"])

    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    web = WebSurfer(settings)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, web, IngestionPipeline(settings, storage, graph))
    actor = auth.actor_for_user("alice", source="test")
    try:
        listed = await kernel.execute("conflict_list", {"limit": 5}, actor=actor)
        assert listed.success is True
        assert listed.data["total"] >= 1
        assert any(item["id"] == conflict_id for item in listed.data["items"])

        # Вердикт по противоречию объявляет одно из знаний устаревшим, поэтому со
        # спеки v3 §5 он идёт через подтверждение человеком: модель предлагает,
        # служба исполняет. Здесь проверяется сам разбор, а не гейт, — цепочка
        # пройдена целиком помощником.
        decided = await run_with_approval(
            kernel,
            storage,
            "conflict_decide",
            {"conflict_id": conflict_id, "decision": "keep_a"},
            actor=actor,
        )
        assert decided.success is True, decided.error
        assert decided.data["status"] == "resolved"
        assert decided.data["winner_id"] == winner

        loser_row = storage.get_knowledge_object(loser, "alice")
        assert str(loser_row.get("lifecycle_stage") or "") == "deprecated"

        listed_after = await kernel.execute("conflict_list", {"limit": 50}, actor=actor)
        assert all(item["id"] != conflict_id for item in listed_after.data["items"])
    finally:
        await web.close()
