"""Behavioural acceptance for the frozen Package B routing corpus.

The sibling holdout test freezes the synthetic questions and their labels.  This
module exercises those same labels through the real prefetch boundaries while
replacing only the semantic arbiter and execution kernel with deterministic
test doubles.  Calendar boundaries and structural answers remain production
code: an arbiter may choose a closed enum, but it never supplies a date or a
count.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import friday.agent_runtime as agent_runtime_module
import friday.execution_kernel as execution_kernel_module
from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _archive_count_projection,
    _classification_text,
    _fast_archive_count_intent,
    _temporal_payload_is_coherent,
)
from friday.execution_kernel import ExecutionKernel, ToolResult
from friday.permissions import ActorContext
from friday.storage._graph import _bounded_visible_timeline_event_rows, _count_visible_timeline_events
from friday.storage.models import Entity, EntityType
from friday.time_routing import TimeIntent, TimeWindow, build_time_window, fast_time_intent

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "package_b_routing_holdout.json"
FIXED_TODAY = date(2026, 8, 8)
FIXED_LOCAL_NOW = "2026-08-08T10:00:00"


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


FIXTURE = _fixture()

TIME_WINDOWS = {
    "k14_past_001": ("2025-03-14", "2025-03-14"),
    "k14_past_002": ("2024-04-07T16:00:00", "2024-04-07T16:59:59"),
    "k14_past_003": ("2026-08-07", "2026-08-07"),
    "k14_past_004": ("2026-08-06", "2026-08-06"),
    "k14_past_005": ("2026-08-05", "2026-08-05"),
    "k14_past_006": ("2026-08-06", "2026-08-08"),
    "k14_past_007": ("2026-07-30", "2026-08-08"),
    "k14_past_008": ("2026-07-27", "2026-08-02"),
    "k14_past_009": ("2026-08-03", "2026-08-08"),
    "k14_past_010": ("2026-07-01", "2026-07-31"),
    "k14_past_011": ("2026-08-01", "2026-08-08"),
    "k14_past_012": ("2025-05-04", "2025-05-09"),
    "k14_past_013": ("2025-06-01", "2025-06-05"),
    "k14_past_014": ("2025-02-01", "2025-02-28"),
    "k14_past_015": ("2026-08-08", "2026-08-08"),
    "k14_future_001": ("2026-08-08", "2026-08-08"),
    "k14_future_002": ("2026-08-08", "2026-08-08"),
    "k14_future_003": ("2026-08-09", "2026-08-09"),
    "k14_future_004": ("2026-08-10", "2026-08-10"),
    "k14_future_005": ("2026-08-08", "2026-08-10"),
    "k14_future_006": ("2026-08-08", "2026-08-14"),
    "k14_future_007": ("2026-08-10", "2026-08-16"),
    "k14_future_008": ("2026-08-08", "2026-08-09"),
    "k14_future_009": ("2026-09-01", "2026-09-30"),
    "k14_future_010": ("2031-09-10", "2031-09-15"),
}

STAT_VALUES = {
    "knowledge_object_count": 101,
    "raw_object_count": 202,
    "file_count": 303,
    "entity_count": 404,
    "relation_count": 505,
}
STAT_ANSWERS = {
    "knowledge_objects": "В личном архиве: Объектов знаний — 101.",
    "raw_objects": "В личном архиве: Исходных материалов — 202.",
    "files": "В личном архиве: Файлов — 303.",
    "entities": "В личном архиве: Сущностей — 404.",
    "relations": "В личном архиве: Связей — 505.",
    "all_stats": (
        "В личном архиве: Объектов знаний — 101; Исходных материалов — 202; "
        "Файлов — 303; Сущностей — 404; Связей — 505."
    ),
}


def _cases(name: str) -> list[Any]:
    return [pytest.param(case, id=case["id"]) for case in FIXTURE[name]]


def _tool(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def _tool_names(tools: list[dict[str, Any]]) -> list[str]:
    return [str((tool.get("function") or {}).get("name") or tool.get("name") or "") for tool in tools]


class _Kernel:
    def __init__(self, *, mode: str = "success") -> None:
        self.mode = mode
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, tool: str, params: dict[str, Any], actor: Any = None) -> ToolResult:  # noqa: ARG002
        self.calls.append((tool, dict(params)))
        if self.mode == "raise":
            raise RuntimeError("synthetic kernel failure")
        if self.mode == "failed":
            return ToolResult(tool, False, error="synthetic failure")
        if tool == "kg_stats":
            data: dict[str, Any] = dict(STAT_VALUES)
            if self.mode == "malformed":
                data["knowledge_object_count"] = "101"
            return ToolResult(tool, True, data)
        if tool in {"what_happened", "upcoming"}:
            if not params.get("since") or not params.get("until"):
                return ToolResult(tool, False, error="synthetic unscoped temporal call")
            asked_about = {
                "since": params.get("since"),
                "until": params.get("until"),
            }
            if self.mode == "wrong_echo":
                asked_about["since"] = "1900-01-01"
            data: dict[str, Any] = {
                "understood": True,
                "asked_about": asked_about,
                "shown": 0,
                "total": 0,
            }
            if self.mode == "missing_page":
                return ToolResult(tool, True, data)
            if tool == "what_happened":
                data.update(
                    {
                        "events": [],
                        "total": {"messages": 0, "documents": 0, "total": 0},
                        "coverage": {
                            "complete": True,
                            "strategy": "complete",
                            "includes_latest": True,
                        },
                    }
                )
            else:
                data["items"] = []
                data["days"] = (
                    date.fromisoformat(str(params["until"])[:10])
                    - date.fromisoformat(str(params["since"])[:10])
                ).days + 1
                data["note"] = "В синтетическом интервале ничего не запланировано."
            if self.mode == "inconsistent_page":
                data["shown"] = 1
            return ToolResult(tool, True, data)
        if tool == "list_tags":
            return ToolResult(
                tool,
                True,
                {
                    "tags": [{"tag": "synthetic", "count": 1}],
                    "count": 1,
                    "total": 1,
                    "truncated": False,
                },
            )
        if tool == "memory_search":
            return ToolResult(tool, True, {"results": [], "shown": 0, "total": 0})
        raise AssertionError(f"unexpected synthetic tool call: {tool}")


class _OracleLLM:
    enabled = True
    total_budget_sec = 120.0
    model = "package-b-oracle"

    def __init__(
        self,
        case: dict[str, Any] | None = None,
        *,
        hostile_time: bool = False,
        hostile_count: bool = False,
        malformed_remainder: bool = False,
        remainder: str = "",
        fail_classifier: bool = False,
    ) -> None:
        self.case = case
        self.hostile_time = hostile_time
        self.hostile_count = hostile_count
        self.malformed_remainder = malformed_remainder
        self.remainder = remainder
        self.fail_classifier = fail_classifier
        self.calls: list[str] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, str]:  # noqa: ARG002
        system = str(messages[0].get("content") or "")
        self.calls.append(system)
        if "Часть просьбы человека уже решена" in system:
            if self.malformed_remainder:
                return {"content": "not-json"}
            return {"content": json.dumps({"остаток": self.remainder}, ensure_ascii=False)}
        if self.fail_classifier:
            raise RuntimeError("synthetic classifier failure")
        if '"direction":"past|future|none"' in system:
            if self.hostile_time:
                answer = {"direction": "past", "window_kind": "single_day"}
            else:
                assert self.case is not None
                expected = self.case["expected"]
                answer = {
                    "direction": expected["time_direction"],
                    "window_kind": expected["time_window_kind"],
                }
            # Dates returned by a compromised classifier are ignored: only the
            # two closed labels above are part of its authority.
            answer.update({"since": "1900-01-01", "until": "2999-12-31"})
            return {"content": json.dumps(answer)}
        if '"scope":"whole_archive|local_selection|none"' in system:
            if self.hostile_count:
                answer = {"scope": "whole_archive", "metric": "all_stats"}
            else:
                assert self.case is not None
                expected = self.case["expected"]
                answer = {
                    "scope": expected["archive_count_scope"],
                    "metric": expected["count_metric"],
                }
            answer["count"] = 999_999
            return {"content": json.dumps(answer)}
        raise AssertionError(f"unexpected arbiter prompt: {system[:80]}")


class _HostileAgentLoopLLM(_OracleLLM):
    """Obey closed arbiters, then hallucinate calls absent from offered schemas."""

    def __init__(
        self,
        case: dict[str, Any] | None,
        attack_calls: list[tuple[str, dict[str, Any]]],
        *,
        remainder: str = "",
    ) -> None:
        super().__init__(case, remainder=remainder)
        self.attack_calls = attack_calls
        self.attacked = False
        self.main_tools: list[list[str]] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        system = str(messages[0].get("content") or "")
        if (
            "Часть просьбы человека уже решена" in system
            or '"direction":"past|future|none"' in system
            or '"scope":"whole_archive|local_selection|none"' in system
        ):
            return await super().chat(messages, **kwargs)

        offered = kwargs.get("tools") or []
        self.main_tools.append(_tool_names(list(offered)))
        if not self.attacked:
            self.attacked = True
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"hostile-{index}",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                    for index, (name, arguments) in enumerate(self.attack_calls)
                ],
                "_queue_wait_sec": 0.0,
            }
        return {"content": "Синтетический остаток обработан.", "_queue_wait_sec": 0.0}


class _MorningDatetime(datetime):
    """A stable local morning for same-day future-hour boundary tests."""

    @classmethod
    def now(cls, tz: Any = None) -> _MorningDatetime:
        local = cls(2026, 8, 8, 10, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        return local.replace(tzinfo=None) if tz is None else local.astimezone(tz)


def _runtime(kernel: _Kernel, llm: _OracleLLM) -> AgentRuntime:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = kernel
    runtime.llm = llm
    runtime._local_today = lambda: FIXED_TODAY  # type: ignore[method-assign]  # noqa: SLF001
    runtime._local_now = lambda: _MorningDatetime.now()  # type: ignore[method-assign]  # noqa: SLF001
    return runtime


def _time_arguments(case: dict[str, Any]) -> dict[str, Any]:
    since, until = TIME_WINDOWS[case["id"]]
    if case["expected"]["time_direction"] == "past":
        if len(since) == 10:
            since += "T00:00:00"
        if len(until) == 10:
            until += "T23:59:59"
        if until[:10] == FIXED_TODAY.isoformat() and until > FIXED_LOCAL_NOW:
            until = FIXED_LOCAL_NOW
        return {"since": since, "until": until, "limit": 40}
    return {"since": since, "until": until}


def test_a_day_duration_is_not_misread_as_an_afternoon_clock() -> None:
    question = "Какие планы на три дня?"
    intent = fast_time_intent(question)

    assert intent is not None
    assert (intent.direction, intent.window_kind) == ("future", "rolling_days")
    assert build_time_window(question, intent, today=FIXED_TODAY) == TimeWindow("2026-08-08", "2026-08-10")


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Что было 25 декабря?", TimeWindow("2025-12-25", "2025-12-25")),
        ("Что запланировано на 10 января?", TimeWindow("2027-01-10", "2027-01-10")),
        ("Что было в июле?", TimeWindow("2026-07-01", "2026-07-31")),
        ("Какие планы на сентябрь?", TimeWindow("2026-09-01", "2026-09-30")),
        ("Что было с 29 декабря по 2 января?", TimeWindow("2025-12-29", "2026-01-02")),
        (
            "Что запланировано с 29 декабря по 2 января?",
            TimeWindow("2026-12-29", "2027-01-02"),
        ),
    ],
)
def test_yearless_calendar_words_are_anchored_by_direction(
    question: str,
    expected: TimeWindow,
) -> None:
    intent = fast_time_intent(question)

    assert intent is not None
    assert build_time_window(question, intent, today=FIXED_TODAY) == expected


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        pytest.param(
            TimeIntent("past", "calendar_month"),
            TimeWindow("2026-08-01", "2026-08-08"),
            id="past-clips-at-today",
        ),
        pytest.param(
            TimeIntent("future", "calendar_month"),
            TimeWindow("2026-08-08", "2026-08-31"),
            id="future-starts-today",
        ),
    ],
)
def test_an_explicit_current_month_and_year_is_clipped_by_time_direction(
    intent: TimeIntent,
    expected: TimeWindow,
) -> None:
    assert build_time_window("август 2026 года", intent, today=FIXED_TODAY) == expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        pytest.param(
            "Что запланировано через две недели?",
            TimeWindow("2026-08-22", "2026-08-22"),
            id="two-weeks-ahead",
        ),
        pytest.param(
            "Что запланировано через месяц?",
            TimeWindow("2026-09-08", "2026-09-08"),
            id="one-month-ahead",
        ),
        pytest.param(
            "Что происходило две недели назад?",
            TimeWindow("2026-07-25", "2026-07-25"),
            id="two-weeks-back",
        ),
        pytest.param(
            "Что происходило месяц назад?",
            TimeWindow("2026-07-08", "2026-07-08"),
            id="one-month-back",
        ),
    ],
)
def test_relative_week_and_month_offsets_resolve_to_the_exact_shifted_day(
    question: str,
    expected: TimeWindow,
) -> None:
    intent = fast_time_intent(question)

    assert intent is not None
    assert intent.window_kind == "single_day"
    assert build_time_window(question, intent, today=FIXED_TODAY) == expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        pytest.param(
            "Что происходило двадцать один день назад?",
            TimeWindow("2026-07-18", "2026-07-18"),
            id="twenty-one-singular-day",
        ),
        pytest.param(
            "Что происходило двадцать два дня назад?",
            TimeWindow("2026-07-17", "2026-07-17"),
            id="twenty-two-days",
        ),
        pytest.param(
            "Что происходило тридцать пять дней назад?",
            TimeWindow("2026-07-04", "2026-07-04"),
            id="thirty-five-days",
        ),
    ],
)
def test_compound_russian_day_numbers_are_never_silently_truncated(
    question: str,
    expected: TimeWindow,
) -> None:
    intent = fast_time_intent(question)

    assert intent == TimeIntent("past", "single_day")
    assert build_time_window(question, intent, today=FIXED_TODAY) == expected


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Какие планы на прошлую неделю?", id="future-over-past-week"),
        pytest.param("Что происходило на следующей неделе?", id="past-over-next-week"),
        pytest.param("Какие планы на последние три дня?", id="future-over-last-days"),
        pytest.param("Что происходило в ближайшие три дня?", id="past-over-nearest-days"),
    ],
)
@pytest.mark.asyncio
async def test_a_direction_that_contradicts_the_persons_time_anchor_fails_closed(
    question: str,
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM())
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert "Не удалось однозначно вычислить" in context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))
    assert "memory_search" in _tool_names(tools)


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Что было 31 февраля 2026 года?", id="february-31"),
        pytest.param("Что было 29 февраля 2026 года?", id="non-leap-february-29"),
        pytest.param("Что было 31 апреля 2026 года?", id="april-31"),
    ],
)
@pytest.mark.asyncio
async def test_an_invalid_calendar_date_fails_closed_before_the_kernel(
    question: str,
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_time=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]
    messages: list[dict[str, Any]] = []

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, messages, [], [], context
    )

    assert kernel.calls == []
    assert "Не удалось однозначно вычислить" in context.structural_answer
    assert "пуст" not in context.structural_answer.casefold()
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))
    assert "memory_search" in _tool_names(tools)
    assert messages and "не называй календарь пустым" in messages[-1]["content"].casefold()


@pytest.mark.parametrize(
    "question",
    [
        "Сколько файлов в архиве проекта Кобальт?",
        "Сколько документов в архиве за июль?",
        "Сколько записей в базе с тегом лазурь?",
        "Сколько материалов в архиве получено вчера?",
        "Сколько всего файлов в архиве за прошлую неделю?",
        "Сколько всего документов в архиве за 2025 год?",
        "Сколько всего документов в архиве от Иванова?",
        "Сколько всего файлов в папке Альфа?",
        "Сколько всего файлов в архиве формата PDF?",
        "Сколько всего удалённых файлов в архиве?",
        "Сколько всего файлов в архиве больше 10 МБ?",
    ],
)
def test_an_explicit_archive_word_does_not_turn_a_filtered_subset_into_a_global_count(
    question: str,
) -> None:
    assert _fast_archive_count_intent(question) is None


@pytest.mark.parametrize(
    ("question", "metric"),
    [
        ("Сколько всего объектов знаний в этой базе?", "knowledge_objects"),
        ("Каков текущий размер базы?", "all_stats"),
        ("Назови общее число файлов в текущем архиве.", "files"),
        ("Сколько всего в архиве файлов?", "files"),
        ("Сколько в архиве файлов всего?", "files"),
    ],
)
@pytest.mark.asyncio
async def test_a_deictic_archive_still_means_the_whole_archive(
    question: str,
    metric: str,
) -> None:
    runtime = _runtime(_Kernel(), _OracleLLM(FIXTURE["global_count_positives"][0]))

    intent = await runtime._archive_count_intent_by_arbiter(question)  # noqa: SLF001

    assert intent is not None
    assert (intent.scope, intent.metric) == ("whole_archive", metric)


@pytest.mark.parametrize("case", _cases("time_positives"))
@pytest.mark.asyncio
async def test_all_frozen_time_questions_force_one_exact_bounded_tool_call(case: dict[str, Any]) -> None:
    kernel = _Kernel()
    llm = _OracleLLM(case)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]
    messages: list[dict[str, Any]] = []
    tools_used: list[str] = []
    evidence: list[dict[str, str]] = []

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        case["question"],
        None,
        tools,
        messages,
        tools_used,
        evidence,
        context,
    )

    expected_tool = case["expected"]["required_tool"]
    assert kernel.calls == [(expected_tool, _time_arguments(case))]
    assert tools_used == [expected_tool]
    assert [item["tool"] for item in evidence] == [expected_tool]
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))
    assert len(messages) == 1
    assert "1900-01-01" not in messages[0]["content"]
    assert "2999-12-31" not in messages[0]["content"]


@pytest.mark.parametrize(
    ("question", "window_kind", "expected_since"),
    [
        pytest.param("Что было на этой неделе?", "calendar_week", "2026-08-03T00:00:00", id="this-week"),
        pytest.param(
            "Что происходило в текущем месяце?",
            "calendar_month",
            "2026-08-01T00:00:00",
            id="current-month",
        ),
        pytest.param(
            "Что было с 4 по 8 августа 2026 года?",
            "explicit_range",
            "2026-08-04T00:00:00",
            id="explicit-range-ending-today",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_past_multiday_window_ending_today_stops_at_local_now(
    question: str,
    window_kind: str,
    expected_since: str,
) -> None:
    case = {
        "expected": {
            "time_direction": "past",
            "time_window_kind": window_kind,
        }
    }
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(case))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == [
        (
            "what_happened",
            {"since": expected_since, "until": FIXED_LOCAL_NOW, "limit": 40},
        )
    ]
    assert context.structural_answer == ""
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


_TIME_CONTROL_KINDS = {
    "external_freshness": "интернет",
    "reminder_action": "действие",
    "file_collection": "файл",
    "ordinary_archive_search": "знание",
    "material_intake": "материал",
    "structural_correction": "поправка",
    "general_conversation": "быт",
    "person_activity": "человек",
    "own_message_search": "знание",
    "general_reasoning": "быт",
}


@pytest.mark.parametrize("case", _cases("time_controls"))
@pytest.mark.asyncio
async def test_all_frozen_time_controls_obey_the_primary_verdict_even_with_a_hostile_arbiter(
    case: dict[str, Any],
) -> None:
    kernel = _Kernel()
    llm = _OracleLLM(case, hostile_time=True)
    runtime = _runtime(kernel, llm)
    kind = _TIME_CONTROL_KINDS[case["expected"]["route"]]
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=(kind, None))
    tools = [_tool("what_happened"), _tool("upcoming")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        case["question"], None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert llm.calls == [], "settled primary intent still reached an independent time arbiter"
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.parametrize(
    "question",
    [
        "Что происходило с проектом Альфа вчера?",
        "Какие события по проекту Альфа были вчера?",
        "Покажи ленту проекта Альфа за вчера.",
        "Что было с договором вчера?",
        "Какие события по заявке К-7 были вчера?",
        "Покажи ленту договора Альфа за вчера.",
    ],
)
@pytest.mark.asyncio
async def test_a_named_archive_subject_never_becomes_an_unfiltered_personal_day(
    question: str,
) -> None:
    kernel = _Kernel()
    llm = _OracleLLM(hostile_time=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert llm.calls == []
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Что происходило с Альфой вчера?", id="bare-project-name"),
        pytest.param("Что делал Иванов вчера?", id="person-surname"),
        pytest.param("Что происходило в отделе продаж вчера?", id="named-department"),
        pytest.param("Что происходило в офисе вчера?", id="named-place"),
    ],
)
@pytest.mark.asyncio
async def test_a_named_temporal_filter_cannot_leak_the_broad_personal_timeline(
    question: str,
) -> None:
    kernel = _Kernel()
    llm = _OracleLLM(hostile_time=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert llm.calls == []
    assert context.structural_answer == ""
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))
    assert "memory_search" in _tool_names(tools)


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(FIXTURE["time_positives"][2], id="unfiltered-yesterday"),
        pytest.param(FIXTURE["time_positives"][11], id="unfiltered-date-range"),
    ],
)
@pytest.mark.asyncio
async def test_expanding_local_subject_filters_does_not_block_a_true_personal_timeline(
    case: dict[str, Any],
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(case))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        case["question"], None, tools, [], [], [], context
    )

    assert kernel.calls == [("what_happened", _time_arguments(case))]
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.parametrize(
    "question",
    [
        "Какие документы появились вчера?",
        "Покажи документы, появившиеся вчера",
    ],
)
@pytest.mark.asyncio
async def test_a_broad_document_category_is_a_timeline_not_a_named_subject_filter(
    question: str,
) -> None:
    case = FIXTURE["time_positives"][2]
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(case))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == [("what_happened", _time_arguments(case))]
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.parametrize("case", _cases("global_count_positives"))
@pytest.mark.asyncio
async def test_all_frozen_global_counts_publish_only_the_requested_exact_aggregate(
    case: dict[str, Any],
) -> None:
    kernel = _Kernel()
    llm = _OracleLLM(case)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("kg_stats"), _tool("memory_search")]
    messages: list[dict[str, Any]] = []
    tools_used: list[str] = []
    evidence: list[dict[str, str]] = []

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        case["question"],
        None,
        tools,
        messages,
        tools_used,
        evidence,
        context,
    )

    metric = case["expected"]["count_metric"]
    assert kernel.calls == [("kg_stats", {})]
    assert tools_used == ["kg_stats"]
    assert [item["tool"] for item in evidence] == ["kg_stats"]
    assert "kg_stats" not in _tool_names(tools)
    assert context.structural_answer == STAT_ANSWERS[metric]
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert len(messages) == 1
    assert "999999" not in messages[0]["content"]


@pytest.mark.parametrize(
    ("question", "metric"),
    [
        pytest.param("Сколько всего документов в моей базе?", "knowledge_objects", id="documents"),
        pytest.param("Сколько всего материалов в моём хранилище?", "raw_objects", id="materials"),
        pytest.param("Сколько всего рёбер в моём графе?", "relations", id="graph-edges"),
        pytest.param("Сколько всего вершин в моём графе?", "entities", id="graph-vertices"),
        pytest.param("Сколько всего исходников в моём архиве?", "raw_objects", id="raw-sources"),
        pytest.param("Сколько всего вложений в моём архиве?", "files", id="attachments"),
    ],
)
@pytest.mark.asyncio
async def test_whole_archive_metric_synonyms_select_the_exact_named_aggregate(
    question: str,
    metric: str,
) -> None:
    case = {
        "expected": {
            "archive_count_scope": "whole_archive",
            "count_metric": metric,
        }
    }
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(case))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("kg_stats")]

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == [("kg_stats", {})]
    assert context.structural_answer == STAT_ANSWERS[metric]
    assert "kg_stats" not in _tool_names(tools)


@pytest.mark.asyncio
async def test_an_unknown_named_archive_metric_fails_closed_without_guessing_a_known_total() -> None:
    question = "Сколько всего квантелей в моём архиве?"
    hostile_case = {
        "expected": {
            "archive_count_scope": "whole_archive",
            "count_metric": "knowledge_objects",
        }
    }
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_case))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("kg_stats")]

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert "kg_stats" not in _tool_names(tools)
    assert context.remainder_known is True
    assert re.search(r"не удалось|неизвест", context.structural_answer, re.IGNORECASE)
    assert not re.search(r"\b\d+\b", context.structural_answer)


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Сколько всего файлов и квантелей в моём архиве?", id="files-and-unknown"),
        pytest.param("Назови общее число сущностей и шмурдиков в графе.", id="entities-and-unknown"),
    ],
)
@pytest.mark.asyncio
async def test_a_known_metric_mixed_with_an_unknown_one_fails_closed_as_a_whole(
    question: str,
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_count=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("kg_stats"), _tool("memory_search")]

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert "kg_stats" not in _tool_names(tools)
    assert "memory_search" in _tool_names(tools)
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert re.search(r"не удалось|неизвест", context.structural_answer, re.IGNORECASE)
    assert not re.search(r"\b(?:101|202|303|404|505)\b", context.structural_answer)


_COUNT_CONTROL_KINDS = {
    "attachment_exact": "файл",
    "archive_filtered_search": "архив",
    "forced_time_prefetch": "архив",
    "own_message_search": "знание",
    "general_reasoning": "быт",
    "external_freshness": "интернет",
    "person_activity": "человек",
}


@pytest.mark.parametrize("case", _cases("local_count_controls"))
@pytest.mark.asyncio
async def test_all_frozen_local_counts_never_substitute_a_whole_archive_stat(case: dict[str, Any]) -> None:
    kernel = _Kernel()
    llm = _OracleLLM(case)
    runtime = _runtime(kernel, llm)
    kind = _COUNT_CONTROL_KINDS[case["expected"]["route"]]
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=(kind, None))
    tools = [_tool("kg_stats"), _tool("memory_search")]
    tools_used: list[str] = []

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        case["question"], None, tools, [], tools_used, [], context
    )

    assert kernel.calls == []
    assert tools_used == []
    assert "kg_stats" not in _tool_names(tools)
    assert context.structural_answer == ""


@pytest.mark.parametrize("mode", ["failed", "raise", "malformed"])
@pytest.mark.asyncio
async def test_a_failed_or_malformed_stats_read_is_an_explicit_failure_never_zero(mode: str) -> None:
    case = FIXTURE["global_count_positives"][0]
    kernel = _Kernel(mode=mode)
    llm = _OracleLLM(case, malformed_remainder=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("kg_stats")]

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        case["question"], None, tools, [], [], [], context
    )

    assert kernel.calls == [("kg_stats", {})]
    assert "Не удалось получить точные счётчики" in context.structural_answer
    assert "не удалось надёжно отделить" in context.structural_answer
    assert not re.search(r"Объектов знаний\s*[—:-]\s*0\b", context.structural_answer)
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert "kg_stats" not in _tool_names(tools)


@pytest.mark.parametrize(
    "mode",
    ["failed", "raise", "wrong_echo", "missing_page", "inconsistent_page"],
)
@pytest.mark.asyncio
async def test_a_failed_or_wrongly_scoped_time_read_is_not_reported_as_an_empty_window(mode: str) -> None:
    case = FIXTURE["time_positives"][2]
    kernel = _Kernel(mode=mode)
    llm = _OracleLLM(case, malformed_remainder=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]
    messages: list[dict[str, Any]] = []

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        case["question"], None, tools, messages, [], [], context
    )

    assert len(kernel.calls) == 1
    assert "Проверить личную ленту" in context.structural_answer
    assert "не удалось надёжно отделить" in context.structural_answer
    assert "событий нет" not in context.structural_answer.casefold()
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))
    assert "не подставляй ноль" in messages[-1]["content"].casefold()


@pytest.mark.asyncio
async def test_a_missing_stats_tool_is_an_explicit_failure_never_a_zero() -> None:
    case = FIXTURE["global_count_positives"][0]
    runtime = _runtime(_Kernel(), _OracleLLM(case, malformed_remainder=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        case["question"], None, [_tool("memory_search")], [], [], [], context
    )

    assert "Не удалось получить точные счётчики" in context.structural_answer
    assert not re.search(r"\b0\b", context.structural_answer)
    assert context.remainder_known is True
    assert context.open_remainder == ""


@pytest.mark.asyncio
async def test_a_missing_selected_time_tool_is_failure_and_the_opposite_tool_is_also_closed() -> None:
    case = FIXTURE["time_positives"][2]
    kernel = _Kernel()
    llm = _OracleLLM(case, malformed_remainder=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("upcoming"), _tool("memory_search")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        case["question"], None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert "Проверить личную ленту" in context.structural_answer
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))
    assert context.remainder_known is True


@pytest.mark.asyncio
async def test_an_uncomputable_time_clause_closes_both_tools_before_preserving_its_tail() -> None:
    message = "Что происходило когда-нибудь, и коротко объясни термин."
    kernel = _Kernel()
    llm = _OracleLLM(remainder="и коротко объясни термин")
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        message, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert "Не удалось однозначно вычислить" in context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == "и коротко объясни термин"
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.asyncio
async def test_a_compound_archive_count_preserves_only_the_unsettled_remainder() -> None:
    case = FIXTURE["global_count_positives"][0]
    kernel = _Kernel()
    llm = _OracleLLM(case, remainder="и коротко объясни, что считается объектом знаний")
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        case["question"] + " И коротко объясни, что считается объектом знаний.",
        None,
        [_tool("kg_stats")],
        [],
        [],
        [],
        context,
    )

    assert context.structural_answer == STAT_ANSWERS["knowledge_objects"]
    assert context.remainder_known is True
    assert context.open_remainder == "и коротко объясни, что считается объектом знаний"


@pytest.mark.parametrize(
    ("message", "remainder", "metric", "excluded_marker"),
    [
        pytest.param(
            "Сколько всего файлов в моём архиве, а также объясни, что считается вложением.",
            "объясни, что считается вложением",
            "files",
            "объясни",
            id="and-also",
        ),
        pytest.param(
            "Сколько всего сущностей в графе плюс покажи их типы.",
            "покажи их типы",
            "entities",
            "покажи",
            id="plus",
        ),
        pytest.param(
            "Сколько всего связей в графе и заодно перечисли типы связей.",
            "перечисли типы связей",
            "relations",
            "перечисли",
            id="and-at-the-same-time",
        ),
    ],
)
@pytest.mark.asyncio
async def test_compound_count_connectors_keep_an_exact_clause_boundary(
    message: str,
    remainder: str,
    metric: str,
    excluded_marker: str,
) -> None:
    projected = _archive_count_projection(message)
    case = {
        "expected": {
            "archive_count_scope": "whole_archive",
            "count_metric": metric,
        }
    }
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(case, remainder=remainder))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        message, None, [_tool("kg_stats")], [], [], [], context
    )

    assert excluded_marker not in projected.casefold()
    assert kernel.calls == [("kg_stats", {})]
    assert context.structural_answer == STAT_ANSWERS[metric]
    assert context.remainder_known is True
    assert context.open_remainder == remainder


@pytest.mark.parametrize(
    ("suffix", "remainder"),
    [
        ("", "Сколько всего объектов знаний хранится в моей базе?"),
        ("", "счётчик 0"),
        ("", "знаний в моей базе"),
        (" И назови число архива.", "назови число архива"),
    ],
)
@pytest.mark.asyncio
async def test_a_replayed_invented_or_still_settled_remainder_is_suppressed(
    suffix: str,
    remainder: str,
) -> None:
    case = FIXTURE["global_count_positives"][0]
    runtime = _runtime(_Kernel(), _OracleLLM(case, remainder=remainder))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        case["question"] + suffix,
        None,
        [_tool("kg_stats")],
        [],
        [],
        [],
        context,
    )

    assert "не удалось надёжно отделить" in context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == ""


@pytest.mark.parametrize(
    "question",
    [
        "Что было вчера и что запланировано завтра?",
        "Что было вчера и будет завтра?",
        "Покажи вчерашние события и завтрашние планы.",
        "Какие были события вчера и какие планы на завтра?",
        "Покажи ленту за вчера и календарь на завтра.",
        "Сравни, что было вчера с тем, что запланировано завтра.",
        "Покажи, что было вчера, затем что запланировано завтра.",
    ],
)
@pytest.mark.asyncio
async def test_mixed_past_and_future_is_split_fail_closed_without_either_tool(
    question: str,
) -> None:
    kernel = _Kernel()
    llm = _OracleLLM()
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]
    messages: list[dict[str, Any]] = []

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question,
        None,
        tools,
        messages,
        [],
        [],
        context,
    )

    expected = (
        "В одном запросе названы и прошлое, и будущее. "
        "Раздели его на два календарных вопроса, чтобы я не потеряла половину."
    )
    assert context.structural_answer == expected
    assert messages == [{"role": "system", "content": expected}]
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert kernel.calls == []
    assert llm.calls == []
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Что было вчера и позавчера?", id="two-past-relative-days"),
        pytest.param("Какие планы на завтра и послезавтра?", id="two-future-relative-days"),
        pytest.param("Что было 5 августа и 7 августа?", id="two-absolute-days"),
        pytest.param("Какие планы на понедельник и среду?", id="two-weekdays"),
        pytest.param("Что было вчера после 15:00?", id="past-after-clock"),
        pytest.param("Какие планы завтра до 18:00?", id="future-before-clock"),
    ],
)
@pytest.mark.asyncio
async def test_unrepresentable_same_direction_targets_fail_closed_before_classification(
    question: str,
) -> None:
    kernel = _Kernel()
    llm = _OracleLLM(hostile_time=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]
    messages: list[dict[str, Any]] = []

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, messages, [], [], context
    )

    expected = (
        "В запросе названо несколько отдельных моментов или незамкнутая граница времени. "
        "Раздели их на отдельные календарные вопросы, чтобы я не потеряла часть интервала."
    )
    assert context.structural_answer == expected
    assert messages == [{"role": "system", "content": expected}]
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert kernel.calls == []
    assert llm.calls == []
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))
    assert "memory_search" in _tool_names(tools)


@pytest.mark.asyncio
async def test_a_past_statement_about_plans_is_not_routed_as_a_future_calendar_question() -> None:
    question = "Планы были запланированы вчера"
    none_case = FIXTURE["time_controls"][10]
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(none_case))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert context.structural_answer == ""
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.parametrize(
    "question",
    [
        "Какие события предстоят сегодня?",
        "Какие события намечены сегодня?",
    ],
)
@pytest.mark.asyncio
async def test_future_events_today_use_upcoming_never_the_past_timeline(question: str) -> None:
    case = FIXTURE["time_positives"][15]
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(case))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]
    tools_used: list[str] = []

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], tools_used, [], context
    )

    assert kernel.calls == [("upcoming", {"since": "2026-08-08", "until": "2026-08-08"})]
    assert tools_used == ["upcoming"]
    assert all(name != "what_happened" for name, _ in kernel.calls)
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.asyncio
async def test_a_future_single_hour_reaches_upcoming_with_its_exact_bounds() -> None:
    question = "Что запланировано завтра в 15:00?"
    case = {
        "expected": {
            "time_direction": "future",
            "time_window_kind": "single_hour",
        }
    }
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(case))
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )
    tools = [_tool("what_happened"), _tool("upcoming")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == [
        (
            "upcoming",
            {
                "since": "2026-08-09T15:00:00",
                "until": "2026-08-09T15:59:59",
            },
        )
    ]
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.asyncio
async def test_a_past_timeline_question_for_a_future_hour_today_fails_before_the_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_runtime_module, "datetime", _MorningDatetime)
    case = FIXTURE["time_positives"][14]
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(case))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        "Что было сегодня в 23:00?", None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert "не удалось" in context.structural_answer.casefold()
    assert context.remainder_known is True
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.asyncio
async def test_execution_kernel_rejects_a_future_hour_but_accepts_the_whole_current_day(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution_kernel_module, "datetime", _MorningDatetime)
    storage.ensure_user("synthetic")
    kernel = ExecutionKernel(settings=settings)
    kernel.storage = storage

    future_hour = await kernel._what_happened(  # noqa: SLF001
        actor=_actor(),
        since="2026-08-08T23:00:00",
        until="2026-08-08T23:59:59",
    )
    whole_day = await kernel._what_happened(  # noqa: SLF001
        actor=_actor(),
        since="2026-08-08T00:00:00",
        until="2026-08-08T23:59:59",
    )

    assert future_hour["understood"] is False
    assert re.search(r"будущ", str(future_hour.get("error") or ""), re.IGNORECASE)
    assert whole_day["understood"] is True
    assert whole_day["shown"] == 0
    assert whole_day["total"] == {"messages": 0, "documents": 0, "total": 0}


@pytest.mark.asyncio
async def test_upcoming_today_excludes_elapsed_exact_times_but_keeps_all_day_rows(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution_kernel_module, "datetime", _MorningDatetime)
    user_id = "synthetic-upcoming-clock-boundary"
    storage.ensure_user(user_id)
    rows = [
        (
            "ent-upcoming-elapsed",
            "elapsed exact-time event",
            "2026-08-08T09:00:00",
            "minute",
        ),
        (
            "ent-upcoming-future-today",
            "future exact-time event",
            "2026-08-08T11:00:00",
            "minute",
        ),
        (
            "ent-upcoming-all-day",
            "day-precision all-day event",
            "2026-08-08",
            "day",
        ),
        (
            "ent-upcoming-future-hour",
            "tomorrow exact-hour event",
            "2026-08-09T15:30:00",
            "minute",
        ),
        (
            "ent-upcoming-outside-hour",
            "tomorrow outside-hour event",
            "2026-08-09T16:00:00",
            "minute",
        ),
    ]
    for entity_id, name, occurred_at, precision in rows:
        storage.create_entity(
            Entity(
                id=entity_id,
                user_id=user_id,
                name=name,
                entity_type=EntityType.EVENT,
            )
        )
        storage.set_entity_time(
            entity_id,
            user_id,
            occurred_at,
            precision=precision,
            source="document:synthetic",
        )

    kernel = ExecutionKernel(settings=settings)
    kernel.storage = storage
    kernel.kg = object()  # type: ignore[assignment]
    kernel.web_surfer = object()  # type: ignore[assignment]
    kernel.ingestion = object()  # type: ignore[assignment]
    actor = ActorContext(user_id=user_id, preset_key="owner", source="test")

    today = await kernel._upcoming(  # noqa: SLF001
        actor=actor,
        since="2026-08-08",
        until="2026-08-08",
    )
    exact_hour = await kernel._upcoming(  # noqa: SLF001
        actor=actor,
        since="2026-08-09T15:00:00",
        until="2026-08-09T15:59:59",
    )

    assert today["understood"] is True
    assert today["shown"] == today["total"] == len(today["items"]) == 2
    assert {item["what"] for item in today["items"]} == {
        "future exact-time event",
        "day-precision all-day event",
    }
    assert exact_hour["understood"] is True
    assert exact_hour["asked_about"]["since"] == "2026-08-09T15:00:00"
    assert exact_hour["asked_about"]["until"] == "2026-08-09T15:59:59"
    assert exact_hour["shown"] == exact_hour["total"] == len(exact_hour["items"]) == 1
    assert exact_hour["items"][0]["what"] == "tomorrow exact-hour event"


@pytest.mark.asyncio
async def test_a_partial_time_replay_is_not_given_back_to_the_main_model() -> None:
    case = FIXTURE["time_positives"][2]
    runtime = _runtime(_Kernel(mode="failed"), _OracleLLM(case, remainder="вчера"))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        case["question"],
        None,
        [_tool("what_happened"), _tool("upcoming")],
        [],
        [],
        [],
        context,
    )

    assert "не удалось надёжно отделить" in context.structural_answer
    assert context.open_remainder == ""


@pytest.mark.parametrize(
    ("tool_name", "payload"),
    [
        ("what_happened", {"shown": 0, "events": [], "total": {"total": 0}}),
        (
            "what_happened",
            {
                "shown": 0,
                "events": [],
                "total": {"messages": 0, "documents": 0, "total": 0},
            },
        ),
        (
            "what_happened",
            {
                "shown": 1,
                "events": [{}],
                "total": {"messages": 1, "documents": 0, "total": 1},
                "coverage": {
                    "complete": "yes",
                    "strategy": "complete",
                    "includes_latest": False,
                },
            },
        ),
        (
            "what_happened",
            {
                "shown": 0,
                "events": [],
                "total": {"messages": 9, "documents": 8, "total": 0},
            },
        ),
        (
            "upcoming",
            {
                "shown": 0,
                "items": [],
                "total": 0,
                "days": 999,
                "asked_about": {"since": "2026-08-08", "until": "2026-08-08"},
            },
        ),
    ],
)
def test_a_plausible_header_cannot_hide_a_contradictory_temporal_payload(
    tool_name: str,
    payload: dict[str, Any],
) -> None:
    assert _temporal_payload_is_coherent(tool_name, payload) is False


@pytest.mark.asyncio
async def test_a_dense_single_source_timeline_includes_the_true_last_event_and_truthful_coverage(
    settings: Any,
    storage: Any,
) -> None:
    user_id = "synthetic-dense-timeline"
    storage.ensure_user(user_id)
    conversation = storage.create_conversation(user_id, "Synthetic dense timeline")
    start = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    rows = [
        (
            f"msg-dense-{index:04d}",
            conversation["id"],
            user_id,
            "user",
            f"synthetic-event-{index:04d}",
            "{}",
            None,
            (start + timedelta(seconds=index)).isoformat(),
        )
        for index in range(401)
    ]
    with storage.transaction() as connection:
        connection.executemany(
            """INSERT INTO messages(
                   id, conversation_id, user_id, role, content,
                   metadata_json, reply_to, created_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    direct = storage.what_happened(
        user_id,
        since=start.isoformat(),
        until=(start + timedelta(seconds=400)).isoformat(),
        limit=40,
    )
    totals = storage.count_what_happened(
        user_id,
        since=start.isoformat(),
        until=(start + timedelta(seconds=400)).isoformat(),
    )

    assert len(direct) == 40
    assert direct[-1]["text"] == "synthetic-event-0400"
    assert totals == {"messages": 401, "documents": 0, "total": 401}

    kernel = ExecutionKernel(settings=settings)
    kernel.storage = storage
    payload = await kernel._what_happened(  # noqa: SLF001
        actor=ActorContext(user_id=user_id, preset_key="owner", source="test"),
        since="2026-08-07T00:00:00",
        until="2026-08-07T23:59:59",
        limit=40,
    )

    assert payload["understood"] is True
    assert payload["shown"] == 40
    assert payload["events"][-1]["text"] == "synthetic-event-0400"
    assert payload["total"] == {"messages": 401, "documents": 0, "total": 401}
    assert payload["coverage"] == {
        "complete": False,
        "strategy": "uniform_interval_sample",
        "includes_latest": True,
    }


@pytest.mark.asyncio
async def test_classifier_failure_log_does_not_copy_the_private_question(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "PRIVATE-SYNTHETIC-DO-NOT-LOG-7f22"
    message = f"Назови число корпуса целиком {secret}"
    kernel = _Kernel()
    llm = _OracleLLM(fail_classifier=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        message, None, [_tool("kg_stats")], [], [], [], context
    )

    assert secret not in caplog.text
    assert kernel.calls == []


def _agent_loop_runtime(
    settings: Any, storage: Any, llm: _HostileAgentLoopLLM, kernel: _Kernel
) -> AgentRuntime:
    """Keep the real routing and tool loop, silence unrelated prefetch branches."""

    runtime = AgentRuntime(settings, storage, llm=llm, kernel=kernel)
    runtime._local_today = lambda: FIXED_TODAY  # type: ignore[method-assign]  # noqa: SLF001

    async def noop(*args: Any, **kwargs: Any) -> None:  # noqa: ARG001
        return None

    async def not_a_person(*args: Any, **kwargs: Any) -> bool:  # noqa: ARG001
        return False

    runtime._prefetch_the_web_if_asked = noop  # type: ignore[method-assign]  # noqa: SLF001
    runtime._prefetch_person_activity = not_a_person  # type: ignore[method-assign]  # noqa: SLF001
    runtime._prefetch_the_archive_if_asked = noop  # type: ignore[method-assign]  # noqa: SLF001
    runtime._prefetch_a_reminder_if_asked = noop  # type: ignore[method-assign]  # noqa: SLF001
    return runtime


def _actor() -> ActorContext:
    return ActorContext(user_id="synthetic", preset_key="owner", source="test")


@pytest.mark.parametrize(
    ("message", "unrelated_marker"),
    [
        pytest.param(
            "Сколько всего объектов знаний в моей базе? И что там по проекту Кобальт?",
            "проект",
            id="project-tail",
        ),
        pytest.param(
            "Сколько всего объектов знаний в моей базе? И какие теги есть?",
            "тег",
            id="tag-tail",
        ),
        pytest.param(
            "Покажи теги и скажи, сколько всего файлов в архиве.",
            "тег",
            id="tags-before-count",
        ),
        pytest.param(
            "Покажи теги, затем скажи, сколько всего файлов в архиве.",
            "тег",
            id="tags-before-count-with-modifier",
        ),
        pytest.param(
            "Скажи, сколько всего файлов в архиве, а потом покажи теги.",
            "тег",
            id="count-before-tags-with-modifier",
        ),
        pytest.param(
            "Что было вчера? И сколько всего объектов знаний в моей базе?",
            "вчера",
            id="time-before-count",
        ),
        pytest.param(
            "Сколько всего объектов знаний в моей базе? И найди документы про Кобальт.",
            "найди",
            id="document-search-tail",
        ),
    ],
)
def test_a_compound_global_count_projection_excludes_the_unrelated_clause(
    message: str,
    unrelated_marker: str,
) -> None:
    projected = _archive_count_projection(message)

    assert "сколько" in projected.casefold()
    assert unrelated_marker not in projected.casefold()


@pytest.mark.parametrize(
    (
        "message",
        "remainder",
        "metric",
        "time_direction",
        "time_window_kind",
        "tail_tool",
        "tail_arguments",
    ),
    [
        pytest.param(
            "Сколько всего объектов знаний в моей базе? И что там по проекту Кобальт?",
            "что там по проекту Кобальт",
            "knowledge_objects",
            "none",
            "none",
            "memory_search",
            {"query": "проект Кобальт"},
            id="count-then-project",
        ),
        pytest.param(
            "Сколько всего объектов знаний в моей базе? И какие теги есть?",
            "",
            "knowledge_objects",
            "none",
            "none",
            "list_tags",
            {},
            id="count-then-tags",
        ),
        pytest.param(
            "Покажи теги и скажи, сколько всего файлов в архиве.",
            "",
            "files",
            "none",
            "none",
            "list_tags",
            {},
            id="tags-then-count",
        ),
        pytest.param(
            "Покажи теги, затем скажи, сколько всего файлов в архиве.",
            "",
            "files",
            "none",
            "none",
            "list_tags",
            {},
            id="tags-then-count-with-modifier",
        ),
        pytest.param(
            "Скажи, сколько всего файлов в архиве, а потом покажи теги.",
            "",
            "files",
            "none",
            "none",
            "list_tags",
            {},
            id="count-then-tags-with-modifier",
        ),
        pytest.param(
            "Что было вчера? И сколько всего объектов знаний в моей базе?",
            "Что было вчера",
            "knowledge_objects",
            "past",
            "single_day",
            "list_tags",
            {},
            id="time-then-count",
        ),
        pytest.param(
            "Сколько всего объектов знаний в моей базе? И найди документы про Кобальт.",
            "найди документы про Кобальт",
            "knowledge_objects",
            "none",
            "none",
            "memory_search",
            {"query": "документы Кобальт"},
            id="count-then-document-search",
        ),
    ],
)
@pytest.mark.asyncio
async def test_compound_global_counts_execute_once_then_preserve_only_the_other_capability(
    message: str,
    remainder: str,
    metric: str,
    time_direction: str,
    time_window_kind: str,
    tail_tool: str,
    tail_arguments: dict[str, Any],
    settings: Any,
    storage: Any,
) -> None:
    case = {
        "expected": {
            "time_direction": time_direction,
            "time_window_kind": time_window_kind,
            "archive_count_scope": "whole_archive",
            "count_metric": metric,
        }
    }
    attack_calls = [("kg_stats", {}), (tail_tool, tail_arguments)]
    if time_direction == "past":
        attack_calls.insert(1, ("what_happened", {}))
        attack_calls.insert(2, ("upcoming", {}))
    llm = _HostileAgentLoopLLM(case, attack_calls, remainder=remainder)
    kernel = _Kernel()
    runtime = _agent_loop_runtime(settings, storage, llm, kernel)
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )
    tools = [_tool("kg_stats"), _tool(tail_tool)]
    if time_direction == "past":
        tools.extend([_tool("what_happened"), _tool("upcoming")])

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        message,
        _actor(),
        tools,
        None,
    )

    assert [call for call in kernel.calls if call[0] == "kg_stats"] == [("kg_stats", {})]
    asks_for_tags = "тег" in message.casefold()
    if asks_for_tags:
        assert context.structural_answer.startswith(STAT_ANSWERS[metric])
        assert "synthetic — 1" in context.structural_answer
        assert context.open_remainder == ""
        assert llm.main_tools == []
        assert sum("Часть просьбы человека уже решена" in call for call in llm.calls) == 1
        assert [call for call in kernel.calls if call[0] == "list_tags"] == [("list_tags", {})]
        assert result["content"] == ""
    else:
        assert context.structural_answer == STAT_ANSWERS[metric]
        assert context.open_remainder == remainder
        assert llm.main_tools
        assert all("kg_stats" not in offered for offered in llm.main_tools)
        assert tail_tool in llm.main_tools[0]
        assert any(name == tail_tool for name, _ in kernel.calls)
        assert result["content"] == "Синтетический остаток обработан."
    if time_direction == "past":
        yesterday = FIXTURE["time_positives"][2]
        assert [call for call in kernel.calls if call[0] == "what_happened"] == [
            ("what_happened", _time_arguments(yesterday))
        ]
        assert all({"what_happened", "upcoming"}.isdisjoint(offered) for offered in llm.main_tools)


