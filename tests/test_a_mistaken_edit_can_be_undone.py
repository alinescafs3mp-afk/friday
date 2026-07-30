"""Версии писались и показывались, а вернуться к ним было нечем.

Поиск по всему пакету на `restore|revert|rollback` находил только восстановление БАЗЫ
из бэкапа. При этом машинерия уже была вся: снимок — готовая строка объекта, откат это
одна правка полями из него.

Цена отсутствия: редактор содержимого в админке — ОДНА textarea с полным текстом
документа, в архиве владельца в среднем на 16 565 знаков. Живая база показывает,
насколько путь не хожен: 1538 строк версий на 1537 объектов, то есть за всё время
отредактирован ровно один объект. Первая же настоящая ошибка упёрлась бы в тупик.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _make(storage, user_id: str = "alice") -> str:
    text = "Первоначальный текст документа про сроки приёмки. " * 5
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="t",
        source_ref=new_id("s"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title="Верный заголовок",
        summary="Верная сводка",
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def test_a_document_returns_to_the_state_it_had(storage):
    storage.ensure_user("alice")
    ko_id = _make(storage)
    storage.update_knowledge_fields(ko_id, "alice", title="Испорчено", content="стёрто")

    restored = storage.restore_knowledge_version(ko_id, "alice", 1)

    assert restored is not None
    assert restored["title"] == "Верный заголовок"
    assert "Первоначальный текст" in str(restored["content"])


def test_the_undo_is_a_new_version_not_a_rewind(storage):
    """История — это то, ради чего она пишется.

    Откат создаёт версию N+1, поэтому откатившийся по ошибке может откатиться обратно.
    """
    storage.ensure_user("alice")
    ko_id = _make(storage)
    storage.update_knowledge_fields(ko_id, "alice", title="Испорчено")
    before = len(storage.list_knowledge_versions(ko_id, "alice"))

    storage.restore_knowledge_version(ko_id, "alice", 1)

    after = storage.list_knowledge_versions(ko_id, "alice")
    assert len(after) == before + 1, "откат перемотал историю вместо того, чтобы её продолжить"
    # И обратно тоже можно.
    back = storage.restore_knowledge_version(ko_id, "alice", 2)
    assert back is not None and back["title"] == "Испорчено"


def test_lifecycle_is_not_dragged_back_with_the_text(storage):
    """Возврат к прежнему ТЕКСТУ не отменяет того, что объект с тех пор архивировали."""
    storage.ensure_user("alice")
    ko_id = _make(storage)
    storage.update_knowledge_fields(ko_id, "alice", title="Другое")
    storage.update_knowledge_fields(ko_id, "alice", lifecycle_stage="archived")

    restored = storage.restore_knowledge_version(ko_id, "alice", 1)

    assert restored is not None
    assert restored["title"] == "Верный заголовок"
    assert restored["lifecycle_stage"] == "archived", "откат текста откатил и жизненный цикл"


def test_a_missing_version_is_refused_by_name(storage):
    storage.ensure_user("alice")
    ko_id = _make(storage)
    with pytest.raises(LookupError, match="not found"):
        storage.restore_knowledge_version(ko_id, "alice", 99)


def test_who_restored_and_from_where_is_written_on_the_object(storage):
    """Человек, открывший запись через полгода, должен видеть это на ней самой."""
    storage.ensure_user("alice")
    ko_id = _make(storage)
    storage.update_knowledge_fields(ko_id, "alice", title="Испорчено")

    restored = storage.restore_knowledge_version(ko_id, "alice", 1, reviewed_by="alice")

    metadata = json.loads(str(restored["metadata_json"] or "{}"))
    assert metadata["restored_from_version"] == 1
    assert metadata["restored_by"] == "alice"


def test_the_route_restores_and_audits(settings, storage):
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        ingest = client.post(
            "/api/ingest",
            json={"content": "Исходное содержимое записи", "force_knowledge": True},
            headers=headers,
        )
        knowledge = ingest.json()["knowledge_object"]
        owner, ko_id = str(knowledge["user_id"]), str(knowledge["id"])

        client.patch(
            f"/api/admin/knowledge/{ko_id}",
            json={"user_id": owner, "title": "Испорченный заголовок"},
            headers=headers,
        )
        response = client.post(
            f"/api/admin/knowledge/{ko_id}/restore",
            json={"user_id": owner, "version": 1},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["restored_from_version"] == 1

    rows = storage.execute("SELECT action FROM audit_log WHERE action='admin.knowledge.restore'").fetchall()
    assert rows, "откат не попал в аудит"


def test_the_route_refuses_a_missing_version(settings):
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        ingest = client.post(
            "/api/ingest", json={"content": "Ещё одна запись", "force_knowledge": True}, headers=headers
        )
        knowledge = ingest.json()["knowledge_object"]
        response = client.post(
            f"/api/admin/knowledge/{knowledge['id']}/restore",
            json={"user_id": knowledge["user_id"], "version": 42},
            headers=headers,
        )
        assert response.status_code == 404


def test_old_snapshots_compress_in_place_and_undo_survives(storage):
    """Каждый снапшот несёт полный content, и чистки не существовало нигде:
    массовое ре-обогащение добавляло копию корпуса в базу навсегда. Полными
    держатся 3 последних версии объекта, старшие сжимаются на месте при записи
    новой — и откат обязан жить к ЛЮБОЙ версии, включая сжатую."""
    ko_id = _make(storage)
    for step in range(5):
        storage.update_knowledge_fields(
            ko_id, "alice", title=f"Правка {step}", content=f"Тело правки {step}. " * 30
        )

    rows = storage.execute(
        """SELECT version, typeof(snapshot_json) AS kind FROM knowledge_object_versions
           WHERE knowledge_object_id=? ORDER BY version""",
        (ko_id,),
    ).fetchall()
    kinds = {int(row["version"]): str(row["kind"]) for row in rows}
    newest = max(kinds)
    assert kinds[newest] == "text" and kinds[newest - 1] == "text" and kinds[newest - 2] == "text", (
        "свежие версии обязаны остаться полным текстом"
    )
    assert kinds[1] == "blob", "старшие версии не сжались"

    # Единственный читатель таблицы отдаёт прежний текст для ЛЮБОЙ версии.
    for item in storage.list_knowledge_versions(ko_id, "alice"):
        snapshot = json.loads(str(item["snapshot_json"]))
        assert "content" in snapshot

    # Откат к самой старой (сжатой) версии возвращает её текст.
    restored = storage.restore_knowledge_version(ko_id, "alice", 1)
    assert restored["title"] == "Верный заголовок"
    assert "Первоначальный текст" in restored["content"]


def test_compression_actually_saves_space(storage):
    """Сжатие, не экономящее байты, — переливание из пустого в порожнее."""
    ko_id = _make(storage)
    for step in range(5):
        storage.update_knowledge_fields(
            ko_id, "alice", content=("Однообразный русский текст правки. " * 200) + str(step)
        )
    row = storage.execute(
        """SELECT LENGTH(snapshot_json) AS packed FROM knowledge_object_versions
           WHERE knowledge_object_id=? AND version=1""",
        (ko_id,),
    ).fetchone()
    original = len(json.dumps({"content": "Первоначальный текст документа про сроки приёмки. " * 5}))
    assert int(row["packed"]) < original, "сжатый снимок не меньше даже усечённого оригинала"
