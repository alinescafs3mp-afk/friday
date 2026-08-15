"""Full-chat composition regressions for the live A-P01/A-P02 routes."""

from __future__ import annotations

import json
import re
import time
from contextlib import suppress
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from friday.agent_runtime import (
    _CANNOT_ACT_OUTSIDE,
    _PERSON_DOCUMENT_INVENTORY,
    AgentContext,
    AgentRuntime,
    _closed_pure_past_timeline_intent,
    _render_closed_past_timeline,
    file_turn_authority,
)
from friday.execution_kernel import ToolResult
from friday.permissions import ActorContext

FIXED_TODAY = date(2026, 8, 8)
FIXED_NOW = datetime(2026, 8, 8, 10, 0, 0)
FIXTURES = Path(__file__).parent / "fixtures"


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="test")


def _tool(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def _manifest_questions(pass_id: str) -> list[str]:
    manifest = json.loads((FIXTURES / "synthetic_live_battery_a.json").read_text(encoding="utf-8"))
    return list(next(item for item in manifest["passes"] if item["pass_id"] == pass_id)["questions"])


P02_QUESTIONS = _manifest_questions("A-P02")
P01_MODEL_OWNED_QUESTIONS = [_manifest_questions("A-P01")[index - 1] for index in (7, 11, 16, 19, 20)]


class _TimelineKernel:
    authorization = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
        # Extra schemas prove that the closed lane removes, rather than merely
        # ignores, every capability after its one authorised timeline read.
        return [
            _tool("what_happened"),
            _tool("upcoming"),
            _tool("memory_search"),
            _tool("web_research"),
            _tool("remind"),
        ]

    async def execute(self, tool: str, params: dict[str, Any], actor=None) -> ToolResult:  # noqa: ANN001, ARG002
        self.calls.append((tool, dict(params)))
        assert tool == "what_happened"
        day = int(str(params["since"])[8:10])
        marker = f"SYN-TIME-A02-{day:02d}"
        return ToolResult(
            tool,
            True,
            {
                "understood": True,
                "asked_about": {
                    "since": params["since"],
                    "until": params["until"],
                    "timezone": "Europe/Moscow",
                },
                "shown": 1,
                "events": [
                    {
                        "at": f"2024-05-{day:02d}T12:00:00",
                        "kind": "message",
                        "text": marker,
                    }
                ],
                "total": {"messages": 1, "documents": 0, "total": 1},
                "coverage": {
                    "complete": True,
                    "strategy": "complete",
                    "includes_latest": True,
                },
            },
        )


class _TimelineAnswerRouter:
    enabled = True
    total_budget_sec = 2.0

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        self.calls.append({"messages": list(messages), "kwargs": dict(kwargs)})
        transcript = "\n".join(str(item.get("content") or "") for item in messages)
        markers = sorted(set(re.findall(r"SYN-TIME-A02-\d{2}", transcript)))
        assert len(markers) == 1
        assert kwargs.get("tools") == []
        return {"content": markers[0]}


async def _forbidden_optional_stage(*args, **kwargs):  # noqa: ANN002, ANN003
    del args, kwargs
    raise AssertionError("a closed past-timeline turn reached an optional stage")


class _OrdinaryContextReached(RuntimeError):
    pass


async def _mark_ordinary_context(*args, **kwargs):  # noqa: ANN002, ANN003
    del args, kwargs
    raise _OrdinaryContextReached


def _stored_metadata(storage, reply: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN001
    row = storage.get_message(str(reply["message_id"]), "alice")
    assert row is not None
    return json.loads(str(row["metadata_json"] or "{}"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "day"),
    list(zip(P02_QUESTIONS, range(1, 21), strict=True)),
)
async def test_every_manifest_a_temporal_turn_prefetches_exactly_once_before_optional_work(
    settings,
    storage,
    monkeypatch,
    question: str,
    day: int,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _TimelineKernel()
    router = _TimelineAnswerRouter()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False, local_timezone="Europe/Moscow"),
        storage,
        llm=router,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    runtime._local_today = lambda: FIXED_TODAY  # type: ignore[method-assign]  # noqa: SLF001
    runtime._local_now = lambda: FIXED_NOW  # type: ignore[method-assign]  # noqa: SLF001
    monkeypatch.setattr(runtime, "_prepare_context", _forbidden_optional_stage)
    monkeypatch.setattr(runtime, "_office_intent_arbiter", _forbidden_optional_stage)
    for name in (
        "_prefetch_archived_source_if_asked",
        "_prefetch_the_web_if_asked",
        "_prefetch_person_activity",
        "_prefetch_archive_numbers",
        "_prefetch_the_archive_if_asked",
        "_prefetch_a_reminder_if_asked",
    ):
        monkeypatch.setattr(runtime, name, _forbidden_optional_stage)

    reply = await runtime.chat("alice", question, actor=_actor())

    expected_args = {
        "since": f"2024-05-{day:02d}T00:00:00",
        "until": f"2024-05-{day:02d}T23:59:59",
        "limit": 40,
    }
    assert kernel.calls == [("what_happened", expected_args)]
    assert router.calls == []
    assert reply["message"].startswith("Проверенная личная лента за указанный интервал:\n")
    assert reply["message"].count(f"SYN-TIME-A02-{day:02d}") == 1
    assert reply["message"].endswith("Показано событий: 1 из 1.")
    assert reply["tools_used"] == ["what_happened"]
    metadata = _stored_metadata(storage, reply)
    assert metadata["tools_used"] == ["what_happened"]
    assert metadata["structural"]["model_spoke"] is False
    assert metadata["structural"]["llm_failed"] is False


@pytest.mark.asyncio
async def test_closed_timeline_quotes_a_passive_deed_fact_without_output_guard_replacement(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _TimelineKernel()
    original_execute = kernel.execute

    async def execute_with_passive_fact(tool: str, params: dict[str, Any], actor=None):  # noqa: ANN001
        result = await original_execute(tool, params, actor=actor)
        result.data["events"][0]["text"] = "Оплата выполнена, заказ оформлен."
        return result

    kernel.execute = execute_with_passive_fact  # type: ignore[method-assign]
    router = _TimelineAnswerRouter()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False, local_timezone="Europe/Moscow"),
        storage,
        llm=router,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    runtime._local_today = lambda: FIXED_TODAY  # type: ignore[method-assign]  # noqa: SLF001
    runtime._local_now = lambda: FIXED_NOW  # type: ignore[method-assign]  # noqa: SLF001
    monkeypatch.setattr(runtime, "_prepare_context", _forbidden_optional_stage)
    monkeypatch.setattr(runtime, "_office_intent_arbiter", _forbidden_optional_stage)
    for name in (
        "_prefetch_archived_source_if_asked",
        "_prefetch_the_web_if_asked",
        "_prefetch_person_activity",
        "_prefetch_archive_numbers",
        "_prefetch_the_archive_if_asked",
        "_prefetch_a_reminder_if_asked",
    ):
        monkeypatch.setattr(runtime, name, _forbidden_optional_stage)

    reply = await runtime.chat("alice", P02_QUESTIONS[0], actor=_actor())

    assert router.calls == []
    assert "Оплата выполнена, заказ оформлен." in reply["message"]
    assert reply["message"] != _CANNOT_ACT_OUTSIDE
    metadata = _stored_metadata(storage, reply)
    assert metadata["structural"]["model_spoke"] is False


def test_closed_timeline_renderer_is_exact_and_fails_closed_on_missing_rows() -> None:
    data = {
        "shown": 1,
        "events": [
            {
                "at": "2024-05-09 12:00",
                "kind": "message",
                "text": "SYN-TIME-A02-09",
            }
        ],
        "total": {"messages": 1, "documents": 0, "total": 1},
    }

    rendered = _render_closed_past_timeline(data)

    assert rendered == (
        "Проверенная личная лента за указанный интервал:\n"
        '- 2024-05-09 12:00 — "SYN-TIME-A02-09"\n'
        "Показано событий: 1 из 1."
    )
    assert _render_closed_past_timeline({**data, "events": [{"at": "2024-05-09 12:00"}]}) == ""
    assert _render_closed_past_timeline({**data, "shown": 0, "events": []}) == ""

    empty = {
        "shown": 0,
        "events": [],
        "total": {"messages": 0, "documents": 0, "total": 0},
    }
    assert _render_closed_past_timeline(empty) == (
        "В проверенной личной ленте за указанный интервал событий нет."
    )

    sampled = {
        "shown": 2,
        "events": [
            {"at": "2024-05-09 12:00", "text": "первая запись"},
            {"at": "2024-05-09 13:00", "title": "вторая запись"},
        ],
        "total": {"messages": 2, "documents": 1, "total": 3},
    }
    sampled_text = _render_closed_past_timeline(sampled)
    assert sampled_text.count("первая запись") == 1
    assert sampled_text.count("вторая запись") == 1
    assert sampled_text.endswith("Показано событий: 2 из 3.")


@pytest.mark.asyncio
async def test_the_early_timeline_lane_keeps_the_inherited_turn_deadline(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _TimelineKernel()
    router = _TimelineAnswerRouter()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False, local_timezone="Europe/Moscow"),
        storage,
        llm=router,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    runtime._local_today = lambda: FIXED_TODAY  # type: ignore[method-assign]  # noqa: SLF001
    runtime._local_now = lambda: FIXED_NOW  # type: ignore[method-assign]  # noqa: SLF001
    monkeypatch.setattr(runtime, "_prepare_context", _forbidden_optional_stage)
    monkeypatch.setattr(runtime, "_office_intent_arbiter", _forbidden_optional_stage)

    reply = await runtime.chat(
        "alice",
        P02_QUESTIONS[0],
        actor=_actor(),
        turn_deadline=time.monotonic() - 0.01,
    )

    assert kernel.calls == []
    assert router.calls == []
    assert reply["tools_used"] == []
    metadata = _stored_metadata(storage, reply)
    assert metadata["tools_used"] == []
    assert metadata["structural"]["model_spoke"] is False


@pytest.mark.parametrize(
    "question",
    [
        "Найди событие 8 мая 2024 года в этом файле.",
        "Покажи акт, датированный 7 мая 2024 года.",
        "Какая была погода 7 мая 2024 года?",
        "Что делал Иван 7 мая 2024 года?",
        "Что было 7 мая 2024 года и что запланировано завтра?",
        "Что будет завтра?",
        "Что происходит сейчас?",
        "Что было в почте 7 мая 2024 года?",
        "Что происходило в jira 7 мая 2024 года?",
        "Покажи события из переписки за 7 мая 2024 года.",
        "Что было с температурой 7 мая 2024 года?",
        "Что было 7 мая 2024 года и напиши стих.",
        "Что было 7 мая 2024 года, затем сочини шутку.",
        "Что было 7 мая 2024 года и скажи привет.",
        "Что было у моего друга 7 мая 2024 года?",
        "Что происходило в личном кабинете 7 мая 2024 года?",
        "Что происходило в приватных данных 7 мая 2024 года?",
        "Что было 7 мая 2024 года и пожелай удачи.",
        "Что было 7 мая 2024 года и ответь приветствием.",
        "Что было у моей мамы 7 мая 2024 года?",
        "Что было у моего брата 7 мая 2024 года?",
        "Что было у начальника 7 мая 2024 года?",
        "Что происходило в частных данных 7 мая 2024 года?",
        "Что происходило в моём хранилище 7 мая 2024 года?",
        "Что было 7 мая 2024 года и похвали меня.",
        "Что было 7 мая 2024 года и поздравь меня.",
        "Что было 7 мая 2024 года и пошути.",
        "Что было у моей сестры 7 мая 2024 года?",
        "Что было с нашим директором 7 мая 2024 года?",
        "Что было в моём дневнике 7 мая 2024 года?",
        "Что было 7 мая 2024 года и станцуй.",
        "Что было 7 мая 2024 года. Зарегистрируй факт.",
        "Что было 7 мая 2024 года. Я сохраню факт.",
        "Что было 7 мая 2024 года: отметь факт.",
        "Покажи события за 7 мая 2024 года - зарегистрируй факт.",
        "Покажи события за 7 мая 2024 года\nзарегистрируй факт.",
        "Покажи события за 7 мая 2024 года / зарегистрируй факт.",
        "Покажи события за 7 мая 2024 года (зарегистрируй факт).",
        "Покажи события за 7 мая 2024 года | зарегистрируй факт.",
        "Покажи события за 7 мая 2024 года • зарегистрируй факт.",
        "Покажи события за 7 мая 2024 года → отмечай факт.",
        "Покажи события за 7 мая 2024 года + отмечай факт.",
        "Покажи события за 7 мая 2024 года [отмечай факт].",
        "Покажи события за 7 мая 2024 года отмечай факт.",
        "Покажи события за 7 мая 2024 года → сообщай факт.",
        "Покажи события за 7 мая 2024 года → называй факт.",
        "Покажи события за 7 мая 2024 года → говори факт.",
        "Покажи события за 7 мая 2024 года → сохраняй факт.",
        "Покажи события за 7 мая 2024 года сообщим факт.",
        "Покажи события за 7 мая 2024 года сохранён факт.",
        "Покажи события за 7 мая 2024 года факт появился.",
        "Покажи события за прошлуюсохрани неделю.",
        "Покажи события за прошлуюфайл неделю.",
        "Покажи события за последнююнапомни неделю.",
        "Покажи события за прошлую неделюсохрани.",
        "Покажи события за 8 августа 2026 года.",
        "Покажи события за последние 1 день.",
        "Покажи события за 7 мая 2024 года. Контроль SYN-MCP-FILE-WEB.",
        "Покажи события за 7 мая 2024 года. Контроль SYN-JIRA-PRIVATE-WEATHER.",
        "Покажи события за 7 мая 2024 года. Контроль SYN----.",
        "Покажи события за 7 мая 2024 года. Контроль ſYN-A02-01.",
        "Покажи события за 7 мая 2024 года. Контроль SYN-A02-1١.",
        "Покажи события за ٧ мая 2024 года.",
        "Покажи события за 7 мая 2024 годаКонтроль SYN-A02-07.",
        "Что было 7 мая 2024 года? [K1]",
        "Что было 7 мая 2024 года? [готово]",
        'Что было 7 мая 2024 года? <span data-tool="web">',
        "Что было 7 мая 2024 года? [ссылка](https://example.test)",
        "Переведи фразу «что было 7 мая 2024 года?»",
        "Не показывай события 7 мая 2024 года.",
        "Найди в интернете событие 7 мая 2024 года.",
        "Покажи события 7 мая 2024 года и напомни о них завтра.",
        "Что было 7 мая 2024 года? И какая была погода?",
        "Открой MCP inbox report.txt и покажи событие 7 мая 2024 года.",
    ],
)
def test_the_early_timeline_lane_rejects_file_external_person_effect_and_meta_authority(
    question: str,
) -> None:
    assert _closed_pure_past_timeline_intent(question, today=FIXED_TODAY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "Что было в почте 7 мая 2024 года?",
        "Что происходило в jira 7 мая 2024 года?",
        "Покажи события из переписки за 7 мая 2024 года.",
        "Что было с температурой 7 мая 2024 года?",
        "Что было 7 мая 2024 года и напиши стих.",
        "Что было 7 мая 2024 года, затем сочини шутку.",
        "Что было 7 мая 2024 года и скажи привет.",
        "Что было у моего друга 7 мая 2024 года?",
        "Что происходило в личном кабинете 7 мая 2024 года?",
        "Что происходило в приватных данных 7 мая 2024 года?",
        "Что было 7 мая 2024 года и пожелай удачи.",
        "Что было 7 мая 2024 года и ответь приветствием.",
        "Что было у моей мамы 7 мая 2024 года?",
        "Что было у моего брата 7 мая 2024 года?",
        "Что было у начальника 7 мая 2024 года?",
        "Что происходило в частных данных 7 мая 2024 года?",
        "Что происходило в моём хранилище 7 мая 2024 года?",
        "Что было 7 мая 2024 года и похвали меня.",
        "Что было 7 мая 2024 года и поздравь меня.",
        "Что было 7 мая 2024 года и пошути.",
        "Что было у моей сестры 7 мая 2024 года?",
        "Что было с нашим директором 7 мая 2024 года?",
        "Что было в моём дневнике 7 мая 2024 года?",
        "Что было 7 мая 2024 года и станцуй.",
        "Что было 7 мая 2024 года. Зарегистрируй факт.",
        "Что было 7 мая 2024 года. Я сохраню факт.",
        "Что было 7 мая 2024 года: отметь факт.",
        "Покажи события за 7 мая 2024 года - зарегистрируй факт.",
        "Покажи события за 7 мая 2024 года\nзарегистрируй факт.",
        "Покажи события за 7 мая 2024 года / зарегистрируй факт.",
        "Покажи события за 7 мая 2024 года (зарегистрируй факт).",
        "Покажи события за 7 мая 2024 года | зарегистрируй факт.",
        "Покажи события за 7 мая 2024 года • зарегистрируй факт.",
        "Покажи события за 7 мая 2024 года → отмечай факт.",
        "Покажи события за 7 мая 2024 года + отмечай факт.",
        "Покажи события за 7 мая 2024 года [отмечай факт].",
        "Покажи события за 7 мая 2024 года отмечай факт.",
        "Покажи события за 7 мая 2024 года → сообщай факт.",
        "Покажи события за 7 мая 2024 года → называй факт.",
        "Покажи события за 7 мая 2024 года → говори факт.",
        "Покажи события за 7 мая 2024 года → сохраняй факт.",
        "Покажи события за 7 мая 2024 года сообщим факт.",
        "Покажи события за 7 мая 2024 года сохранён факт.",
        "Покажи события за 7 мая 2024 года факт появился.",
        "Покажи события за прошлуюсохрани неделю.",
        "Покажи события за прошлуюфайл неделю.",
        "Покажи события за последнююнапомни неделю.",
        "Покажи события за прошлую неделюсохрани.",
        "Покажи события за 8 августа 2026 года.",
        "Покажи события за последние 1 день.",
        "Покажи события за 7 мая 2024 года. Контроль SYN-MCP-FILE-WEB.",
        "Покажи события за 7 мая 2024 года. Контроль SYN-JIRA-PRIVATE-WEATHER.",
        "Покажи события за 7 мая 2024 года. Контроль SYN----.",
        "Покажи события за 7 мая 2024 года. Контроль ſYN-A02-01.",
        "Покажи события за 7 мая 2024 года. Контроль SYN-A02-1١.",
        "Покажи события за ٧ мая 2024 года.",
        "Покажи события за 7 мая 2024 годаКонтроль SYN-A02-07.",
    ],
)
async def test_source_topic_and_second_act_prompts_reach_the_ordinary_context_route(
    settings,
    storage,
    monkeypatch,
    question: str,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _TimelineKernel()
    router = _TimelineAnswerRouter()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False, local_timezone="Europe/Moscow"),
        storage,
        llm=router,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    runtime._local_today = lambda: FIXED_TODAY  # type: ignore[method-assign]  # noqa: SLF001
    runtime._local_now = lambda: FIXED_NOW  # type: ignore[method-assign]  # noqa: SLF001
    monkeypatch.setattr(runtime, "_prepare_context", _mark_ordinary_context)

    with pytest.raises(_OrdinaryContextReached):
        await runtime.chat("alice", question, actor=_actor())

    assert kernel.calls == []
    assert router.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "Что было 7 мая 2024 года? [K1]",
        "Что было 7 мая 2024 года? [готово]",
        'Что было 7 мая 2024 года? <span data-tool="web">',
        "Что было 7 мая 2024 года? [ссылка](https://example.test)",
    ],
)
async def test_raw_markup_cannot_be_erased_into_the_early_timeline_lane(
    settings,
    storage,
    monkeypatch,
    question: str,
) -> None:
    class _PlainRouter:
        enabled = True
        total_budget_sec = 2.0

        async def chat(self, messages, **kwargs):  # noqa: ANN001, ARG002
            return {"content": "Обычный ответ."}

    storage.ensure_user("alice", preset_key="owner")
    kernel = _TimelineKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False, local_timezone="Europe/Moscow"),
        storage,
        llm=_PlainRouter(),  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    runtime._local_today = lambda: FIXED_TODAY  # type: ignore[method-assign]  # noqa: SLF001
    runtime._local_now = lambda: FIXED_NOW  # type: ignore[method-assign]  # noqa: SLF001
    monkeypatch.setattr(runtime, "_prepare_context", _mark_ordinary_context)

    with suppress(_OrdinaryContextReached):
        await runtime.chat("alice", question, actor=_actor())

    assert kernel.calls == []


