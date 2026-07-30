"""Хроника периода в чате: «что у меня за март».

До появления собственной даты документа этот вопрос был неотвечаем: у всего
импортированного разом корпуса дата создания одна — день импорта, и лента
показала бы полторы тысячи записей одним днём. Теперь дата есть у большинства
объектов, и хронология архива стала возможна.

Две ленты в одном ответе намеренно: события графа отвечают «что было», документы
по своей дате — «чем это подтверждено». Порознь они уже существовали (события в
графе, документы нигде) и на вопрос о периоде не отвечали.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from jericho.storage.models import KnowledgeObject, RawObject, new_id
from jericho.telegram_bridge import TelegramBridge
from jericho.telegram_bridge._commands import parse_period


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2023", ("2023-01-01", "2023-12-31")),
        ("2023-03", ("2023-03-01", "2023-03-31")),
        ("март 2023", ("2023-03-01", "2023-03-31")),
        ("марта 2023", ("2023-03-01", "2023-03-31")),
        ("декабрь 2020", ("2020-12-01", "2020-12-31")),
        ("2020-01-01..2020-03-31", ("2020-01-01", "2020-03-31")),
    ],
)
def test_periods_people_actually_type(text, expected):
    parsed = parse_period(text, today=date(2026, 7, 31))
    assert parsed is not None, text
    assert (parsed[0], parsed[1]) == expected


def test_december_does_not_roll_into_the_next_year_wrong():
    """Границу месяца считает арифметика, а не таблица: декабрь — единственный
    случай, где год меняется, и именно на нём такие функции ломаются."""
    assert parse_period("декабрь 2020", today=date(2026, 1, 1))[:2] == ("2020-12-01", "2020-12-31")


def test_an_unparsed_period_is_refused_rather_than_guessed():
    """«Позавчера» и «прошлой весной» — не отказ разбора, а приглашение угадать.
    Угадывание периода здесь того же класса, что выдумывание даты документа."""
    assert parse_period("позавчера", today=date(2026, 7, 31)) is None
    assert parse_period("прошлой весной", today=date(2026, 7, 31)) is None


def test_no_argument_means_the_last_thirty_days():
    parsed = parse_period("", today=date(2026, 7, 31))
    assert parsed is not None and parsed[0] == "2026-07-01" and parsed[1] == "2026-07-31"


def _dated(storage, user_id: str, index: int, document_date: str, title: str) -> str:
    text = f"Тело документа {index}. " * 5
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="upload",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="file",
        content_hash=hashlib.sha256(f"t{index}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="file",
        title=title,
        metadata_json={"document_date": document_date},
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def test_documents_come_back_ordered_by_their_own_date(storage):
    storage.ensure_user("alice")
    older = _dated(storage, "alice", 1, "2023-02-01", "Февральский")
    newer = _dated(storage, "alice", 2, "2023-11-01", "Ноябрьский")
    _dated(storage, "alice", 3, "2025-01-01", "Вне периода")

    rows = storage.list_documents_by_own_date("alice", since="2023-01-01", until="2023-12-31")

    assert [row["id"] for row in rows] == [newer, older], "порядок не по дате документа"
    assert all(row["document_date"].startswith("2023") for row in rows)


def test_a_document_dated_only_by_mention_stays_out_of_the_chronicle(storage):
    """Документ может назвать десяток чужих дат; поставить его в ленту по любой из
    них значит соврать о времени."""
    storage.ensure_user("alice")
    text = "Упоминает 2023-05-05, но своей даты не имеет."
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(b"mention").hexdigest(),
    )
    storage.store_raw_object(raw)
    storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=raw.id,
            content=text,
            content_type="text",
            title="Только упоминание",
            metadata_json={"dates": ["2023-05-05"]},
        )
    )

    assert storage.list_documents_by_own_date("alice", since="2023-01-01", until="2023-12-31") == []


def test_the_chat_answer_shows_both_lanes_and_offers_buttons():
    documents = {
        "items": [
            {"id": "ko_a", "title": "Приказ", "document_date": "2023-03-04"},
            {"id": "ko_b", "title": "Ведомость", "document_date": "2023-03-01"},
        ]
    }
    events = {"items": [{"name": "Приёмка", "occurred_at": "2023-03-02"}]}

    text = TelegramBridge._format_timeline("03.2023", documents, events)

    assert "Хроника 03.2023" in text
    assert "2023-03-02 — Приёмка" in text
    assert "1. 2023-03-04 — Приказ" in text
    markup = TelegramBridge._timeline_reply_markup(documents)
    assert [b["callback_data"] for row in markup["inline_keyboard"] for b in row] == [
        "doc:show:ko_a",
        "doc:show:ko_b",
    ]


def test_an_empty_period_explains_itself_instead_of_looking_like_an_empty_archive():
    text = TelegramBridge._format_timeline("2019", {"items": []}, {"items": []})

    assert "В этот период ничего не датировано" in text
    assert "есть не у каждого документа" in text
    assert TelegramBridge._timeline_reply_markup({"items": []}) is None


def test_the_route_serves_documents_by_own_date(settings, storage):
    from fastapi.testclient import TestClient

    from jericho.permissions import LEGACY_OWNER_USER_ID
    from jericho.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        _dated(app.state.storage, LEGACY_OWNER_USER_ID, 9, "2021-08-08", "Августовский")
        response = client.get(
            "/api/knowledge/by-date?since=2021-01-01&until=2021-12-31",
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["count"] == 1
        assert body["items"][0]["document_date"] == "2021-08-08"


def test_the_literal_route_is_not_shadowed_by_the_id_route(settings):
    """`/knowledge/by-date` объявлен ДО `/{knowledge_id}`; иначе он читался бы как
    идентификатор — эту грабку в проекте уже ловили с `/tags`."""
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/api/knowledge/by-date", headers={"Authorization": f"Bearer {settings.api_token}"}
        )
        assert response.status_code == 200
        assert "items" in response.json()
        assert json.loads(response.text).get("count") == 0
