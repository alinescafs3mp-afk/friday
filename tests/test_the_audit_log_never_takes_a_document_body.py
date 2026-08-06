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

import ast
import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from friday.audit_privacy import (
    sanitize_audit_action,
    sanitize_audit_payload,
    sanitize_audit_target_type,
)
from friday.server import create_app
from friday.storage import FridayStorage, init_storage
from friday.storage.models import AuditEntry, new_id

SECRET = "СЕКРЕТНАЯ-СТРОКА-КОТОРОЙ-НЕ-МЕСТО-В-ЖУРНАЛЕ"


def _string_constants(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return [*_string_constants(node.body), *_string_constants(node.orelse)]
    return []


def test_every_literal_audit_action_is_in_the_exact_storage_allowlist():
    """A new code-owned action must be declared, not silently become unknown."""

    root = Path(__file__).resolve().parents[1] / "friday"
    actions: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in (item for item in ast.walk(tree) if isinstance(item, ast.Call)):
            name = (
                call.func.attr
                if isinstance(call.func, ast.Attribute)
                else call.func.id
                if isinstance(call.func, ast.Name)
                else ""
            )
            if name == "AuditEntry":
                actions.update(
                    value
                    for keyword in call.keywords
                    if keyword.arg == "action"
                    for value in _string_constants(keyword.value)
                )
            elif len(call.args) >= 2 and (
                name == "_audit_cross_tenant_read"
                or (name == "_audit" and "execution_kernel" not in path.parts)
            ):
                actions.update(_string_constants(call.args[1]))
    rejected = sorted(action for action in actions if sanitize_audit_action(action) != action)
    assert not rejected, f"literal audit actions missing from allowlist: {rejected}"


def test_every_literal_audit_target_type_is_in_the_exact_storage_allowlist():
    root = Path(__file__).resolve().parents[1] / "friday"
    target_types: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in (item for item in ast.walk(tree) if isinstance(item, ast.Call)):
            name = (
                call.func.attr
                if isinstance(call.func, ast.Attribute)
                else call.func.id
                if isinstance(call.func, ast.Name)
                else ""
            )
            if name == "AuditEntry":
                target_types.update(
                    value
                    for keyword in call.keywords
                    if keyword.arg == "target_type"
                    for value in _string_constants(keyword.value)
                )
            elif (
                name == "_audit"
                and len(call.args) >= 3
                and "execution_kernel" not in path.parts
                and "executive/service.py" not in path.as_posix()
            ):
                target_types.update(_string_constants(call.args[2]))
    rejected = sorted(
        target_type for target_type in target_types if sanitize_audit_target_type(target_type) != target_type
    )
    assert not rejected, f"literal audit target types missing from allowlist: {rejected}"


def _ingest(client, token: str) -> tuple[str, str]:
    created = client.post(
        "/api/ingest",
        # Секрет спрятан ГЛУБОКО в тексте: так тест отдельно проверяет тело,
        # а ниже — что даже короткий заголовок не оседает в журнале.
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
        assert "content_ref" in recorded or "content_chars" in recorded
        # Даже короткий заголовок — личное содержимое и легко угадывается по хешу.
        assert "Договор аренды склада" not in recorded
        assert "title_chars" in recorded


def test_the_own_edit_and_delete_routes_cannot_copy_a_note_into_audit(settings):
    """The non-admin PATCH/DELETE pair used to bypass the admin fingerprint."""

    edited_secret = "OWN-EDIT-SECRET-8f4e2d7a"
    title_secret = "OWN-TITLE-SECRET-31dca9"
    with TestClient(create_app(settings)) as client:
        token = settings.api_token
        headers = {"Authorization": f"Bearer {token}"}
        knowledge_id, _ = _ingest(client, token)

        edited = client.patch(
            f"/api/knowledge/{knowledge_id}",
            json={
                "title": title_secret,
                "summary": f"summary-{edited_secret}",
                "content": f"body-{edited_secret}",
            },
            headers=headers,
        )
        assert edited.status_code == 200, edited.text
        deleted = client.delete(f"/api/knowledge/{knowledge_id}", headers=headers)
        assert deleted.status_code == 200, deleted.text

        recorded = _audit_text(client, token)
        for private in (SECRET, edited_secret, title_secret):
            assert private not in recorded
        assert "content_ref" in recorded
        assert "content_sha256" not in recorded
        assert "content_chars" in recorded
        assert "title_chars" in recorded


def test_the_central_audit_projection_is_recursive_bounded_and_fail_closed():
    """Unknown metadata keys cannot smuggle text around route-specific filters."""

    private = "AUDIT-SENTINEL-4f9b7c2d"
    payload = {
        private: private,
        "metadata": {
            private: {"title": private},
            "title": private,
            "nested": {
                "description": private,
                "secret_id": private,
                "url_host": f"{private}.example",
                "filename_suffix": f".{private}",
            },
        },
        "secret_id": private,
        "url_host": f"{private}.example",
        "filename_suffix": f".{private}",
        "url": f"https://user:{private}@example.test/private/{private}?token={private}",
        "filename": f"{private}.pdf",
        "chat_id": private,
        "content": private,
        "unknown_private_field": private,
    }

    projected = sanitize_audit_payload(payload, key=b"x" * 32)
    assert projected is not None
    encoded = json.dumps(projected, ensure_ascii=False, sort_keys=True)
    assert private not in encoded
    assert "/private/" not in encoded and "token=" not in encoded and "user:" not in encoded
    assert str(projected["url_host_ref"]).startswith("hostref_")
    assert projected["filename_suffix"] == ".pdf"
    assert projected["chat_id_present"] is True
    assert projected["content_chars"] == len(private)
    assert str(projected["content_ref"]).startswith("fpref_")
    assert "content_sha256" not in projected


def test_payload_schema_does_not_reflect_private_keys_hosts_suffixes_or_tokens():
    private = "private_family_name"
    projected = sanitize_audit_payload(
        {
            "url": f"https://{private}.example.test/a",
            "url_host": f"{private}.example.test",
            "filename": f"x.{private}",
            "path": f"/tmp/x.{private}",
            "changed_fields": ["name", private],
            "signals": ["private_signal"],
            private: {"total": 1},
            f"{private}_list": [1, 2],
            f"{private}_url": "https://example.test/",
            "metadata": {
                private: {"total": 1},
                f"{private}_list": [1, 2, 3],
            },
        },
        key=b"k" * 32,
    )
    assert projected is not None
    encoded = json.dumps(projected, ensure_ascii=False, sort_keys=True)
    assert private not in encoded
    assert "private_signal" not in encoded
    assert projected["changed_fields"] == ["name"]
    assert str(projected["url_host_ref"]).startswith("hostref_")
    assert "filename_suffix" not in projected
    assert "path_suffix" not in projected
    assert projected["private_fields_count"] >= 4
    assert projected["private_items_count"] >= 3


def test_only_exact_safe_file_suffixes_survive_audit_projection():
    projected = sanitize_audit_payload({"filename": "synthetic.pdf", "path": "/tmp/synthetic.privateword"})
    assert projected is not None
    assert projected["filename_suffix"] == ".pdf"
    assert "path_suffix" not in projected


def test_direct_storage_call_cannot_bypass_the_audit_projection(storage):
    """A future caller that skips both HTTP helpers still hits the durable guard."""

    private = "DIRECT-STORAGE-SENTINEL-83ac5e"
    storage.ensure_user("alice")
    audit_id = new_id("audit")
    storage.log_audit(
        AuditEntry(
            id=audit_id,
            user_id="alice",
            action="synthetic.private",
            target_type="import",
            target_id=f"{private}.zip",
            before_json={
                "content": private,
                "metadata": {
                    "secret_id": private,
                    "url_host": f"{private}.example",
                    "filename_suffix": f".{private}",
                },
            },
        )
    )
    row = storage.execute(
        "SELECT action, target_id, before_json FROM audit_log WHERE id=?",
        (audit_id,),
    ).fetchone()
    assert row is not None
    encoded = json.dumps(dict(row), ensure_ascii=False, sort_keys=True)
    assert private not in encoded
    assert row["action"] == "audit.unknown"
    assert str(row["target_id"]).startswith("import:ref:")
    before = json.loads(str(row["before_json"]))
    assert before["content_chars"] == len(private)
    assert str(before["content_ref"]).startswith("fpref_")
    assert "content_sha256" not in before


def test_the_storage_boundary_projects_every_audit_column(storage):
    """Scalar columns are no more trusted than before/after JSON."""

    private = "PRIVATE_SENTINEL_7ab91"
    storage.log_audit(
        AuditEntry(
            id=f"{private}-id",
            user_id=private,
            action=private,
            target_type=private,
            target_id=private,
            before_json={
                "entity_id": private,
                "user_id": private,
                "candidate_id": private,
                "entity_ids": [private],
            },
            after_json={"content": private},
            ip_address=private,
            request_id=private,
            created_at=private,
        )
    )

    row = storage.execute("SELECT * FROM audit_log ORDER BY rowid DESC LIMIT 1").fetchone()
    assert row is not None
    encoded = json.dumps(dict(row), ensure_ascii=False, sort_keys=True)
    assert private not in encoded
    assert str(row["id"]).startswith("audit_")
    assert row["user_id"] == "unknown"
    assert row["action"] == "audit.unknown"
    assert row["target_type"] == "private"
    assert str(row["target_id"]).startswith("private:ref:")
    assert row["ip_address"] == ""
    assert str(row["request_id"]).startswith("reqref_")
    assert str(row["created_at"]).startswith("20")


def test_private_audit_references_are_keyed_stable_and_distinct(storage):
    storage.ensure_user("alice")
    raw_target_a = "PRIVATE_VALUE_A"
    raw_target_b = "PRIVATE_VALUE_B"
    # Equal-shaped text is not enough to claim server provenance: only the
    # in-process marker created by the HTTP middleware may survive verbatim.
    raw_request_a = "0123456789abcdef01234567"
    raw_request_b = "89abcdef0123456701234567"
    audit_ids = [new_id("audit") for _ in range(3)]
    for audit_id, target, request_id in zip(
        audit_ids,
        (raw_target_a, raw_target_a, raw_target_b),
        (raw_request_a, raw_request_a, raw_request_b),
        strict=True,
    ):
        storage.log_audit(
            AuditEntry(
                id=audit_id,
                user_id="alice",
                action="knowledge.import",
                target_type="import",
                target_id=target,
                request_id=request_id,
            )
        )
    rows = [
        storage.execute(
            "SELECT target_id, request_id FROM audit_log WHERE id=?",
            (audit_id,),
        ).fetchone()
        for audit_id in audit_ids
    ]
    assert all(row is not None for row in rows)
    assert rows[0]["target_id"] == rows[1]["target_id"]
    assert rows[0]["request_id"] == rows[1]["request_id"]
    assert rows[0]["target_id"] != rows[2]["target_id"]
    assert rows[0]["request_id"] != rows[2]["request_id"]
    target_ref = str(rows[0]["target_id"]).rsplit(":", 1)[-1]
    request_ref = str(rows[0]["request_id"]).removeprefix("reqref_")
    assert target_ref != request_ref, "HMAC domains collapsed"
    ordinary_digest = hashlib.sha256(f"request_id\0{raw_request_a}".encode()).hexdigest()[:24]
    assert request_ref != ordinary_digest, "request reference is an unkeyed, guessable digest"
    encoded = json.dumps([dict(row) for row in rows], ensure_ascii=False)
    assert raw_target_a not in encoded and raw_target_b not in encoded
    assert raw_request_a not in encoded and raw_request_b not in encoded


def test_content_fingerprints_are_keyed_and_domain_separated(storage):
    """Known text and pre-hashed call sites both cross the keyed durable sink."""

    storage.ensure_user("alice")
    private = "LOW-ENTROPY-AUDIT-PHRASE"
    plain_digest = hashlib.sha256(private.encode()).hexdigest()
    audit_id = new_id("audit")
    storage.log_audit(
        AuditEntry(
            id=audit_id,
            user_id="alice",
            action="knowledge.import",
            target_type="import",
            target_id="synthetic",
            after_json={
                "content": private,
                "query_sha256": plain_digest,
                "code_sha256": plain_digest,
                "url": f"https://example.test/{private}",
            },
        )
    )

    row = storage.execute("SELECT after_json FROM audit_log WHERE id=?", (audit_id,)).fetchone()
    assert row is not None
    encoded = str(row["after_json"])
    after = json.loads(encoded)
    refs = [after[key] for key in ("content_ref", "query_ref", "code_ref", "url_ref")]
    assert all(str(value).startswith("fpref_") for value in refs)
    assert len(set(refs)) == len(refs), "fingerprint HMAC domains collapsed"
    assert private not in encoded and plain_digest not in encoded
    assert not any(key.endswith("_sha256") for key in after)


def test_generated_target_remains_exact_but_unproven_ip_is_pseudonymised(storage):
    storage.ensure_user("alice")
    audit_id = new_id("audit")
    entity_id = new_id("ent")
    storage.log_audit(
        AuditEntry(
            id=audit_id,
            user_id="alice",
            action="entity.update",
            target_type="entity",
            target_id=entity_id,
            before_json={"entity_id": entity_id, "user_id": "alice"},
            ip_address="2001:db8::1%PRIVATE_SCOPE",
        )
    )
    row = storage.execute(
        "SELECT user_id, target_id, before_json, ip_address FROM audit_log WHERE id=?",
        (audit_id,),
    ).fetchone()
    assert row is not None
    assert row["user_id"] == "alice"
    assert row["target_id"] == entity_id
    assert str(row["ip_address"]).startswith("ipref_")
    assert row["ip_address"] != "2001:db8::1"
    assert json.loads(str(row["before_json"])) == {
        "entity_id": entity_id,
        "user_id": "alice",
    }


def test_generated_shape_without_provenance_cannot_forge_target_or_payload(storage):
    storage.ensure_user("alice")
    forged = "ent_0123456789abcdef"
    forged_audit_id = "audit_0123456789abcdef"
    storage.log_audit(
        AuditEntry(
            id=forged_audit_id,
            user_id="alice",
            action="entity.update",
            target_type="entity",
            target_id=forged,
            before_json={"entity_id": forged, "entity_ids": [forged]},
        )
    )

    row = storage.execute(
        "SELECT id, target_id, before_json FROM audit_log ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    payload = json.loads(str(row["before_json"]))
    assert str(row["id"]).startswith("audit_") and row["id"] != forged_audit_id
    assert str(row["target_id"]).startswith("entity:ref:")
    assert str(payload["entity_id"]).startswith("idref_")
    assert str(payload["entity_ids"][0]).startswith("idref_")
    assert forged not in json.dumps(dict(row), ensure_ascii=False)


def test_exact_all_tenants_target_remains_investigable(storage):
    storage.ensure_user("alice")
    exact_id = new_id("audit")
    lookalike_id = new_id("audit")
    for audit_id, target in ((exact_id, "*"), (lookalike_id, "**")):
        storage.log_audit(
            AuditEntry(
                id=audit_id,
                user_id="alice",
                action="admin.users.list",
                target_type="user",
                target_id=target,
            )
        )
    exact = storage.execute(
        "SELECT target_id FROM audit_log WHERE id=?",
        (exact_id,),
    ).fetchone()
    lookalike = storage.execute(
        "SELECT target_id FROM audit_log WHERE id=?",
        (lookalike_id,),
    ).fetchone()
    assert exact is not None and exact["target_id"] == "*"
    assert lookalike is not None
    assert str(lookalike["target_id"]).startswith("user:ref:")


def test_hex_shaped_private_prefix_is_not_mistaken_for_a_generated_id(storage):
    storage.ensure_user("alice")
    crafted = "private_label_0000000000000000"
    audit_id = new_id("audit")
    storage.log_audit(
        AuditEntry(
            id=audit_id,
            user_id="alice",
            action="entity.update",
            target_type="entity",
            target_id=crafted,
            before_json={
                "id": crafted,
                "entity_id": crafted,
                "entity_ids": [crafted],
            },
        )
    )
    row = storage.execute(
        "SELECT target_id, before_json FROM audit_log WHERE id=?",
        (audit_id,),
    ).fetchone()
    assert row is not None
    encoded = json.dumps(dict(row), ensure_ascii=False)
    assert crafted not in encoded
    assert str(row["target_id"]).startswith("entity:ref:")
    payload = json.loads(str(row["before_json"]))
    assert str(payload["id"]).startswith("idref_")
    assert str(payload["entity_id"]).startswith("idref_")
    assert str(payload["entity_ids"][0]).startswith("idref_")


def test_http_audit_filter_and_request_id_cannot_become_audit_content(settings):
    private = "PRIVATE_SENTINEL_7ab91"
    with TestClient(create_app(settings)) as client:
        response = client.get(
            f"/api/admin/audit?user_id={private}",
            headers={
                "Authorization": f"Bearer {settings.api_token}",
                "X-Request-ID": private,
            },
        )
        assert response.status_code == 200, response.text
        row = client.app.state.storage.execute(
            """SELECT target_id, request_id FROM audit_log
                 WHERE action='admin.audit.read' ORDER BY rowid DESC LIMIT 1"""
        ).fetchone()
        assert row is not None
        encoded = json.dumps(dict(row), ensure_ascii=False)
        assert private not in encoded
        assert str(row["target_id"]).startswith("audit_log:ref:")
        assert str(row["request_id"]).startswith("reqref_")


def test_open_redacts_legacy_rows_and_restores_append_only_guards(settings, tmp_path):
    """Existing installations get the same privacy contract as new writes."""

    private = "LEGACY-AUDIT-SENTINEL-28bf74"
    database = tmp_path / "legacy-audit.sqlite3"
    tuned = replace(settings, database_path=database)
    seeded = init_storage(tuned)
    seeded.ensure_user("alice")
    seeded.close(final=True)

    legacy = sqlite3.connect(database)
    legacy.executescript("DROP TRIGGER audit_log_no_update;DROP TRIGGER audit_log_no_delete;")
    legacy.execute("UPDATE schema_meta SET value='v1' WHERE key='audit_payload_privacy'")
    legacy.execute(
        """INSERT INTO audit_log(
               id, user_id, action, target_type, target_id,
               before_json, after_json, ip_address, request_id, created_at
           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"{private}-id",
            private,
            private,
            private,
            private,
            json.dumps(
                {
                    "content": private,
                    "title": private,
                    "metadata": {"secret_id": private, "url_host": f"{private}.example"},
                }
            ),
            json.dumps({"url": f"https://example.test/private/{private}?token={private}"}),
            f"2001:db8::1%{private}",
            private,
            private,
        ),
    )
    legacy.commit()
    legacy.close()
    sqlite_files = [
        database,
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-shm"),
    ]
    assert any(path.exists() and private.encode() in path.read_bytes() for path in sqlite_files), (
        "the synthetic legacy secret never reached SQLite bytes, so the physical-removal check is void"
    )

    migrated = FridayStorage(tuned)
    try:
        row = migrated.execute(
            """SELECT id, user_id, action, target_type, target_id, before_json,
                      after_json, ip_address, request_id, created_at
                 FROM audit_log LIMIT 1"""
        ).fetchone()
        assert row is not None
        encoded = json.dumps(dict(row), ensure_ascii=False, sort_keys=True)
        assert private not in encoded
        assert "/private/" not in encoded and "token=" not in encoded
        assert row["action"] == "audit.unknown"
        assert row["user_id"] == "unknown"
        assert row["target_type"] == "private"
        assert str(row["target_id"]).startswith("private:ref:")
        assert str(row["ip_address"]).startswith("ipref_")
        assert str(row["request_id"]).startswith("reqref_")
        marker = migrated.execute(
            "SELECT value FROM schema_meta WHERE key='audit_payload_privacy'"
        ).fetchone()
        assert marker is not None and marker[0] == "v3"
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            migrated.execute("UPDATE audit_log SET action='forged'")
    finally:
        migrated.close(final=True)
    for path in sqlite_files:
        if path.exists():
            assert private.encode() not in path.read_bytes(), (
                f"legacy audit text survived physically in {path.name}"
            )


def test_v2_plain_fingerprints_and_unproven_ips_are_rekeyed_on_open(settings, tmp_path):
    database = tmp_path / "v2-audit.sqlite3"
    tuned = replace(settings, database_path=database)
    seeded = init_storage(tuned)
    seeded.ensure_user("alice")
    seeded.close(final=True)

    private = "guessable audit phrase"
    plain_digest = hashlib.sha256(private.encode()).hexdigest()
    raw = sqlite3.connect(database)
    raw.executescript("DROP TRIGGER audit_log_no_update;DROP TRIGGER audit_log_no_delete;")
    raw.execute("UPDATE schema_meta SET value='v2' WHERE key='audit_payload_privacy'")
    raw.execute(
        """INSERT INTO audit_log(
               id, user_id, action, target_type, target_id,
               before_json, after_json, ip_address, request_id, created_at
           ) VALUES(?, 'alice', 'knowledge.import', 'import', ?, '{}', ?, ?, '', ?)""",
        (
            "audit_0123456789abcdef",
            "v2-synthetic",
            json.dumps({"query_sha256": plain_digest, "query_chars": len(private)}),
            "192.0.2.44",
            "2026-08-06T00:00:00+00:00",
        ),
    )
    raw.commit()
    raw.close()

    migrated = FridayStorage(tuned)
    try:
        row = migrated.execute(
            "SELECT after_json, ip_address FROM audit_log WHERE action='knowledge.import'"
        ).fetchone()
        marker = migrated.execute(
            "SELECT value FROM schema_meta WHERE key='audit_payload_privacy'"
        ).fetchone()
        assert row is not None and marker is not None
        after = json.loads(str(row["after_json"]))
        assert str(after["query_ref"]).startswith("fpref_")
        assert "query_sha256" not in after
        assert plain_digest not in str(row["after_json"])
        assert str(row["ip_address"]).startswith("ipref_")
        assert row["ip_address"] != "192.0.2.44"
        assert marker[0] == "v3"
    finally:
        migrated.close(final=True)

    for path in (database, database.with_name(database.name + "-wal")):
        if path.exists():
            assert plain_digest.encode() not in path.read_bytes()


def test_legacy_redaction_rolls_back_the_trigger_drop_on_failure(settings, tmp_path, monkeypatch):
    """A crash/error cannot leave the audit table writable between phases."""

    database = tmp_path / "failed-audit-redaction.sqlite3"
    tuned = replace(settings, database_path=database)
    seeded = init_storage(tuned)
    seeded.ensure_user("alice")
    seeded.close(final=True)

    raw = sqlite3.connect(database)
    raw.execute("DELETE FROM schema_meta WHERE key='audit_payload_privacy'")
    raw.execute(
        """INSERT INTO audit_log(
               id, user_id, action, target_type, target_id,
               before_json, after_json, ip_address, request_id, created_at
           ) VALUES(?, 'alice', 'legacy.rollback', 'entity', 'ent_1', '{}', '{}', '', '', ?)""",
        (new_id("audit"), "2026-08-06T00:00:00+00:00"),
    )
    raw.commit()
    raw.close()

    import friday.storage._core as core

    def fail(_raw, **_kwargs):
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(core, "_legacy_audit_payload", fail)
    broken = FridayStorage(tuned)
    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        broken.execute("SELECT 1")
    broken.close(final=True)

    probe = sqlite3.connect(database)
    try:
        triggers = {
            str(row[0])
            for row in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'audit_log_no_%'"
            ).fetchall()
        }
        assert triggers == {"audit_log_no_update", "audit_log_no_delete"}
        assert (
            probe.execute("SELECT value FROM schema_meta WHERE key='audit_payload_privacy'").fetchone()
            is None
        )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            probe.execute("UPDATE audit_log SET action='forged' WHERE action='legacy.rollback'")
    finally:
        probe.close()


def test_busy_privacy_checkpoint_fences_inserts_and_retries_without_rekeying(settings, tmp_path, monkeypatch):
    """A pinned WAL reader leaves v3 pending and the DB fences every new row."""

    database = tmp_path / "busy-audit-redaction.sqlite3"
    tuned = replace(settings, database_path=database)
    seeded = init_storage(tuned)
    seeded.ensure_user("alice")
    seeded.close(final=True)

    reader = sqlite3.connect(database)
    reader.execute("PRAGMA journal_mode=WAL")
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM audit_log").fetchone()

    writer = sqlite3.connect(database)
    writer.executescript("DROP TRIGGER audit_log_no_update;DROP TRIGGER audit_log_no_delete;")
    writer.execute("UPDATE schema_meta SET value='v1' WHERE key='audit_payload_privacy'")
    writer.execute(
        """INSERT INTO audit_log(
               id, user_id, action, target_type, target_id,
               before_json, after_json, ip_address, request_id, created_at
           ) VALUES(?, 'alice', 'knowledge.import', 'import', ?, '{}', '{}', '', ?, ?)""",
        (
            new_id("audit"),
            "private target",
            "private request",
            "2026-08-06T00:00:00+00:00",
        ),
    )
    writer.commit()
    writer.close()

    from friday.storage._core import CoreMixin

    with monkeypatch.context() as scoped:
        scoped.setattr(CoreMixin, "_is_sqlite_busy", staticmethod(lambda _exc: False))
        broken = FridayStorage(tuned)
        with pytest.raises(sqlite3.OperationalError, match="WAL checkpoint is busy"):
            broken.execute("SELECT 1")
        broken.close(final=True)

    probe = sqlite3.connect(database)
    try:
        pending = probe.execute("SELECT value FROM schema_meta WHERE key='audit_payload_privacy'").fetchone()
        first = probe.execute("SELECT target_id, request_id FROM audit_log LIMIT 1").fetchone()
        assert pending is not None and pending[0] == "pending_wal_truncate:v3"
        assert first is not None
        with pytest.raises(sqlite3.DatabaseError, match="privacy migration is pending"):
            probe.execute(
                """INSERT INTO audit_log(
                       id, user_id, action, target_type, target_id,
                       before_json, after_json, ip_address, request_id, created_at
                   ) VALUES(?, 'alice', ?, 'entity', ?, '{}', '{}', '', '', ?)""",
                (
                    new_id("audit"),
                    "RAW-PENDING-ACTION",
                    "RAW-PENDING-TARGET",
                    "2026-08-06T00:00:00+00:00",
                ),
            )
    finally:
        probe.close()

    reader.rollback()
    reader.close()
    retried = FridayStorage(tuned)
    try:
        second = retried.execute("SELECT target_id, request_id FROM audit_log LIMIT 1").fetchone()
        marker = retried.execute("SELECT value FROM schema_meta WHERE key='audit_payload_privacy'").fetchone()
        assert second is not None and tuple(second) == tuple(first)
        assert marker is not None and marker[0] == "v3"
    finally:
        retried.close(final=True)


def test_v3_startup_rejects_a_counterfeit_append_only_guard(settings, tmp_path):
    database = tmp_path / "counterfeit-audit-guard.sqlite3"
    tuned = replace(settings, database_path=database)
    seeded = init_storage(tuned)
    seeded.ensure_user("alice")
    seeded.close(final=True)

    raw = sqlite3.connect(database)
    raw.executescript(
        """DROP TRIGGER audit_log_no_update;
        CREATE TRIGGER audit_log_no_update
        BEFORE UPDATE ON audit_log BEGIN SELECT 1; END;"""
    )
    raw.close()

    broken = FridayStorage(tuned)
    with pytest.raises(RuntimeError, match="append-only guards are missing or altered"):
        broken.execute("SELECT 1")
    broken.close(final=True)


def test_v3_startup_fails_closed_without_the_local_hmac_key(settings, tmp_path):
    database = tmp_path / "missing-audit-key.sqlite3"
    tuned = replace(settings, database_path=database)
    seeded = init_storage(tuned)
    seeded.ensure_user("alice")
    seeded.close(final=True)

    raw = sqlite3.connect(database)
    raw.execute("DELETE FROM schema_meta WHERE key='audit_privacy_hmac_key'")
    raw.commit()
    raw.close()

    broken = FridayStorage(tuned)
    with pytest.raises(RuntimeError, match="HMAC key is missing or invalid"):
        broken.execute("SELECT 1")
    broken.close(final=True)


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
