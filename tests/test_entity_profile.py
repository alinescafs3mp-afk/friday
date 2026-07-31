"""Первый кусок спеки v3 (OPUS_INTEGRATED_SYSTEM_AUDIT_PROMPT.md §6): «вид объекта».

Открыл сущность — видишь связанные документы, теги по ним, честный диапазон
дат (даты ДОКУМЕНТОВ, не загрузки) и сколько связей ждёт проверки человеком,
не только уже подтверждённые. Расширяет существующий инструмент agent'а
entity_lookup — не новый параллельный инструмент, продолжение того же шва.
"""

from __future__ import annotations

import hashlib

import pytest

from jericho.execution_kernel import ExecutionKernel
from jericho.ingestion import IngestionPipeline
from jericho.knowledge_graph import KnowledgeGraph
from jericho.permissions import AuthorizationService
from jericho.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id
from jericho.web_surfer import WebSurfer


def _document(storage, user_id: str, text: str, *, tags: list[str], document_date: str | None) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title="Документ",
        tags_json=tags,
        metadata_json=({"document_date": document_date} if document_date else {}),
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _linked_entity(storage, kg, user_id: str, name: str) -> str:
    entity = Entity(id=new_id("ent"), user_id=user_id, name=name, entity_type=EntityType.PROJECT)
    storage.create_entity(entity)
    return entity.id


@pytest.fixture
def kernel(settings, storage):
    storage.ensure_user("alice")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(settings, storage, graph)
    web = WebSurfer(settings)
    built = ExecutionKernel(auth, settings)
    built.bind_services(storage, graph, web, ingestion)
    return built, auth, storage, graph


@pytest.mark.asyncio
async def test_entity_profile_aggregates_tags_and_date_range(kernel):
    built, auth, storage, graph = kernel
    entity_id = _linked_entity(storage, graph, "alice", "Атлас")
    ko1 = _document(
        storage,
        "alice",
        "Отчёт по проекту Атлас за январь.",
        tags=["отчёт", "квартал"],
        document_date="2026-01-15",
    )
    ko2 = _document(storage, "alice", "Смета по проекту Атлас.", tags=["смета"], document_date="2026-03-02")
    graph.link_knowledge_to_entity(ko1, entity_id, "alice", status="accepted")
    graph.link_knowledge_to_entity(ko2, entity_id, "alice", status="accepted")

    actor = auth.actor_for_user("alice", source="test")
    result = await built.execute("entity_lookup", {"name": "Атлас"}, actor=actor)

    assert result.success is True
    profile = result.data["profile"]
    assert profile["tags"] == ["квартал", "отчёт", "смета"]
    assert profile["document_date_range"] == {"earliest": "2026-01-15", "latest": "2026-03-02"}
    assert profile["documents_without_own_date"] == 0


@pytest.mark.asyncio
async def test_entity_profile_counts_documents_without_their_own_date_separately(kernel):
    """Найдено собственным правилом проекта: дата документа и дата загрузки —
    разные факты, и путать их запрещено с давних пор (см. историю про
    document_date). Документ без document_date не должен молча выпадать из
    диапазона ИЛИ подменяться датой загрузки — он должен быть честно посчитан
    отдельно.

    Мутация: убрать ветку `else: undated_count += 1` — тест обязан покраснеть.
    """
    built, auth, storage, graph = kernel
    entity_id = _linked_entity(storage, graph, "alice", "Атлас")
    ko1 = _document(storage, "alice", "Документ с датой.", tags=[], document_date="2026-01-15")
    ko2 = _document(storage, "alice", "Документ без даты.", tags=[], document_date=None)
    graph.link_knowledge_to_entity(ko1, entity_id, "alice", status="accepted")
    graph.link_knowledge_to_entity(ko2, entity_id, "alice", status="accepted")

    actor = auth.actor_for_user("alice", source="test")
    result = await built.execute("entity_lookup", {"name": "Атлас"}, actor=actor)

    profile = result.data["profile"]
    assert profile["document_date_range"] == {"earliest": "2026-01-15", "latest": "2026-01-15"}
    assert profile["documents_without_own_date"] == 1


