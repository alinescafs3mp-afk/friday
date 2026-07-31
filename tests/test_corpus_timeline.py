"""Хроника корпуса: плотность по времени и честность о том, кто в неё не попал.

Экран строится по СОБСТВЕННОЙ дате документа. На корпусе владельца различных дней в
`updated_at` три на 1537 объектов, то есть лента по дате загрузки показала бы три
деления; своя дата известна у 88%, и оставшиеся 12% не «нулевые» и не «старые» — они
для этого экрана невидимы, поэтому их число возвращается отдельным полем.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jericho.server import create_app
from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _dated(storage, user_id: str, *, title: str, document_date: str | None) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=title,
        raw_content=title,
        content_type="text/plain",
    )
    storage.store_raw_object(raw)
    metadata = {"document_date": document_date} if document_date else {}
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=title,
        title=title,
        metadata_json=metadata,
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def test_histogram_groups_by_year_month_and_day(storage):
    storage.ensure_user("alice")
    owner_user_id = "alice"
    for date_value in ("2023-05-04", "2024-03-01", "2024-03-17", "2024-11-02"):
        _dated(storage, owner_user_id, title=f"док {date_value}", document_date=date_value)

    years = storage.knowledge_date_histogram(owner_user_id, granularity="year")
    assert {row["bucket"]: row["count"] for row in years} == {"2023": 1, "2024": 3}

    months = storage.knowledge_date_histogram(
        owner_user_id, since="2024-01-01", until="2024-12-31", granularity="month"
    )
    assert {row["bucket"]: row["count"] for row in months} == {"2024-03": 2, "2024-11": 1}

    days = storage.knowledge_date_histogram(
        owner_user_id, since="2024-03-01", until="2024-03-31", granularity="day"
    )
    assert {row["bucket"]: row["count"] for row in days} == {"2024-03-01": 1, "2024-03-17": 1}


def test_documents_without_their_own_date_are_counted_not_hidden(storage):
    """Иначе экран читается как полный охват корпуса, а показывает его часть."""
    storage.ensure_user("alice")
    owner_user_id = "alice"
    _dated(storage, owner_user_id, title="с датой", document_date="2024-03-17")
    _dated(storage, owner_user_id, title="без даты", document_date=None)

    assert storage.count_knowledge_without_own_date(owner_user_id) == 1
    # В саму ленту бездатный не попадает — там место только у того, чьё время известно.
    listed = storage.list_documents_by_own_date(owner_user_id)
    assert [item["title"] for item in listed] == ["с датой"]


def test_histogram_is_scoped_to_one_account(storage):
    """Плотность чужого корпуса не должна протекать в свою: это тот же арендатор."""
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    owner_user_id, second_user_id = "alice", "bob"
    _dated(storage, owner_user_id, title="мой", document_date="2024-03-17")
    _dated(storage, second_user_id, title="чужой", document_date="2024-03-18")

    mine = storage.knowledge_date_histogram(owner_user_id, granularity="day")
    assert {row["bucket"] for row in mine} == {"2024-03-17"}


def test_timeline_endpoint_answers_and_picks_granularity(settings, storage):
    """Крупность выбирается по ширине окна, а не человеком, и называется в ответе."""
    storage.ensure_user("alice")
    owner_user_id = "alice"
    _dated(storage, owner_user_id, title="давний", document_date="2015-06-01")
    _dated(storage, owner_user_id, title="свежий", document_date="2024-03-17")

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        wide = client.get(f"/api/admin/knowledge/timeline?user_id={owner_user_id}", headers=headers)
        assert wide.status_code == 200
        assert wide.json()["granularity"] == "year"

        narrow = client.get(
            f"/api/admin/knowledge/timeline?user_id={owner_user_id}&since=2024-03-01&until=2024-03-31",
            headers=headers,
        )
        assert narrow.status_code == 200
        body = narrow.json()
        assert body["granularity"] == "day"
        assert [item["title"] for item in body["items"]] == ["свежий"]
        assert body["undated"] == 0


def test_timeline_path_is_not_swallowed_by_the_inspect_route(settings, storage):
    """`/knowledge/{knowledge_id}` объявлен рядом и проглотил бы литерал.

    Проверяется поведением, а не таблицей маршрутов: инспекция на неизвестном
    идентификаторе отвечает 404, поэтому 200 здесь означает, что запрос попал
    в собственный обработчик хроники, а не в инспекцию с id="timeline".
    """
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        storage.ensure_user("alice")
        response = client.get("/api/admin/knowledge/timeline?user_id=alice", headers=headers)
        assert response.status_code == 200
        assert "buckets" in response.json()
