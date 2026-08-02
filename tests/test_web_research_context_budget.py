"""The web-research result must stay plural all the way into the model context."""

from __future__ import annotations

import asyncio
import json

import pytest

import friday.web_surfer as web_surfer_module
from friday.execution_kernel import ToolResult
from friday.web_surfer import FetchResult, SearchResult, WebSurfer


def _source(number: int) -> dict[str, object]:
    return {
        "url": f"https://source-{number}.example/report",
        "title": f"Source {number}",
        "text": f"SOURCE-{number} " + (chr(64 + number) * 20_000),
        "text_length": 20_009,
        "status_code": 200,
        "error": "",
        "truncated": False,
        "search_title": f"Search result {number}",
        "snippet": f"Snippet {number}",
        "source": "fixture",
    }


def test_each_research_source_survives_the_tool_context_budget() -> None:
    """Before the fix only 1/3 URLs reached the model: the joined JSON was cut at its head."""
    message = ToolResult(
        tool_name="web_research",
        success=True,
        data={
            "query": "same query",
            "sources": [_source(1), _source(2), _source(3)],
            "summary": "Collected 3 readable public sources.",
        },
    ).to_llm_message()

    payload = json.loads(message.partition("\n")[2])

    assert len(message) <= 12_100
    assert [item["url"] for item in payload["sources"]] == [
        "https://source-1.example/report",
        "https://source-2.example/report",
        "https://source-3.example/report",
    ]
    assert [item["text"].split()[0] for item in payload["sources"]] == [
        "SOURCE-1",
        "SOURCE-2",
        "SOURCE-3",
    ]
    assert all(item["truncated"] is True for item in payload["sources"])


class _ResearchHarness:
    research = WebSurfer.research

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        del query, max_results
        return [
            SearchResult(f"Source {number}", f"https://source-{number}.example/", "", "fixture")
            for number in range(1, 4)
        ]

    async def fetch(self, url: str, *, max_length: int) -> FetchResult:
        del max_length
        number = int(url.split("source-", 1)[1].split(".", 1)[0])
        await asyncio.sleep({1: 0.0, 2: 0.01, 3: 0.2}[number])
        text = f"completed source {number}"
        return FetchResult(url, f"Source {number}", text, len(text), status_code=200)


class _SlowSearchHarness(_ResearchHarness):
    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        del query, max_results
        await asyncio.sleep(0.2)
        return []


@pytest.mark.asyncio
async def test_research_returns_completed_sources_before_its_deadline(monkeypatch) -> None:
    """Two completed fetches are evidence; one slow peer must not erase both at timeout."""
    monkeypatch.setattr(web_surfer_module, "_RESEARCH_FETCH_BUDGET", 0.05, raising=False)

    result = await asyncio.wait_for(_ResearchHarness().research("same query", max_sources=3), 0.1)

    assert [item["url"] for item in result["sources"]] == [
        "https://source-1.example/",
        "https://source-2.example/",
    ]
    assert result["requested_sources"] == 3
    assert result["completed_sources"] == 2
    assert result["timed_out_sources"] == 1
    assert "1" in result["summary"]


@pytest.mark.asyncio
async def test_research_search_stage_keeps_margin_for_the_kernel_deadline(monkeypatch) -> None:
    """Provider fallback can itself consume 30 seconds; research must return before the kernel cancels it."""
    monkeypatch.setattr(web_surfer_module, "_RESEARCH_TOTAL_BUDGET", 0.05, raising=False)

    result = await asyncio.wait_for(_SlowSearchHarness().research("same query", max_sources=3), 0.1)

    assert result["sources"] == []
    assert result["search_timed_out"] is True
    assert "timed out" in result["summary"].casefold()


class _CancelledFetchHarness(_ResearchHarness):
    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        del query, max_results
        return [SearchResult("Source 1", "https://source-1.example/", "", "fixture")]

    async def fetch(self, url: str, *, max_length: int) -> FetchResult:
        del url, max_length
        raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_research_does_not_swallow_cancelled_error_as_failed_source() -> None:
    """CancelledError is BaseException — a bare except BaseException used to
    count it as failed_sources and continue, hiding a parent-task cancel.
    """
    with pytest.raises(asyncio.CancelledError):
        await _CancelledFetchHarness().research("same query", max_sources=1)


