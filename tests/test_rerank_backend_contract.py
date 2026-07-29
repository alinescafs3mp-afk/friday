"""Клиент переранжировщика: скоры чужой службы не должны попасть не тем документам.

Ответ приходит списком записей с полем `index`, и порядок записей контрактом НЕ
задан. Разложить их по порядку прихода означало бы приписать оценки чужим документам —
ровно тот класс ошибок, который в индексаторе уже ловили явным учётом смещений, и
который ни один лог не покажет: вектора и скоры выглядят правдоподобно всегда.
"""

from __future__ import annotations

import asyncio
import dataclasses

import httpx
import pytest

from jericho.retrieval._rerank_backend import RerankBackend, rerank_with_backend


def _tuned(settings, **overrides):
    base = {
        "rerank_base_url": "http://rerank.invalid/v1",
        "rerank_model": "bge-reranker",
        "rerank_api_key": "",
        "rerank_timeout_sec": 5.0,
    }
    return dataclasses.replace(settings, **{**base, **overrides})


def _client(monkeypatch, payload, *, status: int = 200):
    seen: list[dict] = []

    class _Response:
        status_code = status

        def raise_for_status(self) -> None:
            if status >= 400:
                raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

        def json(self):
            return payload

    class _Client:
        def __init__(self, *args, **kwargs) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None: ...

        async def post(self, url, **kwargs):
            seen.append({"url": url, **kwargs.get("json", {})})
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return seen


def _items(count: int):
    return [
        {"id": f"ko-{index}", "title": f"Документ {index}", "content": f"тело {index}"}
        for index in range(count)
    ]


def test_scores_are_placed_by_index_not_by_arrival_order(settings, monkeypatch):
    """Служба вправе вернуть записи в любом порядке — раскладываем по `index`."""
    _client(
        monkeypatch,
        {
            "results": [
                {"index": 2, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.1},
                {"index": 1, "relevance_score": 0.5},
            ]
        },
    )
    backend = RerankBackend(_tuned(settings))
    assert asyncio.run(backend.scores("вопрос", ["а", "б", "в"])) == [0.1, 0.5, 0.9]


def test_a_short_or_duplicated_answer_is_refused_rather_than_padded(settings, monkeypatch):
    """Недостача или повтор — не повод дописать нули: документ уехал бы вниз молча."""
    _client(monkeypatch, {"results": [{"index": 0, "relevance_score": 0.9}]})
    assert asyncio.run(RerankBackend(_tuned(settings)).scores("в", ["а", "б", "в"])) is None

    _client(
        monkeypatch,
        {
            "results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.5},
                {"index": 1, "relevance_score": 0.1},
            ]
        },
    )
    assert asyncio.run(RerankBackend(_tuned(settings)).scores("в", ["а", "б", "в"])) is None


def test_an_out_of_range_index_is_refused(settings, monkeypatch):
    _client(
        monkeypatch,
        {
            "results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 7, "relevance_score": 0.5},
            ]
        },
    )
    assert asyncio.run(RerankBackend(_tuned(settings)).scores("в", ["а", "б"])) is None


def test_a_non_numeric_score_is_refused_rather_than_guessed(settings, monkeypatch):
    _client(
        monkeypatch,
        {
            "results": [
                {"index": 0, "relevance_score": "высокий"},
                {"index": 1, "relevance_score": 0.5},
            ]
        },
    )
    assert asyncio.run(RerankBackend(_tuned(settings)).scores("в", ["а", "б"])) is None


def test_it_is_off_until_configured(settings, monkeypatch):
    """Пустой адрес или модель — шаг выключен и в сеть не ходит."""
    calls = _client(monkeypatch, {"results": []})
    assert RerankBackend(_tuned(settings, rerank_base_url="")).enabled is False
    assert asyncio.run(RerankBackend(_tuned(settings, rerank_base_url="")).scores("в", ["а"])) is None
    assert RerankBackend(_tuned(settings, rerank_model="")).enabled is False
    assert not calls, "выключенный переранжировщик обратился к сети"


def test_an_overloaded_service_earns_a_pause(settings, monkeypatch):
    """Пауза здесь дешевле, чем у эмбеддингов: пока она держится, поиск идёт прежним
    порядком, а не остаётся без результатов."""
    _client(monkeypatch, {}, status=503)
    backend = RerankBackend(_tuned(settings))
    assert asyncio.run(backend.scores("в", ["а", "б"])) is None
    assert backend.cooling_down is True

    calls = _client(monkeypatch, {"results": [{"index": 0, "relevance_score": 1.0}]})
    assert asyncio.run(backend.scores("в", ["а"])) is None
    assert not calls, "во время паузы клиент всё равно постучался в службу"


def test_reordering_keeps_every_item_and_records_the_score(settings, monkeypatch):
    _client(
        monkeypatch,
        {
            "results": [
                {"index": 0, "relevance_score": 0.2},
                {"index": 1, "relevance_score": 0.9},
                {"index": 2, "relevance_score": 0.5},
            ]
        },
    )
    items = _items(3)
    result = asyncio.run(rerank_with_backend(RerankBackend(_tuned(settings)), "вопрос", items))

    assert result is not None
    assert [item["id"] for item in result] == ["ko-1", "ko-2", "ko-0"]
    assert result[0]["_rerank_score"] == 0.9
    assert sorted(item["id"] for item in result) == sorted(item["id"] for item in items)


def test_a_threshold_demotes_but_never_drops(settings, monkeypatch):
    """Порог опускает вниз, а не выбрасывает: гейт доказательств уже отработал, и
    второе решение о том, что человеку не показывать, здесь принимать нечем."""
    _client(
        monkeypatch,
        {
            "results": [
                {"index": 0, "relevance_score": 0.05},
                {"index": 1, "relevance_score": 0.95},
                {"index": 2, "relevance_score": 0.02},
            ]
        },
    )
    result = asyncio.run(
        rerank_with_backend(RerankBackend(_tuned(settings)), "вопрос", _items(3), min_score=0.5)
    )
    assert result is not None
    assert len(result) == 3, "порог выбросил документы вместо того, чтобы опустить их"
    assert result[0]["id"] == "ko-1"


def test_documents_are_bounded_before_they_are_sent(settings, monkeypatch):
    """У cross-encoder свой предел длины; резать надо до отправки, а не ловить отказ."""
    from jericho.retrieval._rerank_backend import DOCUMENT_CHARS

    seen = _client(monkeypatch, {"results": [{"index": 0, "relevance_score": 1.0}]})
    asyncio.run(RerankBackend(_tuned(settings)).scores("вопрос", ["я" * 50_000]))
    assert seen and all(len(text) <= DOCUMENT_CHARS for text in seen[0]["documents"])


@pytest.mark.parametrize("count", [0, 1])
def test_nothing_to_reorder_is_not_a_service_call(settings, monkeypatch, count):
    calls = _client(monkeypatch, {"results": []})
    assert asyncio.run(rerank_with_backend(RerankBackend(_tuned(settings)), "в", _items(count))) is None
    assert not calls
