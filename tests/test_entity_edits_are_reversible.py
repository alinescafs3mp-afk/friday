"""Спека v3 §2: исправление объекта обязано быть ОБРАТИМЫМ.

Снимки версий сущности писались с самого начала (`entity_versions`), а обратного
хода не было вовсе: у знаний откат есть давно (`restore_knowledge_version`), у
сущностей — не было. На корпусе, где 4349 узлов-людей и 149 войсковых частей
заведены автоматическими правилами, первая же правка не того узла необратима.

Здесь проверяется весь путь: хранилище → HTTP (self-service, `kg.write`) →
карточка объекта (что показывается) → кнопка отката в Telegram.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.server import create_app
from jericho.storage.models import Entity, EntityType, new_id
from jericho.telegram_bridge import TelegramBridge


def _entity(storage, name: str = "Атлас") -> str:
    entity = Entity(
        id=new_id("ent"),
        user_id=LEGACY_OWNER_USER_ID,
        name=name,
        entity_type=EntityType.PROJECT,
        description="исходное описание",
    )
    storage.create_entity(entity)
    return entity.id


def test_restoring_an_entity_version_is_a_new_version_not_a_rewind(settings, storage):
    """Откат — обычная правка, поэтому история не стирается и откат обратим.

    Мутация: писать снимок ПОВЕРХ (без роста версии) или удалять версии старше
    восстановленной — тест обязан покраснеть на числе версий или на обратном откате.
    """
    from jericho.knowledge_graph import KnowledgeGraph

    storage.ensure_user(LEGACY_OWNER_USER_ID)
    kg = KnowledgeGraph(storage)
    entity_id = _entity(storage)

    kg.update_entity(LEGACY_OWNER_USER_ID, entity_id, name="Атлас-2")
    kg.update_entity(LEGACY_OWNER_USER_ID, entity_id, name="Атлас-3")
    assert kg.get_entity(entity_id, LEGACY_OWNER_USER_ID)["name"] == "Атлас-3"
    versions_before = len(storage.list_entity_versions(entity_id, LEGACY_OWNER_USER_ID))

    restored = kg.restore_entity_version(LEGACY_OWNER_USER_ID, entity_id, 2, reviewed_by="tester")
    assert restored is not None
    assert restored["name"] == "Атлас-2", "вернулось состояние снимка, а не соседнее"

    versions_after = storage.list_entity_versions(entity_id, LEGACY_OWNER_USER_ID)
    assert len(versions_after) == versions_before + 1, "откат — НОВАЯ версия, история не перематывается"
    assert int(versions_after[0]["version"]) > int(versions_before)

    # И обратно: человек, откатившийся по ошибке, возвращается к тому, что было.
    back = kg.restore_entity_version(LEGACY_OWNER_USER_ID, entity_id, 3, reviewed_by="tester")
    assert back is not None and back["name"] == "Атлас-3"


def test_restore_records_who_rolled_back_and_from_which_version(settings, storage):
    from jericho.knowledge_graph import KnowledgeGraph

    storage.ensure_user(LEGACY_OWNER_USER_ID)
    kg = KnowledgeGraph(storage)
    entity_id = _entity(storage)
    kg.update_entity(LEGACY_OWNER_USER_ID, entity_id, name="Переименованный")

    restored = kg.restore_entity_version(LEGACY_OWNER_USER_ID, entity_id, 1, reviewed_by="alice")
    assert restored is not None
    import json

    metadata = json.loads(str(restored.get("metadata_json") or "{}"))
    assert metadata["restored_from_version"] == 1
    assert metadata["restored_by"] == "alice"


def test_restore_refuses_a_version_that_does_not_exist(settings, storage):
    from jericho.knowledge_graph import KnowledgeGraph

    storage.ensure_user(LEGACY_OWNER_USER_ID)
    kg = KnowledgeGraph(storage)
    entity_id = _entity(storage)
    with pytest.raises(LookupError):
        kg.restore_entity_version(LEGACY_OWNER_USER_ID, entity_id, 99)


def test_restore_does_not_cross_tenants(settings, storage):
    """Чужая сущность не восстанавливается даже по точному id: скоуп арендатора
    стоит в самом запросе версий, а не проверяется отдельно сверху."""
    from jericho.knowledge_graph import KnowledgeGraph

    storage.ensure_user(LEGACY_OWNER_USER_ID)
    storage.ensure_user("bob", preset_key="user")
    kg = KnowledgeGraph(storage)
    entity_id = _entity(storage)
    kg.update_entity(LEGACY_OWNER_USER_ID, entity_id, name="Правка владельца")

    with pytest.raises(LookupError):
        kg.restore_entity_version("bob", entity_id, 1)


def test_http_restore_is_self_service_and_audited(settings):
    """Маршрут гейтится `kg.write` (он есть у пресета `user`), пишет аудит и
    возвращает восстановленное состояние — не 500 и не молчаливый успех."""
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        kg = app.state.kg
        entity_id = _entity(storage)
        kg.update_entity(LEGACY_OWNER_USER_ID, entity_id, name="Ошибочное имя")
        headers = {"Authorization": f"Bearer {settings.api_token}"}

        response = client.post(f"/api/kg/entities/{entity_id}/restore", json={"version": 1}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["entity"]["name"] == "Атлас"
        assert response.json()["restored_from_version"] == 1

        missing = client.post(f"/api/kg/entities/{entity_id}/restore", json={"version": 77}, headers=headers)
        assert missing.status_code == 404

        bad = client.post(f"/api/kg/entities/{entity_id}/restore", json={"version": "вчера"}, headers=headers)
        assert bad.status_code == 400

        audit = storage.list_audit_log(LEGACY_OWNER_USER_ID, limit=50)
        assert any(row.get("action") == "entity.restore" for row in audit), (
            "откат — изменение канонического объекта, он обязан быть в аудите"
        )


def test_profile_reports_edit_history_and_the_version_a_rollback_would_reach(settings):
    """Карточка объекта показывает, что его правили, и куда ведёт откат.

    Мутация: отдавать `restorable_version` = последняя версия (то есть текущее
    состояние) — тест обязан покраснеть: откат «к самому себе» ничего не меняет,
    а кнопка обещает обратное.
    """
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        kg = app.state.kg
        entity_id = _entity(storage)
        profile = kg.entity_profile(entity_id, LEGACY_OWNER_USER_ID)
        assert profile["edits"]["versions"] == 1
        assert profile["edits"]["restorable_version"] is None, "править ещё нечего — откатывать не к чему"

        kg.update_entity(LEGACY_OWNER_USER_ID, entity_id, name="Новое имя")
        profile = kg.entity_profile(entity_id, LEGACY_OWNER_USER_ID)
        assert profile["edits"]["versions"] == 2
        assert profile["edits"]["restorable_version"] == 1
        assert profile["edits"]["last_edited_at"]

        response = client.get(
            "/api/kg/entity-profile",
            params={"name": "Новое имя"},
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 200
        assert response.json()["edits"]["restorable_version"] == 1


@pytest.mark.asyncio
async def test_telegram_undo_button_carries_the_version_it_was_shown_for(settings, storage, monkeypatch):
    """Версия едет В КНОПКЕ, а не вычисляется в момент нажатия.

    Иначе между показом карточки и нажатием могла бы вклиниться другая правка, и
    «отменить последнюю» отменило бы не ту, которую человек видел на экране.

    Мутация: собрать `callback_data` без версии (`ent:undo:{id}`) и брать версию
    из свежего профиля в обработчике — тест обязан покраснеть.
    """
    bridge = TelegramBridge.__new__(TelegramBridge)
    sent: list[dict] = []

    async def _fake_send(_self, _telegram, chat_id, text, *, reply_markup=None, **_kwargs):
        sent.append({"text": text, "markup": reply_markup})

    async def _fake_backend_json(*_args, **_kwargs):
        return {
            "entity": {"id": "ent_abc123", "name": "Атлас"},
            "profile": {"tags": [], "document_date_range": None, "documents_without_own_date": 0},
            "relations": [],
            "knowledge_objects": [],
            "knowledge_objects_total": 0,
            "pending_relations_count": 0,
            "event_time": None,
            "edits": {"versions": 3, "last_edited_at": "2026-07-30T10:00:00Z", "restorable_version": 2},
        }

    monkeypatch.setattr(TelegramBridge, "_send_message", _fake_send, raising=False)
    monkeypatch.setattr(TelegramBridge, "_backend_json", _fake_backend_json, raising=False)

    await bridge._send_entity_profile(None, None, 1, "42", {"id": 42}, "Атлас")

    assert sent, "карточка не отправлена"
    assert "Правок: 2, последняя от 2026-07-30" in sent[0]["text"]
    markup = sent[0]["markup"]
    assert markup, "кнопка отката должна быть, откатывать есть к чему"
    button = markup["inline_keyboard"][0][0]
    assert button["callback_data"] == "ent:undo:ent_abc123.2"
    assert len(button["callback_data"].encode()) <= 64, "Telegram: 64 байта на callback_data"
