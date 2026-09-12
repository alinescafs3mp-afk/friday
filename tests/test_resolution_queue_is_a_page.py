"""Очередь слияний в админке называет себя страницей, а не всем объёмом.

Хранилище давно умеет и лимит, и смещение, и счёт (`count_resolution_candidates`),
а маршрут не брал ничего: отдавал умолчательные 500 строк и `count`, равный длине
той же страницы. На корпусе владельца кандидатур 45 947 — оператор видел «500» и
не мог отличить это от «всего 500».

Второе, из той же семьи: `knowledge_count` строки считался как
`len(get_entity_knowledge(..., limit=1000))`. Для сущности с 45 000 документов это
ровно 1000 — число, которое ничего не значит, но выглядит как факт; и стоило оно
500 строк × 2 сущности × выборку в тысячу записей на каждую отрисовку экрана.

Родственное: карточка объекта, где та же ошибка была замерена (у 200 самых широких
сущностей все 200 занижали счёт документов).
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from friday.server import create_app
from friday.storage.models import (
    Entity,
    EntityResolutionCandidate,
    EntityType,
    KnowledgeObject,
    RawObject,
    ResolutionStatus,
    new_id,
)


def _seed_candidates(storage, user_id: str, count: int) -> list[str]:
    storage.ensure_user(user_id)
    ids: list[str] = []
    for index in range(count):
        left = Entity(
            id=new_id("ent"), user_id=user_id, name=f"Иванов И.И. {index}", entity_type=EntityType.PERSON
        )
        right = Entity(
            id=new_id("ent"), user_id=user_id, name=f"Иванов Иван {index}", entity_type=EntityType.PERSON
        )
        storage.create_entity(left)
        storage.create_entity(right)
        stored = storage.store_resolution_candidate(
            EntityResolutionCandidate(
                id=new_id("res"),
                user_id=user_id,
                entity_a_id=left.id,
                entity_b_id=right.id,
                confidence=0.9 - index * 0.001,
                resolution_method="name_similarity",
                evidence_json={"reason": "тест"},
            )
        )
        ids.append(stored.id)
    return ids


@pytest.fixture
def admin(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        user_id = client.get("/api/admin/users", headers=headers).json()["items"][0]["id"]
        yield app, client, headers, user_id


def test_the_queue_reports_the_whole_size_not_the_page_size(admin) -> None:
    """Мутация: убрать `total` из ответа маршрута — тест краснеет."""
    app, client, headers, user_id = admin
    _seed_candidates(app.state.storage, user_id, 12)

    response = client.get(
        f"/api/admin/resolutions?user_id={user_id}&status=suggested&limit=5", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["items"]) == 5, "лимит не применён — страница не страница"
    assert body["count"] == 5
    assert body["total"] == 12, (
        "оператор видит длину страницы и не может отличить её от полного объёма очереди"
    )
    assert body["limit"] == 5
    assert body["offset"] == 0


def test_the_next_page_shows_what_the_first_one_hid(admin) -> None:
    import json as _json

    app, client, headers, user_id = admin
    storage = app.state.storage
    ids = _seed_candidates(storage, user_id, 12)

    def _sql_row(table: str, row_id: str) -> dict:
        allowed = {
            "entity_resolution_candidates": "entity_resolution_candidates",
            "entities": "entities",
        }
        row = storage.execute(
            f"SELECT * FROM {allowed[table]} WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row is not None, f"{table} {row_id} missing"
        return dict(row)

    def _audit_ordered() -> list[dict]:
        rows = storage.execute(
            "SELECT id, user_id, action, target_type, target_id, before_json, "
            "after_json, ip_address, request_id, created_at "
            "FROM audit_log ORDER BY rowid ASC"
        ).fetchall()
        return [dict(row) for row in rows]

    def _selected_state() -> dict:
        return {
            "ids": list(ids),
            "candidates": [_sql_row("entity_resolution_candidates", item) for item in ids],
            "entities": [
                _sql_row("entities", _sql_row("entity_resolution_candidates", item)["entity_a_id"])
                for item in ids
            ]
            + [
                _sql_row("entities", _sql_row("entity_resolution_candidates", item)["entity_b_id"])
                for item in ids
            ],
            "audit": _audit_ordered(),
        }

    def _page_card(candidate_id: str) -> dict:
        cand = _sql_row("entity_resolution_candidates", candidate_id)
        left = _sql_row("entities", cand["entity_a_id"])
        right = _sql_row("entities", cand["entity_b_id"])
        confidence = float(cand["confidence"])
        if confidence >= 0.95:
            recommendation = "strong_merge_candidate"
        elif confidence >= 0.78:
            recommendation = "compare_context"
        else:
            recommendation = "manual_review"
        return {
            "id": cand["id"],
            "entity_a_id": cand["entity_a_id"],
            "entity_b_id": cand["entity_b_id"],
            "confidence": confidence,
            "resolution_method": "name_similarity",
            "status": "suggested",
            "created_at": str(cand["created_at"] or ""),
            "resolved_at": str(cand["resolved_at"] or ""),
            "entity_a": {
                "id": left["id"],
                "name": left["name"],
                "entity_type": "person",
                "knowledge_count": 0,
                "relation_count": 0,
            },
            "entity_b": {
                "id": right["id"],
                "name": right["name"],
                "entity_type": "person",
                "knowledge_count": 0,
                "relation_count": 0,
            },
            "recommendation": recommendation,
        }

    def _envelope(items: list[dict], *, offset: int) -> dict:
        return {
            "user_id": user_id,
            "items": items,
            "count": 5,
            "total": 12,
            "limit": 5,
            "offset": offset,
            "matched_at_least": 6,
            "truncated": True,
        }

    before = _selected_state()
    first = client.get(
        f"/api/admin/resolutions?user_id={user_id}&status=suggested&limit=5&offset=0",
        headers=headers,
    )
    second = client.get(
        f"/api/admin/resolutions?user_id={user_id}&status=suggested&limit=5&offset=5",
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert _selected_state() == before

    first_cards = [_page_card(item_id) for item_id in ids[:5]]
    second_cards = [_page_card(item_id) for item_id in ids[5:10]]
    assert [item["id"] for item in first_cards] == ids[:5]
    assert [item["id"] for item in second_cards] == ids[5:10]
    assert first.json() == _envelope(first_cards, offset=0)
    assert second.json() == _envelope(second_cards, offset=5)
    assert [item["id"] for item in first.json()["items"]] == ids[:5]
    assert [item["id"] for item in second.json()["items"]] == ids[5:10]
    assert first.json()["count"] == 5
    assert second.json()["count"] == 5
    assert first.json()["total"] == 12
    assert second.json()["total"] == 12
    assert first.json()["limit"] == 5
    assert second.json()["limit"] == 5
    assert first.json()["offset"] == 0
    assert second.json()["offset"] == 5
    assert first_cards[0]["entity_a"]["name"] == "Иванов И.И. 0"
    assert first_cards[0]["confidence"] == 0.9
    assert first_cards[0]["recommendation"] == "compare_context"
    assert set(item["id"] for item in first.json()["items"]).isdisjoint(
        item["id"] for item in second.json()["items"]
    )
    _ = _json


def test_the_document_count_of_a_row_is_a_count_not_a_page_length(admin) -> None:
    """Мутация: вернуть `len(get_entity_knowledge(..., limit=N))` — тест краснеет.

    Порог взят с запасом ВЫШЕ прежнего лимита выборки, чтобы разница была не
    стилистической: при старом коде число упиралось бы в размер страницы.
    """
    app, client, headers, user_id = admin
    storage = app.state.storage
    ids = _seed_candidates(storage, user_id, 1)
    candidate = storage.list_resolution_candidates(user_id, ResolutionStatus.SUGGESTED)[0]
    entity_id = candidate["entity_a_id"]

    documents = 25
    for index in range(documents):
        text = f"Условия договора номер {index}."
        raw = RawObject(
            id=new_id("raw"),
            user_id=user_id,
            source="test",
            source_ref=new_id("src"),
            raw_content=text,
            content_type="text",
            content_hash=hashlib.sha256(f"{user_id}-{index}".encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        knowledge = KnowledgeObject(
            id=new_id("ko"),
            user_id=user_id,
            raw_object_id=raw.id,
            title=f"Договор №{index}",
            content=text,
            content_type="text",
        )
        storage.store_knowledge_object(knowledge)
        storage.link_knowledge_entity(user_id, knowledge.id, entity_id, status="accepted")

    body = client.get(f"/api/admin/resolutions?user_id={user_id}&status=suggested", headers=headers).json()
    row = next(item for item in body["items"] if item["id"] == ids[0])
    assert row["entity_a"]["knowledge_count"] == documents, (
        "счётчик документов взят из длины страницы выборки, а не посчитан"
    )
