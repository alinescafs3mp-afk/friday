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

from friday.retrieval._rerank_backend import RerankBackend, rerank_with_backend


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


def test_the_order_is_the_score_and_nothing_else(settings, monkeypatch):
    """Здесь был параметр `min_score`, и он ДОКАЗУЕМО ничего не делал.

    Он «опускал вниз» документы ниже порога, разбивая список на «выше» и «ниже» и
    склеивая обратно. Список к тому моменту уже отсортирован по убыванию скора,
    поэтому склейка возвращала ровно исходный порядок при ЛЮБОМ пороге. Параметр
    читался как решение и был ничем — удалён; порог теперь считается там, где он
    виден человеку (`strategy.rerank_confident`).

    Проба держит инвариант, который от него оставался и был настоящим: состав не
    меняется, порядок — строго по убыванию скора.
    """
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
    items = _items(3)
    result = asyncio.run(rerank_with_backend(RerankBackend(_tuned(settings)), "вопрос", items))

    assert result is not None
    assert [item["id"] for item in result] == ["ko-1", "ko-0", "ko-2"]
    assert sorted(item["id"] for item in result) == sorted(item["id"] for item in items)
    scores = [item["_rerank_score"] for item in result]
    assert scores == sorted(scores, reverse=True)


def test_documents_are_bounded_before_they_are_sent(settings, monkeypatch):
    """У cross-encoder свой предел длины; резать надо до отправки, а не ловить отказ."""
    from friday.retrieval._rerank_backend import DOCUMENT_CHARS

    seen = _client(monkeypatch, {"results": [{"index": 0, "relevance_score": 1.0}]})
    asyncio.run(RerankBackend(_tuned(settings)).scores("вопрос", ["я" * 50_000]))
    assert seen and all(len(text) <= DOCUMENT_CHARS for text in seen[0]["documents"])


def test_nothing_to_reorder_is_not_a_service_call(settings, monkeypatch):
    calls = _client(monkeypatch, {"results": []})
    assert asyncio.run(rerank_with_backend(RerankBackend(_tuned(settings)), "в", _items(0))) is None
    assert not calls


def test_a_single_item_is_scored_for_the_threshold(settings, monkeypatch):
    """Один кандидат не переставляется, но ОЦЕНИВАЕТСЯ.

    Раньше гард `len < 2` оставлял единственный правдоподобный документ без
    скора — и порог уверенности не применялся ровно там, где ложный ответ
    убедительнее всего: одиночка не с чем сравнивать.
    """
    calls = _client(monkeypatch, {"results": [{"index": 0, "relevance_score": 0.03}]})
    out = asyncio.run(rerank_with_backend(RerankBackend(_tuned(settings)), "в", _items(1)))

    assert calls, "единственный кандидат остался без оценки"
    assert out is not None and out[0]["_rerank_score"] == 0.03


def _overflow_client(monkeypatch, *, first_status: int, first_text: str):
    """Первый запрос отвечает отказом, последующие — честными скорами."""
    calls: list[int] = []

    class _Response:
        def __init__(self, status: int, payload, text: str = "") -> None:
            self.status_code = status
            self._payload = payload
            self.text = text

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs) -> None: ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None: ...

        async def post(self, url, **kwargs):
            documents = kwargs.get("json", {}).get("documents", [])
            calls.append(len(documents))
            if len(calls) == 1:
                return _Response(first_status, {}, text=first_text)
            return _Response(
                200,
                {"results": [{"index": index, "relevance_score": 0.5} for index in range(len(documents))]},
            )

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return calls


def test_a_context_overflow_400_is_split_not_swallowed(settings, monkeypatch):
    """vLLM отвечает на переполнение контекста не 413, а 400 с текстом про длину.

    Раньше такой отказ падал в общий except → None: поиск молча оставался без
    переранжирования и порога, а сервис при этом жив — промах грубой оценки
    размера стоил не лишнего запроса, а тишины.
    """
    calls = _overflow_client(
        monkeypatch,
        first_status=400,
        first_text=(
            "This model's maximum context length is 16384 tokens. "
            "However, you requested 20000 tokens in the input."
        ),
    )
    scores = asyncio.run(RerankBackend(_tuned(settings)).scores("вопрос", ["тело"] * 4))

    assert scores == [0.5, 0.5, 0.5, 0.5]
    assert calls == [4, 2, 2], "переполнение не поделено пополам"


def test_an_ordinary_400_is_still_a_refusal(settings, monkeypatch):
    """400 без признака переполнения — настоящий отказ, делить его бессмысленно."""
    calls = _overflow_client(monkeypatch, first_status=400, first_text="Bad Request: unknown model")
    scores = asyncio.run(RerankBackend(_tuned(settings)).scores("вопрос", ["тело"] * 4))

    assert scores is None
    assert calls == [4], "обычный 400 не должен вызывать деление"