def test_the_slot_budget_keeps_the_matching_passage_not_the_top_of_the_page() -> None:
    """Выдержка по запросу обязана пережить ВТОРОЙ срез — бюджет слота.

    Починка «кусок вокруг совпадения вместо шапки страницы» была сделана в
    `FetchResult.to_dict`, но её потолок (12 000 знаков) больше того, что реально
    влезает в слот источника: на трёх источниках это около 3 600 знаков. Второй
    срез резал с ГОЛОВЫ, то есть отменял первую починку — заявление «модель
    получает кусок вокруг совпадения» держалось только до момента, когда
    источников становилось больше одного.

    Сценарий — тот же, на котором мерили изначально: ответ лежит на позиции 9 500
    страницы в 11 900 знаков.

    Мутация: вернуть `source["text"] = text[:per_source]` — тест обязан
    покраснеть.
    """
    filler = "верхнее меню и навигация. " * 380  # ~9 500 знаков шапки
    page = filler + "MARKER-42 ответ на вопрос лежит здесь. " + "хвост страницы. " * 150
    assert page.index("MARKER-42") > 9_000, "стенд не воспроизводит сценарий: маркер слишком близко к началу"

    sources = [
        {
            "url": f"https://source-{number}.example/report",
            "title": f"Source {number}",
            "text": page,
            "text_length": len(page),
            "status_code": 200,
        }
        for number in (1, 2, 3)
    ]
    message = ToolResult(
        tool_name="web_research",
        success=True,
        data={"query": "MARKER-42", "sources": sources, "summary": "3 источника"},
    ).to_llm_message()

    payload = json.loads(message.partition("\n")[2])
    texts = [item["text"] for item in payload["sources"]]
    assert len(texts) == 3
    for index, text in enumerate(texts, start=1):
        assert "MARKER-42" in text, (
            f"источник {index}: до модели дошла шапка страницы, а не место совпадения "
            f"(первые знаки: {text[:60]!r})"
        )


class _UnreadableFirstPagesHarness:
    """Первые три страницы отдают 200 и пустой текст — как сайты на скриптах."""

    research = WebSurfer.research

    def __init__(self) -> None:
        self.fetched: list[str] = []

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        del query, max_results
        return [
            SearchResult(f"Source {n}", f"https://source-{n}.example/", "", "fixture")
            for n in range(1, 7)
        ]

    async def fetch(self, url: str, *, max_length: int) -> FetchResult:
        del max_length
        self.fetched.append(url)
        number = int(url.split("source-", 1)[1].split(".", 1)[0])
        if number <= 3:
            return FetchResult(url, "", "", 0, status_code=200)
        text = "Нефть Brent торгуется по 78,40 доллара за баррель."
        return FetchResult(url, f"Source {number}", text, len(text), status_code=200)


@pytest.mark.anyio
async def test_the_spare_results_are_used_when_the_first_pages_are_unreadable() -> None:
    """Мутация: убрать вторую волну чтения — тест краснеет.

    `search` спрашивает ВДВОЕ больше результатов, чем читает, и хвост просто
    лежал. Замерено на «сколько стоит нефть Brent»: TradingView отдаёт котировку
    скриптом, текста нет — человек получал «точная цифра в текстовом фрагменте
    не раскрылась», хотя следующая ссылка той же выдачи отвечала. Одиннадцать из
    двенадцати остальных вопросов при этом дали конкретное значение.
    """
    harness = _UnreadableFirstPagesHarness()
    result = await harness.research("цена нефти Brent", max_sources=3)

    assert len(harness.fetched) > 3, "запас из выдачи так и не прочитан"
    readable = [
        item
        for item in result["sources"]
        if not item["error"] and str(item.get("text") or "").strip()
    ]
    assert readable, "все источники пусты, хотя четвёртая ссылка отвечала"
    assert "78,40" in readable[0]["text"]
    assert result["requested_sources"] >= 4


class _ReadableHarness(_UnreadableFirstPagesHarness):
    async def fetch(self, url: str, *, max_length: int) -> FetchResult:
        del max_length
        self.fetched.append(url)
        return FetchResult(url, "", "Ставка 14,00%", 13, status_code=200)


@pytest.mark.anyio
async def test_a_readable_first_page_needs_no_second_wave() -> None:
    """Контроль: лишних чтений не делаем, когда фактура уже есть."""
    harness = _ReadableHarness()
    await harness.research("ключевая ставка", max_sources=3)
    assert len(harness.fetched) == 3, f"прочитано лишнее: {harness.fetched}"
