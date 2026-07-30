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


# --- порог отсекает ------------------------------------------------------------


def _knowledge(storage, user_id: str, text: str) -> str:
    from jericho.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user(user_id)
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        title=text[:60],
        summary=text[:120],
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _reranker(by_position):
    """Переранжировщик, раздающий заданные скоры по порядку прихода."""

    async def call(query, items):
        out = []
        for index, item in enumerate(items):
            copy = dict(item)
            copy["_rerank_score"] = by_position[index % len(by_position)]
            out.append(copy)
        return out

    return call


def _searcher(storage, scores, **kwargs):
    from jericho.retrieval import HybridSearcher

    options = {"rerank_top": 20, "rerank_confident_min": 0.10}
    options.update(kwargs)
    return HybridSearcher(storage, None, record_usage=False, reranker=_reranker(scores), **options)


def _corpus(storage, count=8):
    for index in range(count):
        _knowledge(storage, "owner", f"Договор поставки оборудования номер {index} на склад")


async def test_documents_below_the_threshold_do_not_reach_the_page(settings, storage):
    """Замер размена в `_rerank_backend`: доля отвечающих 43.5% -> 78.6%."""
    _corpus(storage)
    result = await _searcher(storage, [0.99, 0.99, 0.004]).search(
        "owner", "договор поставки оборудования", limit=8
    )

    assert result["strategy"]["rerank_dropped"] > 0
    assert all(item["_rerank_score"] >= 0.10 for item in result["results"])


async def test_the_cut_happens_before_the_page_is_trimmed(settings, storage):
    """Отсечь ПОСЛЕ обрезки значило бы отдать места страницы отвергнутым.

    Здесь каждый третий кандидат негоден. Режем сначала — страница полная; режем
    после — в ней остаются две строки из трёх.
    """
    _corpus(storage, count=12)
    result = await _searcher(storage, [0.99, 0.99, 0.004]).search(
        "owner", "договор поставки оборудования", limit=3
    )

    assert len(result["results"]) == 3, "отсев съел места в странице"


async def test_a_pool_with_no_answer_comes_back_empty_and_says_why(settings, storage):
    """Пустая выдача обязана отличаться от пустого архива — иначе совет будет неверным."""
    _corpus(storage)
    result = await _searcher(storage, [0.004]).search("owner", "договор поставки оборудования")

    assert result["results"] == []
    assert result["strategy"]["rerank_dropped"] >= 2


async def test_a_zero_threshold_keeps_everything(settings, storage):
    """0 — это «не отсеивать»: скор не бывает отрицательным, порог перестаёт быть гейтом."""
    _corpus(storage)
    result = await _searcher(storage, [0.004], rerank_confident_min=0.0).search(
        "owner", "договор поставки оборудования", limit=8
    )

    assert result["results"], "нулевой порог не должен ничего резать"
    assert "rerank_dropped" not in result["strategy"]


async def test_the_trace_names_the_threshold_as_the_reason(settings, storage):
    """`rerank_below_threshold` — такая же названная причина, как три остальные.

    Без неё снятый за порог документ лежал бы в трейсе как «не поместился в
    страницу», и вопрос «он же точно про это» снова остался бы без ответа.
    """
    _corpus(storage)
    result = await _searcher(storage, [0.99, 0.004]).search(
        "owner", "договор поставки оборудования", limit=8, explain=True
    )

    cut = [row for row in result["trace"] if row.get("reason") == "rerank_below_threshold"]
    assert cut, "отсев переранжировщиком не назван в трейсе"
    assert all(row["rerank_score"] < 0.10 for row in cut), "причина названа, а число не показано"


async def test_a_small_result_set_is_filtered_too(settings, storage):
    """Отсев нужен ИМЕННО когда нашлось мало: три негодных убедительнее двадцати.

    Раньше шаг запускался только при «кандидатов больше запрошенного» — потому что
    только переставлял. С порогом у него вторая работа.
    """
    _corpus(storage, count=3)
    result = await _searcher(storage, [0.004]).search("owner", "договор поставки оборудования", limit=10)

    assert result["results"] == []
    assert result["strategy"]["rerank_dropped"] == 3


# --- размер страницы не должен менять, ЧТО найдено ----------------------------


async def test_a_small_page_does_not_scan_less_of_the_archive(settings, storage):
    """Один вопрос давал РАЗНЫЕ ответы в Telegram и в админке.

    Ширина отбора кандидатов росла от размера страницы: FTS брал `limit × 5`, пул —
    `limit × 10`. Telegram просит восемь, админка двадцать — и это разные пулы на один
    вопрос. Замерено на корпусе владельца: при `limit=8` порог оставлял пустыми 12
    вопросов из 32, при `limit=20` — 10, разница целиком в составе пула.

    Собирать надо на глубину переранжирования; урезание до страницы — шаг ПОСЛЕ.
    """
    for index in range(150):
        _knowledge(storage, "owner", f"Договор поставки оборудования номер {index} на склад")

    narrow = await _searcher(storage, [0.99]).search("owner", "договор поставки", limit=2)
    wide = await _searcher(storage, [0.99]).search("owner", "договор поставки", limit=20)

    assert narrow["strategy"].get("lexical_pool_scanned") == wide["strategy"].get("lexical_pool_scanned"), (
        "узкая страница просмотрела меньше архива, чем широкая"
    )
    assert not narrow["strategy"].get("lexical_pool_capped"), (
        "пул упёрся в потолок только из-за размера страницы"
    )
