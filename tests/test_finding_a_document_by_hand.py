"""Найти документ руками — единственное, чего в админке не было вовсе.

Замерено на корпусе владельца (1537 объектов), и каждое число объясняет, почему без
строки поиска раздел был непригоден:

* важность лежит в полосе 0.66..0.72 — 325, 288, 279 и 225 объектов на значение, то
  есть первая страница это сотни неразличимых строк;
* различных дней в `updated_at` — ТРИ на весь архив, так что вторичный ключ порядка
  тоже не задаёт;
* два служебных тега стоят на 1524 объектах из 1537, а всего тегов 1693 — чипы как
  ось навигации вырождены.

Оставалось листать полторы тысячи строк. При этом заголовки содержательны: 1265
различных на 1537 объектов, средняя длина 28.5 знака.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from jericho.server import create_app
from jericho.storage.models import KnowledgeObject, RawObject, new_id


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _make(storage, user_id: str, title: str, *, summary: str = "", filename: str = "") -> str:
    text = f"{title}. Тело документа про сроки и приёмку. " * 10
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(title.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=title,
        summary=summary,
        # Поле принимает СЛОВАРЬ: хранилище сериализует его само, и переданная
        # строка была бы закодирована дважды — json_extract тогда молча вернёт NULL.
        metadata_json={"filename": filename} if filename else {},
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def test_a_document_is_found_by_a_fragment_of_its_title(storage):
    """Человек помнит обрывок, а не словоформу: «поверка вес» обязано находить."""
    storage.ensure_user("alice")
    wanted = _make(storage, "alice", "Рапорт о поверке весов на складе")
    for index in range(20):
        _make(storage, "alice", f"Ведомость расчёта номер {index}")

    found = storage.list_knowledge_objects("alice", query="поверк")
    assert [item["id"] for item in found] == [wanted]
    assert storage.count_filtered_knowledge_objects("alice", query="поверк") == 1


def test_the_search_is_case_insensitive_including_cyrillic(storage):
    """SQLite `lower()` сворачивает только латиницу — здесь нужен свой casefold."""
    storage.ensure_user("alice")
    wanted = _make(storage, "alice", "ПРОТОКОЛ ИСПЫТАНИЙ")

    assert [i["id"] for i in storage.list_knowledge_objects("alice", query="протокол")] == [wanted]
    assert [i["id"] for i in storage.list_knowledge_objects("alice", query="Протокол")] == [wanted]


def test_the_summary_and_the_file_name_are_searchable_too(storage):
    """Заголовок у импортированного документа бывает служебным, а имя файла — нет."""
    storage.ensure_user("alice")
    by_summary = _make(storage, "alice", "Документ 1", summary="Про аттестацию сварщиков")
    by_file = _make(storage, "alice", "Документ 2", filename="приказ-об-отпуске.docx")

    assert [i["id"] for i in storage.list_knowledge_objects("alice", query="аттестац")] == [by_summary]
    assert [i["id"] for i in storage.list_knowledge_objects("alice", query="отпуске")] == [by_file]


def test_the_count_answers_the_same_question_as_the_page(storage):
    """Правило проекта: показанное число не может быть длиной обрезанной страницы.

    Счёт и выборка обязаны строиться ОДНИМ фильтром, иначе пагинация врёт в обе
    стороны — это здесь уже ловили на фильтре по тегу.
    """
    storage.ensure_user("alice")
    for index in range(12):
        _make(storage, "alice", f"Рапорт номер {index}")
    for index in range(5):
        _make(storage, "alice", f"Ведомость номер {index}")

    page = storage.list_knowledge_objects("alice", query="рапорт", limit=5)
    assert len(page) == 5
    assert storage.count_filtered_knowledge_objects("alice", query="рапорт") == 12


def test_search_composes_with_the_tag_filter_rather_than_replacing_it(storage):
    storage.ensure_user("alice")
    text = "Тело документа про сроки. " * 10
    for title, tags in (("Рапорт весовой", ["склад"]), ("Рапорт кадровый", ["кадры"])):
        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="t",
            source_ref=new_id("s"),
            raw_content=text,
            content_type="text",
            content_hash=hashlib.sha256(title.encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        storage.store_knowledge_object(
            KnowledgeObject(
                id=new_id("ko"),
                user_id="alice",
                raw_object_id=raw.id,
                content=text,
                content_type="text",
                title=title,
                tags_json=tags,
            )
        )

    both = storage.list_knowledge_objects("alice", query="рапорт")
    assert len(both) == 2
    narrowed = storage.list_knowledge_objects("alice", query="рапорт", tag="кадры")
    assert len(narrowed) == 1
    assert narrowed[0]["title"] == "Рапорт кадровый"


def test_an_empty_query_changes_nothing(storage):
    """Пустая строка не должна вести себя как фильтр, отсекающий всё."""
    storage.ensure_user("alice")
    for index in range(4):
        _make(storage, "alice", f"Документ {index}")

    assert len(storage.list_knowledge_objects("alice", query="")) == 4
    assert len(storage.list_knowledge_objects("alice", query="   ")) == 4
    assert len(storage.list_knowledge_objects("alice", query=None)) == 4


def test_a_percent_sign_in_the_query_does_not_match_everything(storage):
    """`%` в LIKE — подстановочный знак, и человек, набравший его, должен искать знак."""
    storage.ensure_user("alice")
    _make(storage, "alice", "Отчёт 50% выполнения")
    _make(storage, "alice", "Обычный документ")

    found = storage.list_knowledge_objects("alice", query="50%")
    assert len(found) == 1, "процент сработал как подстановка и выдал всё подряд"
    assert "50%" in str(found[0]["title"])


def test_the_admin_route_passes_the_query_through(client, settings, storage):
    """Маршрут обязан отдавать и отфильтрованный список, и СВОЙ счёт."""
    storage.ensure_user("alice")
    for index in range(6):
        _make(storage, "alice", f"Рапорт {index}")
    _make(storage, "alice", "Посторонняя ведомость")

    headers = {"Authorization": f"Bearer {settings.api_token}"}
    response = client.get("/api/admin/knowledge?user_id=alice&q=рапорт", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 6
    assert len(body["items"]) == 6
    assert all("Рапорт" in str(item["title"]) for item in body["items"])


# --- дословный поиск по исходному тексту --------------------------------------


def test_source_search_is_reachable_over_http(client, settings, storage):
    """Он существовал только в CLI, то есть был недоступен человеку.

    Замерено на корпусе владельца: 93% загруженных знаков живут только в
    `raw_objects` — Knowledge Object несёт нормализованную, часто сокращённую версию.
    Значит точная фраза из документа без этого пути не находилась вовсе, а нужна она
    ровно тогда, когда подводит ранжирование (из пяти выданных отвечают два).
    """
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    ingest = client.post(
        "/api/ingest",
        json={"content": "Приказ номер 447-К о поверке весового оборудования", "force_knowledge": True},
        headers=headers,
    )
    assert ingest.status_code == 200, ingest.text
    owner = str((ingest.json().get("knowledge_object") or {}).get("user_id") or "")

    response = client.get(f"/api/admin/source-search?user_id={owner}&q=весового", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"], "дословная фраза из исходного текста не нашлась"
    assert body["count"] == len(body["items"])


def test_source_search_requires_the_admin_capability(client, settings):
    """Он читает ИСХОДНЫЙ текст чужого арендатора — значит гейт тот же, что у остальных."""
    response = client.get("/api/admin/source-search?q=что-нибудь")
    assert response.status_code in (401, 403)


def test_the_page_length_is_not_presented_as_a_total(client, settings, storage):
    """Правило проекта: показанное число не может выдавать длину среза за полный объём.

    FTS не отдаёт полного числа совпадений без второго запроса, поэтому поле честно
    называется `count` и равно длине страницы — а интерфейс подписывает его как
    «показано», а не «найдено всего».
    """
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    owner = ""
    for index in range(6):
        ingest = client.post(
            "/api/ingest",
            json={"content": f"Ведомость расчёта номер {index} по объекту приёмки", "force_knowledge": True},
            headers=headers,
        )
        owner = str((ingest.json().get("knowledge_object") or {}).get("user_id") or owner)

    response = client.get(f"/api/admin/source-search?user_id={owner}&q=ведомость&limit=2", headers=headers)
    body = response.json()
    assert len(body["items"]) <= 2
    assert body["count"] == len(body["items"]), "count разошёлся с длиной страницы"
    assert body["limit"] == 2
