"""«и т.д.» — не идентификатор, и требовать его дословно нельзя.

`identifier_mismatch` стоит ПЕРВЫМ в `_exclusion_reason` и не имеет обхода: токен,
который гейт признал идентификатором, обязан встретиться в документе дословно, иначе
документ выбрасывается. Значит любая ошибка распознавания опустошает ответ.

Замерено на 342 настоящих документах: к работающему запросу добавлено « и т.д.» —
и целевой документ ушёл из ответа в 15 случаях из 15 (было 14/15 в топ-10).
С «примерно 12.5 процента» поиск вернул **ноль результатов на любой запрос**,
потому что литерала `12.5` нет ни в одном документе.
"""

from __future__ import annotations

import pytest

from friday.retrieval import HybridSearcher

identifiers = HybridSearcher._query_identifiers  # noqa: SLF001


@pytest.mark.parametrize(
    "query",
    [
        "график отпусков и т.д.",
        "перечень имущества и т.п.",
        "и.о. начальника подписал",
        "cost centres e.g. logistics",
        "примерно 12.5 процента",
        "совещание в 9.00",
        "раздел 4.2 приказа",
    ],
)
def test_ordinary_language_is_not_a_hard_requirement(query):
    assert identifiers(query) == set(), query


@pytest.mark.parametrize(
    "query,expected",
    [
        ("смотри BRK.A и BRK.B", {"brk.a", "brk.b"}),
        ("файл config.yaml", {"config.yaml"}),
        ("бумага US0378331005 и т.п.", {"us0378331005"}),
        ("параметр autovacuum_vacuum_scale_factor.", {"autovacuum_vacuum_scale_factor"}),
        ("отчёт ПК-04-04", {"пк-04-04"}),
    ],
)
def test_a_real_code_is_still_required_verbatim(query, expected):
    """Ровно та причина, ради которой гейт существует: BRK.A не удовлетворяется BRK.B."""
    assert identifiers(query) == expected


def test_an_abbreviation_next_to_a_real_code_does_not_hide_it():
    """Смесь — обычный случай: сокращение выброшено, код остался обязательным."""
    assert identifiers("нужен BRK.A и т.д.") == {"brk.a"}


def test_a_single_part_token_with_a_dot_is_not_an_abbreviation():
    """Правило про односимвольные части не должно съесть `X.` или `config.yaml`."""
    assert identifiers("файл config.yaml") == {"config.yaml"}


@pytest.mark.asyncio
async def test_a_common_russian_phrase_no_longer_empties_the_answer(storage, settings):
    """Сквозь настоящий поиск: тот же запрос с «и т.д.» и без него."""
    from friday.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user("alice")
    text = (
        "График дежурств на квартал. Дежурство сдаётся в девять утра, приём смены "
        "оформляется записью в журнале, замена согласуется заранее."
    )
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="test",
        source_ref=new_id("source"),
        raw_content=text,
        content_type="text",
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id="alice",
        raw_object_id=raw.id,
        content=text,
        title="График дежурств",
        summary=text[:100],
    )
    storage.store_knowledge_object(ko)

    searcher = HybridSearcher(storage, None, record_usage=False)
    plain = await searcher.search("alice", "график отпусков", limit=5)
    with_filler = await searcher.search("alice", "график отпусков и т.д.", limit=5)

    assert [item["id"] for item in plain["results"]] == [ko.id]
    assert [item["id"] for item in with_filler["results"]] == [ko.id]