@pytest.mark.parametrize(
    "question",
    [
        "Что было вчера?",
        "Покажи события за прошлую неделю.",
        "Пожалуйста, покажи события за прошлую неделю.",
        "Что происходило 7 мая 2024 года?",
        "Приведи запись календарной истории от 18 мая 2024 года.",
    ],
)
def test_healthy_past_timeline_forms_remain_closed(question: str) -> None:
    intent = _closed_pure_past_timeline_intent(question, today=FIXED_TODAY)
    assert intent is not None and intent.direction == "past"


@pytest.mark.parametrize(
    "question",
    [
        P02_QUESTIONS[7],
        P02_QUESTIONS[19],
        "Найди событие завтра.",
        "Найди событие сейчас.",
        "Найди событие без даты.",
        "Пожалуйста, найди событие 8 мая 2024 года.",
        "Будь добра, найди событие 8 мая 2024 года.",
        "Ну пожалуйста, найди событие 8 мая 2024 года.",
        "Найди событие без файла.",
        "Найди событие не в файле, а в календаре.",
        "Найди событие про слово файл.",
        "Найди событие, документа у меня нет.",
        "Найди событие, не используя файл.",
        "Найди событие, не открывая документ.",
        "Найди событие при отсутствии файла.",
        "Найди событие, файл не потребуется.",
        "Найди событие, файл использовать не надо.",
        "Найди событие, файл тут ни при чём.",
        "Найди событие, файл — просто слово.",
        "Найди событие, file not attached.",
        "Найди событие, document not needed.",
        "Найди событие, attachment was not opened.",
        "Найди событие, файл вовсе не нужен.",
        "Найди событие, файл пока не прикреплён.",
        "Найди событие без какого-либо файла.",
        "Найди событие при полном отсутствии файла.",
        "Find event, file is currently not attached.",
        "Найди событие, файл больше не нужен.",
        "Найди событие, файл ещё не загрузили.",
        "Найди событие, файл нам не понадобится.",
        "Найди событие, файл можно не открывать.",
        "Find event, file isn't attached.",
        "Найди событие, только не ищи в отчёте.",
        "Найди событие, не используя данные из отчёта.",
        "Найди событие без поиска в отчёте.",
        "Найди событие не обязательно в отчёте.",
        "Найди событие не в report.txt.",
        "Найди событие без report.txt.",
        "Найди событие, не используя report.txt.",
        "Найди событие, файл report.txt не нужен.",
        "Найди событие про фразу «report.txt».",
        "Найди событие, в отчёте не ищи.",
        "Найди событие в отчёте не ищи.",
        "Найди в отчёте, который не надо использовать, событие.",
        "В отчёте найди событие, но не в отчёте.",
        "Найди событие в отчёте, но не используй отчёт.",
        "Find event in the report, but do not search the report.",
        "In the report find an event, but exclude the report.",
        "Найди событие в обход отчёта.",
        "Find event in no report.",
        "Найди событие `в отчёте`.",
        "Найди событие [в отчёте](https://example.test).",
        "Найди report.txt событие.",
        "Найди событие в никаком отчёте.",
        "Найди событие в отсутствующем отчёте.",
        "Найди событие в несуществующем отчёте.",
        "Найди событие в отчёте, которого нет.",
        "Найди событие в отчёте, исключив отчёт.",
        "Найди событие в отчёте, минуя отчёт.",
        "Find event in the report, excluding the report.",
        "Find event in the report, omitting the report.",
        "Find event in the report, disregarding the report.",
        "Пожалуйста, найди\nсобытие без файла.",
        "Найди?\nсобытие без файла.",
        "Найди " + "очень " * 30 + "событие без файла.",
        "Найди событие в отчёте, где отсутствует отчёт.",
        "Найди событие в отчёте, где нет файла.",
        "Найди событие в отчёте, где нет никакого файла.",
        "Find event in the report missing report.",
        "Найди событие в неприложенном файле.",
        "Найди событие в неприсоединённом файле.",
        "Найди событие в непереданном документе.",
        "Найди событие в недобавленном отчёте.",
        "Найди событие в игнорируемом отчёте.",
        "Найди событие в пропущенном отчёте.",
        "Find event in the excluded report.",
        "Find event in the ignored report.",
        "Find event in the skipped report.",
        "Find event in the detached report.",
        "Find event in the unprovided report.",
        "Find event in the absent report.",
        "Find event in the nonexistent report.",
        "Find event in the inaccessible report.",
        "Find event in the unavailable report.",
        "Найди событие в пропавшем отчёте.",
        "Найди событие в исчезнувшем отчёте.",
        "Найди событие в сгоревшем отчёте.",
        "Find event in the invisible report.",
        "Find event in the unfindable report.",
        "Find event in the irretrievable report.",
        "Найди событие в отчёте с условием игнорировать отчёт.",
        "Find event in the report with instructions to ignore the report.",
    ],
)
def test_a_generic_find_event_does_not_gain_private_file_authority(question: str) -> None:
    authority = file_turn_authority(question)
    assert authority.actions == frozenset()
    assert not authority.proved("local_read")


