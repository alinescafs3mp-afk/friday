"""Политику графа объявляет вызывающий, а не умолчание.

Замер записан в `sol/SOL.md` §3: на 20 документных эталонах живого архива
расширение по графу роняло recall@10 0.35 -> 0.15, MRR 0.153 -> 0.063 и стоило
+556 мс (107 -> 662). Отдельный набор из 12 реляционных кейсов дал `net_gain=2`,
поэтому канал включается ровно измеренному языковому классу.

Дефект, который эти пробы закрывают, был не в логике, а в том, КТО принимает
решение. Из семи дорог, зовущих `HybridSearcher.search`, четыре объявляли режим
вслух, а три молчали — и получали граф по умолчанию: публичный `GET /api/search`,
админский `eval_search` и синтетический прибор `retrieval_bench`. Человек в панели
искал одним поиском, Пятница в чате отвечала другим, а прибор мерил третий.

Пробы ПРОВОДОЧНЫЕ: смотрят, с чем ПОЗВАЛИ поиск, а не то, что параметр существует
в сигнатуре. Обязательные мутации перечислены в `sol/PROPOSALS.md` #40 и #41.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from friday.retrieval import HybridSearcher, is_relational_query
from friday.server import create_app

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

ORDINARY = "Атлас квартальный отчёт"
RELATIONAL = "с кем работал Альфа"


def test_the_two_probe_queries_are_on_opposite_sides_of_the_classifier() -> None:
    """Иначе весь файл проверял бы одну ветку и молчал бы про вторую."""

    assert is_relational_query(ORDINARY) is False
    assert is_relational_query(RELATIONAL) is True


class _GraphSpy:
    """Считает обходы графа. `search_entities` — ДРУГОЙ канал (`include_entities`),
    он не запрещён и специально учитывается отдельно."""

    def __init__(self) -> None:
        self.traversals: list[dict] = []
        self.entity_lookups: list[str] = []

    def context_for_query(self, user_id, query, **kwargs):
        self.traversals.append({"user_id": user_id, "query": query, **kwargs})
        return {
            "as_of": kwargs.get("as_of", ""),
            "known_at": kwargs.get("known_at", ""),
            "roots": [],
            "nodes": [],
            "entities": [],
            "relations": [],
            "paths": [],
            "knowledge_candidates": [],
        }

    def search_entities(self, user_id, query, *, limit=5):
        del limit
        self.entity_lookups.append(query)
        return []


class _SearchSpy:
    """Подменяет поиск и запоминает, с чем его позвали."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, user_id, query, **kwargs):
        self.calls.append({"user_id": user_id, "query": query, **kwargs})
        return {"query": query, "results": [], "count": 0, "trace": []}


@pytest.mark.asyncio
async def test_a_silent_caller_reaches_no_graph_traversal(storage):
    """Сторож самого умолчания.

    Мутация: вернуть `graph_expansion: bool = True` в сигнатуре
    `HybridSearcher.search` — эта проба обязана покраснеть. Без неё инверсия
    умолчания держится только на трёх исправленных дорогах, и четвёртая снова
    включит граф молча.
    """

    storage.ensure_user("alice")
    spy = _GraphSpy()

    await HybridSearcher(storage, record_usage=False).search("alice", ORDINARY, kg=spy)

    assert spy.traversals == [], (
        "поиск обошёл граф, хотя вызывающий об этом не просил: умолчание снова принимает решение за него"
    )


@pytest.mark.asyncio
async def test_asking_for_the_graph_out_loud_still_reaches_it(storage):
    """Обратная сторона: инверсия умолчания не должна отключить канал вовсе."""

    storage.ensure_user("alice")
    spy = _GraphSpy()

    await HybridSearcher(storage, record_usage=False).search(
        "alice", RELATIONAL, kg=spy, graph_expansion=True
    )

    assert len(spy.traversals) == 1


@pytest.mark.parametrize(
    ("query", "params", "expected"),
    [
        (ORDINARY, {}, False),
        (RELATIONAL, {}, True),
        (ORDINARY, {"as_of": "2024-03-05"}, True),
        (ORDINARY, {"known_at": "2026-08-04T12:30:00+03:00"}, True),
    ],
    ids=["обычный", "реляционный", "названная дата", "названный снимок"],
)
def test_public_search_route_declares_the_agent_policy(settings, monkeypatch, query, params, expected):
    """`GET /api/search` обязан спрашивать ровно то же, что агент.

    Мутация: убрать `graph_expansion` из вызова в `friday/server.py` — обычный
    запрос снова получит граф, и первый случай покраснеет.
    """

    app = create_app(settings)
    spy = _SearchSpy()
    headers = {"Authorization": f"Bearer {settings.api_token}"}

    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(app.state.hybrid_searcher, "search", spy)
        monkeypatch.setattr(
            app.state.storage,
            "relation_history_status",
            lambda _user_id, *, known_at="": {
                "known_at": known_at,
                "known_at_floor": "2026-08-01T00:00:00.000000Z",
                "history_complete": True,
                "identity_basis": "current_names",
            },
        )
        response = client.get("/api/search", params={"q": query, **params}, headers=headers)

    assert response.status_code == 200
    assert spy.calls, "маршрут вообще не позвал поиск — проба проверяет не то"
    assert spy.calls[0].get("graph_expansion") is expected


def test_admin_eval_search_declares_the_policy_and_writes_no_usage(settings, monkeypatch):
    """Диагностика обязана мерить ту дорогу, которую показывает, и не двигать её.

    Мутации: убрать `graph_expansion` — покраснеет первый assert; убрать
    `record_usage=False` — покраснеет второй, потому что счётчик обращений
    читается обратно ранжированием.
    """

    app = create_app(settings)
    spy = _SearchSpy()
    headers = {"Authorization": f"Bearer {settings.api_token}"}

    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(app.state.hybrid_searcher, "search", spy)
        ordinary = client.get("/api/admin/eval/search", params={"q": ORDINARY}, headers=headers)
        relational = client.get("/api/admin/eval/search", params={"q": RELATIONAL}, headers=headers)

    assert ordinary.status_code == 200
    assert relational.status_code == 200
    assert [call.get("graph_expansion") for call in spy.calls] == [False, True]
    assert all(call.get("record_usage") is False for call in spy.calls), (
        "диагностический прогон записал обращение, а ранжирование читает этот "
        "счётчик обратно — список меняет сам себя при каждом открытии"
    )


@pytest.mark.asyncio
async def test_the_bench_measures_the_road_that_ships():
    """Прибор мерил поиск с графом, а в бою обычный запрос идёт без него.

    Мутация: убрать `graph_expansion` из `measure()` в `tools/retrieval_bench.py`
    — проба покраснеет на первом же обычном запросе золотого набора.
    """

    from retrieval_bench import GOLD, measure

    spy = _SearchSpy()
    searcher = type("_Bench", (), {"search": staticmethod(spy)})()

    await measure(searcher, _GraphSpy(), "bench", 10)

    assert len(spy.calls) == len(GOLD)
    for call in spy.calls:
        assert call.get("graph_expansion") is is_relational_query(call["query"]), (
            f"прибор мерит не ту дорогу на запросе {call['query']!r}"
        )
