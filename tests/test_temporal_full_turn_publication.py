"""Full-caller temporal regressions; synthetic doubles, no external services."""

import json
from dataclasses import replace
from datetime import datetime

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime, file_turn_authority
from friday.execution_kernel import ToolResult
from friday.permissions import ActorContext

NOW = datetime(2026, 8, 8, 10)
VALUE = "opaque-λ-0042\\KeepBytes"


def tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


class Kernel:
    def __init__(self, *, offered=True, corrupt=False):
        self.calls = []
        self.offered = offered
        self.corrupt = corrupt

    def get_tool_definitions(self, actor, topic=""):
        return [tool("what_happened"), tool("upcoming")] if self.offered else [tool("speak")]

    async def execute(self, name, params, actor=None):
        self.calls.append((name, dict(params)))
        assert name == "what_happened"
        data = {
            "understood": True,
            "asked_about": {"since": params["since"], "until": params["until"], "timezone": "Europe/Moscow"},
            "shown": 1,
            "events": [{"kind": "message", "at": params["since"], "text": VALUE}],
            "total": {"messages": 1, "documents": 0, "total": 1},
            "coverage": {"complete": True, "strategy": "complete", "includes_latest": True},
        }
        if self.corrupt:
            data["asked_about"]["until"] = "2024-01-01T23:59:59"
        return ToolResult(name, True, data)


class HostileModel:
    enabled = True
    total_budget_sec = 2.0

    def __init__(self, replay=False):
        self.calls = []
        self.replay = replay

    async def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        if "Уже решена вот эта часть" in str(messages[0].get("content")):
            remainder = str(messages[-1]["content"]) if self.replay else ""
            return {"content": json.dumps({"остаток": remainder}, ensure_ascii=False)}
        return {"content": "Выдуманное событие в 12:00: WRONG-OPAQUE-VALUE"}


async def no_prefetch(*args, **kwargs):
    return False


def runtime_for(settings, storage, monkeypatch, kernel, model):
    runtime = AgentRuntime(
        replace(settings, verify_answers=False, local_timezone="Europe/Moscow"),
        storage,
        llm=model,
        kernel=kernel,
    )
    runtime._local_today = lambda: NOW.date()
    runtime._local_now = lambda: NOW

    async def misclassified(user_id, message, conversation_id, **kwargs):
        return AgentContext(
            user_id=user_id, conversation_id=conversation_id, outward_verdict=("знание", None)
        )

    monkeypatch.setattr(runtime, "_prepare_context", misclassified)
    for name in (
        "_prefetch_archived_source_if_asked",
        "_prefetch_the_web_if_asked",
        "_prefetch_person_activity",
        "_prefetch_archive_numbers",
        "_prefetch_the_archive_if_asked",
        "_prefetch_a_reminder_if_asked",
    ):
        monkeypatch.setattr(runtime, name, no_prefetch)
    return runtime


