"""Даты из документов были извлечены и выброшены — теперь по ним можно искать.

Замерено на архиве владельца: у 630 объектов из 1537 в метаданных лежит список дат,
в среднем по пять на документ, 3180 значений всего. Работа была сделана и не
использовалась НИГДЕ: ни колонкой, ни индексом, ни параметром листинга, ни сортировкой.
А `created_at` у 1531 объекта из 1537 — один и тот же день загрузки, то есть по нему
искать бессмысленно.

Формы, в которых даты лежат (замерено): 2537 как дд.мм.гггг, 345 в ISO, 223 — это
ВРЕМЯ («1:25»), остальное мусор вроде «00.00.0000».

Условие — «документ УПОМИНАЕТ дату в диапазоне», а не «дата документа такая». Второго
данные не дают: документ называет несколько дат, и какая его собственная — неизвестно.
"""

from __future__ import annotations

import hashlib

import pytest

from jericho.storage._core import _iso_date
from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _make(storage, user_id: str, index: int, dates: list[str]) -> str:
    text = f"Документ {index}. " * 10
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(f"{user_id}-{index}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=f"Документ {index}",
        metadata_json={"dates": dates},
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


# --- нормализация -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("27.12.2025", "2025-12-27"),
        ("1.5.2020", "2020-05-01"),
        ("2023-03-15", "2023-03-15"),
        ("00.00.0000", None),
        ("1:25", None),
        ("31.02.2020", None),
        ("01.01.0001", None),
        ("", None),
        (None, None),
    ],
)
def test_only_real_dates_survive_normalisation(raw, expected):
    """Время и мусор обязаны отсеиваться ЗДЕСЬ.

    Иначе «за март 2023» вернёт документы, в которых нет ни одной мартовской даты, и
    человек перестанет фильтру верить. `31.02.2020` выглядит датой и датой не является;
    `01.01.0001` — артефакт разбора, а не дата документа.
    """
    assert _iso_date(raw) == expected


# --- фильтр -------------------------------------------------------------------


def test_a_period_selects_documents_that_mention_a_date_inside_it(storage):
    storage.ensure_user("alice")
    march = _make(storage, "alice", 1, ["15.03.2023"])
    _make(storage, "alice", 2, ["10.09.2021"])
    _make(storage, "alice", 3, [])

    found = storage.list_knowledge_objects("alice", since="2023-03-01", until="2023-03-31")
    assert [item["id"] for item in found] == [march]


def test_both_bounds_are_optional(storage):
    storage.ensure_user("alice")
    old = _make(storage, "alice", 1, ["01.01.2015"])
    recent = _make(storage, "alice", 2, ["01.01.2025"])

    assert [i["id"] for i in storage.list_knowledge_objects("alice", since="2020-01-01")] == [recent]
    assert [i["id"] for i in storage.list_knowledge_objects("alice", until="2020-01-01")] == [old]


def test_a_document_with_several_dates_matches_on_any_of_them(storage):
    """Так и задумано: «упоминает», а не «датирован»."""
    storage.ensure_user("alice")
    wanted = _make(storage, "alice", 1, ["01.02.2019", "15.03.2023", "20.11.2024"])

    for since, until in (("2019-01-01", "2019-12-31"), ("2023-01-01", "2023-12-31")):
        assert [i["id"] for i in storage.list_knowledge_objects("alice", since=since, until=until)] == [
            wanted
        ]


def test_times_and_junk_never_land_inside_a_range(storage):
    """`1:25` не должен превратиться в дату и попасть в любой диапазон."""
    storage.ensure_user("alice")
    _make(storage, "alice", 1, ["1:25", "00.00.0000", "не дата"])

    assert storage.list_knowledge_objects("alice", since="1900-01-01", until="2200-01-01") == []


def test_the_count_uses_the_same_period_as_the_page(storage):
    """Правило проекта: счёт и выборка отвечают на ОДИН вопрос."""
    storage.ensure_user("alice")
    for index in range(9):
        _make(storage, "alice", index, ["15.03.2023"])
    for index in range(9, 14):
        _make(storage, "alice", index, ["15.03.2021"])

    page = storage.list_knowledge_objects("alice", since="2023-01-01", until="2023-12-31", limit=4)
    assert len(page) == 4
    assert storage.count_filtered_knowledge_objects("alice", since="2023-01-01", until="2023-12-31") == 9


def test_a_period_composes_with_the_title_search(storage):
    storage.ensure_user("alice")
    wanted = _make(storage, "alice", 1, ["15.03.2023"])
    _make(storage, "alice", 2, ["15.03.2023"])

    both = storage.list_knowledge_objects("alice", since="2023-01-01", until="2023-12-31")
    assert len(both) == 2
    narrowed = storage.list_knowledge_objects(
        "alice", since="2023-01-01", until="2023-12-31", query="Документ 1"
    )
    assert [item["id"] for item in narrowed] == [wanted]


def test_the_route_rejects_a_malformed_date_instead_of_ignoring_it(settings):
    """Опечатка в дате не должна тихо снимать фильтр — иначе человек увидит весь архив
    и решит, что за период ничего нет."""
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        response = client.get("/api/admin/knowledge?user_id=alice&since=март", headers=headers)
        assert response.status_code == 422