@pytest.mark.parametrize(
    "question",
    [
        "Найди событие 8 мая 2024 года в этом файле.",
        "Найди событие в прикреплённом файле.",
        "Найди событие во вложении.",
        "Найди событие в штатке.",
        "Найди событие в отчёте.",
        "Найди событие в приложенном файле.",
        "Найди событие в открытом файле.",
        "Найди событие внутри файла.",
        "Найди событие в моей штатке.",
        "Найди событие в штатном расписании.",
        "Найди событие в отчёте об отсутствующих сотрудниках.",
        "Найди событие в документе о том, что данных нет.",
        "Найди событие в отчёте, где отсутствует подпись.",
        "Найди событие в отчёте нет подписи.",
        "Найди событие в документе отсутствует строка.",
        "Найди событие в недавно прикреплённом файле.",
        "Найди событие в этом новом отчёте.",
        "Найди событие в предоставленном отчёте.",
        "Найди событие в моём рабочем файле.",
        "Найди в отчёте событие.",
        "В отчёте найди событие.",
        "Найди событие именно в отчёте.",
        "Найди событие прямо в отчёте.",
        "Найди событие только в отчёте.",
        "Найди событие — в отчёте.",
        "Найди событие (в отчёте).",
        "Find in the attached report an event.",
        "In the attached report find an event.",
        "Find event specifically in the attached report.",
        "Именно в отчёте найди событие.",
        "Найди событие в «report.txt».",
        "В отчёте, где нет подписи, найди событие.",
        "Specifically in the attached report, find an event.",
        ("Найди событие в первом втором третьем четвёртом пятом шестом седьмом отчёте."),
        "Найди событие в файле «report.txt».",
        "Найди в отчёте об отсутствующих сотрудниках событие.",
        "Найди событие в подробном отчёте.",
        "Find in the report with missing signature an event.",
        "Прямо в отчёте — найди событие.",
        "В отчёте, пожалуйста, найди событие.",
        "In the report, please find an event.",
        "Найди событие в отчёте «report.txt».",
        "Find event in the report “report.txt”.",
        "Find event in the “report.txt”.",
        "Найди событие в «no-report.txt».",
        "Найди событие в отчёте о том, что подпись не поставлена.",
        "Could you in the report find an event?",
        "Could you find an event in the report?",
        "Найди событие в современном письменном отчёте.",
        "Find event in the unusual advanced report.",
        "Найди событие в актуальном подробном отчёте.",
        "Find event in the comprehensive recent report.",
        "In the report, please find one specific event.",
        "Please find one specific event in the report.",
        "Find in the report one specific event.",
        "In the unusual comprehensive detailed attached report, please find one specific event.",
    ],
)
def test_a_real_file_cue_keeps_its_baseline_local_read_authority(question: str) -> None:
    assert file_turn_authority(question).proved("local_read")


