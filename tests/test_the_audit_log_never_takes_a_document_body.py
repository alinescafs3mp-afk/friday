"""Журнал аудита не берёт тело документа — ни от одного маршрута.

Две причины, и каждой хватило бы одной.

Первая: `audit_log` защищён триггерами `audit_log_no_update` и `audit_log_no_delete`,
то есть строку из него нельзя ни исправить, ни удалить. Всё, что туда записано,
переживает и правку, и мягкое удаление, и `purge` — операцию, чья единственная цель
уничтожить всякий след. Проект уже чинил ровно это у самого purge; три соседних
маршрута продолжали писать полную строку Knowledge Object вместе с `content`.

Вторая: журнал отдаётся по праву `admin.audit.read` (риск 2), а содержимое — по
`admin.all_data.read` (риск 3), и иерархии «старшее включает младшее» в системе нет.
Значит аккаунт, которому выдали только «читать журнал», получал через него тела
чужих документов.

Правило простое: в журнал идёт `_knowledge_fingerprint` — кто, какого размера, с
какой контрольной суммой, — но не сам текст.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from friday.server import create_app

SECRET = "СЕКРЕТНАЯ-СТРОКА-КОТОРОЙ-НЕ-МЕСТО-В-ЖУРНАЛЕ"


def _ingest(client, token: str) -> tuple[str, str]:
    created = client.post(
        "/api/ingest",
        # Секрет спрятан ГЛУБОКО в тексте: заголовок выводится из начала документа
        # и попадает в отпечаток намеренно, поэтому различать надо тело, а не первую
        # строку. Иначе тест ловил бы разрешённое.
        json={
            "content": (
                "Договор аренды склада между сторонами, предмет и порядок расчётов. "
                + ("Условия поставки и приёмки описаны в приложении. " * 6)
                + SECRET
            ),
            "force_knowledge": True,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert created.status_code in {200, 201}, created.text
    body = created.json()
    knowledge_id = str(body.get("knowledge_object_id") or (body.get("knowledge_object") or {}).get("id"))
    user_id = str(body.get("user_id") or (body.get("knowledge_object") or {}).get("user_id"))
    assert knowledge_id and knowledge_id != "None", created.text
    return knowledge_id, user_id


def _audit_text(client, token: str) -> str:
    response = client.get("/api/admin/audit?limit=500", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    return json.dumps(response.json(), ensure_ascii=False)


def test_the_mutating_routes_write_a_fingerprint_not_the_text(settings):
    with TestClient(create_app(settings)) as client:
        token = settings.api_token
        headers = {"Authorization": f"Bearer {token}"}
        knowledge_id, user_id = _ingest(client, token)

        # Три маршрута, которые клали в журнал всю строку объекта.
        client.post(
            "/api/admin/cleanup/legacy/apply",
            json={
                "user_id": user_id,
                "action": "keep",
                "knowledge_ids": [knowledge_id],
                "require_suspect": False,
                "reason": "тест",
            },
            headers=headers,
        )
        client.post(
            "/api/admin/lifecycle/apply",
            json={"user_id": user_id, "action": "keep", "knowledge_ids": [knowledge_id]},
            headers=headers,
        )
        client.post(f"/api/admin/knowledge/{knowledge_id}/reenrich", json={}, headers=headers)

        recorded = _audit_text(client, token)
        assert SECRET not in recorded, (
            "тело документа осело в журнале, который нельзя ни исправить, ни удалить"
        )
        # Отпечаток при этом на месте: расследование не должно остаться без опоры.
        assert "content_sha256" in recorded or "content_chars" in recorded
        # Заголовок в отпечатке остаётся НАМЕРЕННО — без него в журнале нельзя
        # понять, о каком объекте речь. Это записано в самом `_knowledge_fingerprint`.
        assert "Договор аренды склада" in recorded


def test_a_journal_reader_cannot_reach_content_through_the_journal(settings):
    """Права разной высоты не должны сходиться в одной строке журнала.

    `admin.audit.read` это риск 2, содержимое — риск 3, и старшее не включает
    младшее автоматически.
    """
    with TestClient(create_app(settings)) as client:
        token = settings.api_token
        headers = {"Authorization": f"Bearer {token}"}
        knowledge_id, user_id = _ingest(client, token)
        client.post(
            "/api/admin/lifecycle/apply",
            json={"user_id": user_id, "action": "keep", "knowledge_ids": [knowledge_id]},
            headers=headers,
        )
        assert SECRET not in _audit_text(client, token)