CASES = [
    ("Проверь temporal index и сообщи запись для дня 5 июня 2024 года.", "2024-06-05", "23:59:59"),
    (
        "Изолируй суточный интервал 6 июня 2024 года и назови попавшее в него событие.",
        "2024-06-06",
        "23:59:59",
    ),
    (
        "Нужен факт, привязанный именно к полуночи календарного дня 8 июня 2024 года.",
        "2024-06-08",
        "00:59:59",
    ),
    ("Проверь временной индекс и сообщи событие за 19 февраля 2023 года.", "2023-02-19", "23:59:59"),
    (
        "Нужен факт, привязанный именно к полуночи календарного дня 29 февраля 2024 года.",
        "2024-02-29",
        "00:59:59",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("question,day,until", CASES)
@pytest.mark.parametrize("entry", ["chat", "agentic_loop"])
@pytest.mark.parametrize("replay", [False, True])
async def test_exact_timeline_crosses_full_caller_and_hostile_model(
    settings, storage, monkeypatch, question, day, until, entry, replay
):
    storage.ensure_user("alice", preset_key="owner")
    kernel, model = Kernel(), HostileModel(replay)
    runtime = runtime_for(settings, storage, monkeypatch, kernel, model)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    if entry == "chat":
        result = await runtime.chat("alice", question, actor=actor)
        answer = result["message"]
        stored = storage.get_message(result["message_id"], "alice")
        assert json.loads(stored["metadata_json"])["tools_used"] == ["what_happened"]
    else:
        context = AgentContext(user_id="alice", conversation_id="unit", outward_verdict=("знание", None))
        result = await runtime._agentic_loop(
            context, question, actor, kernel.get_tool_definitions(actor), None
        )
        answer = context.structural_answer + str(result.get("content") or "")
    assert kernel.calls == [
        ("what_happened", {"since": day + "T00:00:00", "until": day + "T" + until, "limit": 40})
    ]
    assert result["tools_used"] == ["what_happened"]
    assert json.dumps(VALUE, ensure_ascii=False) in answer
    assert "WRONG-OPAQUE-VALUE" not in answer
    assert day + "T00:00:00" in answer


@pytest.mark.parametrize(
    "question",
    [
        "Переведи: «Проверь temporal index и сообщи событие за 5 июня 2024 года».",
        "Не показывай событие за 5 июня 2024 года.",
        "Проверь синтаксис temporal index в файле от 5 июня 2024 года.",
        "Покажи событие Олега за 5 июня 2024 года.",
        "Проверь этот файл и покажи событие в нём от 5 июня 2024 года.",
        "Что было в среде разработки?",
        "Что было 5 июня 2024 года и что запланировано завтра?",
        "Что было 5 июня 2024 года в 12:00 UTC?",
    ],
)
@pytest.mark.asyncio
async def test_counterexamples_never_gain_a_timeline_call(settings, storage, monkeypatch, question):
    storage.ensure_user("alice", preset_key="owner")
    kernel, model = Kernel(), HostileModel()
    runtime = runtime_for(settings, storage, monkeypatch, kernel, model)
    await runtime.chat(
        "alice", question, actor=ActorContext(user_id="alice", preset_key="owner", source="test")
    )
    assert kernel.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("offered,corrupt", [(False, False), (True, True)])
async def test_absent_schema_and_unverified_payload_cannot_publish_events(
    settings, storage, monkeypatch, offered, corrupt
):
    storage.ensure_user("alice", preset_key="owner")
    kernel, model = Kernel(offered=offered, corrupt=corrupt), HostileModel(replay=True)
    runtime = runtime_for(settings, storage, monkeypatch, kernel, model)
    result = await runtime.chat(
        "alice", CASES[0][0], actor=ActorContext(user_id="alice", preset_key="owner", source="test")
    )
    assert result["tools_used"] == (["what_happened"] if offered else [])
    assert json.dumps(VALUE, ensure_ascii=False) not in result["message"]
    assert "WRONG-OPAQUE-VALUE" not in result["message"]
    assert len(kernel.calls) == int(offered)


def test_file_authority_keeps_real_source_and_releases_temporal_check():
    assert not file_turn_authority(CASES[0][0]).proved("local_read")
    assert file_turn_authority("Проверь файл с событием за 5 июня 2024 года.").proved("local_read")


@pytest.mark.parametrize(
    "question",
    [
        "Изолируйте интервал 2 мая 2023 года и назовите попавшее в него событие.",
        "В суточном окне 29 февраля 2024 года есть один факт; назовите его.",
    ],
)
def test_bound_calendar_pronoun_cannot_become_attachment_authority(question):
    assert not file_turn_authority(question).proved("local_read")


@pytest.mark.parametrize(
    "question",
    [
        "Изолируй интервал 6 июня 2024 года и назови попавшее в него событие; прочитай файл report.txt.",
        "Изолируй интервал 6 июня 2024 года и назови попавшее в него событие; покажи содержимое этого файла.",
        "Покажи событие за 6 июня 2024 года в нём.",
    ],
)
def test_temporal_anaphor_does_not_hide_an_independent_file_source(question):
    assert file_turn_authority(question).proved("local_read")


@pytest.mark.asyncio
async def test_structural_calendar_read_preserves_independent_request(settings, storage, monkeypatch):
    class RemainderModel(HostileModel):
        async def chat(self, messages, **kwargs):
            return {"content": json.dumps({"остаток": "объясни рекурсию"}, ensure_ascii=False)}

    storage.ensure_user("alice", preset_key="owner")
    kernel = Kernel()
    runtime = runtime_for(settings, storage, monkeypatch, kernel, RemainderModel())
    context = AgentContext(user_id="alice", conversation_id="unit", outward_verdict=("архив", None))
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    await runtime._prefetch_the_timeline_if_asked(
        "Проверь временной индекс за 19 февраля 2023 года; объясни рекурсию",
        actor,
        kernel.get_tool_definitions(actor),
        [],
        [],
        [],
        context,
    )
    assert [name for name, _ in kernel.calls] == ["what_happened"]
    assert json.dumps(VALUE, ensure_ascii=False) in context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == "объясни рекурсию"