@pytest.mark.parametrize("tag_first", [False, True], ids=["local-before-tags", "tags-before-local"])
@pytest.mark.asyncio
async def test_local_selection_compound_tail_survives_without_global_stats_authority(
    tag_first: bool,
    settings: Any,
    storage: Any,
) -> None:
    case = FIXTURE["local_count_controls"][7]
    local_question = case["question"].rstrip("?.!")
    if tag_first:
        expected_remainder = local_question[:1].lower() + local_question[1:]
        message = f"Покажи доступные теги и заодно {expected_remainder}."
    else:
        expected_remainder = local_question
        message = case["question"] + " И покажи доступные теги."
    llm = _HostileAgentLoopLLM(
        case,
        [("kg_stats", {}), ("list_tags", {})],
    )
    kernel = _Kernel()
    runtime = _agent_loop_runtime(settings, storage, llm, kernel)
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        message,
        _actor(),
        [_tool("kg_stats"), _tool("list_tags")],
        None,
    )

    assert kernel.calls == [("list_tags", {})]
    assert llm.main_tools and all(offered == [] for offered in llm.main_tools)
    assert "synthetic — 1" in context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == expected_remainder
    assert not any("Часть просьбы человека уже решена" in call for call in llm.calls)
    assert result["content"] == "Синтетический остаток обработан."


