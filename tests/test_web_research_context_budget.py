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
            SearchResult(f"Source {n}", f"https://source-{n}.example/", "", "fixture") for n in range(1, 7)
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
async def test_research_refills_all_unreadable_first_wave_to_source_target() -> None:
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
    assert len(harness.fetched) == 6
    assert len(result["sources"]) == 3
    assert all("78,40" in item["text"] for item in result["sources"])
    assert result["target_sources"] == 3
    assert result["requested_sources"] == 6
    assert result["completed_sources"] == 3
    assert result["failed_sources"] == 3
    assert result["timed_out_sources"] == 0


class _ReadableHarness(_UnreadableFirstPagesHarness):
    async def fetch(self, url: str, *, max_length: int) -> FetchResult:
        del max_length
        self.fetched.append(url)
        return FetchResult(url, "", "Ставка 14,00%", 13, status_code=200)


@pytest.mark.anyio
async def test_research_does_not_fetch_spares_when_source_target_is_complete() -> None:
    """A full first wave does not spend quota or time on speculative pages."""
    harness = _ReadableHarness()
    result = await harness.research("ключевая ставка", max_sources=3)
    assert len(harness.fetched) == 3, f"прочитано лишнее: {harness.fetched}"
    assert result["target_sources"] == 3
    assert result["requested_sources"] == result["completed_sources"] == 3
    assert result["failed_sources"] == result["timed_out_sources"] == 0


@pytest.mark.anyio
async def test_research_target_is_bounded_by_available_results() -> None:
    harness = _ScriptedRefillHarness(["complete"])

    result = await harness.research("one available source", max_sources=3)

    assert harness.fetched == [1]
    assert result["target_sources"] == 1
    assert result["requested_sources"] == result["completed_sources"] == 1


class _ScriptedRefillHarness:
    research = WebSurfer.research

    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = outcomes
        self.fetched: list[int] = []
        self.cancelled: list[int] = []

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        del query
        return [
            SearchResult(f"Source {number}", f"https://source-{number}.example/", "", "fixture")
            for number in range(1, min(max_results, len(self.outcomes)) + 1)
        ]

    async def fetch(self, url: str, *, max_length: int) -> FetchResult:
        del max_length
        number = int(url.split("source-", 1)[1].split(".", 1)[0])
        self.fetched.append(number)
        outcome = self.outcomes[number - 1]
        if outcome == "slow":
            try:
                await asyncio.sleep(10)
            finally:
                self.cancelled.append(number)
        if outcome == "empty":
            return FetchResult(url, "", "", 0, status_code=200)
        if outcome == "error":
            return FetchResult(url, "", "", 0, status_code=503, error="HTTP 503")
        text = f"complete source {number}"
        return FetchResult(
            url,
            f"Source {number}",
            text,
            len(text),
            status_code=200,
            truncated=outcome == "truncated",
        )


@pytest.mark.anyio
async def test_research_refills_partial_first_wave_to_source_target() -> None:
    harness = _ScriptedRefillHarness(["complete", "complete", "empty", "complete", "complete", "complete"])

    result = await harness.research("synthetic fact", max_sources=3)

    assert harness.fetched == [1, 2, 3, 4]
    assert [item["url"] for item in result["sources"]] == [
        "https://source-1.example/",
        "https://source-2.example/",
        "https://source-4.example/",
    ]
    assert (
        result["target_sources"],
        result["requested_sources"],
        result["completed_sources"],
        result["failed_sources"],
        result["timed_out_sources"],
    ) == (3, 4, 3, 1, 0)


@pytest.mark.anyio
async def test_research_replaces_truncated_page_with_complete_spare() -> None:
    harness = _ScriptedRefillHarness(
        ["complete", "complete", "truncated", "complete", "complete", "complete"]
    )

    result = await harness.research("synthetic fact", max_sources=3)

    assert harness.fetched == [1, 2, 3, 4]
    assert all(item["truncated"] is False for item in result["sources"])
    assert all("source-3.example" not in item["url"] for item in result["sources"])
    assert (
        result["target_sources"],
        result["requested_sources"],
        result["completed_sources"],
        result["failed_sources"],
        result["timed_out_sources"],
    ) == (3, 4, 3, 1, 0)


@pytest.mark.anyio
async def test_research_exhausted_spares_remains_partial_with_exact_attempt_counters() -> None:
    harness = _ScriptedRefillHarness(["complete", "complete", "empty", "empty", "error", "empty"])

    result = await harness.research("synthetic fact", max_sources=3)

    assert harness.fetched == [1, 2, 3, 4, 5, 6]
    assert len(result["sources"]) == 2
    assert (
        result["target_sources"],
        result["requested_sources"],
        result["completed_sources"],
        result["failed_sources"],
        result["timed_out_sources"],
    ) == (3, 6, 2, 4, 0)


@pytest.mark.anyio
async def test_research_retains_useful_partial_row_only_when_target_cannot_be_filled() -> None:
    harness = _ScriptedRefillHarness(["complete", "complete", "truncated", "empty", "error", "empty"])

    result = await harness.research("synthetic fact", max_sources=3)

    assert harness.fetched == [1, 2, 3, 4, 5, 6]
    assert len(result["sources"]) == 3
    assert result["sources"][-1]["url"] == "https://source-3.example/"
    assert result["sources"][-1]["truncated"] is True
    assert (
        result["target_sources"],
        result["requested_sources"],
        result["completed_sources"],
        result["failed_sources"],
        result["timed_out_sources"],
    ) == (3, 6, 3, 3, 0)


@pytest.mark.anyio
async def test_research_deadline_deficit_remains_partial_and_cancels_pending(monkeypatch) -> None:
    monkeypatch.setattr(web_surfer_module, "_RESEARCH_TOTAL_BUDGET", 0.03, raising=False)
    monkeypatch.setattr(web_surfer_module, "_RESEARCH_FETCH_BUDGET", 0.03, raising=False)
    harness = _ScriptedRefillHarness(["complete", "complete", "slow", "complete", "complete", "complete"])

    result = await asyncio.wait_for(harness.research("synthetic fact", max_sources=3), 0.2)

    assert harness.fetched == [1, 2, 3]
    assert harness.cancelled == [3]
    assert (
        result["target_sources"],
        result["requested_sources"],
        result["completed_sources"],
        result["failed_sources"],
        result["timed_out_sources"],
    ) == (3, 3, 2, 0, 1)
