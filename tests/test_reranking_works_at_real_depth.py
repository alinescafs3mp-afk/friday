"""Переранжирование должно работать на той глубине, на которой его включают.

Клиент отправлял весь пул одним запросом. Служба считает токены ПО ВСЕМ ПАРАМ сразу,
и вопрос входит в каждую: двадцать документов по 4000 знаков — около 36 тысяч токенов
при пределе 16384. Отказ ловился общим `except`, `scores` возвращала None, поиск
оставался в прежнем порядке — и ни в выдаче, ни в журнале это не выглядело поломкой.
Настроенное переранжирование просто не работало бы никогда.

Числа предела и плотности замерены на живой службе и записаны в `_rerank_backend`.
"""

from __future__ import annotations

import asyncio
import dataclasses

import httpx

from jericho.retrieval._rerank_backend import RerankBackend

SERVICE_TOKEN_LIMIT = 16_384


def _tuned(settings, **overrides):
    base = {
        "rerank_base_url": "http://rerank.invalid/v1",
        "rerank_model": "cross-encoder",
        "rerank_api_key": "",
        "rerank_timeout_sec": 20.0,
    }
    return dataclasses.replace(settings, **{**base, **overrides})


def _service(monkeypatch, *, clock=None, cost_sec: float = 0.0):
    """Служба с настоящим пределом: считает токены по всем парам и отказывает 413.

    Скор берётся ИЗ ТЕКСТА документа, а не из его места в запросе. Иначе проба не
    отличила бы правильную склейку половин от любой другой.
    """
    calls: list[int] = []

    class _Response:
        def __init__(self, status: int, payload: dict) -> None:
            self.status_code = status
            self._payload = payload
            self.text = "" if status == 200 else "Tokenized request exceeds 16384 tokens."

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
            body = kwargs.get("json", {})
            documents = body["documents"]
            query = body["query"]
            calls.append(len(documents))
            if clock is not None:
                clock[0] += cost_sec
            tokens = len(documents) * (80 + len(query) / 2.3) + sum(len(text) for text in documents) / 2.3
            if tokens > SERVICE_TOKEN_LIMIT:
                return _Response(413, {})
            results = [
                {"index": index, "relevance_score": float(text.split()[1]) / 100.0}
                for index, text in enumerate(documents)
            ]
            return _Response(200, {"results": list(reversed(results))})

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return calls


def _page(count: int, chars: int = 4_000) -> list[str]:
    return [f"документ {index:02d} " + "я" * chars for index in range(count)]


def test_a_full_page_of_results_is_actually_reranked(settings, monkeypatch):
    """Двадцать документов по 4000 знаков — и каждый получает СВОЙ скор."""
    calls = _service(monkeypatch)
    documents = _page(20)

    scores = asyncio.run(RerankBackend(_tuned(settings)).scores("что известно о поверке", documents))

    assert scores is not None, "пул рабочего размера не должен обнулять переранжирование"
    assert scores == [index / 100.0 for index in range(20)]
    assert len(calls) > 1, "такой пул обязан делиться"
    assert sum(calls) == 20, "каждый документ оценивается ровно один раз"


def test_the_split_is_not_paid_for_twice(settings, monkeypatch):
    """Заведомо большой запрос не отправляется: размер оценивается заранее.

    Проверяется по числу пар в ПЕРВОМ обращении — если бы клиент шёл напролом, оно
    было бы полным пулом с гарантированным отказом.
    """
    calls = _service(monkeypatch)

    asyncio.run(RerankBackend(_tuned(settings)).scores("вопрос", _page(20)))

    assert calls[0] < 20


def test_the_split_shares_one_deadline(settings, monkeypatch):
    """Половины наследуют срок, а не таймаут: поиск не ждёт кратно обещанного."""
    clock = [1_000.0]
    monkeypatch.setattr("jericho.retrieval._rerank_backend.time.monotonic", lambda: clock[0])
    calls = _service(monkeypatch, clock=clock, cost_sec=3.0)
    started = clock[0]

    result = asyncio.run(RerankBackend(_tuned(settings, rerank_timeout_sec=5.0)).scores("вопрос", _page(20)))

    assert result is None, "уложиться в срок не удалось — честнее вернуть прежний порядок"
    assert clock[0] - started < 5.0 + 3.0, "срок общий на операцию, а не на каждую половину"
    assert len(calls) <= 2


def test_a_short_pool_still_goes_in_one_request(settings, monkeypatch):
    """Деление включается по размеру, а не всегда: лишние обращения тоже цена."""
    calls = _service(monkeypatch)

    scores = asyncio.run(RerankBackend(_tuned(settings)).scores("вопрос", _page(3, chars=500)))

    assert scores == [0.0, 0.01, 0.02]
    assert calls == [3]