@pytest.mark.asyncio
async def test_a_settled_global_count_keeps_its_real_remainder_but_cannot_be_repeated_by_the_model(
    settings: Any,
    storage: Any,
) -> None:
    case = FIXTURE["global_count_positives"][0]
    remainder = "объясни термин объект знаний"
    llm = _HostileAgentLoopLLM(
        case,
        [("kg_stats", {}), ("memory_search", {"query": "объект знаний"})],
        remainder=remainder,
    )
    kernel = _Kernel()
    runtime = _agent_loop_runtime(settings, storage, llm, kernel)
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        case["question"] + " И объясни термин «объект знаний».",
        _actor(),
        [_tool("kg_stats"), _tool("memory_search")],
        None,
    )

    assert kernel.calls == [("kg_stats", {}), ("memory_search", {"query": "объект знаний"})]
    assert context.structural_answer == STAT_ANSWERS["knowledge_objects"]
    assert context.open_remainder == remainder
    assert "kg_stats" not in llm.main_tools[0]
    assert "memory_search" in llm.main_tools[0]
    assert result["content"] == "Синтетический остаток обработан."


@pytest.mark.asyncio
async def test_a_hostile_one_word_count_remainder_cannot_reopen_model_speech(
    settings: Any,
    storage: Any,
) -> None:
    case = FIXTURE["global_count_positives"][0]
    llm = _HostileAgentLoopLLM(case, [("kg_stats", {})], remainder="файлов")
    kernel = _Kernel()
    runtime = _agent_loop_runtime(settings, storage, llm, kernel)
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        case["question"],
        _actor(),
        [_tool("kg_stats"), _tool("memory_search")],
        None,
    )

    assert kernel.calls == [("kg_stats", {})]
    assert context.structural_answer.startswith(STAT_ANSWERS["knowledge_objects"])
    assert "не удалось надёжно отделить" in context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert llm.main_tools == []
    assert llm.attacked is False
    assert result["content"] == ""