@pytest.mark.asyncio
async def test_entity_profile_reports_no_date_range_when_nothing_is_dated(kernel):
    built, auth, storage, graph = kernel
    entity_id = _linked_entity(storage, graph, "alice", "Атлас")
    ko1 = _document(storage, "alice", "Без даты первый.", tags=[], document_date=None)
    graph.link_knowledge_to_entity(ko1, entity_id, "alice", status="accepted")

    actor = auth.actor_for_user("alice", source="test")
    result = await built.execute("entity_lookup", {"name": "Атлас"}, actor=actor)

    profile = result.data["profile"]
    assert profile["document_date_range"] is None
    assert profile["documents_without_own_date"] == 1


@pytest.mark.asyncio
async def test_entity_profile_counts_pending_relations_separately_from_confirmed(kernel):
    """Подтверждённые связи (`relations`) и ожидающие проверки (`pending_relations_count`)
    — разные вещи. Карточка сущности, показывающая только подтверждённые,
    молчала бы про очередь ревью, которая касается именно этой сущности.

    Мутация: вернуть 0 вместо настоящего счётчика в `count_pending_relations` —
    тест обязан покраснеть.
    """
    built, auth, storage, graph = kernel
    left_id = _linked_entity(storage, graph, "alice", "Атлас")
    right_id = _linked_entity(storage, graph, "alice", "Полярис")
    ko = _document(storage, "alice", "Атлас использует Полярис.", tags=[], document_date=None)
    graph.link_knowledge_to_entity(ko, left_id, "alice", status="accepted")
    graph.link_knowledge_to_entity(ko, right_id, "alice", status="accepted")
    storage.store_relation_candidate(
        "alice", left_id, right_id, "uses", confidence=0.8, evidence={"method": "test"}
    )

    actor = auth.actor_for_user("alice", source="test")
    result = await built.execute("entity_lookup", {"name": "Атлас"}, actor=actor)

    assert result.data["relations"] == [], "связь ещё не подтверждена — не должна быть в relations"
    assert result.data["pending_relations_count"] == 1


@pytest.mark.asyncio
async def test_entity_profile_tags_do_not_leak_across_tenants(kernel):
    built, auth, storage, graph = kernel
    storage.ensure_user("bob", preset_key="user")
    entity_id = _linked_entity(storage, graph, "alice", "Атлас")
    ko = _document(storage, "alice", "Секретный проект.", tags=["секрет"], document_date="2026-01-01")
    graph.link_knowledge_to_entity(ko, entity_id, "alice", status="accepted")

    bob = auth.actor_for_user("bob", source="test")
    result = await built.execute("entity_lookup", {"name": "Атлас"}, actor=bob)

    assert result.data["found"] is False, "чужая сущность не должна находиться по имени"


def test_http_entity_profile_by_name_matches_the_agent_tool_shape(settings):
    """GET /api/kg/entity-profile?name=... — та же композиция (`kg.entity_profile`),
    что и агентский инструмент entity_lookup, но по HTTP и без участия модели.
    Основа для детерминированной команды /profile в Telegram, которая не зависит
    от того, решит ли модель позвать инструмент (см. TASKS.md #72)."""
    from fastapi.testclient import TestClient

    from jericho.permissions import LEGACY_OWNER_USER_ID
    from jericho.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        storage = app.state.storage
        kg = app.state.kg
        entity_id = _linked_entity(storage, kg, LEGACY_OWNER_USER_ID, "Атлас")
        ko = _document(
            storage, LEGACY_OWNER_USER_ID, "Отчёт по Атласу.", tags=["отчёт"], document_date="2026-02-01"
        )
        kg.link_knowledge_to_entity(ko, entity_id, LEGACY_OWNER_USER_ID, status="accepted")

        response = client.get("/api/kg/entity-profile", params={"name": "Атлас"}, headers=owner)
        assert response.status_code == 200
        body = response.json()
        assert body["entity"]["id"] == entity_id
        assert body["profile"]["tags"] == ["отчёт"]
        assert body["profile"]["document_date_range"] == {"earliest": "2026-02-01", "latest": "2026-02-01"}
        assert body["pending_relations_count"] == 0

        missing = client.get("/api/kg/entity-profile", params={"name": "Нет такой сущности"}, headers=owner)
        assert missing.status_code == 404
