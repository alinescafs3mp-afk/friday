"""Profile (user model) + Chronicle (temporal presence) organs.

Profile: deterministic model of people/projects/interests, on-demand endpoint,
capability-gated. Chronicle: episodic window query + "on this day" resurfacing
push (dedup per day, quiet hours, allowlist). Neither writes to the graph.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jericho.knowledge_graph import KnowledgeGraph
from jericho.organs import ServiceContext, build_registry
from jericho.organs.chronicle import ChronicleOrgan, _years_ago, build_on_this_day, chronicle_on_this_day
from jericho.organs.profile import PROFILE_CAPABILITY, ProfileOrgan, build_profile, format_profile
from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.server import create_app
from jericho.storage.models import EntityType, KnowledgeObject, RawObject, new_id


def _seed_telegram_user(storage, chat_id: str) -> str:
    user_id = f"telegram:test:{chat_id}"
    storage.ensure_user(user_id, source="telegram", metadata={"chat_id": chat_id})
    return user_id


def _seed_knowledge(
    storage, user_id: str, title: str, tags: list[str], *, created_at: str | None = None
) -> str:
    content = title
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(new_id("h").encode()).hexdigest(),
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
    if created_at:
        # Commit the backdated timestamp so it is visible on other connections
        # (the chronicle worker / endpoint reads it from a different thread).
        with storage.transaction() as conn:
            conn.execute("UPDATE knowledge_objects SET created_at=? WHERE id=?", (created_at, ko.id))
    return ko.id


def _link(storage, ko_id: str, user_id: str, name: str, entity_type: EntityType) -> None:
    graph = KnowledgeGraph(storage)
    entity = graph.create_entity(user_id, name, entity_type)
    graph.link_knowledge_to_entity(ko_id, entity["id"], user_id, status="accepted", reviewed_by=user_id)


# --- profile --------------------------------------------------------------


def test_build_profile_ranks_people_projects_interests(storage):
    storage.ensure_user("alice")
    k1 = _seed_knowledge(storage, "alice", "Встреча с Иваном", ["работа", "встречи"])
    k2 = _seed_knowledge(storage, "alice", "Ещё про Ивана", ["работа"])
    _seed_knowledge(storage, "alice", "Разное", ["работа"])
    _link(storage, k1, "alice", "Иван Петров", EntityType.PERSON)
    _link(storage, k2, "alice", "Иван Петров", EntityType.PERSON)
    graph = KnowledgeGraph(storage)
    graph.create_container("alice", "Ремонт", kind="project")
    box = graph.create_container("alice", "Идеи", kind="collection")
    graph.link_knowledge_to_entity(k1, box["id"], "alice", status="accepted", reviewed_by="alice")

    profile = build_profile(storage, "alice")
    assert profile["knowledge_total"] == 3
    assert profile["people"][0]["name"] == "Иван Петров"
    assert profile["people"][0]["knowledge_count"] == 2
    # Only containers with members appear.
    assert any(p["name"] == "Идеи" for p in profile["projects"])
    assert not any(p["name"] == "Ремонт" for p in profile["projects"])
    tags = {t["tag"] for t in profile["interests"]}
    assert "работа" in tags

    message = format_profile(profile, portrait="Портрет: фокус на работе.")
    assert "Ваш профиль" in message
    assert "Иван Петров" in message
    assert "Портрет: фокус на работе." in message


def test_profile_endpoint_gated_and_returns_model(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        _seed_knowledge(app.state.storage, LEGACY_OWNER_USER_ID, "Заметка", ["t"])
        response = client.get("/api/profile", headers=owner)
        assert response.status_code == 200, response.text
        assert response.json()["profile"]["knowledge_total"] == 1
        assert "Ваш профиль" in response.json()["message"]
        assert client.get("/api/profile").status_code == 401


def test_profile_organ_contributes_capability_only():
    organ = ProfileOrgan()
    assert PROFILE_CAPABILITY in list(organ.capabilities())
    ctx = _dummy_ctx()
    assert list(organ.workers(ctx)) == []  # profile is pull-only, no push
    assert organ.router() is not None


# --- chronicle ------------------------------------------------------------


def test_years_ago_labels():
    now = datetime(2026, 7, 23, tzinfo=UTC)
    assert _years_ago("2025-07-23T10:00:00+00:00", now) == "год назад"
    assert _years_ago("2023-07-23T10:00:00+00:00", now) == "3 года назад"
    assert _years_ago("2019-07-23T10:00:00+00:00", now) == "7 лет назад"
    assert _years_ago("2026-07-23T10:00:00+00:00", now) == ""  # this year → no label


def test_on_this_day_finds_past_year_anniversaries(storage):
    storage.ensure_user("alice")
    now = datetime.now(UTC)
    last_year = now.replace(year=now.year - 1).isoformat()
    _seed_knowledge(storage, "alice", "Годовщина", ["memory"], created_at=last_year)
    _seed_knowledge(storage, "alice", "Сегодняшнее", ["today"])  # created today, not an anniversary

    memories = build_on_this_day(storage, "alice", now)
    titles = [m["title"] for m in memories]
    assert "Годовщина" in titles
    assert "Сегодняшнее" not in titles
    assert memories[0]["ago"] == "год назад"


@pytest.mark.asyncio
async def test_chronicle_worker_pushes_on_this_day_and_dedups(storage):
    settings = _chronicle_settings()
    user_id = _seed_telegram_user(storage, "5001")
    now = datetime.now(UTC)
    _seed_knowledge(
        storage,
        user_id,
        "Год назад — старт Orion",
        ["orion"],
        created_at=now.replace(year=now.year - 1).isoformat(),
    )

    ctx = ServiceContext(
        settings=settings, storage=storage, kg=KnowledgeGraph(storage), ingestion=None, llm=None
    )
    await chronicle_on_this_day(ctx)

    pending = storage.list_pending_notifications(limit=100)
    assert len(pending) == 1
    assert "В этот день" in pending[0]["body"]
    assert "Orion" in pending[0]["body"]

    # Same calendar day → no duplicate.
    await chronicle_on_this_day(ctx)
    assert len(storage.list_pending_notifications(limit=100)) == 1


@pytest.mark.asyncio
async def test_chronicle_worker_silent_without_anniversaries(storage):
    settings = _chronicle_settings()
    user_id = _seed_telegram_user(storage, "5001")
    _seed_knowledge(storage, user_id, "Только сегодня", [])  # created now, nothing from the past

    ctx = ServiceContext(
        settings=settings, storage=storage, kg=KnowledgeGraph(storage), ingestion=None, llm=None
    )
    await chronicle_on_this_day(ctx)
    assert storage.list_pending_notifications(limit=100) == []


def test_chronicle_window_endpoint(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        _seed_knowledge(app.state.storage, LEGACY_OWNER_USER_ID, "На этой неделе", ["w"])
        response = client.get("/api/chronicle", params={"days": 7}, headers=owner)
        assert response.status_code == 200, response.text
        titles = [k["title"] for k in response.json()["window"]["recent_knowledge"]]
        assert "На этой неделе" in titles
        assert client.get("/api/chronicle").status_code == 401


def test_chronicle_organ_uses_all_three_extension_points():
    organ = ChronicleOrgan()
    assert list(organ.capabilities())
    assert any(w.name == "chronicle_on_this_day" for w in organ.workers(_dummy_ctx()))
    assert organ.router() is not None


def _chronicle_settings():
    from jericho.config import load_settings

    return replace(load_settings(), chronicle_enabled=True, quiet_hours_start=0, quiet_hours_end=0)


def _dummy_ctx():
    from jericho.config import load_settings

    return ServiceContext(settings=load_settings(), storage=None, kg=None, ingestion=None, llm=None)


def test_registry_includes_profile_and_chronicle(settings):
    names = {o.name for o in build_registry(settings).organs}
    assert {"profile", "chronicle"} <= names


def test_a_self_registered_newcomer_is_a_valid_push_target(settings, storage):
    """Открытая регистрация впускает человека РЕШЕНИЕМ BACKEND — он же заводит
    учётку и кладёт `chat_id` в её метаданные.

    Мост это учитывал, а органы проверяли только статический список, и
    самозарегистрированный человек не получал НИ ОДНОГО проактивного сообщения:
    ни напоминаний, ни хроники, ни предупреждений sentinel. Молча — отказ
    выглядел как «ему нечего сказать».

    Мутация: вернуть проверку «только статический список» — тест обязан
    покраснеть на первом же утверждении.
    """
    from dataclasses import replace

    from jericho.organs import may_push_to

    storage.ensure_user("newbie", preset_key="newcomer")
    storage.update_user("newbie", metadata_json={"chat_id": "7777"})
    storage.ensure_user("listed", preset_key="user")
    storage.update_user("listed", metadata_json={"chat_id": "5001"})

    opened = replace(settings, telegram_allowed_chat_ids=[5001], telegram_open_registration=True)
    assert may_push_to(opened, storage, "newbie", "7777") is True
    assert may_push_to(opened, storage, "listed", "5001") is True

    # Открытую регистрацию выключили — новичкам больше не пишем, deny-by-default.
    closed = replace(settings, telegram_allowed_chat_ids=[5001], telegram_open_registration=False)
    assert may_push_to(closed, storage, "newbie", "7777") is False
    assert may_push_to(closed, storage, "listed", "5001") is True

    # Чужой чат не проходит ни при каких настройках.
    assert may_push_to(opened, storage, "listed", "999999") is False


def test_promoting_a_newcomer_does_not_silence_their_notifications(settings, storage):
    """Повышение пресета — награда, а не наказание.

    Первая редакция гейта смотрела на `preset_key == "newcomer"`, и человек терял
    ВСЕ проактивные сообщения ровно в тот момент, когда владелец повышал его до
    обычного пресета: то есть за то, что его признали своим. Спрашивать надо факт
    ВПУСКА (`self_registered`, который пишет сам backend), а не текущую роль.

    Мутация: убрать проверку `self_registered` — тест обязан покраснеть.
    """
    from dataclasses import replace

    from jericho.organs import may_push_to

    storage.ensure_user("promoted", preset_key="newcomer")
    storage.update_user("promoted", metadata_json={"chat_id": "7777", "self_registered": True})
    opened = replace(settings, telegram_allowed_chat_ids=[5001], telegram_open_registration=True)
    assert may_push_to(opened, storage, "promoted", "7777") is True

    # Владелец повышает его — ровно то, ради чего админка и делалась.
    storage.update_user("promoted", preset_key="user")
    assert may_push_to(opened, storage, "promoted", "7777") is True, (
        "повышение пресета молча отключило человеку все проактивные сообщения"
    )

    # Посторонний аккаунт, которого backend не впускал, по-прежнему не проходит.
    storage.ensure_user("stranger", preset_key="user")
    storage.update_user("stranger", metadata_json={"chat_id": "8888"})
    assert may_push_to(opened, storage, "stranger", "8888") is False