@pytest.mark.asyncio
async def test_a_hostile_one_word_time_remainder_cannot_contradict_a_structural_failure(
    settings: Any,
    storage: Any,
) -> None:
    case = FIXTURE["time_positives"][2]
    llm = _HostileAgentLoopLLM(
        case,
        [("what_happened", {}), ("upcoming", {})],
        remainder="события",
    )
    kernel = _Kernel(mode="failed")
    runtime = _agent_loop_runtime(settings, storage, llm, kernel)
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        case["question"],
        _actor(),
        [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")],
        None,
    )

    assert kernel.calls == [("what_happened", _time_arguments(case))]
    assert "Проверить личную ленту" in context.structural_answer
    assert "не удалось надёжно отделить" in context.structural_answer
    assert "событий нет" not in context.structural_answer.casefold()
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert llm.main_tools == []
    assert llm.attacked is False
    assert result["content"] == ""


@pytest.mark.parametrize(
    ("case_index", "kind"),
    [
        pytest.param(13, "быт", id="primary-small-talk"),
        pytest.param(15, "человек", id="nonlocal-person"),
        pytest.param(8, "знание", id="dated-project-document"),
    ],
)
@pytest.mark.asyncio
async def test_settled_non_timeline_controls_cannot_model_call_either_temporal_tool(
    case_index: int,
    kind: str,
    settings: Any,
    storage: Any,
) -> None:
    case = FIXTURE["time_controls"][case_index]
    llm = _HostileAgentLoopLLM(
        case,
        [("what_happened", {}), ("upcoming", {})],
    )
    kernel = _Kernel()
    runtime = _agent_loop_runtime(settings, storage, llm, kernel)
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=(kind, None),
    )

    await runtime._agentic_loop(  # noqa: SLF001
        context,
        case["question"],
        _actor(),
        [_tool("what_happened"), _tool("upcoming"), _tool("list_tags")],
        None,
    )

    assert kernel.calls == []
    assert {"what_happened", "upcoming"}.isdisjoint(llm.main_tools[0])


