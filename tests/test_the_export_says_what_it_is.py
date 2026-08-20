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

from fastapi.testclient import TestClient

from friday.server import create_app


def test_body_free_response_does_not_advertise_a_nonexistent_plaintext_vault(settings):
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        client.post(
            "/api/ingest", json={"content": "Запись для выгрузки", "force_knowledge": True}, headers=headers
        )
        response = client.post("/api/admin/exports", json={}, headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()
        ways = " ".join(body["to_move_your_data"]).casefold()
        assert "sqlite" in ways, "не назван настоящий путь переноса"
        assert "vault" not in ways, "отключённая plaintext-проекция выдана за существующий перенос"
        assert "no importer exists" in str(body["readable_by"]).casefold()


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
