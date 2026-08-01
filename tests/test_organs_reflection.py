"""Reflection organ — periodic self-review digest + on-demand endpoint.

Covers the deterministic digest content, the min-knowledge and quiet-hours
gates, weekly dedup, the organ-contributed ``reflection.read`` capability, and
the ``GET /api/reflection`` endpoint (fast deterministic path + opt-in
model synthesis). No canonical knowledge is written by reflection.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from friday.knowledge_graph import KnowledgeGraph
from friday.organs import ServiceContext
from friday.organs.reflection import (
    REFLECTION_CAPABILITY,
    ReflectionOrgan,
    build_reflection,
    format_reflection,
)
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import KnowledgeObject, RawObject, new_id


def _reflection_settings(*, min_knowledge: int = 1, quiet_start: int = 0, quiet_end: int = 0):
    from friday.config import load_settings

    return replace(
        load_settings(),
        reflection_enabled=True,
        reflection_min_knowledge=min_knowledge,
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
    )


def _seed_telegram_user(storage, chat_id: str) -> str:
    user_id = f"telegram:test:{chat_id}"
    storage.ensure_user(user_id, source="telegram", metadata={"chat_id": chat_id})
    return user_id


def _seed_knowledge(storage, user_id: str, title: str, content: str, tags: list[str]) -> None:
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
        tags_json=tags,
    )
    storage.store_knowledge_object(ko)


# --- deterministic digest -------------------------------------------------


def test_build_reflection_summarises_state(storage):
    storage.ensure_user("alice")
    _seed_knowledge(storage, "alice", "Заметка A", "про питон", ["python", "идеи"])
    _seed_knowledge(storage, "alice", "Заметка B", "про дом", ["python"])

    digest = build_reflection(storage, "alice")
    assert digest["knowledge_total"] == 2
    assert digest["active"] == 2
    tags = {t["tag"]: t["count"] for t in digest["top_tags"]}
    assert tags.get("python") == 2
    assert "Заметка A" in digest["recent_titles"]

    message = format_reflection(digest, narrative="Итог: фокус на python.")
    assert "Взгляд на вашу базу знаний" in message
    assert "python" in message
    assert "Итог: фокус на python." in message


# --- worker: enqueue, gates, dedup ---------------------------------------


@pytest.mark.asyncio
async def test_reflection_worker_enqueues_weekly_digest_and_dedups(storage):
    settings = _reflection_settings(min_knowledge=1)
    user_id = _seed_telegram_user(storage, "5001")
    _seed_knowledge(storage, user_id, "Проект Orion", "переходит на PostgreSQL", ["orion"])

    from friday.organs.reflection import reflection_digest

    ctx = ServiceContext(
        settings=settings, storage=storage, kg=KnowledgeGraph(storage), ingestion=None, llm=None
    )
    await reflection_digest(ctx)

    pending = storage.list_pending_notifications(limit=100)
    assert len(pending) == 1
    assert "Взгляд на вашу базу знаний" in pending[0]["body"]
    assert pending[0]["chat_id"] == "5001"

    # A second run in the same ISO week does not enqueue another digest.
    await reflection_digest(ctx)
    assert len(storage.list_pending_notifications(limit=100)) == 1


@pytest.mark.asyncio
async def test_reflection_worker_skips_almost_empty_base(storage):
    settings = _reflection_settings(min_knowledge=3)
    user_id = _seed_telegram_user(storage, "5001")
    _seed_knowledge(storage, user_id, "Одна", "мало", [])  # below threshold

    from friday.organs.reflection import reflection_digest

    ctx = ServiceContext(
        settings=settings, storage=storage, kg=KnowledgeGraph(storage), ingestion=None, llm=None
    )
    await reflection_digest(ctx)
    assert storage.list_pending_notifications(limit=100) == []


@pytest.mark.asyncio
async def test_reflection_worker_respects_quiet_hours(storage):
    now_hour = datetime.now(UTC).hour
    settings = _reflection_settings(min_knowledge=1, quiet_start=now_hour, quiet_end=(now_hour + 1) % 24)
    user_id = _seed_telegram_user(storage, "5001")
    _seed_knowledge(storage, user_id, "Ночью", "текст", [])

    from friday.organs.reflection import reflection_digest

    ctx = ServiceContext(
        settings=settings, storage=storage, kg=KnowledgeGraph(storage), ingestion=None, llm=None
    )
    await reflection_digest(ctx)
    assert storage.list_pending_notifications(limit=100) == []


# --- capability + endpoint ------------------------------------------------


def test_reflection_organ_contributes_capability():
    organ = ReflectionOrgan()
    assert REFLECTION_CAPABILITY in list(organ.capabilities())
    assert REFLECTION_CAPABILITY.source == "organ"
    assert any(w.name == "reflection_digest" for w in organ.workers(_dummy_ctx()))


def test_reflection_endpoint_returns_digest_for_actor(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        _seed_knowledge(app.state.storage, LEGACY_OWNER_USER_ID, "Запись", "контент", ["t"])

        response = client.get("/api/reflection", headers=owner)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["digest"]["knowledge_total"] == 1
        assert "Взгляд на вашу базу знаний" in body["message"]

        # The capability is registered and enforced: an unauthenticated call fails.
        assert client.get("/api/reflection").status_code == 401


def _dummy_ctx():
    from friday.config import load_settings

    return ServiceContext(settings=load_settings(), storage=None, kg=None, ingestion=None, llm=None)