@pytest.mark.asyncio
async def test_a_positive_time_route_executes_only_its_selected_prefetch_despite_hostile_model_calls(
    settings: Any,
    storage: Any,
) -> None:
    case = FIXTURE["time_positives"][2]
    llm = _HostileAgentLoopLLM(
        case,
        [("what_happened", {}), ("upcoming", {})],
    )
    kernel = _Kernel()
    runtime = _agent_loop_runtime(settings, storage, llm, kernel)
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        case["question"],
        _actor(),
        [_tool("what_happened"), _tool("upcoming"), _tool("list_tags")],
        None,
    )

    assert kernel.calls == [("what_happened", _time_arguments(case))]
    assert {"what_happened", "upcoming"}.isdisjoint(llm.main_tools[0])
    assert result["content"] == "Синтетический остаток обработан."


@pytest.mark.parametrize(
    "question",
    [
        pytest.param(
            "Что было вчера? [подробнее](https://example.test/2026-08-06)",
            id="hidden-markdown-url-date",
        ),
        pytest.param("Что было вче\u200bра?", id="cf-split-yesterday"),
        pytest.param("Что было вче**ра**?", id="emphasis-split-yesterday"),
        pytest.param(
            "Что было вче[ра](https://example.test/archive)?",
            id="link-split-yesterday",
        ),
    ],
)
@pytest.mark.asyncio
async def test_only_visible_temporal_text_controls_exact_routing_and_the_multi_guard(
    question: str,
) -> None:
    case = FIXTURE["time_positives"][2]
    kernel = _Kernel()
    llm = _OracleLLM(case)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == [("what_happened", _time_arguments(case))]
    assert context.structural_answer == ""
    assert "несколько отдельных моментов" not in context.structural_answer
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.asyncio
async def test_invisible_and_markdown_splits_preserve_the_visible_multiple_target_guard() -> None:
    question = "Что было вче\u200bра и поза**вчера**?"
    kernel = _Kernel()
    llm = _OracleLLM(hostile_time=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]
    messages: list[dict[str, Any]] = []

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, messages, [], [], context
    )

    expected = (
        "В запросе названо несколько отдельных моментов или незамкнутая граница времени. "
        "Раздели их на отдельные календарные вопросы, чтобы я не потеряла часть интервала."
    )
    assert kernel.calls == []
    assert llm.calls == []
    assert context.structural_answer == expected
    assert messages == [{"role": "system", "content": expected}]
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.asyncio
async def test_a_future_declaration_is_not_a_request_to_read_the_calendar() -> None:
    question = "Завтра планируем созвон с командой."
    case = {
        "expected": {
            "time_direction": "none",
            "time_window_kind": "none",
        }
    }
    kernel = _Kernel()
    llm = _OracleLLM(case)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]
    messages: list[dict[str, Any]] = []

    assert fast_time_intent(question) is None
    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, messages, [], [], context
    )

    assert kernel.calls == []
    assert llm.calls == []
    assert messages == []
    assert context.structural_answer == ""
    assert context.remainder_known is False
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))
    assert "memory_search" in _tool_names(tools)


