"""The web-research result must stay plural all the way into the model context."""

from __future__ import annotations

import asyncio
import json

import pytest

import jericho.web_surfer as web_surfer_module
from jericho.execution_kernel import ToolResult
from jericho.web_surfer import FetchResult, SearchResult, WebSurfer


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
