"""Exact absolute reminders do not depend on a probabilistic classifier."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _exact_absolute_reminder_request,
)
from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.web_surfer import WebSurfer

FIXTURES = Path(__file__).parent / "fixtures"


def _frozen_questions(battery: str) -> list[str]:
    manifest = json.loads((FIXTURES / f"synthetic_live_battery_{battery.casefold()}.json").read_text())
    reminder_pass = next(item for item in manifest["passes"] if item["pass_id"] == f"{battery}-P08")
    return list(reminder_pass["questions"])


FROZEN_REMINDERS = [
    (battery, index, question)
    for battery in ("A", "B")
    for index, question in enumerate(_frozen_questions(battery), start=1)
]


class _Classifier:
    def __init__(self, payload: str = "") -> None:
        self.enabled = True
        self.payload = payload
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        self.calls += 1
        if not self.payload:
            pytest.fail("an exact absolute reminder reached the classifier")
        return {"content": self.payload}


class _RecordingKernel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def execute(self, tool: str, params: dict[str, str], actor=None):  # noqa: ANN001, ARG002
        self.calls.append((tool, params))

        class _Result:
            success = True
            error = ""
            data = {
                "created": True,
                "what": params["what"],
                "on": params["when"],
                "at": "",
                "requested_when": params["when"],
                "delivery_scheduled": True,
            }

            def to_llm_message(self) -> str:
                return "Напоминание поставлено."

        return _Result()


class _OutboundTrapRuntime:
    def __init__(self) -> None:
        self.kernel = _RecordingKernel()

    async def _mentions_someone_from_the_archive(self, message, actor):  # noqa: ANN001, ARG002
        return False


def _runtime(*, classifier_payload: str = "") -> AgentRuntime:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _RecordingKernel()
    runtime.llm = _Classifier(classifier_payload)
    runtime.settings = None
    return runtime


def _prefetch(runtime: AgentRuntime, message: str):
    context = AgentContext(conversation_id="c", user_id="u", outward_verdict=("", None))
    messages: list[dict] = []
    used: list[str] = []
    evidence: list[dict[str, str]] = []
    tools = [
        {"function": {"name": "remind"}},
        {"function": {"name": "memory_search"}},
        {"function": {"name": "remind"}},
    ]
    bound = AgentRuntime._prefetch_a_reminder_if_asked.__get__(runtime, AgentRuntime)
    done = asyncio.run(bound(message, context, None, tools, messages, used, evidence))
    return done, context, tools, used, evidence


@pytest.mark.parametrize(("battery", "index", "question"), FROZEN_REMINDERS)
def test_every_frozen_exact_reminder_is_created_without_classifier_variance(
    battery: str,
    index: int,
    question: str,
) -> None:
    runtime = _runtime()

    done, context, tools, used, evidence = _prefetch(runtime, question)

    month = 9 if battery == "A" else 10
    marker = f"SYN-REMINDER-{battery}08-{index:02d}"
    expected_when = f"2035-{month:02d}-{index:02d}"
    assert done is True
    assert runtime.llm.calls == 0
    assert runtime.kernel.calls == [("remind", {"what": marker, "when": expected_when})]
    assert used == ["remind"]
    assert [tool["function"]["name"] for tool in tools] == ["memory_search"]
    assert [item["tool"] for item in evidence] == ["remind"]
    assert context.remainder_known is True and context.open_remainder == ""
    assert context.structural_answer.count(marker) == 1


@pytest.mark.parametrize(
    "message",
    [
        "Стоит ли поставить напоминание «купить молоко» на 5 сентября 2035 года?",
        "Отмени напоминание «купить молоко» на 5 сентября 2035 года.",
        "В примере «поставь напоминание на 5 сентября 2035 года» точный текст «купить молоко».",
        "Если отчёт одобрят, поставь напоминание на 5 сентября 2035 года с текстом «сдать отчёт».",
        "Поставь напоминание на 5 сентября 2035 года с текстом «сдать отчёт», если его одобрят.",
        "Сохрани точный текст «купить молоко» в документ от 5 сентября 2035 года.",
        "Не ставь напоминание «купить молоко» на 5 сентября 2035 года.",
    ],
    ids=[
        "query",
        "cancel",
        "quoted",
        "conditional-prefix",
        "conditional-suffix",
        "not-a-reminder",
        "negated",
    ],
)
def test_adversarial_non_requests_never_take_the_deterministic_effect_path(message: str) -> None:
    assert _exact_absolute_reminder_request(message) is None
    runtime = _runtime(classifier_payload='{"напоминание": "нет", "что": "", "когда": "", "остаток": ""}')

    done, context, tools, used, evidence = _prefetch(runtime, message)

    assert done is False
    assert runtime.kernel.calls == []
    assert context.structural_answer == ""
    assert used == [] and evidence == []
    assert [tool["function"]["name"] for tool in tools] == ["memory_search"]


def test_an_unmistakable_compound_remainder_survives_after_the_effect() -> None:
    runtime = _runtime()

    done, context, tools, used, _ = _prefetch(
        runtime,
        "Поставь напоминание на 5 сентября 2035 года с точным текстом «сдать отчёт», "
        "и расскажи статус проекта.",
    )

    assert done is True and used == ["remind"]
    assert context.open_remainder == "расскажи статус проекта."
    assert context.remainder_known is True
    assert [tool["function"]["name"] for tool in tools] == ["memory_search"]


def test_an_exact_structured_form_is_closed_before_any_web_prefetch() -> None:
    runtime = _OutboundTrapRuntime()
    context = AgentContext(
        conversation_id="c",
        user_id="u",
        outward_verdict=("интернет", "private reminder body"),
    )
    bound = AgentRuntime._prefetch_the_web_if_asked.__get__(runtime, AgentRuntime)

    asyncio.run(
        bound(
            "Сохрани due 5 сентября 2035 года и body «закрытая тема» дословно.",
            None,
            [{"function": {"name": "web_research"}}],
            [],
            [],
            [],
            notice=[],
            context=context,
        )
    )

    assert runtime.kernel.calls == []


def test_the_exact_path_keeps_authorization_and_never_calls_the_classifier() -> None:
    runtime = _runtime()
    context = AgentContext(conversation_id="c", user_id="u", outward_verdict=("", None))
    bound = AgentRuntime._prefetch_a_reminder_if_asked.__get__(runtime, AgentRuntime)

    done = asyncio.run(
        bound(
            "Поставь напоминание на 5 сентября 2035 года с текстом «сдать отчёт».",
            context,
            None,
            [],
            [],
            [],
            [],
        )
    )

    assert done is False
    assert runtime.kernel.calls == []
    assert runtime.llm.calls == 0


def test_one_exact_request_leaves_one_effect_and_one_audited_invocation(settings, storage) -> None:
    storage.ensure_user("boss", preset_key="owner")
    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    actor = authorization.actor_for_user("boss", source="test")
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = kernel
    runtime.llm = _Classifier()
    runtime.settings = settings
    tools = [{"function": {"name": "remind"}}]
    context = AgentContext(conversation_id="c", user_id="boss", outward_verdict=("", None))
    used: list[str] = []
    evidence: list[dict[str, str]] = []
    planned = date(date.today().year + 2, 9, 5)
    message = f"Поставь напоминание на 5 сентября {planned.year} года с точным текстом «сдать отчёт»."
    bound = AgentRuntime._prefetch_a_reminder_if_asked.__get__(runtime, AgentRuntime)

    first = asyncio.run(bound(message, context, actor, tools, [], used, evidence))
    second = asyncio.run(bound(message, context, actor, tools, [], used, evidence))

    assert first is True and second is False
    assert runtime.llm.calls == 0
    assert used == ["remind"] and tools == []
    entities = storage.execute(
        "SELECT id FROM entities WHERE user_id=? AND name=?",
        (actor.own_id, "сдать отчёт"),
    ).fetchall()
    assert len(entities) == 1
    entity_id = str(entities[0]["id"])
    assert (
        storage.execute(
            "SELECT COUNT(*) AS count FROM entity_time WHERE entity_id=? AND occurred_at=?",
            (entity_id, planned.isoformat()),
        ).fetchone()["count"]
        == 1
    )
    assert (
        storage.execute(
            "SELECT COUNT(*) AS count FROM private_entity_owners WHERE entity_id=? AND person_id=?",
            (entity_id, actor.own_id),
        ).fetchone()["count"]
        == 1
    )
    audit_rows = storage.execute(
        "SELECT after_json FROM audit_log WHERE user_id=? AND action='tool.invoke' "
        "AND target_type='tool' AND target_id='remind' ORDER BY rowid",
        (actor.own_id,),
    ).fetchall()
    assert [json.loads(row["after_json"])["reason"] for row in audit_rows] == ["started", "ok"]