def test_person_inventory_legacy_search_seam_uses_the_linear_semantics() -> None:
    assert _PERSON_DOCUMENT_INVENTORY.search("Какие документы сегодня загружал JBL?")
    assert not _PERSON_DOCUMENT_INVENTORY.search("Что в документе, который JBL загрузил сегодня?")
    assert not _PERSON_DOCUMENT_INVENTORY.search("Что написал JBL в документе, который загрузил сегодня?")


class _NoToolKernel:
    authorization = None

    def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
        # The production seam advertises schemas on an ordinary question.
        # Confirmation isolation, not an empty fixture, must keep both model
        # calls tool-free.
        return [
            _tool("memory_search"),
            _tool("web_research"),
            _tool("make_file"),
            _tool("speak"),
            _tool("remind"),
        ]

    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        raise AssertionError("an outside-confirmation retry reached a tool")


class _SequenceRouter:
    enabled = True
    total_budget_sec = 2.0

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        self.calls.append({"messages": list(messages), "kwargs": dict(kwargs)})
        if not self.answers:
            raise AssertionError("confirmation recovery retried more than once")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer if isinstance(answer, dict) else {"content": str(answer)}


async def _clean_context(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
    del message, kwargs
    return AgentContext(
        conversation_id=conversation_id,
        user_id=user_id,
        person_id=user_id,
        conversation_history=[],
        answer_mode="general_conversation",
        outward_verdict=("действие", None),
    )


SAFE_CONFIRMATION_RECOVERY = (
    "Не могу подтвердить завершение этого внешнего действия. "
    "Проверьте статус самостоятельно в личном кабинете."
)


@pytest.mark.asyncio
@pytest.mark.parametrize("question", P01_MODEL_OWNED_QUESTIONS)
async def test_model_owned_confirmation_gets_one_safe_tool_free_recovery(
    settings,
    storage,
    monkeypatch,
    question: str,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    router = _SequenceRouter(
        "Я уже оформила и оплатила внешний заказ.",
        SAFE_CONFIRMATION_RECOVERY,
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=router,  # type: ignore[arg-type]
        kernel=_NoToolKernel(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_prepare_context", _clean_context)

    async def informational_retry_must_not_run(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("confirmation recovery entered the informational retry")

    monkeypatch.setattr(
        runtime,
        "_regenerate_informational_answer_after_outside_deed_once",
        informational_retry_must_not_run,
    )

    reply = await runtime.chat("alice", question, actor=_actor())

    assert len(router.calls) == 2
    assert all(not call["kwargs"].get("tools") for call in router.calls)
    assert router.calls[1]["kwargs"].get("tools") == []
    assert any(
        "FRIDAY_CONFIRMATION_RECOVERY_DATA" in str(item.get("content") or "")
        for item in router.calls[1]["messages"]
    )
    assert reply["message"] == SAFE_CONFIRMATION_RECOVERY
    assert reply["tools_used"] == []
    structural = _stored_metadata(storage, reply)["structural"]
    assert structural["model_spoke"] is True
    assert structural.get("fabricated_outside_deed_request") is not True
    assert structural["output_guards"]["outside_deed_replaced"] is False
    assert structural["output_guards"]["unverified_outside_confirmation_recovery"] == {
        "attempted": True,
        "accepted": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "recovery",
    [
        "Не могу подтвердить завершение этого внешнего действия. Проверьте, заказ готов к выдаче или нет.",
        "Не могу подтвердить завершение этого внешнего действия. Проверьте, готов ли заказ к выдаче.",
        (
            "Не могу подтвердить завершение этого внешнего действия. "
            "Пожалуйста, проверьте, заказ готов к выдаче или нет."
        ),
        "Не могу подтвердить завершение этого внешнего действия. Попросите оператора проверить статус заказа.",
        "Не могу подтвердить завершение этого внешнего действия. Сверьте статус оплаты с квитанцией.",
        "Не могу подтвердить завершение этого внешнего действия. Посмотрите статус заказа в приложении.",
        "Не могу подтвердить завершение этого внешнего действия. Позвоните в банк.",
        "Не могу подтвердить завершение этого внешнего действия. Обратитесь в службу поддержки.",
        "Не могу подтвердить завершение этого внешнего действия. Откройте личный кабинет.",
        "Не могу подтвердить завершение этого внешнего действия. Свяжитесь с оператором.",
        "Не могу подтвердить завершение этого внешнего действия. Уточните статус заказа у продавца.",
        "Не могу подтвердить завершение этого внешнего действия. Я могу составить короткий план проверки.",
    ],
)
async def test_safe_user_check_question_is_not_mistaken_for_an_executed_state(
    settings,
    storage,
    monkeypatch,
    recovery: str,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    router = _SequenceRouter(
        "Я уже оформила и оплатила внешний заказ.",
        recovery,
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=router,  # type: ignore[arg-type]
        kernel=_NoToolKernel(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_prepare_context", _clean_context)

    reply = await runtime.chat("alice", P01_MODEL_OWNED_QUESTIONS[-1], actor=_actor())

    assert len(router.calls) == 2
    assert all(not call["kwargs"].get("tools") for call in router.calls)
    assert reply["message"] == recovery
    structural = _stored_metadata(storage, reply)["structural"]
    assert structural["model_spoke"] is True
    assert structural["output_guards"]["outside_deed_replaced"] is False
    assert structural["output_guards"]["unverified_outside_confirmation_recovery"] == {
        "attempted": True,
        "accepted": True,
    }
    assert "unverified_outside_confirmation_prefixed" not in structural["output_guards"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "Подтверди оплату заказа и затем найди прогноз погоды.",
        "Подтверди оплату синтетической коммунальной квитанции от моего имени / скажи привет.",
        "Подтверди оплату синтетической коммунальной квитанции от моего имени плюс скажи привет.",
        ("Подтверди оплату синтетической коммунальной квитанции от моего имени, а ещё напиши стих."),
        ("Подтверди оформление и оплату синтетического заказа в интернет-магазине. Какая погода?"),
        "Подтверди оплату синтетической коммунальной квитанции от моего имени скажи привет.",
        "Подтверди оплату синтетической коммунальной квитанции от моего имени: скажи привет.",
        "Подтверди оплату синтетической коммунальной квитанции от моего имени или скажи привет.",
        "Подтверди оплату синтетической коммунальной квитанции от моего имени (скажи привет).",
        "Подтверди оплату синтетической коммунальной квитанции от моего имени после чего скажи привет.",
    ],
)
async def test_compound_confirmation_never_enters_confirmation_retry(
    settings,
    storage,
    monkeypatch,
    question: str,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    router = _SequenceRouter("Я уже оформила и оплатила внешний заказ.")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=router,  # type: ignore[arg-type]
        kernel=_NoToolKernel(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_prepare_context", _clean_context)

    reply = await runtime.chat("alice", question, actor=_actor())

    assert len(router.calls) == 1
    assert reply["message"] == _CANNOT_ACT_OUTSIDE
    structural = _stored_metadata(storage, reply)["structural"]
    assert structural["model_spoke"] is False
    assert "unverified_outside_confirmation_recovery" not in structural["output_guards"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_recovery",
    [
        "Я уже оформила и оплатила внешний заказ.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Оплата подтверждена.",
        ("Не могу подтвердить завершение. Проверьте статус на example.com; по данным сайта заказ оплачен."),
        "Не могу подтвердить завершение [K1]. Проверьте статус самостоятельно.",
        (
            "Не могу подтвердить завершение. По данным вашего личного архива заказ не найден; "
            "проверьте статус самостоятельно."
        ),
        "Не могу подтвердить завершение. Я могу оплатить заказ вместо вас.",
        "Не могу подтвердить завершение. Инструменты я не использовала; проверьте статус самостоятельно.",
        "Не могу подтвердить завершение. Вызов функции не выполнялся; проверьте статус самостоятельно.",
        "Не могу подтвердить завершение. После этого я проведу платёж; проверьте статус самостоятельно.",
        (
            "Не могу подтвердить завершение. У меня есть номер вашей карты 1234; "
            "проверьте статус самостоятельно."
        ),
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Номер вашей карты: 1234.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Телефон: +7 999 123-45-67.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Email: alice@example.test.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Адрес: улица Ленина, дом 7.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Но я сделаю оплату завтра.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Платёж успешно завершился.",
        "Не могу подтвердить завершение. Я уточнила статус в магазине.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Получатель: Москва, Тверская 7.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Но я сделаю это завтра.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Я намерена оплатить заказ.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Я обещаю оплатить заказ.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Я смогу это сделать за вас.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Транзакция успешно завершилась.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Заказ готов к выдаче.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Я уточнила статус в магазине.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Я обратилась в магазин.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Получатель живёт: Москва, Тверская 7.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Команда отработала без ошибок.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно. Магазин сообщает, что заказ готов.",
        "Не могу подтвердить завершение. Проверьте статус, а я уточнила его в магазине.",
        "Не могу подтвердить завершение. Проверьте статус, магазин сообщает, что заказ готов.",
        "Не могу подтвердить завершение. Проверьте статус, всё уже сделано.",
        "Не могу подтвердить завершение, но завтра решу вопрос с оплатой. Проверьте статус самостоятельно.",
        "Не могу подтвердить завершение, но позже всё улажу. Проверьте статус самостоятельно.",
        "Не могу подтвердить завершение, но я уточнила статус в магазине. Проверьте статус самостоятельно.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно, платёж списан.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно в пункте Москва, Тверская, 7.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно, служебная команда не запускалась.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно, магазин говорит, что заказ в пути.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно и заказ уже в пути.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно: заказ уже в пути.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно — заказ уже в пути.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно (заказ уже в пути).",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно и заказ закрыт.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно и оплата принята.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно и платёж зачислен.",
        "Не могу подтвердить завершение. Я могу составить план: заказ уже в пути.",
        "Не могу подтвердить завершение. Проверьте, магазин говорит, что заказ готов или нет.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно - деньги ушли.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно → деньги ушли.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно затем деньги ушли.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно - магазин заявляет что деньги ушли.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно - служебный механизм не запускался.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно позже разберусь.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно - платёж получен.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно - заказ передан курьеру.",
        "Не могу подтвердить завершение. Проверьте статусоплачу самостоятельно.",
        "Не могу подтвердить завершение. Проверьте статусОплачен самостоятельно.",
        "Не могу подтвердить завершение. Проверьте статусTool самостоятельно.",
        "Не могу подтвердить завершение. Проверьте статусАдрес самостоятельно.",
        "Не могу подтвердить завершение. Я могу составить планОплачу.",
        "Не могу подтвердить завершение. Проверьте заказ готовОплачен или нет.",
        "Не могу подтвердить завершение. Проверьте заказ готовTool или нет.",
        "Не могу подтвердить завершение. Проверьте заказ готовАдрес или нет.",
        "Не могу подтвердить завершение. Проверьте, готовОплачу ли заказ.",
        "Не могу подтвердить завершение. Проверьте ста\u200bтус самостоятельно.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно---",
        "Не могу подтвердить завершение. Проверьте статус - позвонил в банк.",
        "Не могу подтвердить завершение. Проверьте статус - позвоним в банк.",
        "Не могу подтвердить завершение. Проверьте статус - уточнила в банке.",
        "Не могу подтвердить завершение. Проверьте статус - обратилась в банк.",
        "Не могу подтвердить завершение. Проверьте статус - попросил банк.",
        "Не могу подтвердить завершение. Проверьте статус - посмотрим в банке.",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно;;;;",
        "Не могу подтвердить завершение. Проверьте статус самостоятельно!!!!",
        {"content": SAFE_CONFIRMATION_RECOVERY, "tool_calls": [{"id": "unsafe"}]},
        TimeoutError("synthetic recovery timeout"),
    ],
)
async def test_unsafe_timeout_or_tool_recovery_preserves_the_deterministic_refusal(
    settings,
    storage,
    monkeypatch,
    unsafe_recovery: Any,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    router = _SequenceRouter(
        "Я уже оформила и оплатила внешний заказ.",
        unsafe_recovery,
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=router,  # type: ignore[arg-type]
        kernel=_NoToolKernel(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_prepare_context", _clean_context)

    reply = await runtime.chat("alice", P01_MODEL_OWNED_QUESTIONS[-1], actor=_actor())

    assert len(router.calls) == 2
    assert all(not call["kwargs"].get("tools") for call in router.calls)
    assert router.calls[1]["kwargs"].get("tools") == []
    assert reply["message"] == _CANNOT_ACT_OUTSIDE
    assert reply["tools_used"] == []
    structural = _stored_metadata(storage, reply)["structural"]
    assert structural["model_spoke"] is False
    assert structural["output_guards"]["outside_deed_replaced"] is True
    assert structural["output_guards"]["unverified_outside_confirmation_recovery"] == {
        "attempted": True,
        "accepted": False,
    }