@pytest.mark.parametrize(
    ("question", "expected_intent", "expected_window"),
    [
        pytest.param(
            "Что было на минувшей неделе?",
            TimeIntent("past", "calendar_week"),
            TimeWindow("2026-07-27", "2026-08-02"),
            id="elapsed-week",
        ),
        pytest.param(
            "Что было в предыдущем месяце?",
            TimeIntent("past", "calendar_month"),
            TimeWindow("2026-07-01", "2026-07-31"),
            id="previous-month",
        ),
        pytest.param(
            "Какие планы на будущую неделю?",
            TimeIntent("future", "calendar_week"),
            TimeWindow("2026-08-10", "2026-08-16"),
            id="future-week",
        ),
        pytest.param(
            "Какие планы на грядущий месяц?",
            TimeIntent("future", "calendar_month"),
            TimeWindow("2026-09-01", "2026-09-30"),
            id="coming-month",
        ),
        pytest.param(
            "Что запланировано на предстоящую неделю?",
            TimeIntent("future", "calendar_week"),
            TimeWindow("2026-08-10", "2026-08-16"),
            id="forthcoming-week",
        ),
        pytest.param(
            "Что было на позапрошлой неделе?",
            TimeIntent("past", "calendar_week"),
            TimeWindow("2026-07-20", "2026-07-26"),
            id="week-before-last",
        ),
        pytest.param(
            "Что было в позапрошлом месяце?",
            TimeIntent("past", "calendar_month"),
            TimeWindow("2026-06-01", "2026-06-30"),
            id="month-before-last",
        ),
    ],
)
def test_calendar_synonyms_shift_to_the_exact_adjacent_period(
    question: str,
    expected_intent: TimeIntent,
    expected_window: TimeWindow,
) -> None:
    intent = fast_time_intent(question)

    assert intent == expected_intent
    assert build_time_window(question, intent, today=FIXED_TODAY) == expected_window


@pytest.mark.parametrize(
    ("question", "hostile_kind", "expected_arguments"),
    [
        pytest.param(
            "Расскажи события 1 августа 2026.",
            "calendar_month",
            {"since": "2026-08-01T00:00:00", "until": "2026-08-01T23:59:59", "limit": 40},
            id="full-date-cannot-become-month",
        ),
        pytest.param(
            "Перечисли события вчера.",
            "calendar_week",
            {"since": "2026-08-07T00:00:00", "until": "2026-08-07T23:59:59", "limit": 40},
            id="relative-day-cannot-become-week",
        ),
        pytest.param(
            "Дай хронику прошлого месяца.",
            "calendar_week",
            {"since": "2026-07-01T00:00:00", "until": "2026-07-31T23:59:59", "limit": 40},
            id="named-month-cannot-become-week",
        ),
        pytest.param(
            "Расскажи события августа 2026.",
            "calendar_week",
            {"since": "2026-08-01T00:00:00", "until": FIXED_LOCAL_NOW, "limit": 40},
            id="current-month-cannot-become-week",
        ),
    ],
)
@pytest.mark.asyncio
async def test_an_arbiter_cannot_widen_the_lexically_proven_window_kind(
    question: str,
    hostile_kind: str,
    expected_arguments: dict[str, Any],
) -> None:
    case = {"expected": {"time_direction": "past", "time_window_kind": hostile_kind}}
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(case))
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )

    fast = fast_time_intent(question, today=FIXED_TODAY)
    if fast is not None:
        # A neutral event query with one fully anchored absolute day is now a
        # code-owned route.  The other lexical shapes still exercise the
        # hostile arbiter; neither path may widen the proven day/month.
        assert question == "Расскажи события 1 августа 2026."
        assert fast == TimeIntent("past", "single_day")
    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question,
        None,
        [_tool("what_happened"), _tool("upcoming")],
        [],
        [],
        [],
        context,
    )

    assert kernel.calls == [("what_happened", expected_arguments)]


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        pytest.param(
            "Какие планы на минувшую неделю?",
            TimeIntent("future", "calendar_week"),
            id="future-over-elapsed-week",
        ),
        pytest.param(
            "Какие планы на предыдущий месяц?",
            TimeIntent("future", "calendar_month"),
            id="future-over-previous-month",
        ),
        pytest.param(
            "Что было на будущей неделе?",
            TimeIntent("past", "calendar_week"),
            id="past-over-future-week",
        ),
        pytest.param(
            "Что происходило в грядущем месяце?",
            TimeIntent("past", "calendar_month"),
            id="past-over-coming-month",
        ),
        pytest.param(
            "Что было на предстоящей неделе?",
            TimeIntent("past", "calendar_week"),
            id="past-over-forthcoming-week",
        ),
        pytest.param(
            "Какие планы на позапрошлый месяц?",
            TimeIntent("future", "calendar_month"),
            id="future-over-month-before-last",
        ),
    ],
)
def test_calendar_synonyms_cannot_override_a_conflicting_direction(
    question: str,
    intent: TimeIntent,
) -> None:
    assert build_time_window(question, intent, today=FIXED_TODAY) is None


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        pytest.param(
            "Что было в прошлую субботу?",
            TimeWindow("2026-08-01", "2026-08-01"),
            id="previous-same-weekday",
        ),
        pytest.param(
            "Что было в предыдущую субботу?",
            TimeWindow("2026-08-01", "2026-08-01"),
            id="previous-synonym-same-weekday",
        ),
        pytest.param(
            "Что было в минувшую субботу?",
            TimeWindow("2026-08-01", "2026-08-01"),
            id="elapsed-synonym-same-weekday",
        ),
        pytest.param(
            "Какие планы на следующую субботу?",
            TimeWindow("2026-08-15", "2026-08-15"),
            id="next-same-weekday",
        ),
        pytest.param(
            "Какие планы на будущую субботу?",
            TimeWindow("2026-08-15", "2026-08-15"),
            id="future-synonym-same-weekday",
        ),
        pytest.param(
            "Какие планы на предстоящую субботу?",
            TimeWindow("2026-08-15", "2026-08-15"),
            id="forthcoming-synonym-same-weekday",
        ),
    ],
)
def test_an_adjective_moves_the_same_weekday_by_a_full_week(
    question: str,
    expected: TimeWindow,
) -> None:
    intent = fast_time_intent(question)

    assert intent == TimeIntent("past" if "Что было" in question else "future", "single_day")
    assert build_time_window(question, intent, today=FIXED_TODAY) == expected


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Какие планы на минувшую неделю?", id="future-request-over-elapsed-period"),
        pytest.param("Что происходило в предстоящем месяце?", id="past-request-over-future-period"),
    ],
)
@pytest.mark.asyncio
async def test_a_natural_direction_conflict_cannot_switch_to_the_opposite_temporal_tool(
    question: str,
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_time=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert "Не удалось однозначно вычислить" in context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Что происходило за две недели?", id="bare-two-past-weeks"),
        pytest.param("Какие планы на две недели?", id="bare-two-future-weeks"),
        pytest.param("Что происходило за три месяца?", id="bare-three-past-months"),
        pytest.param("Какие планы на два месяца?", id="bare-two-future-months"),
        pytest.param("Что происходило последние две недели?", id="two-calendar-weeks"),
        pytest.param("Какие планы на ближайшие три месяца?", id="three-calendar-months"),
        pytest.param("Что было в июле и августе?", id="two-named-months"),
        pytest.param("Что было на прошлой неделе и в июле?", id="week-and-month"),
    ],
)
@pytest.mark.asyncio
async def test_multiple_or_quantified_named_periods_fail_closed_as_an_unrepresentable_union(
    question: str,
) -> None:
    kernel = _Kernel()
    llm = _OracleLLM(hostile_time=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert llm.calls == []
    assert "несколько отдельных моментов" in context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))
    assert "memory_search" in _tool_names(tools)


@pytest.mark.parametrize(
    ("question", "time_direction", "window_kind"),
    [
        pytest.param(
            "Что происходило сто пять дней назад?",
            "past",
            "single_day",
            id="one-hundred-five-days",
        ),
        pytest.param(
            "Что происходило пару дней назад?",
            "past",
            "single_day",
            id="a-couple-of-days",
        ),
        pytest.param(
            "Что происходило сто пять недель назад?",
            "past",
            "calendar_week",
            id="one-hundred-five-weeks",
        ),
        pytest.param(
            "Какие планы через несколько недель?",
            "future",
            "calendar_week",
            id="several-weeks",
        ),
        pytest.param(
            "Что происходило полтора месяца назад?",
            "past",
            "calendar_month",
            id="one-and-a-half-months",
        ),
    ],
)
@pytest.mark.asyncio
async def test_an_unsupported_quantity_cannot_become_its_tail_or_a_default_period(
    question: str,
    time_direction: str,
    window_kind: str,
) -> None:
    hostile_intent = TimeIntent(time_direction, window_kind)
    case = {
        "expected": {
            "time_direction": time_direction,
            "time_window_kind": window_kind,
        }
    }
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(case))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]

    assert build_time_window(question, hostile_intent, today=FIXED_TODAY) is None
    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert "Не удалось однозначно вычислить" in context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Что было вчера в 24:00?", id="hour-24-colon"),
        pytest.param("Что было вчера в 24 часа?", id="hour-24-word"),
        pytest.param("Что было вчера в 99:99?", id="impossible-clock"),
    ],
)
@pytest.mark.asyncio
async def test_an_invalid_clock_fails_closed_instead_of_expanding_to_the_whole_day(
    question: str,
) -> None:
    kernel = _Kernel()
    llm = _OracleLLM(hostile_time=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert llm.calls == []
    assert "недопустимое время суток" in context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Что было вчера после двух часов дня?", id="after-spoken-clock"),
        pytest.param("Какие планы завтра до пяти часов вечера?", id="before-spoken-clock"),
        pytest.param("Что было вчера после восьми вечера?", id="after-spoken-period-clock"),
        pytest.param("Какие планы завтра до девяти утра?", id="before-spoken-period-clock"),
        pytest.param("Что было вчера до полудня?", id="before-noon"),
        pytest.param("Какие планы завтра после полуночи?", id="after-midnight"),
    ],
)
@pytest.mark.asyncio
async def test_a_relational_spoken_clock_fails_closed_as_an_open_boundary(
    question: str,
) -> None:
    kernel = _Kernel()
    llm = _OracleLLM(hostile_time=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert llm.calls == []
    assert "незамкнутая граница времени" in context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Что ты делала вчера?", id="assistant-subject"),
        pytest.param("Что она делала вчера?", id="third-person-subject"),
        pytest.param("Что происходило у проекта Альфа вчера?", id="at-project"),
        pytest.param("Какие планы проекта Альфа на завтра?", id="project-plans"),
        pytest.param("Что было по поводу заявки К-7 вчера?", id="about-application"),
    ],
)
@pytest.mark.asyncio
async def test_an_explicit_temporal_subject_never_becomes_the_askers_broad_timeline(
    question: str,
) -> None:
    kernel = _Kernel()
    llm = _OracleLLM(hostile_time=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert llm.calls == []
    assert context.structural_answer == ""
    assert context.remainder_known is False
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))
    assert "memory_search" in _tool_names(tools)


