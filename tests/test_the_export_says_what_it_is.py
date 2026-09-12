"""Выгрузка выдавала себя за путь переезда, не будучи им.

Формат `jericho-user-export-v3` не читает НИЧТО: строка встречается во всём коде
дважды — там, где пишется, и в тесте. Ни `import_user`, ни чтения этого формата не
существует; `jericho import` — про документы, а не про выгрузку.

Замерено на архиве владельца, повторением ровно тех же запросов: 1683 raw-объекта,
1532 знания, 1533 версии, 1671 inbox — 150.8 МБ UTF-8 компактной сериализации (файл
пишется с отступами, то есть ещё в полтора-два раза больше), пик памяти 759 МБ при
3.9 ГБ доступных. Оригиналы файлов (684 МБ) и векторы в выгрузку не входят.

Человек, считающий её способом уйти, узнает правду в худший момент — когда Friday уже
нет. Поэтому ответ обязан называть настоящие пути: копия SQLite с каталогом файлов и
явно включённый `memory-vault` (Markdown читается чем угодно). Body-free режим не
должен рекламировать проекцию, которой backend не создаёт.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from tests.test_organs_profile_chronicle import _seed_knowledge


def test_body_free_response_does_not_advertise_a_nonexistent_plaintext_vault(settings):
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        content = "Запись для выгрузки"
        ingested = client.post(
            "/api/ingest", json={"content": content, "force_knowledge": True}, headers=headers
        )
        assert ingested.status_code == 200, ingested.text
        knowledge_id = ingested.json()["knowledge_object"]["id"]
        storage = client.app.state.storage
        stored = storage.get_knowledge_object(knowledge_id, LEGACY_OWNER_USER_ID)
        assert stored and stored["content"] == content, "export_http_seed"
        raw_id = stored["raw_object_id"]
        assert storage.get_raw_object(raw_id, LEGACY_OWNER_USER_ID)["raw_content"] == content
        foreign = "local:export-foreign"
        foreign_content = "FOREIGN_EXPORT_CONTENT_CANARY"
        storage.ensure_user(foreign)
        foreign_id = _seed_knowledge(storage, foreign, foreign_content, [])
        prior_audit = storage.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        response = client.post("/api/admin/exports", json={}, headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()
        ways = " ".join(body["to_move_your_data"]).casefold()
        assert "sqlite" in ways, "не назван настоящий путь переноса"
        assert "vault" not in ways, "отключённая plaintext-проекция выдана за существующий перенос"
        assert "no importer exists" in str(body["readable_by"]).casefold()
        receipt = body["export"]
        path = Path(receipt["path"])
        assert path.parent.resolve() == settings.exports_dir.resolve(), "export_http_artifact_root"
        assert path.name == receipt["filename"] and path.is_file(), "export_http_artifact"
        artifact = path.read_bytes()
        assert len(artifact) == receipt["size_bytes"] > 0, "export_http_size"
        downloaded = client.get(f"/api/admin/exports/{receipt['filename']}/download", headers=headers)
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == artifact, "export_http_exact_download"
        assert downloaded.headers["content-type"].split(";")[0] == "application/json"
        exported = json.loads(downloaded.content)
        assert exported["format"] == "jericho-user-export-v3"
        assert exported["user"]["id"] == LEGACY_OWNER_USER_ID, "export_http_principal"
        assert [(row["id"], row["content"]) for row in exported["knowledge_objects"]] == [
            (knowledge_id, content)
        ], "export_http_knowledge"
        assert [(row["id"], row["raw_content"]) for row in exported["raw_objects"]] == [(raw_id, content)], (
            "export_http_raw"
        )
        packed = json.dumps(exported, ensure_ascii=False)
        for marker in (foreign, foreign_id, foreign_content, settings.api_token):
            assert marker not in packed, "export_http_private_content"
        audit = [dict(row) for row in storage.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()]
        assert [(row["user_id"], row["action"], row["target_type"]) for row in audit[prior_audit:]] == [
            (LEGACY_OWNER_USER_ID, "admin.export.create", "user"),
            (LEGACY_OWNER_USER_ID, "admin.export.download", "export"),
        ], "export_http_audit"
        assert audit[prior_audit]["target_id"] == LEGACY_OWNER_USER_ID, "export_http_audit_owner"
        assert re.fullmatch(r"export:ref:[0-9a-f]{24}", audit[-1]["target_id"]), "export_http_audit_reference"
        for marker in (content, foreign_content, settings.api_token, receipt["filename"]):
            assert marker not in json.dumps(audit, ensure_ascii=False), "export_http_audit_privacy"


def test_explicit_full_owner_response_names_the_readable_vault(settings):
    from dataclasses import replace

    with TestClient(create_app(replace(settings, memory_vault_mode="full_owner"))) as client:
        response = client.post(
            "/api/admin/exports",
            json={},
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        ways = " ".join(response.json()["to_move_your_data"]).casefold()
        assert "vault" in ways


def test_it_admits_what_it_leaves_behind(settings):
    """Оригиналы файлов — 684 МБ на этом архиве, и без них выгрузка не архив."""
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/admin/exports", json={}, headers={"Authorization": f"Bearer {settings.api_token}"}
        )
    missing = " ".join(response.json()["not_included"]).casefold()
    assert "файл" in missing and "вектор" in missing
    assert "engineer" in missing and "ledger" in missing


def test_the_export_does_not_run_on_the_event_loop():
    """Она синхронная и на секунды подвешивала ВЕСЬ сервер: ни HTTP, ни Telegram,
    пока строится словарь на сотню миллионов знаков."""
    from friday.admin_api._maintenance import create_export

    source = inspect.getsource(create_export)
    assert "run_blocking" in source, "выгрузка снова строится прямо на event loop"


def test_no_importer_is_advertised_anywhere():
    """Проба фиксирует ФАКТ, на котором стоит формулировка.

    Появится импортёр — тест упадёт, и текст ответа надо будет переписать. Именно так:
    сначала возможность, потом обещание.
    """
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "friday"
    sources = " ".join(path.read_text(encoding="utf-8") for path in package.rglob("*.py"))
    assert "def import_user" not in sources, "появился импортёр — ответ выгрузки всё ещё говорит, что его нет"
