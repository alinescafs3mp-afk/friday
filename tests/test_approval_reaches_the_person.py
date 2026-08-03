"""Заявка доходит до человека и решается там же (спека v3 §5).

Гейт, о котором человек не узнаёт, — это не защита, а тихая поломка: действие
блокируется, а причина видна только тому, кто догадается заглянуть в отдельную
команду. Поэтому проверяется вся цепочка: модель предложила → заявка ушла в чат
С КНОПКАМИ → нажатие исполнило действие → повторное нажатие не исполнило его
второй раз.

Отдельно проверяется, что «решение записано» и «действие выполнено» — разные
факты. Подтверждённое действие может не состояться (право отобрали, аргументы
изменились, сбой), и выдавать первое за второе нельзя.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from friday.server import create_app
from friday.storage.models import Entity, EntityResolutionCandidate, EntityType, new_id


def _pending_as_bridge(client, settings) -> dict:
    """Очередь глазами моста: маршрут отдаёт её только по подписи моста."""
    import time
    import uuid

    from friday.security import sign_bridge_request

    path = "/api/notifications/pending?limit=20"
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    signer = "42"
    response = client.get(
        path,
        headers={
            "X-Friday-Timestamp": str(timestamp),
            "X-Friday-User": signer,
            "X-Friday-Chat": signer,
            "X-Friday-Nonce": nonce,
            "X-Friday-Signature": sign_bridge_request(
                settings.telegram_bridge_secret,
                timestamp=timestamp,
                method="GET",
                path=path,
                external_user_id=signer,
                chat_id=signer,
                nonce=nonce,
                body=b"",
            ),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _candidate(storage, user_id: str) -> str:
    left = Entity(id=new_id("ent"), user_id=user_id, name="Иванов И.И.", entity_type=EntityType.PERSON)
    right = Entity(id=new_id("ent"), user_id=user_id, name="Иванов Иван", entity_type=EntityType.PERSON)
    storage.create_entity(left)
    storage.create_entity(right)
    return storage.store_resolution_candidate(
        EntityResolutionCandidate(
            id=new_id("res"),
            user_id=user_id,
            entity_a_id=left.id,
            entity_b_id=right.id,
            confidence=0.9,
            resolution_method="name_similarity",
            evidence_json={"reason": "похожие имена"},
        )
    ).id


@pytest.fixture
def api(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        user_id = client.get("/api/admin/users", headers=headers).json()["items"][0]["id"]
        yield app, client, headers, user_id


@pytest.mark.asyncio
async def test_a_request_is_pushed_to_the_person_with_buttons(settings, storage):
    """Мутация: убрать постановку уведомления в `_request_approval` — тест краснеет."""
    from friday.execution_kernel import ExecutionKernel
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.permissions import AuthorizationService
    from friday.web_surfer import WebSurfer

    storage.ensure_user("alice", preset_key="owner")
    # Чат, в который проактивные сообщения вообще доставляются.
    storage.update_user("alice", metadata_json=json.dumps({"chat_id": "42"}))
    candidate_id = _candidate(storage, "alice")

    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    actor = auth.actor_for_user("alice", source="test")

    requested = await kernel.execute(
        "entity_merge_decide", {"candidate_id": candidate_id, "decision": "accept"}, actor=actor
    )
    approval_id = requested.data["approval_id"]

    pending = storage.list_pending_notifications(limit=10)
    mine = [row for row in pending if row.get("kind") == "approval"]
    assert mine, "заявка создана, но человек о ней не узнает"
    assert mine[0]["dedup_key"] == f"approval:{approval_id}", (
        "у уведомления нет ссылки на заявку — кнопку решения по нему не построить"
    )
    assert "решение" in mine[0]["body"].casefold()


def test_the_route_executes_on_approval_and_only_once(api):
    app, client, headers, user_id = api
    storage = app.state.storage
    candidate_id = _candidate(storage, user_id)
    approval = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": candidate_id, "decision": "accept"},
        summary="Слить «Иванов И.И.» и «Иванов Иван»",
    )

    listed = client.get("/api/me/approvals", headers=headers).json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == approval["id"]

    response = client.post(
        f"/api/approvals/{approval['id']}/decide", json={"decision": "approve"}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["executed"] is True, body.get("error")
    assert body["approval"]["status"] == "done"
    assert str(storage.get_resolution_candidate(candidate_id, user_id)["status"]) == "merged"

    # Повторное нажатие той же кнопки — обычное дело в чате, и оно не должно ни
    # выполнять действие второй раз, ни выглядеть поломкой.
    again = client.post(
        f"/api/approvals/{approval['id']}/decide", json={"decision": "approve"}, headers=headers
    )
    assert again.status_code == 404


def test_a_rejection_does_not_execute(api):
    app, client, headers, user_id = api
    storage = app.state.storage
    candidate_id = _candidate(storage, user_id)
    approval = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": candidate_id, "decision": "accept"},
        summary="Слить два узла",
    )
    response = client.post(
        f"/api/approvals/{approval['id']}/decide", json={"decision": "reject"}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["executed"] is False
    assert str(storage.get_resolution_candidate(candidate_id, user_id)["status"]) == "suggested"


def test_an_approval_that_cannot_execute_says_so(api):
    """Согласие человека и успех исполнения — разные факты.

    Мутация: возвращать `executed: True` без проверки `result.success` — тест
    краснеет.
    """
    app, client, headers, user_id = api
    approval = storage_approval = app.state.storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": "res_does_not_exist", "decision": "accept"},
        summary="Слить несуществующее",
    )
    del storage_approval
    response = client.post(
        f"/api/approvals/{approval['id']}/decide", json={"decision": "approve"}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["executed"] is False
    assert body["error"], "исполнение провалилось молча"
    assert body["approval"]["status"] == "failed"


def test_another_tenant_cannot_decide_your_action(api):
    app, client, headers, user_id = api
    storage = app.state.storage
    approval = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": "res_1", "decision": "accept"},
        summary="Слить два узла",
    )
    storage.ensure_user("mallory", preset_key="user")
    import hashlib
    import secrets

    raw_token = secrets.token_urlsafe(32)
    storage.create_api_token(
        "mallory", hashlib.sha256(raw_token.encode()).hexdigest(), label="test", ttl_seconds=3600
    )
    other = {"Authorization": f"Bearer {raw_token}"}

    response = client.post(
        f"/api/approvals/{approval['id']}/decide", json={"decision": "approve"}, headers=other
    )
    assert response.status_code in {403, 404}
    assert storage.get_action_approval(approval["id"], user_id)["status"] == "pending"


def test_the_chat_command_lists_what_waits_and_names_unknown_outcomes(api):
    """`/approvals` — единственное место, где видно и ожидающее, и неизвестное."""
    app, client, headers, user_id = api
    storage = app.state.storage
    storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": "res_1", "decision": "accept"},
        summary="Слить два узла",
    )
    stale = storage.create_action_approval(
        user_id,
        tool="code_run",
        payload={"code": "print(1)"},
        summary="Выполнить код",
    )
    storage.decide_action_approval(stale["id"], user_id, decision="approve", decided_by=user_id)
    storage.claim_action_approval(stale["id"], user_id)
    storage.mark_action_approval_uncertain(stale["id"], user_id, error="прервано")

    waiting = client.get("/api/me/approvals?status=pending", headers=headers).json()
    unknown = client.get("/api/me/approvals?status=uncertain", headers=headers).json()
    assert waiting["total"] == 1
    assert unknown["total"] == 1, "действие с неизвестным исходом нигде не видно"
    assert json.loads(json.dumps(unknown["items"][0]))["error"] == "прервано"


def test_the_bridge_receives_what_it_needs_to_draw_the_buttons(api):
    """Проверяется ВЫДАЧА мосту, а не строка в таблице.

    Между «в очереди лежит правильная запись» и «мост может нарисовать кнопку»
    стоит маршрут `/api/notifications/pending`, который сам решает, какие поля
    отдать. Пока он отдавал только id/chat_id/body, заявка приходила человеку
    текстом «нужно ваше решение», а решить её в этом же сообщении было нечем.
    """
    app, client, headers, user_id = api
    storage = app.state.storage
    approval = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": "res_1", "decision": "accept"},
        summary="Слить два узла",
    )
    storage.update_user(user_id, metadata_json=json.dumps({"chat_id": "42"}))
    storage.enqueue_notification(
        user_id,
        "42",
        f"Нужно ваше решение: {approval['summary']}",
        kind="approval",
        dedup_key=f"approval:{approval['id']}",
    )

    items = _pending_as_bridge(client, app.state.settings)["items"]
    mine = [item for item in items if item.get("kind") == "approval"]
    assert mine, "мост не узнает, что это заявка, и отправит её без кнопок"
    assert mine[0]["dedup_key"] == f"approval:{approval['id']}", (
        "мосту нечего подставить в callback_data — кнопка решения не построится"
    )


def test_a_bystander_pressing_the_button_changes_nothing(api):
    """Кнопка в общей комнате доступна всем, кто её видит.

    Callback приходит от того, КТО НАЖАЛ, а маршрут решает от имени этого актора.
    Значит защита — не в кнопке, а в том, что заявка принадлежит владельцу: чужому
    маршрут обязан ответить «не найдено», не подтвердив даже её существования.

    Мутация: искать заявку без `user_id` — тест краснеет.
    """
    app, client, headers, user_id = api
    storage = app.state.storage
    candidate_id = _candidate(storage, user_id)
    approval = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": candidate_id, "decision": "accept"},
        summary="Слить два узла",
    )

    import hashlib
    import secrets

    storage.ensure_user("bystander", preset_key="user")
    raw_token = secrets.token_urlsafe(32)
    storage.create_api_token(
        "bystander", hashlib.sha256(raw_token.encode()).hexdigest(), label="test", ttl_seconds=3600
    )

    response = client.post(
        f"/api/approvals/{approval['id']}/decide",
        json={"decision": "approve"},
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert response.status_code in {403, 404}
    assert storage.get_action_approval(approval["id"], user_id)["status"] == "pending"
    assert str(storage.get_resolution_candidate(candidate_id, user_id)["status"]) == "suggested"