@pytest.mark.asyncio
async def test_a_subject_filter_wins_before_the_multiple_time_target_guard() -> None:
    question = "Что происходило у проекта Альфа вчера и позавчера?"
    kernel = _Kernel()
    llm = _OracleLLM(hostile_time=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]
    messages: list[dict[str, Any]] = []

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, messages, [], [], context
    )

    assert kernel.calls == []
    assert llm.calls == []
    assert messages == []
    assert context.structural_answer == ""
    assert context.remainder_known is False
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))
    assert "memory_search" in _tool_names(tools)


@pytest.mark.parametrize(
    ("question", "metric"),
    [
        pytest.param(
            "Подскажи, пожалуйста, сколько всего реально сохранённых файлов сейчас лежит в моём архиве?",
            "files",
            id="polite-files",
        ),
        pytest.param(
            "Какое общее число исходных материалов фактически имеется в личном хранилище на данный момент?",
            "raw_objects",
            id="verbose-raw-materials",
        ),
        pytest.param(
            "Скажи точно, сколько всего отношений прямо сейчас хранится в моём графе?",
            "relations",
            id="exact-relations",
        ),
    ],
)
@pytest.mark.asyncio
async def test_extra_benign_wording_keeps_a_valid_whole_archive_metric_exact(
    question: str,
    metric: str,
) -> None:
    case = {
        "expected": {
            "archive_count_scope": "whole_archive",
            "count_metric": metric,
        }
    }
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(case))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("kg_stats"), _tool("memory_search")]

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == [("kg_stats", {})]
    assert context.structural_answer == STAT_ANSWERS[metric]
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert "kg_stats" not in _tool_names(tools)
    assert "memory_search" in _tool_names(tools)


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Сколько всего тегов в моём архиве?", id="tags"),
        pytest.param("Сколько всего проектов в моей базе?", id="projects"),
        pytest.param("Назови общее число папок в личном хранилище.", id="folders"),
        pytest.param("Сколько всего категорий во всём архиве?", id="categories"),
    ],
)
@pytest.mark.asyncio
async def test_a_generic_unsupported_whole_archive_metric_gets_a_structural_refusal(
    question: str,
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_count=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("kg_stats"), _tool("memory_search")]

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert "Не удалось однозначно определить" in context.structural_answer
    assert not re.search(r"\b(?:101|202|303|404|505)\b", context.structural_answer)
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert "kg_stats" not in _tool_names(tools)
    assert "memory_search" in _tool_names(tools)


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Сколько всего файлов и тегов в моём архиве?", id="files-and-tags"),
        pytest.param("Сколько всего знаний и проектов в моей базе?", id="knowledge-and-projects"),
        pytest.param("Назови общее число сущностей и папок в графе.", id="entities-and-folders"),
    ],
)
@pytest.mark.asyncio
async def test_a_known_metric_conjoined_with_a_real_unsupported_metric_refuses_the_whole_count(
    question: str,
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_count=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("kg_stats")]

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert "Не удалось однозначно определить" in context.structural_answer
    assert not re.search(r"\b(?:101|202|303|404|505)\b", context.structural_answer)
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert "kg_stats" not in _tool_names(tools)


@pytest.mark.parametrize("kind", ["быт", "знание", "интернет"])
@pytest.mark.asyncio
async def test_a_stronger_non_archive_verdict_leaves_a_metalinguistic_count_mention_untouched(
    kind: str,
) -> None:
    question = "Почему фраза «сколько всего файлов в архиве» звучит как запрос точного количества?"
    kernel = _Kernel()
    llm = _OracleLLM(hostile_count=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=(kind, None))
    tools = [_tool("kg_stats"), _tool("memory_search")]
    messages: list[dict[str, Any]] = []

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        question, None, tools, messages, [], [], context
    )

    assert kernel.calls == []
    assert llm.calls == []
    assert messages == []
    assert context.structural_answer == ""
    assert context.remainder_known is False
    assert context.open_remainder == ""
    assert "kg_stats" not in _tool_names(tools)
    assert "memory_search" in _tool_names(tools)


@pytest.mark.parametrize(
    ("tool_name", "payload"),
    [
        pytest.param(
            "what_happened",
            {
                "shown": 1,
                "events": [
                    {
                        "kind": "message",
                        "at": "2026-08-09 12:00",
                        "text": "outside the echoed day",
                    }
                ],
                "total": {"messages": 1, "documents": 0, "total": 1},
                "coverage": {
                    "complete": True,
                    "strategy": "complete",
                    "includes_latest": True,
                },
                "asked_about": {
                    "since": "2026-08-08T00:00:00",
                    "until": "2026-08-08T23:59:59",
                },
            },
            id="past-item-outside-echo",
        ),
        pytest.param(
            "upcoming",
            {
                "shown": 1,
                "items": [
                    {
                        "what": "outside the echoed day",
                        "on": "2026-08-09T12:00:00",
                        "when": "завтра",
                        "at": "12:00",
                        "mine": False,
                    }
                ],
                "total": 1,
                "days": 1,
                "asked_about": {"since": "2026-08-08", "until": "2026-08-08"},
                "note": "",
            },
            id="future-item-outside-echo",
        ),
        pytest.param(
            "upcoming",
            {
                "shown": 0,
                "items": [],
                "total": 0,
                "days": 1,
                "asked_about": {"since": "2026-08-08", "until": "2026-08-08"},
                "note": "",
            },
            id="empty-calendar-without-note",
        ),
        pytest.param(
            "upcoming",
            {
                "shown": 1,
                "items": [
                    {
                        "what": "inside the echoed day",
                        "on": "2026-08-08T12:00:00",
                        "when": "сегодня",
                        "at": "12:00",
                        "mine": False,
                    }
                ],
                "total": 1,
                "days": 1,
                "asked_about": {"since": "2026-08-08", "until": "2026-08-08"},
                "note": "ничего не запланировано",
            },
            id="nonempty-calendar-with-empty-note-claim",
        ),
    ],
)
def test_temporal_payload_items_and_empty_notes_must_agree_with_the_echoed_interval(
    tool_name: str,
    payload: dict[str, Any],
) -> None:
    assert _temporal_payload_is_coherent(tool_name, payload) is False


