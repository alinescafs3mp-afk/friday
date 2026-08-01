"""Уровень, который видит, ЧТО человек работал, но не ЧТО он написал.

Владелец решил, что старший видит сразу всё, включая содержимое. Промежуточного
уровня не было вовсе: либо полный `admin.all_data.read`, либо ничего. Между
«знать, что человек три недели ничего не сдаёт» и «читать его переписку» разница
не в степени, а в роде, и первое не должно покупаться вторым.

`admin.activity.read` — этот уровень. Здесь проверяется не наличие способности, а
то, что она НЕ открывает содержимое ни на одной из трёх поверхностей: HTTP,
инструмент агента и список инструментов. И что старший при этом не потерял
ничего.

Роль собирается кастомным пресетом, а не встроенным ключом: набор способностей
для наблюдателя — решение владельца, и захардкоженный список сделал бы это
решение за него.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from friday.server import create_app
from friday.storage.models import RawObject, new_id

SECRET = "Зарплата Иванова 145000 рублей, премия по итогам квартала"
FILENAME = "ведомость-по-зарплате.xlsx"


def _upload(storage, user_id: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="upload",
        source_ref="/mnt/секретный-каталог/" + FILENAME,
        raw_content=SECRET,
        content_type="file",
        content_hash=hashlib.sha256(SECRET.encode()).hexdigest(),
        metadata_json={"filename": FILENAME, "size_bytes": len(SECRET)},
    )
    storage.store_raw_object(raw)
    return raw.id


@pytest.fixture
def instance(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        storage.ensure_user("subject", preset_key="user")
        _upload(storage, "subject")

        # Роль наблюдателя — ровно две способности.
        made = client.post(
            "/api/admin/presets",
            json={
                "preset_key": "overseer",
                "name": "Наблюдатель",
                "capabilities": ["admin.users.read", "admin.activity.read"],
            },
            headers=owner,
        )
        assert made.status_code == 200, made.text
        client.post(
            "/api/admin/users",
            json={"id": "watcher", "preset_key": "overseer"},
            headers=owner,
        )
        token = client.post("/api/admin/tokens", json={"user_id": "watcher"}, headers=owner)
        assert token.status_code == 200, token.text
        yield client, storage, owner, {"Authorization": f"Bearer {token.json()['token']}"}


def test_the_overseer_learns_the_shape_and_none_of_the_substance(instance):
    client, _, _, watcher = instance
    response = client.get("/api/admin/users/subject/activity", headers=watcher)
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["content"] == "redacted"
    assert payload["summary"]["arrivals"] == 1, "наблюдатель должен видеть, что поступление было"
    item = payload["items"][0]
    assert item["activity"] == "upload"
    assert item["content_chars"] == len(SECRET), "объём — это не содержимое, он должен остаться"

    blob = response.text
    for secret in ("145000", "Иванова", "ведомость", "секретный-каталог"):
        assert secret not in blob, f"уровень без содержимого отдал {secret!r}"


def test_the_full_administrator_lost_nothing(instance):
    client, _, owner, _ = instance
    payload = client.get("/api/admin/users/subject/activity", headers=owner).json()
    assert payload["content"] == "full"
    assert "145000" in payload["items"][0]["preview"]
    assert payload["items"][0]["filename"] == FILENAME


def test_the_overseer_cannot_reach_content_from_any_other_direction(instance):
    """Урезать одну поверхность бессмысленно, если рядом открыта другая.

    Тот же аккаунт лежит в /knowledge, /inbox, /files, /conversations — все под
    `admin.all_data.read`. Уровень не должен получать ни один из них.
    """
    client, _, _, watcher = instance
    for path in (
        "/api/admin/knowledge",
        "/api/admin/inbox",
        "/api/admin/files",
        "/api/admin/conversations",
        "/api/admin/entities",
    ):
        response = client.get(path, params={"user_id": "subject"}, headers=watcher)
        assert response.status_code == 403, (
            f"{path} отдал {response.status_code} наблюдателю без прав на данные"
        )


def test_the_log_says_which_authority_answered(instance):
    """Иначе по журналу нельзя отличить «посмотрел объём» от «прочитал написанное»."""
    client, storage, owner, watcher = instance
    client.get("/api/admin/users/subject/activity", headers=watcher)
    client.get("/api/admin/users/subject/activity", headers=owner)

    rows = [
        row for row in storage.list_audit_log(None, limit=50) if row["action"] == "admin.user.activity.read"
    ]
    modes = {row["user_id"]: _after(row).get("content") for row in rows}
    assert modes.get("watcher") == "redacted"
    assert "full" in modes.values(), f"чтение с содержимым не отмечено как full: {modes}"
    assert any(_after(row).get("granted_by") == "admin.activity.read" for row in rows)


def _after(row) -> dict:
    import json

    try:
        return json.loads(row["after_json"] or "{}")
    except (TypeError, ValueError):
        return {}


def test_the_overseer_cannot_grant_itself_the_wider_level(instance):
    """Проверка способностей сравнивает точный идентификатор, иерархии нет.

    Значит наблюдатель не может делегировать `admin.all_data.read` — он им не
    владеет. Это свойство механизма, а не отдельная защита, и именно поэтому его
    стоит закрепить: оно исчезнет молча, если кто-нибудь введёт «старшая
    способность включает младшую».
    """
    client, _, _, watcher = instance
    response = client.put(
        "/api/admin/users/watcher/permissions/admin.all_data.read",
        json={"effect": "allow"},
        headers=watcher,
    )
    assert response.status_code == 403, response.text
