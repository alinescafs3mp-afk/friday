"""Novel adversarial contracts for closed temporal routing.

These probes cover lexical and timezone boundaries that were not present in
the frozen Package B holdout.  They deliberately use only synthetic kernels:
no private archive content or external service is involved.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _has_temporal_subject_filter,
    _temporal_payload_is_coherent,
)
from friday.execution_kernel import ToolResult
from friday.time_routing import (
    TimeIntent,
    TimeWindow,
    build_time_window,
    fast_time_intent,
    is_temporal_read_request,
    temporal_routing_text,
)

FIXED_TODAY = date(2026, 8, 8)
FIXED_NOW = datetime(2026, 8, 8, 10, 0, 0)
_LIVE_A = json.loads(
    (Path(__file__).parent / "fixtures" / "synthetic_live_battery_a.json").read_text(encoding="utf-8")
)
_LIVE_A_P02 = next(item for item in _LIVE_A["passes"] if item["pass_id"] == "A-P02")["questions"]
_LIVE_B = json.loads(
    (Path(__file__).parent / "fixtures" / "synthetic_live_battery_b.json").read_text(encoding="utf-8")
)
_LIVE_B_P02 = next(item for item in _LIVE_B["passes"] if item["pass_id"] == "B-P02")["questions"]


def _tool(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


class _NoModel:
    enabled = False


class _TimezoneKernel:
    def __init__(self, *, echoed_timezone: str = "Europe/Moscow") -> None:
        self.echoed_timezone = echoed_timezone
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, tool: str, params: dict[str, Any], actor: Any = None) -> ToolResult:  # noqa: ARG002
        self.calls.append((tool, dict(params)))
        since = str(params["since"])
        until = str(params["until"])
        if tool == "what_happened":
            return ToolResult(
                tool,
                True,
                {
                    "understood": True,
                    "asked_about": {
                        "since": since,
                        "until": until,
                        "timezone": self.echoed_timezone,
                    },
                    "shown": 0,
                    "events": [],
                    "total": {"messages": 0, "documents": 0, "total": 0},
                    "coverage": {
                        "complete": True,
                        "strategy": "complete",
                        "includes_latest": True,
                    },
                },
            )
        assert tool == "upcoming"
        return ToolResult(
            tool,
            True,
            {
                "understood": True,
                "asked_about": {
                    "since": since,
                    "until": until,
                    "timezone": self.echoed_timezone,
                },
                "shown": 0,
                "items": [],
                "total": 0,
                "days": (date.fromisoformat(until[:10]) - date.fromisoformat(since[:10])).days + 1,
                "note": "В синтетическом интервале ничего не запланировано.",
            },
        )


def _runtime(kernel: _TimezoneKernel) -> AgentRuntime:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = kernel
    runtime.llm = _NoModel()
    runtime.settings = SimpleNamespace(local_timezone="Europe/Moscow")
    runtime._local_today = lambda: FIXED_TODAY  # type: ignore[method-assign]  # noqa: SLF001
    runtime._local_now = lambda: FIXED_NOW  # type: ignore[method-assign]  # noqa: SLF001
    return runtime


@pytest.mark.parametrize(
    ("question", "day"),
    [(question, index) for index, question in enumerate(_LIVE_A_P02, start=1)],
)
def test_every_frozen_a_p02_event_question_has_one_code_owned_past_day(
    question: str,
    day: int,
) -> None:
    intent = fast_time_intent(question, today=FIXED_TODAY)

    assert intent == TimeIntent("past", "single_day")
    assert build_time_window(question, intent, today=FIXED_TODAY) == TimeWindow(
        f"2024-05-{day:02d}",
        f"2024-05-{day:02d}",
    )


@pytest.mark.parametrize("question,day", [(q, i) for i, q in enumerate(_LIVE_B_P02, start=1)])
@pytest.mark.parametrize("outward_kind", ["архив", "знание"])
@pytest.mark.asyncio
async def test_b_event_index_requests_execute_the_named_window(question, day, outward_kind) -> None:
    # B08 names midnight: preserve its hour instead of widening it to the day
    # just because the other nineteen prompts ask for whole calendar days.
    midnight = day == 8
    intent = fast_time_intent(question, today=FIXED_TODAY)
    assert intent == TimeIntent("past", "single_hour" if midnight else "single_day")
    expected_day = f"2024-06-{day:02d}"
    assert build_time_window(question, intent, today=FIXED_TODAY) == TimeWindow(
        expected_day + ("T00:00:00" if midnight else ""),
        expected_day + ("T00:59:59" if midnight else ""),
    )
    kernel = _TimezoneKernel()
    runtime = _runtime(kernel)
    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question,
        None,
        [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")],
        [],
        [],
        [],
        AgentContext(
            conversation_id="synthetic-b",
            user_id="synthetic",
            outward_verdict=(outward_kind, None),
        ),
    )
    assert kernel.calls == [
        (
            "what_happened",
            {
                "since": expected_day + "T00:00:00",
                "until": expected_day + ("T00:59:59" if midnight else "T23:59:59"),
                "limit": 40,
            },
        )
    ]


@pytest.mark.parametrize(
    "question",
    [
        "Не ищи событие за 8 июня 2024 года.",
        "Не ищи по тексту: не показывай событие за 8 июня 2024 года.",
        "Не ищи по тексту: покажи событие за 8 июня 2024 года, но не показывай его.",
        "Не ищи по тексту: покажи событие за 8 июня 2024 года; не ищите его.",
        "Не ищи по тексту: покажи событие за 8 июня 2024 года, но не надо показывать.",
        "Не ищи по тексту: покажи событие за 8 июня 2024 года; не нужно называть.",
        "Не ищи по тексту: покажи событие за 8 июня 2024 года, я не хочу перечислять.",
        "Не ищи по тексту: перескажи документ за 8 июня 2024 года.",
        "Не ищи по тексту: прочитай событие в документе от 8 июня 2024 года.",
        "Не ищи по тексту: прочитай событие Олега за 8 июня 2024 года.",
        "Отдели дату документа от valid-time: что произошло у Олега 8 июня 2024 года?",
        "Переведи: «Не ищи по тексту: прочитай событие за 8 июня 2024 года».",
        "Восстанови удалённое событие на 8 июня 2024 года.",
        "Верни событие в календарь на 8 июня 2024 года.",
        "В суточном окне 8 июня 2024 года должен быть один эпизод.",
        "Проверь синтаксис temporal index в документе за 8 июня 2024 года.",
        "Сверь дату документа 8 июня 2024 года: что произошло?",
    ],
)
@pytest.mark.asyncio
async def test_event_index_method_prefix_does_not_grant_unrelated_authority(question) -> None:
    kernel = _TimezoneKernel()
    runtime = _runtime(kernel)
    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question,
        None,
        [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")],
        [],
        [],
        [],
        AgentContext(
            conversation_id="synthetic-negative",
            user_id="synthetic",
            outward_verdict=("архив", None),
        ),
    )
    assert kernel.calls == []


@pytest.mark.parametrize(
    "ending",
    [
        ", но не показывай его.",
        "; не ищите его.",
        ", но не надо показывать.",
        "; не нужно называть.",
        ", я не хочу перечислять.",
    ],
)
def test_method_prefix_keeps_later_countermand_visible(ending):
    question = "Не ищи по тексту: покажи событие за 8 июня 2024 года" + ending
    assert temporal_routing_text(question) == question
    assert fast_time_intent(question, today=FIXED_TODAY) is None


@pytest.mark.parametrize("verb", ["случится", "случатся", "произойдёт", "произойдут"])
@pytest.mark.parametrize("subject", ["", "в календарной истории "])
@pytest.mark.asyncio
async def test_future_morphology_cannot_be_taken_for_past(verb, subject):
    question = f"Что {verb} {subject}10 сентября 2031 года?"
    intent = fast_time_intent(question, today=FIXED_TODAY)
    assert intent == TimeIntent("future", "single_day")
    assert build_time_window(question, intent, today=FIXED_TODAY) == TimeWindow("2031-09-10", "2031-09-10")
    kernel = _TimezoneKernel()
    runtime = _runtime(kernel)
    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question,
        None,
        [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")],
        [],
        [],
        [],
        AgentContext(conversation_id="future-probe", user_id="synthetic", outward_verdict=("архив", None)),
    )
    assert kernel.calls == [
        (
            "upcoming",
            {
                "since": "2031-09-10",
                "until": "2031-09-10",
            },
        )
    ]


@pytest.mark.parametrize(
    "question",
    [
        "Что случится с сервером 10 сентября 2031 года?",
        "Что произойдёт с рейсом 10 сентября 2031 года?",
        "Что случится с Олегом 10 сентября 2031 года?",
        "Что случится с сервером в календарной истории 10 сентября 2031 года?",
    ],
)
@pytest.mark.asyncio
async def test_future_morphology_does_not_expand_personal_calendar_authority(question):
    assert is_temporal_read_request(question) is False
    assert fast_time_intent(question, today=FIXED_TODAY) is None
    kernel = _TimezoneKernel()
    runtime = _runtime(kernel)

    async def forbidden_arbiter(*args, **kwargs):
        del args, kwargs
        raise AssertionError("non-calendar subject reached the temporal arbiter")

    runtime._time_intent_by_arbiter = forbidden_arbiter  # type: ignore[method-assign]  # noqa: SLF001
    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question,
        None,
        [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")],
        [],
        [],
        [],
        AgentContext(
            conversation_id="future-negative",
            user_id="synthetic",
            outward_verdict=("архив", None),
        ),
    )
    assert kernel.calls == []


@pytest.mark.parametrize("verb", ["случился", "случилась", "случилось", "случились"])
def test_past_morphology_still_selects_the_named_past_day(verb):
    question = f"Что {verb} 8 июня 2024 года?"
    intent = fast_time_intent(question, today=FIXED_TODAY)
    assert intent == TimeIntent("past", "single_day")
    assert build_time_window(question, intent, today=FIXED_TODAY) == TimeWindow("2024-06-08", "2024-06-08")


@pytest.mark.parametrize(
    "question,filtered",
    [
        ("Изолируйте интервал 2 мая 2024 года и назовите попавшее в него событие.", False),
        ("В суточном окне 2 мая 2024 года есть один факт; назовите его.", False),
        ("Назови его событие за 2 мая 2024 года.", True),
        ("Прочитай событие Олега за 2 мая 2024 года.", True),
        ("Прочитай событие Олега Иванова за 2 мая 2024 года.", True),
        ("Изолируй суточный интервал 2 мая 2024 года и назови попавшее в него событие Олега.", True),
        ("В суточном окне 2 мая 2024 года есть один факт; назови его и расскажи про его планы.", True),
    ],
)
def test_dated_event_anaphora_does_not_erase_a_person_filter(question, filtered):
    assert _has_temporal_subject_filter(question) is filtered


@pytest.mark.asyncio
async def test_every_frozen_a_p02_event_question_executes_the_exact_past_tool() -> None:
    for day, question in enumerate(_LIVE_A_P02, start=1):
        kernel = _TimezoneKernel()
        runtime = _runtime(kernel)

        await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
            question,
            None,
            [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")],
            [],
            [],
            [],
            AgentContext(
                conversation_id="synthetic",
                user_id="synthetic",
                outward_verdict=("архив", None),
            ),
        )

        assert kernel.calls == [
            (
                "what_happened",
                {
                    "since": f"2024-05-{day:02d}T00:00:00",
                    "until": f"2024-05-{day:02d}T23:59:59",
                    "limit": 40,
                },
            )
        ]


@pytest.mark.parametrize(
    "question",
    [
        pytest.param(
            "Переведи фразу «Какое событие было записано 1 мая 2024 года?».",
            id="quoted-event-question",
        ),
        pytest.param(
            "Покажи событие, описанное в документе от 1 мая 2024 года.",
            id="dated-document",
        ),
        pytest.param(
            "Какое погодное событие было 1 мая 2024 года?",
            id="weather-event",
        ),
        pytest.param(
            "Запиши событие в календарь на 1 мая 2024 года.",
            id="write-action",
        ),
        pytest.param(
            "Синтетическое событие было записано 1 мая 2024 года.",
            id="declaration",
        ),
    ],
)
def test_an_absolute_date_without_a_personal_timeline_read_speech_act_stays_closed(
    question: str,
) -> None:
    assert fast_time_intent(question, today=FIXED_TODAY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "Переведи фразу «Какое событие было записано 1 мая 2024 года?».",
        "Покажи событие, описанное в документе от 1 мая 2024 года.",
        "Какое погодное событие было 1 мая 2024 года?",
        "Запиши событие в календарь на 1 мая 2024 года.",
        "Синтетическое событие было записано 1 мая 2024 года.",
    ],
)
async def test_absolute_date_controls_never_execute_either_timeline_tool(question: str) -> None:
    kernel = _TimezoneKernel()
    runtime = _runtime(kernel)
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question,
        None,
        tools,
        [],
        [],
        [],
        AgentContext(
            conversation_id="synthetic",
            user_id="synthetic",
            outward_verdict=("архив", None),
        ),
    )

    assert kernel.calls == []
    assert [str((tool.get("function") or {}).get("name") or "") for tool in tools] == ["memory_search"]


def test_a_neutral_absolute_event_request_derives_future_from_the_local_day() -> None:
    question = "Какое событие стоит во временной линии 10 сентября 2031 года?"
    intent = fast_time_intent(question, today=FIXED_TODAY)

    assert intent == TimeIntent("future", "single_day")
    assert build_time_window(question, intent, today=FIXED_TODAY) == TimeWindow(
        "2031-09-10",
        "2031-09-10",
    )


def test_the_document_noun_act_does_not_swallow_the_unrelated_word_activity() -> None:
    assert fast_time_intent(
        "Какое событие активности было записано 1 мая 2024 года?",
        today=FIXED_TODAY,
    ) == TimeIntent("past", "single_day")
    assert (
        fast_time_intent(
            "Покажи событие из акта от 1 мая 2024 года.",
            today=FIXED_TODAY,
        )
        is None
    )


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Что было вчера в 10:00 UTC?", id="utc"),
        pytest.param("Что было вчера в 10:00 по времени Нью-Йорка?", id="named-foreign-zone"),
        pytest.param("Что было вчера в 10:00 по токийскому времени?", id="generic-named-zone"),
        pytest.param("Что было вчера в 10:00 MSK?", id="msk-abbreviation"),
        pytest.param("Что было вчера в 10:00 CET?", id="cet-abbreviation"),
    ],
)
@pytest.mark.asyncio
async def test_an_explicit_input_timezone_is_not_silently_reinterpreted_as_local_time(
    question: str,
) -> None:
    kernel = _TimezoneKernel()
    runtime = _runtime(kernel)
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )
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

    assert kernel.calls == []
    assert {"what_happened", "upcoming"}.isdisjoint(
        str((tool.get("function") or {}).get("name") or "") for tool in tools
    )
    assert any("часов" in str(message.get("content") or "").casefold() for message in messages)
    assert "часовой пояс" in context.structural_answer.casefold()


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("«Что было вчера?» — цитата для макета.", id="quoted-question"),
        pytest.param("Переведи на английский: «Что было вчера?»", id="translation"),
        pytest.param("Фраза «что было вчера?» приведена как пример.", id="declarative-example"),
        pytest.param("Я помню, что было вчера.", id="reported-speech"),
    ],
)
def test_quoted_declarative_or_translation_text_is_not_a_timeline_read(text: str) -> None:
    assert fast_time_intent(text) is None


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Я помню, что было вчера.", id="reported-speech"),
        pytest.param("Переведи фразу «Что было вчера?» на английский.", id="translation"),
        pytest.param("Фраза «что было вчера» состоит из трёх слов.", id="declarative-example"),
    ],
)
@pytest.mark.asyncio
async def test_non_request_temporal_text_preserves_the_ordinary_answer_path(text: str) -> None:
    kernel = _TimezoneKernel()
    runtime = _runtime(kernel)
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        text,
        None,
        tools,
        [],
        [],
        [],
        context,
    )

    assert kernel.calls == []
    assert context.structural_answer == ""
    assert context.remainder_known is False
    assert [str((tool.get("function") or {}).get("name") or "") for tool in tools] == ["memory_search"]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Что было сделано посредством API?", id="posredstvom-is-not-wednesday"),
        pytest.param("Что было со средством измерения?", id="instrument-is-not-wednesday"),
        pytest.param("Что было с майнингом?", id="mining-is-not-may"),
        pytest.param("Что было с 5 маяками?", id="beacons-are-not-may"),
        pytest.param("Что было в 15-е издание включено?", id="ordinal-adjective-is-not-a-date"),
    ],
)
def test_calendar_stems_do_not_match_inside_unrelated_words(text: str) -> None:
    assert fast_time_intent(text) is None
    assert build_time_window(text, TimeIntent("past", "single_day"), today=FIXED_TODAY) is None


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Что было посредством API?", id="posredstvom"),
        pytest.param("Что было со средством измерения?", id="instrument"),
        pytest.param("Что было с майнингом?", id="mining"),
        pytest.param("Что было с 5 маяками?", id="beacons"),
        pytest.param("Что было в 15-е издание включено?", id="ordinal-adjective"),
    ],
)
@pytest.mark.asyncio
async def test_calendar_lexeme_collisions_preserve_the_ordinary_answer_path(text: str) -> None:
    kernel = _TimezoneKernel()
    runtime = _runtime(kernel)
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        text,
        None,
        tools,
        [],
        [],
        [],
        context,
    )

    assert kernel.calls == []
    assert context.structural_answer == ""
    assert [str((tool.get("function") or {}).get("name") or "") for tool in tools] == ["memory_search"]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Что известно о планете завтра?", id="planet-is-not-a-plan"),
        pytest.param("Покажи характеристики планшета на следующей неделе.", id="tablet-is-not-a-plan"),
        pytest.param("Что известно про планету Марс?", id="planet-without-date"),
        pytest.param("Какие модели планшетов есть?", id="tablet-without-date"),
    ],
)
def test_plan_prefix_inside_an_unrelated_noun_is_not_a_future_calendar_intent(text: str) -> None:
    assert fast_time_intent(text) is None


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Что известно про планету Марс?", id="planet"),
        pytest.param("Какие модели планшетов есть?", id="tablet"),
    ],
)
@pytest.mark.asyncio
async def test_plan_prefix_collisions_preserve_the_ordinary_answer_path(text: str) -> None:
    kernel = _TimezoneKernel()
    runtime = _runtime(kernel)
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        text,
        None,
        [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")],
        [],
        [],
        [],
        context,
    )

    assert kernel.calls == []
    assert context.structural_answer == ""


@pytest.mark.parametrize(
    ("text", "intent", "expected"),
    [
        pytest.param(
            "Что было с 1 июля по 3 августа 2025?",
            TimeIntent("past", "explicit_range"),
            TimeWindow("2025-07-01", "2025-08-03"),
            id="same-year-past-range",
        ),
        pytest.param(
            "Что было с 29 декабря по 2 января 2025 года?",
            TimeIntent("past", "explicit_range"),
            TimeWindow("2024-12-29", "2025-01-02"),
            id="past-range",
        ),
        pytest.param(
            "Что запланировано с 29 декабря по 2 января 2027 года?",
            TimeIntent("future", "explicit_range"),
            TimeWindow("2026-12-29", "2027-01-02"),
            id="future-range",
        ),
    ],
)
def test_an_explicit_year_on_the_right_range_endpoint_binds_the_left_endpoint(
    text: str,
    intent: TimeIntent,
    expected: TimeWindow,
) -> None:
    assert build_time_window(text, intent, today=FIXED_TODAY) == expected


@pytest.mark.asyncio
async def test_a_right_endpoint_year_reaches_the_real_prefetch_arguments() -> None:
    kernel = _TimezoneKernel()
    runtime = _runtime(kernel)
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        "Что было с 1 июля по 3 августа 2025?",
        None,
        [_tool("what_happened"), _tool("upcoming")],
        [],
        [],
        [],
        context,
    )

    assert kernel.calls == [
        (
            "what_happened",
            {
                "since": "2025-07-01T00:00:00",
                "until": "2025-08-03T23:59:59",
                "limit": 40,
            },
        )
    ]
    # Verified calendar results now publish from code on ordinary turns too.
    # With no remainder model, the existing fail-closed notice is retained;
    # the original temporal request must never be sent to free synthesis.
    assert context.structural_answer.startswith(
        "В проверенной личной ленте за указанный интервал событий нет."
    )
    assert context.remainder_known is True
    assert context.open_remainder == ""


@pytest.mark.asyncio
async def test_a_payload_timezone_must_match_the_runtime_timezone() -> None:
    kernel = _TimezoneKernel(echoed_timezone="America/New_York")
    runtime = _runtime(kernel)
    messages: list[dict[str, Any]] = []
    evidence: list[dict[str, str]] = []

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        "Какие планы на завтра?",
        None,
        [_tool("what_happened"), _tool("upcoming")],
        messages,
        [],
        evidence,
    )

    assert kernel.calls == [("upcoming", {"since": "2026-08-09", "until": "2026-08-09"})]
    assert any("проверить календарь" in str(message.get("content") or "").casefold() for message in messages)
    assert evidence == []


def test_a_valid_but_foreign_payload_timezone_is_not_its_own_authority() -> None:
    payload = {
        "understood": True,
        "asked_about": {
            "since": "2026-08-07T10:00:00",
            "until": "2026-08-07T10:59:59",
            "timezone": "UTC",
        },
        "shown": 1,
        "total": {"messages": 1, "documents": 0, "total": 1},
        "events": [
            {
                "kind": "message",
                "at": "2026-08-07T10:30:00+00:00",
                "text": "synthetic",
            }
        ],
        "coverage": {"complete": True, "strategy": "complete", "includes_latest": True},
    }

    assert (
        _temporal_payload_is_coherent(
            "what_happened",
            payload,
            expected_timezone="Europe/Moscow",
        )
        is False
    )


def test_a_payload_in_the_configured_timezone_normalizes_offset_items_before_comparison() -> None:
    payload = {
        "understood": True,
        "asked_about": {
            "since": "2026-08-07T10:00:00",
            "until": "2026-08-07T10:59:59",
            "timezone": "Europe/Moscow",
        },
        "shown": 1,
        "total": {"messages": 1, "documents": 0, "total": 1},
        "events": [
            {
                "kind": "message",
                "at": "2026-08-07T07:30:00+00:00",
                "text": "synthetic",
            }
        ],
        "coverage": {"complete": True, "strategy": "complete", "includes_latest": True},
    }

    assert _temporal_payload_is_coherent(
        "what_happened",
        payload,
        expected_timezone="Europe/Moscow",
    )


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("А что было вчера?", id="leading-conjunction"),
        pytest.param("Скажи, что было вчера?", id="say-imperative"),
        pytest.param("Можно узнать, что было вчера?", id="permission-frame"),
        pytest.param("Мне интересно, что было вчера?", id="interest-frame"),
    ],
)
@pytest.mark.asyncio
async def test_a_legitimate_indirect_speech_act_still_reads_the_exact_timeline(question: str) -> None:
    kernel = _TimezoneKernel()
    runtime = _runtime(kernel)
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question,
        None,
        [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")],
        [],
        [],
        [],
        context,
    )

    assert kernel.calls == [
        (
            "what_happened",
            {
                "since": "2026-08-07T00:00:00",
                "until": "2026-08-07T23:59:59",
                "limit": 40,
            },
        )
    ]
    # Verified calendar results now publish from code on ordinary turns too.
    # With no remainder model, the existing fail-closed notice is retained;
    # the original temporal request must never be sent to free synthesis.
    assert context.structural_answer.startswith(
        "В проверенной личной ленте за указанный интервал событий нет."
    )
    assert context.remainder_known is True
    assert context.open_remainder == ""


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Покажи фразу «что было вчера».", id="show-quoted-phrase"),
        pytest.param(
            "Расскажи, как переводится «что было вчера».",
            id="explain-translation",
        ),
    ],
)
@pytest.mark.asyncio
async def test_an_allowed_leading_verb_does_not_turn_metalinguistic_text_into_a_read(
    question: str,
) -> None:
    kernel = _TimezoneKernel()
    runtime = _runtime(kernel)
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
    assert context.structural_answer == ""
    assert [str((tool.get("function") or {}).get("name") or "") for tool in tools] == ["memory_search"]


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Что было в среде разработки?", id="development-environment"),
        pytest.param("Что было при воскресении героя?", id="resurrection"),
        pytest.param("Что было с романом «Август»?", id="quoted-month-title"),
    ],
)
@pytest.mark.asyncio
async def test_exact_calendar_homonyms_preserve_the_ordinary_answer_path(question: str) -> None:
    kernel = _TimezoneKernel()
    runtime = _runtime(kernel)
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
    assert context.structural_answer == ""
    assert [str((tool.get("function") or {}).get("name") or "") for tool in tools] == ["memory_search"]


@pytest.mark.parametrize(
    ("question", "expected_since", "expected_until"),
    [
        pytest.param(
            "Что было в среду?",
            "2026-08-05T00:00:00",
            "2026-08-05T23:59:59",
            id="wednesday",
        ),
        pytest.param(
            "Что было в воскресенье?",
            "2026-08-02T00:00:00",
            "2026-08-02T23:59:59",
            id="sunday",
        ),
    ],
)
@pytest.mark.asyncio
async def test_unambiguous_weekday_forms_remain_calendar_reads(
    question: str,
    expected_since: str,
    expected_until: str,
) -> None:
    kernel = _TimezoneKernel()
    runtime = _runtime(kernel)

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        question,
        None,
        [_tool("what_happened"), _tool("upcoming")],
        [],
        [],
        [],
    )

    assert kernel.calls == [
        (
            "what_happened",
            {"since": expected_since, "until": expected_until, "limit": 40},
        )
    ]


@pytest.mark.asyncio
async def test_friday_as_an_explicit_subject_never_becomes_the_askers_weekday() -> None:
    kernel = _TimezoneKernel()
    runtime = _runtime(kernel)
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        outward_verdict=("архив", None),
    )
    tools = [_tool("what_happened"), _tool("upcoming"), _tool("memory_search")]

    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        "Что было у Пятницы в календаре?",
        None,
        tools,
        [],
        [],
        [],
        context,
    )

    assert kernel.calls == []
    assert context.structural_answer == ""
    assert [str((tool.get("function") or {}).get("name") or "") for tool in tools] == ["memory_search"]


@pytest.mark.parametrize(
    ("question", "intent", "expected"),
    [
        pytest.param(
            "Что было с 29 февраля по 1 марта 2024?",
            TimeIntent("past", "explicit_range"),
            TimeWindow("2024-02-29", "2024-03-01"),
            id="past-leap-day",
        ),
        pytest.param(
            "Что запланировано с 29 февраля по 1 марта 2028?",
            TimeIntent("future", "explicit_range"),
            TimeWindow("2028-02-29", "2028-03-01"),
            id="future-leap-day",
        ),
    ],
)
def test_a_right_endpoint_leap_year_validates_the_implicit_left_day(
    question: str,
    intent: TimeIntent,
    expected: TimeWindow,
) -> None:
    assert build_time_window(question, intent, today=FIXED_TODAY) == expected


@pytest.mark.parametrize(
    "question",
    [
        "Что происходило с 1 августа 2026 по 10:00 2 августа 2026?",
        "Что происходило с 1 августа 2026 по 10 часов 2 августа 2026?",
        "Что происходило с 1 августа 2026 по 10 утра 2 августа 2026?",
    ],
)
def test_an_explicit_range_never_discards_its_clock_endpoint(question: str) -> None:
    intent = fast_time_intent(question)

    assert intent == TimeIntent("past", "explicit_range")
    assert build_time_window(question, intent, today=FIXED_TODAY) is None


@pytest.mark.parametrize(
    "question",
    [
        "Что происходило с прошлой недели по эту неделю?",
        "Покажи события с прошлой недели до конца этой недели.",
        "Что происходило с начала прошлой недели по конец этой недели?",
    ],
)
def test_a_multiweek_range_never_narrows_to_one_named_week(question: str) -> None:
    intent = fast_time_intent(question)

    assert intent == TimeIntent("past", "explicit_range")
    assert build_time_window(question, intent, today=FIXED_TODAY) is None
    assert (
        build_time_window(
            question,
            TimeIntent("past", "calendar_week"),
            today=FIXED_TODAY,
        )
        is None
    )


def _past_payload_for_new_york(*, at: str) -> dict[str, Any]:
    return {
        "understood": True,
        "asked_about": {
            "since": "2026-11-01T01:00:00",
            "until": "2026-11-01T01:59:59",
            "timezone": "America/New_York",
        },
        "shown": 1,
        "events": [{"kind": "message", "at": at, "text": "synthetic"}],
        "total": {"messages": 1, "documents": 0, "total": 1},
        "coverage": {"complete": True, "strategy": "complete", "includes_latest": True},
    }


def test_a_dst_fold_payload_is_bound_to_the_kernels_first_fold_semantics() -> None:
    first_fold = _past_payload_for_new_york(at="2026-11-01T05:30:00+00:00")
    second_fold = _past_payload_for_new_york(at="2026-11-01T06:30:00+00:00")

    assert _temporal_payload_is_coherent(
        "what_happened",
        first_fold,
        expected_timezone="America/New_York",
    )
    assert not _temporal_payload_is_coherent(
        "what_happened",
        second_fold,
        expected_timezone="America/New_York",
    )


def test_a_nonexistent_dst_wall_hour_cannot_prove_an_empty_timeline() -> None:
    payload = {
        "understood": True,
        "asked_about": {
            "since": "2026-03-08T02:00:00",
            "until": "2026-03-08T02:59:59",
            "timezone": "America/New_York",
        },
        "shown": 0,
        "events": [],
        "total": {"messages": 0, "documents": 0, "total": 0},
        "coverage": {"complete": True, "strategy": "complete", "includes_latest": True},
    }

    assert not _temporal_payload_is_coherent(
        "what_happened",
        payload,
        expected_timezone="America/New_York",
    )