def test_timeline_storage_join_rejects_a_time_row_from_a_different_tenant(storage: Any) -> None:
    owner_id = "synthetic-timeline-join-owner"
    foreign_id = "synthetic-timeline-join-foreign"
    entity_id = "ent-synthetic-cross-tenant-time"
    storage.ensure_user(owner_id)
    storage.ensure_user(foreign_id)
    storage.create_entity(
        Entity(
            id=entity_id,
            user_id=owner_id,
            name="cross-tenant sentinel",
            entity_type=EntityType.EVENT,
        )
    )
    with storage.transaction() as connection:
        connection.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, occurred_end,
                   precision, source, updated_at
               ) VALUES(?, ?, ?, NULL, 'minute', 'document:synthetic', ?)""",
            (entity_id, foreign_id, "2026-08-09T12:00:00", "2026-08-08T00:00:00+00:00"),
        )

    rows = _bounded_visible_timeline_event_rows(
        storage,
        owner_id,
        owner_id,
        start="2026-08-09",
        end="2026-08-09T23:59:59",
    )
    total = _count_visible_timeline_events(
        storage,
        owner_id,
        owner_id,
        start="2026-08-09",
        end="2026-08-09T23:59:59",
    )

    assert rows == []
    assert total == 0


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Какие планы завтра до 9 утра?", id="numeric-before-morning"),
        pytest.param("Что было вчера после 8 вечера?", id="numeric-after-evening"),
        pytest.param("Что было вчера с девяти утра?", id="spoken-from-morning"),
        pytest.param("Что было вчера после восьми?", id="spoken-hour-without-period"),
        pytest.param("Какие планы завтра до 9?", id="numeric-hour-without-unit"),
        pytest.param(
            "Что происходило с 1 августа 2026 по 10:00 2 августа 2026?",
            id="range-through-colon-clock",
        ),
        pytest.param(
            "Что происходило с 1 августа 2026 по 10 часов 2 августа 2026?",
            id="range-through-clock-unit",
        ),
        pytest.param(
            "Что происходило с 1 августа 2026 по 10 утра 2 августа 2026?",
            id="range-through-morning-clock",
        ),
    ],
)
@pytest.mark.asyncio
async def test_every_natural_open_clock_boundary_fails_closed(question: str) -> None:
    kernel = _Kernel()
    llm = _OracleLLM(hostile_time=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert "незамкнутая граница времени" in context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.parametrize(
    "question",
    [
        "Что происходило с прошлой недели по эту неделю?",
        "Покажи события с прошлой недели до конца этой недели.",
        "Что происходило с начала прошлой недели по конец этой недели?",
    ],
)
@pytest.mark.asyncio
async def test_a_multiweek_boundary_fails_closed_instead_of_dropping_one_week(
    question: str,
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_time=True))
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question,
        None,
        tools,
        [],
        [],
        [],
        context,
    )

    assert kernel.calls == []
    assert "Не удалось однозначно вычислить" in context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Что происходило в течение двух недель?", id="genitive-two-weeks"),
        pytest.param("Какие планы в течение двух месяцев?", id="genitive-two-months"),
        pytest.param("Что происходило в течение полутора месяцев?", id="genitive-one-and-half"),
        pytest.param("Какие планы на обе недели?", id="both-weeks"),
        pytest.param("Что было за два с половиной месяца?", id="two-and-half-months"),
    ],
)
@pytest.mark.asyncio
async def test_an_inflected_bare_quantified_period_never_collapses_to_one_current_period(
    question: str,
) -> None:
    kernel = _Kernel()
    llm = _OracleLLM(hostile_time=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert "несколько отдельных моментов" in context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Что Пётр делал вчера?", id="actor-before-past-verb"),
        pytest.param("Чем Пётр занимался вчера?", id="actor-before-activity-verb"),
        pytest.param("Что было насчёт заявки К-7 вчера?", id="about-application-synonym"),
        pytest.param("Что было про проект Альфа вчера?", id="about-project-short-preposition"),
        pytest.param("Что вчера делал Пётр?", id="day-between-question-and-verb"),
        pytest.param("Что делал вчера Пётр?", id="day-between-verb-and-actor"),
        pytest.param("Что Пётр Иванов делал вчера?", id="two-token-actor-name"),
        pytest.param("Что было вокруг заявки К-7 вчера?", id="around-application"),
        pytest.param("Что было для проекта Альфа вчера?", id="for-project"),
    ],
)
@pytest.mark.asyncio
async def test_natural_subject_word_order_never_becomes_the_askers_broad_timeline(
    question: str,
) -> None:
    kernel = _Kernel()
    llm = _OracleLLM(hostile_time=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]
    messages: list[dict[str, Any]] = []

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, messages, [], [], context
    )

    assert kernel.calls == []
    assert llm.calls == []
    assert messages == []
    assert context.structural_answer == ""
    assert context.remainder_known is False
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))
    assert "memory_search" in _tool_names(tools)


@pytest.mark.parametrize(
    ("question", "expected_call"),
    [
        pytest.param(
            "Что происходило на прошедшей неделе?",
            (
                "what_happened",
                {
                    "since": "2026-07-27T00:00:00",
                    "until": "2026-08-02T23:59:59",
                    "limit": 40,
                },
            ),
            id="elapsed-week-synonym",
        ),
        pytest.param(
            "Какие планы на наступающем месяце?",
            ("upcoming", {"since": "2026-09-01", "until": "2026-09-30"}),
            id="coming-month-synonym",
        ),
        pytest.param(
            "Что было в прошедшую субботу?",
            (
                "what_happened",
                {
                    "since": "2026-08-01T00:00:00",
                    "until": "2026-08-01T23:59:59",
                    "limit": 40,
                },
            ),
            id="elapsed-same-weekday-synonym",
        ),
        pytest.param(
            "Какие планы на наступающую субботу?",
            ("upcoming", {"since": "2026-08-15", "until": "2026-08-15"}),
            id="coming-same-weekday-synonym",
        ),
        pytest.param(
            "Что было на истёкшей неделе?",
            (
                "what_happened",
                {
                    "since": "2026-07-27T00:00:00",
                    "until": "2026-08-02T23:59:59",
                    "limit": 40,
                },
            ),
            id="expired-week-synonym",
        ),
    ],
)
@pytest.mark.asyncio
async def test_ordinary_previous_and_next_period_synonyms_keep_their_direction(
    question: str,
    expected_call: tuple[str, dict[str, Any]],
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_time=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question,
        None,
        [_tool("what_happened"), _tool("upcoming")],
        [],
        [],
        [],
        context,
    )

    assert kernel.calls == [expected_call]


@pytest.mark.parametrize(
    "question",
    [
        pytest.param(
            "Что было [тогда](tg://resolve?domain=2025-01-01)?",
            id="telegram-scheme",
        ),
        pytest.param(
            "Что было [тогда](https://example.invalid/path_(2025-01-01))?",
            id="balanced-parentheses-in-destination",
        ),
    ],
)
@pytest.mark.asyncio
async def test_every_markdown_link_destination_is_hidden_from_temporal_classification(
    question: str,
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_time=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))

    assert _classification_text(question) == "Что было тогда?"
    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question,
        None,
        [_tool("what_happened"), _tool("upcoming")],
        [],
        [],
        [],
        context,
    )

    assert kernel.calls == []


@pytest.mark.parametrize(
    "question",
    [
        pytest.param(
            "Сколько всего файлов вместе с категориями в моём архиве?",
            id="files-together-with-unknown",
        ),
        pytest.param(
            "Сколько всего файлов наряду с категориями в моём архиве?",
            id="files-alongside-unknown",
        ),
        pytest.param(
            "Сколько всего файлов либо категорий в моём архиве?",
            id="files-or-unknown",
        ),
        pytest.param(
            "Сколько всего файлов/категорий в моём архиве?",
            id="files-slash-unknown",
        ),
        pytest.param(
            "Сколько всего файлов да категорий в моём архиве?",
            id="files-and-unknown-colloquial",
        ),
    ],
)
@pytest.mark.asyncio
async def test_every_natural_mixed_metric_connector_refuses_a_partial_exact_count(
    question: str,
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_count=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("kg_stats"), _tool("memory_search")]

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert "Не удалось однозначно определить" in context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert "kg_stats" not in _tool_names(tools)


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Сколько всего PDF-файлов в моём архиве?", id="pdf-file-subset"),
        pytest.param(
            "Сколько всего файлов категории PDF в моём архиве?",
            id="named-file-category-subset",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_qualified_file_subset_never_publishes_the_whole_file_total(question: str) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_count=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("kg_stats"), _tool("memory_search")]

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert STAT_ANSWERS["files"] not in context.structural_answer
    assert "kg_stats" not in _tool_names(tools)


@pytest.mark.parametrize(
    "question",
    [
        pytest.param(
            "Сколько всего документов по проекту Альфа в моём архиве?",
            id="knowledge-by-project",
        ),
        pytest.param(
            "Сколько всего материалов за июль в моём архиве?",
            id="raw-materials-by-month",
        ),
        pytest.param(
            "Сколько всего связей по теме Альфа в моём графе?",
            id="relations-by-topic",
        ),
        pytest.param(
            "Сколько всего сущностей типа человек в моём графе?",
            id="entities-by-type",
        ),
        pytest.param(
            "Сколько всего файлов с тегом Альфа в моём архиве?",
            id="files-by-tag",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_filtered_known_metric_never_publishes_the_unfiltered_archive_total(
    question: str,
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_count=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("kg_stats"), _tool("memory_search")]

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert "kg_stats" not in _tool_names(tools)


@pytest.mark.parametrize(
    ("question", "allowed_call"),
    [
        pytest.param(
            "Что было седьмого августа?",
            (
                "what_happened",
                {
                    "since": "2026-08-07T00:00:00",
                    "until": "2026-08-07T23:59:59",
                    "limit": 40,
                },
            ),
            id="one-spoken-ordinal",
        ),
        pytest.param(
            "Что было первого и второго августа?",
            None,
            id="two-spoken-ordinals",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_spoken_ordinal_day_never_silently_widens_to_the_whole_month(
    question: str,
    allowed_call: tuple[str, dict[str, Any]] | None,
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_time=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    if allowed_call is None:
        assert kernel.calls == []
    else:
        assert kernel.calls in ([], [allowed_call])
    if not kernel.calls:
        assert context.structural_answer
        assert context.remainder_known is True
        assert context.open_remainder == ""


def test_an_accepted_temporal_payload_never_reaches_the_model_as_mid_json_truncation() -> None:
    items = [
        {
            "what": "Ж" * 240,
            "on": "2026-08-08T12:00:00",
            "when": "сегодня",
            "at": "12:00",
            "mine": False,
        }
        for _ in range(40)
    ]
    payload = {
        "shown": 40,
        "items": items,
        "total": 40,
        "days": 1,
        "asked_about": {"since": "2026-08-08", "until": "2026-08-08"},
        "note": "",
    }
    coherent = _temporal_payload_is_coherent("upcoming", payload)
    rendered = ToolResult("upcoming", True, payload).to_llm_message()

    assert not (coherent and rendered.endswith("… (truncated)"))


def test_an_offset_item_outside_the_echoed_local_hour_is_not_accepted_as_naive_wall_time() -> None:
    payload = {
        "shown": 1,
        "items": [
            {
                "what": "synthetic offset event",
                "on": "2026-08-08T00:30:00-05:00",
                "when": "сегодня",
                "at": "00:30",
                "mine": False,
            }
        ],
        "total": 1,
        "days": 1,
        "asked_about": {
            "since": "2026-08-08T00:00:00",
            "until": "2026-08-08T00:59:59",
            "timezone": "Europe/Moscow",
        },
        "note": "",
    }

    assert _temporal_payload_is_coherent("upcoming", payload) is False


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Что было вчера в 8?", id="numeric-bare-hour"),
        pytest.param("Что было вчера в восемь?", id="spoken-bare-hour"),
    ],
)
@pytest.mark.asyncio
async def test_a_bare_exact_clock_never_silently_widens_to_the_whole_day(question: str) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_time=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]
    exact_call = (
        "what_happened",
        {
            "since": "2026-08-07T08:00:00",
            "until": "2026-08-07T08:59:59",
            "limit": 40,
        },
    )

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls in ([], [exact_call])
    if not kernel.calls:
        assert context.structural_answer
        assert context.remainder_known is True
        assert context.open_remainder == ""


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Что было вчера около восьми?", id="approximate-spoken-clock"),
        pytest.param("Что было вчера примерно в 8?", id="approximate-numeric-clock"),
        pytest.param("Что было вчера утром?", id="morning-part-of-day"),
        pytest.param("Что было вечером 7 августа?", id="evening-part-of-day"),
    ],
)
@pytest.mark.asyncio
async def test_an_unsupported_subday_qualifier_fails_closed_instead_of_reading_the_whole_day(
    question: str,
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_time=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == ""


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Что было в начале июля?", id="beginning-of-month"),
        pytest.param("Какие планы на конец августа?", id="end-of-month"),
    ],
)
@pytest.mark.asyncio
async def test_a_vague_part_of_a_calendar_period_never_widens_to_the_full_period(
    question: str,
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_time=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == ""


@pytest.mark.parametrize(
    ("question", "metric"),
    [
        pytest.param("Сколько всего файлов в базе знаний?", "files", id="files-in-knowledge-base"),
        pytest.param(
            "Сколько всего сущностей в графе знаний?",
            "entities",
            id="entities-in-knowledge-graph",
        ),
        pytest.param(
            "Сколько всего файлов в хранилище документов?",
            "files",
            id="files-in-document-storage",
        ),
        pytest.param(
            "Сколько всего связей между сущностями в моём графе?",
            "relations",
            id="relations-between-entities",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_corpus_descriptor_noun_does_not_turn_one_requested_metric_into_all_stats(
    question: str,
    metric: str,
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_count=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("kg_stats"), _tool("memory_search")]

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == [("kg_stats", {})]
    assert context.structural_answer == STAT_ANSWERS[metric]
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert "kg_stats" not in _tool_names(tools)


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Не показывай, что было вчера.", id="explicit-negative-command"),
        pytest.param("Я не спрашиваю, что было вчера.", id="explicit-negative-mention"),
    ],
)
@pytest.mark.asyncio
async def test_a_negated_timeline_intent_never_reads_or_publishes_the_timeline(question: str) -> None:
    kernel = _Kernel()
    llm = _OracleLLM(hostile_time=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]
    messages: list[dict[str, Any]] = []

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, messages, [], [], context
    )

    assert kernel.calls == []
    assert llm.calls == []
    assert messages == []
    assert context.structural_answer == ""
    assert context.remainder_known is False
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


@pytest.mark.parametrize(
    "question",
    [
        pytest.param(
            "Не называй, сколько всего файлов в моём архиве.",
            id="explicit-negative-command",
        ),
        pytest.param(
            "Я не спрашиваю, сколько всего файлов в архиве.",
            id="explicit-negative-mention",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_negated_count_intent_never_reads_or_publishes_archive_stats(question: str) -> None:
    kernel = _Kernel()
    llm = _OracleLLM(hostile_count=True)
    runtime = _runtime(kernel, llm)
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("kg_stats"), _tool("memory_search")]
    messages: list[dict[str, Any]] = []

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        question, None, tools, messages, [], [], context
    )

    assert kernel.calls == []
    assert llm.calls == []
    assert messages == []
    assert context.structural_answer == ""
    assert context.remainder_known is False
    assert "kg_stats" not in _tool_names(tools)


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Что происходило последние -2 дня?", id="negative-duration"),
        pytest.param("Что происходило последние 2.5 дня?", id="decimal-duration"),
        pytest.param("Что происходило 2.5 дня назад?", id="decimal-relative-day"),
        pytest.param("Что происходило последние 1/2 дня?", id="fractional-duration"),
    ],
)
@pytest.mark.asyncio
async def test_a_non_integral_or_signed_day_quantity_never_routes_from_a_numeric_tail(
    question: str,
) -> None:
    kernel = _Kernel()
    runtime = _runtime(kernel, _OracleLLM(hostile_time=True))
    context = AgentContext(conversation_id="synthetic", user_id="synthetic", outward_verdict=("архив", None))
    tools = [_tool("what_happened"), _tool("upcoming")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question, None, tools, [], [], [], context
    )

    assert kernel.calls == []
    assert context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert {"what_happened", "upcoming"}.isdisjoint(_tool_names(tools))


def test_an_impossible_upcoming_item_clock_cannot_pass_a_day_window_as_an_all_day_item() -> None:
    payload = {
        "shown": 1,
        "items": [
            {
                "what": "synthetic impossible-clock event",
                "on": "2026-08-09",
                "when": "завтра",
                "at": "99:99",
                "mine": False,
            }
        ],
        "total": 1,
        "days": 1,
        "asked_about": {
            "since": "2026-08-09",
            "until": "2026-08-09",
            "timezone": "Europe/Moscow",
        },
        "note": "",
    }

    assert _temporal_payload_is_coherent("upcoming", payload) is False
