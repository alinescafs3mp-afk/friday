"""Маршруты внешних источников: объявить и посмотреть, но не через них — секрет.

Через эти дороги строка подключения не проходит ни в одну сторону. Причина не в
аккуратности: объявленный источник виден в панели, попадает в резервную копию и в
журнал действий — пароль от чужой боевой базы уехал бы во все три места сразу.

Второе свойство — граница арендаторов: список отдаёт СВОИ источники, чужой не
виден и не удаляется.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from friday.server import create_app

DECLARED = {"name": "hr", "kind": "sqlite", "dsn_env": "TEST_HR_DSN", "description": "кадры"}


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as opened:
        opened.owner = {"Authorization": f"Bearer {settings.api_token}"}  # type: ignore[attr-defined]
        opened.storage = app.state.storage  # type: ignore[attr-defined]
        yield opened


def test_a_declared_source_is_listed_without_any_secret(client, monkeypatch) -> None:
    monkeypatch.setenv("TEST_HR_DSN", "postgresql://user:s3cret@10.0.0.9/hr")
    created = client.post("/api/admin/data-sources", json=DECLARED, headers=client.owner)
    assert created.status_code == 200, created.text

    listed = client.get("/api/admin/data-sources", headers=client.owner)
    assert listed.status_code == 200
    body = listed.text
    # Ни в объявлении, ни в списке секрета быть не может — его там просто нет.
    for leak in ("s3cret", "10.0.0.9", "postgresql://"):
        assert leak not in created.text, f"объявление вернуло {leak}"
        assert leak not in body, f"список вернул {leak}"
    source = listed.json()["sources"][0]
    assert source["name"] == "hr"
    assert source["dsn_env"] == "TEST_HR_DSN"
    # Переменная задана — человек должен видеть, что источник рабочий.
    assert source["secret_present"] is True


def test_a_source_whose_variable_is_unset_says_so(client, monkeypatch) -> None:
    """Объявить заранее можно. Молчать об этом нельзя — иначе запрос упадёт непонятно."""

    monkeypatch.delenv("TEST_HR_DSN", raising=False)
    client.post("/api/admin/data-sources", json=DECLARED, headers=client.owner)

    source = client.get("/api/admin/data-sources", headers=client.owner).json()["sources"][0]
    assert source["secret_present"] is False

    schema = client.get("/api/admin/data-sources/hr/schema", headers=client.owner)
    assert schema.status_code == 409
    assert "TEST_HR_DSN" in schema.json()["detail"]


def test_the_schema_route_reads_the_real_database(client, monkeypatch, tmp_path) -> None:
    import sqlite3

    external = tmp_path / "hr.sqlite3"
    conn = sqlite3.connect(external)
    conn.execute("CREATE TABLE staff (id INTEGER PRIMARY KEY, unit TEXT NOT NULL)")
    conn.execute("INSERT INTO staff(unit) VALUES ('склад')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("TEST_HR_DSN", str(external))

    client.post("/api/admin/data-sources", json=DECLARED, headers=client.owner)
    schema = client.get("/api/admin/data-sources/hr/schema", headers=client.owner)
    assert schema.status_code == 200, schema.text
    tables = schema.json()["tables"]
    assert "staff" in tables
    assert {column["column"] for column in tables["staff"]} == {"id", "unit"}


def test_a_bad_declaration_is_refused_with_a_reason(client) -> None:
    for broken, expected in (
        ({**DECLARED, "name": "Кадры HR"}, "Имя источника"),
        ({**DECLARED, "kind": "oracle"}, "вид источника"),
        ({**DECLARED, "dsn_env": "hr_dsn"}, "переменной окружения"),
    ):
        answer = client.post("/api/admin/data-sources", json=broken, headers=client.owner)
        assert answer.status_code == 400, broken
        assert expected in answer.json()["detail"]


def test_an_unknown_source_is_not_found(client) -> None:
    assert client.delete("/api/admin/data-sources/hr", headers=client.owner).status_code == 404
    assert client.get("/api/admin/data-sources/hr/schema", headers=client.owner).status_code == 404


def test_forgetting_a_source_removes_it(client) -> None:
    client.post("/api/admin/data-sources", json=DECLARED, headers=client.owner)
    assert client.delete("/api/admin/data-sources/hr", headers=client.owner).status_code == 200
    assert client.get("/api/admin/data-sources", headers=client.owner).json()["sources"] == []


def test_the_list_is_scoped_to_the_account(client) -> None:
    """Источник соседа не виден и не удаляется даже владельцем-администратором."""

    client.storage.ensure_user("bob", source="test", external_id="bob")
    client.storage.register_data_source(
        "bob", name="hr", kind="sqlite", dsn_env="BOB_HR_DSN", created_by="bob"
    )
    client.post("/api/admin/data-sources", json=DECLARED, headers=client.owner)

    mine = client.get("/api/admin/data-sources", headers=client.owner).json()["sources"]
    assert [row["dsn_env"] for row in mine] == ["TEST_HR_DSN"], "в список затесался чужой источник"
    # И объявление своего не тронуло чужой — тот же дефект, что чинила схема 29.
    assert client.storage.get_data_source("bob", "hr")["dsn_env"] == "BOB_HR_DSN"


def test_declaring_a_source_is_written_into_the_audit(client) -> None:
    client.post("/api/admin/data-sources", json=DECLARED, headers=client.owner)
    actions = [row["action"] for row in client.storage.list_audit_log(limit=20)]
    assert "admin.data_source.declare" in actions

    client.delete("/api/admin/data-sources/hr", headers=client.owner)
    actions = [row["action"] for row in client.storage.list_audit_log(limit=20)]
    assert "admin.data_source.forget" in actions


def test_the_routes_require_the_right(client, settings) -> None:
    """Без права `data.read` список не отдаётся, без права управления — не пишется."""

    assert client.get("/api/admin/data-sources").status_code in (401, 403)
    assert client.post("/api/admin/data-sources", json=DECLARED).status_code in (401, 403)
    assert client.delete("/api/admin/data-sources/hr").status_code in (401, 403)
    assert client.get("/api/admin/data-sources/hr/schema").status_code in (401, 403)
