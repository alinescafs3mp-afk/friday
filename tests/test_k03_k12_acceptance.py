"""Synthetic acceptance for the still-open K03/K12 live-test findings.

The corpus contains no production conversation text.  It freezes only the
observable contracts recovered from the live-test classifier:

* K03: a natural-language tag inventory is one code-owned read, with an exact
  corpus total, an honest distinction between empty/failure/filtered pages, and
  a compound request's unrelated remainder preserved;
* K12: Friday's system prompt must not forbid the Markdown which the Telegram
  transport actually renders, while the renderer remains an escaping boundary.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import pytest

from friday.agent_runtime import SYSTEM_PROMPT, AgentContext, AgentRuntime
from friday.execution_kernel import ExecutionKernel, ToolResult
from friday.permissions import ActorContext
from friday.telegram_bridge._markup import to_telegram_html


def _tool(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


@dataclass
class _PagedTagStorage:
    rows: list[dict[str, Any]]
    exact_total: int

    def __post_init__(self) -> None:
        self.conn = _SnapshotConnection()
        self.list_calls: list[int] = []
        self.count_calls = 0
        self.list_snapshot_states: list[bool] = []
        self.count_snapshot_states: list[bool] = []

    def list_knowledge_tags(self, user_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        assert user_id == "synthetic"
        self.list_calls.append(limit)
        self.list_snapshot_states.append(self.conn.in_transaction)
        return self.rows[:limit]

    def count_knowledge_tags(self, user_id: str) -> int:
        assert user_id == "synthetic"
        self.count_calls += 1
        self.count_snapshot_states.append(self.conn.in_transaction)
        return self.exact_total


class _SnapshotConnection:
    def __init__(self) -> None:
        self.in_transaction = False
        self.begin_calls = 0
        self.rollback_calls = 0

    def execute(self, statement: str) -> None:
        assert statement == "BEGIN"
        assert self.in_transaction is False
        self.in_transaction = True
        self.begin_calls += 1

    def rollback(self) -> None:
        assert self.in_transaction is True
        self.in_transaction = False
        self.rollback_calls += 1


def _kernel_with_storage(storage: _PagedTagStorage) -> ExecutionKernel:
    kernel = ExecutionKernel()
    kernel.storage = storage  # type: ignore[assignment]
    # ``_list_tags`` consumes only storage, while ``_require_services`` verifies
    # that the production kernel has completed its ordinary service binding.
    kernel.kg = object()  # type: ignore[assignment]
    kernel.web_surfer = object()  # type: ignore[assignment]
    kernel.ingestion = object()  # type: ignore[assignment]
    return kernel


@pytest.mark.asyncio
async def test_k03_tag_total_is_an_exact_count_not_the_storage_page_length() -> None:
    """Mutation: derive ``total`` from ``len(list_knowledge_tags(...))``."""

    rows = [{"tag": f"метка-{index:03d}", "count": index + 1} for index in range(263)]
    storage = _PagedTagStorage(rows, exact_total=263)
    kernel = _kernel_with_storage(storage)

    payload = await kernel._list_tags(  # noqa: SLF001
        actor=ActorContext(user_id="synthetic", preset_key="owner", source="test")
    )

    assert payload["count"] == 40, "модели снова отдана неограниченная страница тегов"
    assert payload["total"] == 263, "длина страницы снова выдана за точное число тегов"
    assert payload["truncated"] is True
    assert storage.count_calls == 1, "точный независимый счётчик не был прочитан ровно один раз"


@pytest.mark.asyncio
async def test_k03_tag_page_and_total_are_read_from_one_database_snapshot() -> None:
    """Mutation: run the page and total SELECTs outside `_storage_read_snapshot`."""

    storage = _PagedTagStorage([{"tag": "проект", "count": 2}], exact_total=1)
    kernel = _kernel_with_storage(storage)

    payload = await kernel._list_tags(  # noqa: SLF001
        actor=ActorContext(user_id="synthetic", preset_key="owner", source="test")
    )

    assert payload["total"] == 1
    assert storage.list_snapshot_states == [True]
    assert storage.count_snapshot_states == [True]
    assert storage.conn.begin_calls == 1
    assert storage.conn.rollback_calls == 1
    assert storage.conn.in_transaction is False


@pytest.mark.asyncio
async def test_k03_a_noise_filtered_page_is_not_reported_as_an_archive_with_no_tags() -> None:
    """Mutation: treat an empty useful page as zero stored tags.

    A large corpus can legitimately filter ubiquitous carrier tags such as
    ``document`` and ``application``.  That is a filtered inventory, not proof
    that the archive contains no tags at all.
    """

    storage = _PagedTagStorage([], exact_total=2)
    kernel = _kernel_with_storage(storage)

    payload = await kernel._list_tags(  # noqa: SLF001
        actor=ActorContext(user_id="synthetic", preset_key="owner", source="test")
    )

    assert payload["tags"] == []
    assert payload["count"] == 0
    assert payload["total"] == 2, "отфильтрованная страница снова названа отсутствием тегов"
    assert payload["truncated"] is True


class _TagKernel:
    def __init__(self, result: ToolResult | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, name: str, arguments: dict[str, Any], *, actor: Any) -> ToolResult:
        del actor
        self.calls.append((name, dict(arguments)))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _RemainderLLM:
    enabled = True
    total_budget_sec = 10.0

    def __init__(self, remainder: str = "") -> None:
        self.remainder = remainder
        self.calls: list[str] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, str]:
        del kwargs
        system = str(messages[0].get("content") or "")
        self.calls.append(system)
        if "Часть просьбы человека уже решена" in system:
            return {"content": json.dumps({"остаток": self.remainder}, ensure_ascii=False)}
        raise AssertionError("a code-owned tag inventory reached generative speech")


def _tag_runtime(kernel: _TagKernel, llm: _RemainderLLM) -> AgentRuntime:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = kernel
    runtime.llm = llm
    return runtime


async def _prefetch_tags(
    question: str,
    result: ToolResult | BaseException,
    *,
    remainder: str = "",
) -> tuple[AgentContext, _TagKernel, list[dict[str, Any]], list[str], list[dict[str, str]]]:
    kernel = _TagKernel(result)
    runtime = _tag_runtime(kernel, _RemainderLLM(remainder))
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )
    tools = [_tool("list_tags"), _tool("memory_search")]
    messages: list[dict[str, Any]] = []
    tools_used: list[str] = []
    evidence: list[dict[str, str]] = []

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        question,
        ActorContext(user_id="synthetic", preset_key="owner", source="test"),
        tools,
        messages,
        tools_used,
        evidence,
        context,
    )
    return context, kernel, tools, tools_used, evidence


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Какие у меня теги и сколько записей у каждого?", id="which-tags-and-counts"),
        pytest.param("Покажи список тегов моей базы.", id="show-tag-list"),
        pytest.param("Перечисли доступные теги в архиве.", id="enumerate-available-tags"),
    ],
)
@pytest.mark.asyncio
async def test_k03_a_natural_tag_inventory_is_settled_by_one_code_owned_call(question: str) -> None:
    """Mutations: miss a natural wording, leave ``list_tags`` model-callable,
    or replace the structural answer with a system hint for the model.
    """

    result = ToolResult(
        "list_tags",
        True,
        {
            "tags": [
                {"tag": "проект", "count": 7},
                {"tag": "смета", "count": 3},
                {"tag": "поверка", "count": 2},
            ],
            "count": 3,
            "total": 3,
            "truncated": False,
        },
    )

    context, kernel, tools, tools_used, evidence = await _prefetch_tags(question, result)

    assert kernel.calls == [("list_tags", {})], "инвентарь тегов прочитан не ровно один раз"
    assert tools_used == ["list_tags"]
    assert len(evidence) == 1 and evidence[0]["tool"] == "list_tags"
    assert all(tool["function"]["name"] != "list_tags" for tool in tools), (
        "после точного чтения модель всё ещё может повторно вызвать list_tags"
    )
    answer = context.structural_answer.casefold()
    for tag, count in (("проект", 7), ("смета", 3), ("поверка", 2)):
        assert tag in answer and re.search(rf"{tag}[^\n\d]{{0,12}}{count}\b", answer), (
            f"кодовый ответ потерял тег {tag} или его точный счётчик"
        )
    assert context.remainder_known is True
    assert context.open_remainder == ""


@pytest.mark.asyncio
async def test_k03_a_compound_tag_request_keeps_only_its_unrelated_remainder_open() -> None:
    """Mutation: settle the whole compound turn, or return the tag clause to the model."""

    result = ToolResult(
        "list_tags",
        True,
        {
            "tags": [{"tag": "поверка", "count": 4}],
            "count": 1,
            "total": 1,
            "truncated": False,
        },
    )
    remainder = "объясни термин объект знаний"

    context, kernel, tools, _, _ = await _prefetch_tags(
        "Какие теги есть в базе? И объясни термин «объект знаний».",
        result,
        remainder=remainder,
    )

    assert kernel.calls == [("list_tags", {})]
    assert all(tool["function"]["name"] != "list_tags" for tool in tools)
    assert "поверка" in context.structural_answer.casefold()
    assert context.remainder_known is True
    assert context.open_remainder == remainder


@pytest.mark.parametrize(
    ("result", "must_contain", "must_not_claim_empty"),
    [
        pytest.param(
            ToolResult(
                "list_tags",
                True,
                {"tags": [], "count": 0, "total": 0, "truncated": False},
            ),
            "нет",
            False,
            id="genuinely-empty",
        ),
        pytest.param(
            ToolResult(
                "list_tags",
                True,
                {"tags": [], "count": 0, "total": 2, "truncated": True},
            ),
            "2",
            True,
            id="noise-filtered",
        ),
        pytest.param(
            ToolResult("list_tags", False, error="synthetic unavailable"),
            "не удалось",
            True,
            id="tool-failure",
        ),
        pytest.param(
            RuntimeError("synthetic unavailable"),
            "не удалось",
            True,
            id="tool-exception",
        ),
    ],
)
@pytest.mark.asyncio
async def test_k03_empty_filtered_and_failed_tag_reads_have_distinct_code_owned_answers(
    result: ToolResult | BaseException,
    must_contain: str,
    must_not_claim_empty: bool,
) -> None:
    """Mutations: turn a failed/filtered read into the same answer as true emptiness."""

    context, kernel, tools, _, _ = await _prefetch_tags("Какие теги есть в базе?", result)

    assert kernel.calls == [("list_tags", {})]
    assert all(tool["function"]["name"] != "list_tags" for tool in tools)
    answer = context.structural_answer.casefold()
    assert must_contain in answer
    if must_not_claim_empty:
        assert not re.search(r"(?:тегов\s+(?:нет|не\s+найдено)|баз\w*\s+пуст)", answer)
    assert context.remainder_known is True
    assert context.open_remainder == ""


@pytest.mark.asyncio
async def test_k03_an_unavailable_tag_capability_is_not_reported_as_used() -> None:
    """Mutation: append ``list_tags`` to audit output without authorization or a call."""

    kernel = _TagKernel(AssertionError("unavailable capability was executed"))
    runtime = _tag_runtime(kernel, _RemainderLLM())
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )
    tools = [_tool("memory_search")]
    messages: list[dict[str, Any]] = []
    tools_used: list[str] = []
    evidence: list[dict[str, str]] = []

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        "Какие теги есть в базе?",
        ActorContext(user_id="synthetic", preset_key="owner", source="test"),
        tools,
        messages,
        tools_used,
        evidence,
        context,
    )

    assert kernel.calls == []
    assert tools_used == []
    assert evidence == []
    assert "не удалось" in context.structural_answer.casefold()
    assert context.remainder_known is True
    assert context.open_remainder == ""


def test_k12_system_prompt_agrees_with_the_confirmed_telegram_markdown_renderer() -> None:
    """Mutation: restore the old claim that Telegram shows Markdown as raw punctuation."""

    prompt = SYSTEM_PROMPT.casefold()
    false_contracts = (
        "мессенджер без разметки",
        "не используй **",
        "markdown-символы приходят к человеку сырыми знаками",
    )
    assert not any(claim in prompt for claim in false_contracts), (
        "system prompt всё ещё запрещает разметку, которую Telegram уже отображает"
    )

    format_contract = " ".join(
        line.strip()
        for line in SYSTEM_PROMPT.splitlines()
        if "размет" in line.casefold() or "markdown" in line.casefold()
    ).casefold()
    assert re.search(r"telegram|телеграм", format_contract)
    assert re.search(r"поддерж|преобраз|отображ|рендер", format_contract)
    for marker in ("**", "`", "[текст](https://example.invalid)", "| столбец |"):
        assert marker.casefold() in format_contract, f"контракт не описывает поддерживаемую форму {marker}"


def test_k12_the_confirmed_renderer_formats_markdown_without_trusting_raw_html_or_unsafe_links() -> None:
    """Mutations: bypass escaping, or turn an arbitrary Markdown URL into Telegram HTML."""

    rendered = to_telegram_html(
        "**<script>alert(1)</script>** "
        "[опасно](javascript:alert(2)) "
        "[источник](https://example.invalid/path?a=1&b=2)"
    )

    assert "<b>&lt;script&gt;alert(1)&lt;/script&gt;</b>" in rendered
    assert "<script>" not in rendered
    assert 'href="javascript:' not in rendered
    assert "[опасно](javascript:alert(2))" in rendered
    assert '<a href="https://example.invalid/path?a=1&amp;b=2">источник</a>' in rendered


def test_k12_markdown_inside_a_safe_url_never_becomes_html_inside_href() -> None:
    """Mutation: expose an already-built href to the later inline-format regexes."""

    rendered = to_telegram_html("[**источник**](https://example.invalid/**segment**?q=_value_&gone=~~old~~)")

    assert rendered == (
        '<a href="https://example.invalid/**segment**?q=_value_&amp;gone=~~old~~"><b>источник</b></a>'
    )
    href = rendered.split('href="', 1)[1].split('"', 1)[0]
    assert all(marker not in href for marker in ("<b>", "<i>", "<s>"))


def test_k12_inline_code_inside_a_url_fails_closed_before_placeholder_restore() -> None:
    """A stashed code span must never expand into markup inside ``href``."""

    source = '[`источник`](https://example.invalid/`"><b>segment</b>`_(a))'
    rendered = to_telegram_html(source)

    assert rendered == (
        "[<code>источник</code>]("
        "https://example.invalid/<code>&quot;&gt;&lt;b&gt;segment&lt;/b&gt;</code>_(a))"
    )
    assert '<a href="' not in rendered
    assert "\x00" not in rendered


def test_k12_inline_code_in_label_keeps_balanced_destination_clickable() -> None:
    """The URL guard must not disable label code or ordinary balanced links."""

    rendered = to_telegram_html("[`код`](https://example.invalid/a_(b))")

    assert rendered == '<a href="https://example.invalid/a_(b)"><code>код</code></a>'


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "[wiki](https://example.invalid/a_(b))",
            '<a href="https://example.invalid/a_(b)">wiki</a>',
        ),
        (
            "[поиск](https://example.invalid/search?q=a(b)c)",
            '<a href="https://example.invalid/search?q=a(b)c">поиск</a>',
        ),
        (
            "[вложенная](https://example.invalid/a_(b(c)))",
            '<a href="https://example.invalid/a_(b(c))">вложенная</a>',
        ),
    ],
)
def test_k12_balanced_parentheses_are_part_of_the_link_destination(
    source: str,
    expected: str,
) -> None:
    """A valid parenthesised URL must never become a plausible wrong link."""

    assert to_telegram_html(source) == expected


def test_k12_an_unbalanced_link_destination_fails_closed_as_literal_text() -> None:
    source = "[wiki](https://example.invalid/a_(b)"

    assert to_telegram_html(source) == source
