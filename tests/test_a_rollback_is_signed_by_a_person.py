"""Откат версии подписывается ЧЕЛОВЕКОМ, а не арендатором.

Подпись пишется на самой записи ради вопроса «кто это откатил» — тот, кто
откроет документ через полгода, должен видеть ответ на нём самом, а не идти в
журнал. В общем архиве `user_id` один на всех: подпись «откатил common» не
отвечает ни на один вопрос, ради которого её пишут.

Найдено чтением 2026-08-05: из десяти мест, передающих `reviewed_by`, девять
берут `own_id` (учётку человека), и только маршрут отката знания брал `user_id`.
Аудит при этом всё это время писал правильно — то есть журнал и сама запись
называли РАЗНЫХ авторов одного действия.
"""

from __future__ import annotations

import hashlib
import inspect

import pytest
from fastapi.testclient import TestClient

from friday.server import create_app
from friday.storage.models import KnowledgeObject, RawObject, new_id


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as opened:
        opened.owner = {"Authorization": f"Bearer {settings.api_token}"}  # type: ignore[attr-defined]
        opened.storage = app.state.storage  # type: ignore[attr-defined]
        yield opened


def _document(storage, user_id: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content="Первая редакция",
        content_type="text",
        content_hash=hashlib.sha256(b"rollback").hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content="Первая редакция",
        content_type="text",
        title="Приказ",
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def test_the_signature_names_the_person_not_the_tenant(client) -> None:
    """Мутация: вернуть `reviewed_by=actor.user_id` — тест краснеет."""

    storage = client.storage
    users = client.get("/api/admin/users", headers=client.owner).json()["items"]
    tenant = users[0]["id"]
    knowledge_id = _document(storage, tenant)
    storage.update_knowledge_fields(knowledge_id, tenant, content="Вторая редакция", reviewed_by=tenant)

    answer = client.post(
        f"/api/admin/knowledge/{knowledge_id}/restore",
        json={"user_id": tenant, "version": 1},
        headers=client.owner,
    )
    assert answer.status_code == 200, answer.text

    restored = storage.get_knowledge_object(knowledge_id, tenant)
    metadata = restored.get("metadata") or restored.get("metadata_json") or {}
    if isinstance(metadata, str):
        import json

        metadata = json.loads(metadata)
    assert metadata.get("restored_from_version") == 1
    signed_by = str(metadata.get("restored_by") or "")
    assert signed_by, "откат не подписан вовсе"

    # Запись и журнал обязаны называть ОДНОГО автора. В личном архиве это
    # свойство слабое (там оба идентификатора совпадают, и оно выполняется даже
    # у сломанного кода) — сильную проверку держит соседний тест на общем
    # архиве. Здесь важно, что путь целиком проходит и подпись вообще появляется.
    actions = [
        row for row in storage.list_audit_log(None, limit=20) if row["action"] == "admin.knowledge.restore"
    ]
    assert actions, "откат не попал в журнал"
    assert signed_by == actions[0]["user_id"], "запись и журнал называют РАЗНЫХ авторов одного действия"


def test_in_a_shared_archive_the_signature_tells_people_apart(storage) -> None:
    """Сильная проверка: там, где человек и арендатор РАЗНЫЕ.

    В личном архиве `own_id` равен `user_id`, и любая подпись выглядит верной —
    ровно поэтому дефект жил незамеченным. Различие видно только в общем архиве.
    """

    from friday.permissions import ActorContext

    storage.ensure_user("common", source="test", external_id="common")
    knowledge_id = _document(storage, "common")
    storage.update_knowledge_fields(knowledge_id, "common", content="Вторая", reviewed_by="common")

    borya = ActorContext(
        user_id="common", preset_key="user", source="test", shared_tenant=True, person_id="человек-Б"
    )
    assert borya.own_id == "человек-Б" and borya.user_id == "common", "стенд не воспроизводит общий архив"

    storage.restore_knowledge_version(knowledge_id, "common", 1, reviewed_by=borya.own_id)

    restored = storage.get_knowledge_object(knowledge_id, "common")
    metadata = restored.get("metadata") or restored.get("metadata_json") or {}
    if isinstance(metadata, str):
        import json

        metadata = json.loads(metadata)
    assert metadata.get("restored_by") == "человек-Б"
    assert metadata.get("restored_by") != "common", "подпись назвала архив вместо человека"


def test_every_reviewed_by_in_the_admin_api_takes_the_person(client) -> None:
    """Сторож на класс: соседний маршрут не должен снова взять арендатора.

    Проверяется исходный текст, потому что дефект был именно в ОДНОМ вызове из
    десяти — остальные девять всё это время писали правильно, и никакой тест
    поведения этого не замечал.
    """

    from friday import admin_api
    from friday.admin_api import _knowledge

    for module in (_knowledge, admin_api):
        # Строка ЦЕЛИКОМ, а не до первой запятой: первая редакция этой проверки
        # обрывалась на `reviewed_by=getattr(actor` — то есть до слова `user_id`
        # не доходила и мутацию пережила. Проверено мутацией, а не глазами.
        for line in inspect.getsource(module).splitlines():
            if "reviewed_by=" not in line:
                continue
            argument = line.split("reviewed_by=", 1)[1]
            assert "user_id" not in argument or "own_id" in argument, (
                f"{module.__name__}: подпись берёт арендатора, а не человека — {line.strip()!r}"
            )


def test_the_rollback_creates_a_new_version_rather_than_erasing_history(client) -> None:
    """Откатившийся по ошибке должен иметь возможность откатиться обратно."""

    storage = client.storage
    tenant = client.get("/api/admin/users", headers=client.owner).json()["items"][0]["id"]
    knowledge_id = _document(storage, tenant)
    storage.update_knowledge_fields(knowledge_id, tenant, content="Вторая редакция", reviewed_by=tenant)
    before = len(storage.list_knowledge_versions(knowledge_id, tenant))

    client.post(
        f"/api/admin/knowledge/{knowledge_id}/restore",
        json={"user_id": tenant, "version": 1},
        headers=client.owner,
    )

    after = storage.list_knowledge_versions(knowledge_id, tenant)
    assert len(after) == before + 1, "история перемотана, а не дополнена"
    assert storage.get_knowledge_object(knowledge_id, tenant)["content"] == "Первая редакция"
