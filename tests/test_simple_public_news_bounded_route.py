"""Bounded full-chat contract for one simple public-news request.

The fixtures are synthetic: they do not read a live conversation and never
contact a model, network provider, service, or production database.  This file
pins the narrow route which owns one self-contained news roundup; compound
web/effect requests deliberately remain on the ordinary agentic route.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

import friday.agent_runtime as agent_runtime
from friday.agent_runtime import AgentContext, AgentRuntime
from friday.agent_runtime.llm import LLMRouter
from friday.execution_kernel import ToolResult
from friday.orchestration.capability_outcome import (
    CapabilityOutcomeError,
    CapabilityOutcomeStatus,
    load_accepted_capability_outcome_receipt,
)
from friday.orchestration.simple_public_news_outcome import SimplePublicNewsOutcomeError
from friday.permissions import ActorContext, AuthorizationService

OWNER = "simple_public_news_owner"
REQUEST = "Сделай сводку по новостям СВО на зарубежных сайтах"
PUBLIC_URL = "https://foreign.synthetic.example.com/svo-update"
PUBLIC_TITLE = "Synthetic foreign bulletin"
PUBLIC_FACT = "Synthetic foreign bulletin reports a confirmed dated Ukraine development."
LONG_EVIDENCE_TAIL = "TAIL-EVIDENCE confirms the bounded verifier received the final source bytes."
UNSUPPORTED_FACT = "UNSUPPORTED-SYNTHESIS claims a fictional development absent from every source."
REPAIRED_FACT = "The repaired roundup reports only the confirmed dated Ukraine development."
VERIFIER_PRIVATE_SENTINEL = "VERIFIER-SENTINEL-MUST-STAY-PRIVATE"


def _actor() -> ActorContext:
    return ActorContext(user_id=OWNER, preset_key="owner", source="synthetic-test")


class _AllowAll:
    def authorize(self, actor, capability, **kwargs):  # noqa: ANN001, ARG002
        return SimpleNamespace(allowed=True)


def _tool(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "synthetic bounded capability",
            "parameters": {"type": "object"},
        },
    }


async def _prepare_without_retrieval(
    user_id: str,
    message: str,
    conversation_id: str,
    **kwargs: Any,
) -> AgentContext:
    del kwargs
    return AgentContext(
        conversation_id=conversation_id,
        user_id=user_id,
        person_id=user_id,
        conversation_history=[],
        search_query=message,
        outward_verdict=("интернет", AgentRuntime.web_query_from(message)),
    )


class _SyntheticNewsKernel:
    authorization = _AllowAll()

    def __init__(self, *, include_reminder: bool = True, source_text: str = PUBLIC_FACT) -> None:
        self.include_reminder = include_reminder
        self.source_text = source_text
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
        definitions = [_tool("web_research")]
        if self.include_reminder:
            definitions.append(_tool("remind"))
        return definitions

    def get_tool(self, name: str):  # noqa: ANN201
        if name != "web_research":
            return None
        return SimpleNamespace(
            name="web_research",
            security_id="web.research",
            risk="mutate",
        )

    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        assert tool == "web_research", "the simple route selected a second capability"
        self.calls.append((str(tool), dict(params)))
        freshness = str(params.get("freshness") or "")
        return ToolResult(
            tool,
            True,
            {
                "outbound_attempted": True,
                "query": str(params.get("query") or ""),
                "freshness": freshness,
                "applied_search_filters": {"freshness": freshness},
                "sources": [
                    {
                        "url": PUBLIC_URL,
                        "title": PUBLIC_TITLE,
                        "text": self.source_text,
                        "text_length": len(self.source_text),
                        "status_code": 200,
                        "error": "",
                        "truncated": False,
                    }
                ],
                "requested_sources": 1,
                "completed_sources": 1,
                "failed_sources": 0,
                "timed_out_sources": 0,
                "search_timed_out": False,
            },
        )


class _AirportCollisionKernel(_SyntheticNewsKernel):
    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        assert tool == "web_research"
        self.calls.append((str(tool), dict(params)))
        freshness = str(params.get("freshness") or "")
        airport = "Sheremetyevo is the largest airport in Russia; departure board and terminal schedule."
        return ToolResult(
            tool,
            True,
            {
                "outbound_attempted": True,
                "query": str(params.get("query") or ""),
                "freshness": freshness,
                "applied_search_filters": {"freshness": freshness},
                "sources": [
                    {
                        "url": "https://www.svo.aero/en/timetable/departures/",
                        "title": "SVO airport departures",
                        "text": airport,
                        "text_length": len(airport),
                        "status_code": 200,
                        "error": "",
                        "truncated": False,
                    }
                ],
                "requested_sources": 1,
                "completed_sources": 1,
                "failed_sources": 0,
                "timed_out_sources": 0,
                "search_timed_out": False,
            },
        )


class _MixedCollisionKernel(_SyntheticNewsKernel):
    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        assert tool == "web_research"
        self.calls.append((str(tool), dict(params)))
        freshness = str(params.get("freshness") or "")
        airport = "Sheremetyevo is the largest airport in Russia; departure board and terminal schedule."
        return ToolResult(
            tool,
            True,
            {
                "outbound_attempted": True,
                "query": str(params.get("query") or ""),
                "freshness": freshness,
                "applied_search_filters": {"freshness": freshness},
                "sources": [
                    {
                        "url": PUBLIC_URL,
                        "title": PUBLIC_TITLE,
                        "text": PUBLIC_FACT,
                        "text_length": len(PUBLIC_FACT),
                        "status_code": 200,
                        "error": "",
                        "truncated": False,
                    },
                    {
                        "url": "https://www.svo.aero/en/timetable/departures/",
                        "title": "SVO airport departures",
                        "text": airport,
                        "text_length": len(airport),
                        "status_code": 200,
                        "error": "",
                        "truncated": False,
                    },
                ],
                "requested_sources": 2,
                "completed_sources": 2,
                "failed_sources": 0,
                "timed_out_sources": 0,
                "search_timed_out": False,
            },
        )


class _EmptyNewsKernel(_SyntheticNewsKernel):
    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        assert tool == "web_research"
        self.calls.append((str(tool), dict(params)))
        freshness = str(params.get("freshness") or "")
        return ToolResult(
            tool,
            True,
            {
                "outbound_attempted": True,
                "query": str(params.get("query") or ""),
                "freshness": freshness,
                "applied_search_filters": {"freshness": freshness},
                "source_class": str(params.get("source_class") or ""),
                "sources": [],
                "requested_sources": 0,
                "completed_sources": 0,
                "failed_sources": 0,
                "timed_out_sources": 0,
                "search_timed_out": False,
            },
        )


class _UnavailableNewsKernel(_EmptyNewsKernel):
    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        result = await super().execute(tool, params, actor=actor)
        result.data["search_failed"] = True
        result.data["error"] = "provider unavailable"
        return result


class _MalformedEmptySourceClassNewsKernel(_EmptyNewsKernel):
    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        result = await super().execute(tool, params, actor=actor)
        result.data["source_class"] = False
        return result


class _TopicMismatchNewsKernel(_EmptyNewsKernel):
    transport_success = True
    source_class_satisfied: Any = True

    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        result = await super().execute(tool, params, actor=actor)
        result.data.update(
            {
                "target_sources": 1,
                "requested_sources": 1,
                "failed_sources": 1,
                "topic_filtered_sources": 1,
                "search_failed": True,
                "error": "topic_mismatch",
                "source_class_satisfied": self.source_class_satisfied,
                "topic_class": str(params.get("topic_class") or ""),
                "topic_class_satisfied": False,
            }
        )
        return ToolResult(tool, self.transport_success, result.data)


class _MalformedTopicMismatchNewsKernel(_TopicMismatchNewsKernel):
    source_class_satisfied = 0


class _TransportFailedTopicMismatchNewsKernel(_TopicMismatchNewsKernel):
    transport_success = False


class _MalformedTargetNewsKernel(_SyntheticNewsKernel):
    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        result = await super().execute(tool, params, actor=actor)
        result.data["target_sources"] = "3"
        return result


class _MalformedSourceMetadataNewsKernel(_SyntheticNewsKernel):
    def __init__(self, variant: str) -> None:
        super().__init__()
        self.variant = variant

    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        result = await super().execute(tool, params, actor=actor)
        result.data["target_sources"] = 1
        if self.variant == "non_json_object":
            snippet: Any = object()
        else:
            snippet = {"payload": "x" * 100_000}
            for _level in range(16):
                snippet = {"nested": [snippet]}
        result.data["sources"][0]["snippet"] = snippet
        return result


class _OverreturningNewsKernel(_SyntheticNewsKernel):
    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        result = await super().execute(tool, params, actor=actor)
        for index in range(2, 5):
            text = f"{PUBLIC_FACT} Synthetic corroborating source {index}."
            result.data["sources"].append(
                {
                    "url": f"https://foreign-{index}.synthetic.example.com/news",
                    "title": f"Synthetic source {index}",
                    "text": text,
                    "text_length": len(text),
                    "status_code": 200,
                    "error": "",
                    "truncated": False,
                }
            )
        result.data["requested_sources"] = 4
        result.data["completed_sources"] = 4
        return result


class _RefilledTargetNewsKernel(_SyntheticNewsKernel):
    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        result = await super().execute(tool, params, actor=actor)
        for index in range(2, 4):
            text = f"{PUBLIC_FACT} Synthetic refill source {index}."
            result.data["sources"].append(
                {
                    "url": f"https://foreign-refill-{index}.synthetic.example.com/news",
                    "title": f"Synthetic refill {index}",
                    "text": text,
                    "text_length": len(text),
                    "status_code": 200,
                    "error": "",
                    "truncated": False,
                }
            )
        result.data.update(
            {
                "target_sources": 3,
                "requested_sources": 4,
                "completed_sources": 3,
                "failed_sources": 1,
            }
        )
        return result


class _LegacyDirectOverflowNewsKernel(_SyntheticNewsKernel):
    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        result = await super().execute(tool, params, actor=actor)
        text = f"{PUBLIC_FACT} Synthetic direct adapter corroboration."
        result.data["sources"].append(
            {
                "url": "https://foreign-direct.synthetic.example.com/news",
                "title": "Synthetic direct source",
                "text": text,
                "text_length": len(text),
                "status_code": 200,
                "error": "",
                "truncated": False,
            }
        )
        result.data["requested_sources"] = 1
        result.data["completed_sources"] = 2
        return result


class _OversizedTargetNewsKernel(_OverreturningNewsKernel):
    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        result = await super().execute(tool, params, actor=actor)
        result.data["target_sources"] = 4
        return result


class _WrongQueryNewsKernel(_SyntheticNewsKernel):
    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        result = await super().execute(tool, params, actor=actor)
        result.data["query"] = "different provider query"
        return result


class _CanonicalDuplicateTargetNewsKernel(_OverreturningNewsKernel):
    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        result = await super().execute(tool, params, actor=actor)
        result.data["sources"][1]["url"] = "https://foreign.synthetic.example.com:443/svo-update#duplicate"
        result.data["target_sources"] = 3
        return result


class _TargetMixedCollisionKernel(_RefilledTargetNewsKernel):
    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        result = await super().execute(tool, params, actor=actor)
        result.data["topic_filtered_sources"] = 1
        return result


class _DuplicateNewsKernel(_SyntheticNewsKernel):
    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        result = await super().execute(tool, params, actor=actor)
        result.data["sources"].append(dict(result.data["sources"][0]))
        result.data["requested_sources"] = 2
        result.data["completed_sources"] = 2
        return result


class _LegacyMissingRowFieldsNewsKernel(_SyntheticNewsKernel):
    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        result = await super().execute(tool, params, actor=actor)
        result.data["sources"][0].pop("error")
        result.data["sources"][0].pop("truncated")
        return result


class _OneShotNewsModel:
    enabled = True
    model = "synthetic-one-shot-news"
    total_budget_sec = 360.0

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001
        snapshot = {
            "messages": [dict(item) for item in messages],
            "tools": tools,
            "kwargs": dict(kwargs),
        }
        self.calls.append(snapshot)
        assert tools == [], "a bounded news model stage was offered an agentic tool schema"
        rendered = json.dumps(messages, ensure_ascii=False)
        assert REQUEST in rendered
        assert PUBLIC_FACT in rendered
        assert PUBLIC_TITLE in rendered
        assert PUBLIC_URL in rendered
        assert "svo.aero" not in rendered.casefold()
        assert "airport departures" not in rendered.casefold()
        if "FRIDAY_VERIFICATION_DATA" in rendered:
            return {
                "content": json.dumps(
                    {
                        "ok": True,
                        "request_satisfied": True,
                        "score": 1.0,
                        "issues": [],
                    }
                ),
                "tool_calls": None,
                "finish_reason": "stop",
                "_queue_wait_sec": 0.0,
            }
        return {
            "content": f"{PUBLIC_FACT}\n\nИсточник: [{PUBLIC_TITLE}]({PUBLIC_URL})",
            "tool_calls": None,
            "_queue_wait_sec": 0.0,
        }


class _RevokingNewsModel(_OneShotNewsModel):
    def __init__(self, authorization: AuthorizationService) -> None:
        super().__init__()
        self.authorization = authorization
        self.revoked = False

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001
        if not self.revoked:
            self.authorization.deny_permission(OWNER, "web.research")
            self.revoked = True
        return await super().chat(messages, tools=tools, **kwargs)


class _ProductionShapeNewsRouter(LLMRouter):
    """An LLMRouter instance without network I/O, to pin production kwargs."""

    def __init__(self, settings) -> None:  # noqa: ANN001
        super().__init__(replace(settings, llm_enabled=True))
        self.retry_flags: list[bool] = []

    async def chat(self, messages, *, tools=None, allow_retries=True, **_kwargs):  # noqa: ANN001
        self.retry_flags.append(bool(allow_retries))
        assert tools == []
        rendered = json.dumps(messages, ensure_ascii=False)
        assert PUBLIC_FACT in rendered
        if "FRIDAY_VERIFICATION_DATA" in rendered:
            return {
                "content": json.dumps(
                    {
                        "ok": True,
                        "request_satisfied": True,
                        "score": 1.0,
                        "issues": [],
                    }
                ),
                "tool_calls": None,
                "finish_reason": "stop",
                "_queue_wait_sec": 0.0,
            }
        return {
            "content": f"{PUBLIC_FACT}\n\nИсточник: [{PUBLIC_TITLE}]({PUBLIC_URL})",
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


class _CancellableHangingModel:
    enabled = True
    model = "synthetic-cancellable-hang"
    total_budget_sec = 360.0

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.cancelled = asyncio.Event()

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001
        self.calls.append(
            {
                "messages": [dict(item) for item in messages],
                "tools": tools,
                "kwargs": dict(kwargs),
            }
        )
        assert tools == [], "the hanging synthesis received effect authority"
        rendered = json.dumps(messages, ensure_ascii=False)
        assert PUBLIC_FACT in rendered
        assert PUBLIC_URL in rendered
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("an unset asyncio.Event unexpectedly completed")


class _MixedOneShotNewsModel(_OneShotNewsModel):
    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001
        rendered = "\n".join(str(item.get("content") or "") for item in messages)
        if "FRIDAY_VERIFICATION_DATA" in rendered:
            return await super().chat(messages, tools=tools, **kwargs)
        assert '"completed_sources": 1' in rendered, rendered
        assert '"failed_sources": 1' in rendered, rendered
        assert '"requested_sources": 2' in rendered, rendered
        assert "Accepted 1 readable public source." in rendered, rendered
        assert '"completed_sources": 2' not in rendered, rendered
        return await super().chat(messages, tools=tools, **kwargs)


class _RepairingNewsModel:
    enabled = True
    model = "synthetic-repairing-news"
    total_budget_sec = 360.0

    def __init__(self, *, final_passes: bool) -> None:
        self.final_passes = final_passes
        self.calls: list[dict[str, Any]] = []
        self.stages: list[str] = []
        self.verifier_calls = 0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001
        rendered = json.dumps(messages, ensure_ascii=False)
        self.calls.append(
            {
                "messages": [dict(item) for item in messages],
                "tools": tools,
                "kwargs": dict(kwargs),
            }
        )
        assert tools == []
        if "FRIDAY_VERIFICATION_DATA" in rendered:
            self.stages.append("verify")
            self.verifier_calls += 1
            assert LONG_EVIDENCE_TAIL in rendered
            passes = self.verifier_calls == 2 and self.final_passes
            return {
                "content": json.dumps(
                    {
                        "ok": passes,
                        "request_satisfied": passes,
                        "score": 1.0 if passes else 0.0,
                        "issues": [] if passes else [VERIFIER_PRIVATE_SENTINEL],
                    }
                ),
                "finish_reason": "stop",
            }
        if "FRIDAY_REPAIR_DATA" in rendered:
            self.stages.append("repair")
            assert LONG_EVIDENCE_TAIL in rendered
            return {
                "content": REPAIRED_FACT if self.final_passes else f"{UNSUPPORTED_FACT} Again.",
                "finish_reason": "stop",
            }
        self.stages.append("synthesis")
        assert LONG_EVIDENCE_TAIL in rendered
        return {"content": UNSUPPORTED_FACT, "finish_reason": "stop"}


class _UnknownVerifierNewsModel(_OneShotNewsModel):
    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001
        rendered = json.dumps(messages, ensure_ascii=False)
        if "FRIDAY_VERIFICATION_DATA" in rendered:
            self.calls.append(
                {
                    "messages": [dict(item) for item in messages],
                    "tools": tools,
                    "kwargs": dict(kwargs),
                }
            )
            assert tools == []
            return {"content": "verifier unavailable", "finish_reason": "stop"}
        return await super().chat(messages, tools=tools, **kwargs)


class _ExhaustiveNewsModel(_OneShotNewsModel):
    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001
        rendered = json.dumps(messages, ensure_ascii=False)
        result = await super().chat(messages, tools=tools, **kwargs)
        if "FRIDAY_VERIFICATION_DATA" not in rendered:
            result["content"] = f"Это полный обзор. {PUBLIC_FACT}"
        return result


async def _forbid_secondary_stage(*args, **kwargs):  # noqa: ANN002, ANN003
    raise AssertionError("simple public-news route entered a forbidden secondary stage")


async def _forbid_repair(*args, **kwargs):  # noqa: ANN002, ANN003
    raise AssertionError("a passed simple public-news synthesis entered repair")


def _runtime(
    settings,
    storage,
    *,
    model: Any,
    kernel: Any,
    verify_answers: bool = True,
) -> AgentRuntime:
    storage.ensure_user(OWNER, preset_key="owner")
    return AgentRuntime(
        replace(
            settings,
            verify_answers=verify_answers,
            verify_min_answer_chars=1,
            llm_timeout_sec=240.0,
        ),
        storage,
        llm=model,
        kernel=kernel,
    )


def _telegram_body(reply: dict[str, Any]) -> str:
    from friday.telegram_bridge._callbacks import CallbacksMixin

    return CallbacksMixin._format_response_message(reply)  # noqa: SLF001


def _stored_news_receipt(storage, reply):  # noqa: ANN001, ANN201
    stored = storage.get_message(str(reply["message_id"]), OWNER)
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    return load_accepted_capability_outcome_receipt(metadata), metadata


@pytest.mark.parametrize(
    "news_prompt",
    (
        "Расскажи новости с международных сайтов",
        "Show international news from international media",
    ),
)
def test_international_news_wording_preserves_the_foreign_source_class(news_prompt: str) -> None:
    authority = agent_runtime.file_turn_authority(news_prompt)
    assert authority.actions == frozenset({"web"})
    assert agent_runtime._public_news_site_request(authority.speech)  # noqa: SLF001
    assert agent_runtime._web_source_class_on_speech(authority.speech) == "foreign"  # noqa: SLF001


@pytest.mark.parametrize(
    ("news_prompt", "expected_freshness"),
    (
        ("Сделай сводку новостей на зарубежных сайтах", "day"),
        ("Сделай сводку сегодняшних новостей на зарубежных сайтах", "day"),
        ("Сделай сводку новостей за последнюю неделю на зарубежных сайтах", "week"),
        ("Show international news from the last month", "month"),
        ("Show international news from the past year", "year"),
        ("Собери новости зарубежных СМИ последние 7 дней.", "week"),
        ("Show world news over the last 7 days.", "week"),
        ("Какие мировые новости вышли вчера?", None),
        ("Сделай сводку новостей за июль 2025 года на зарубежных сайтах", None),
        ("Сделай сводку новостей за последние 2 недели на зарубежных сайтах", None),
        ("Собери новости зарубежных СМИ на этой неделе.", None),
        ("Собери новости зарубежных СМИ этой недели.", None),
        ("Собери новости зарубежных СМИ за понедельник.", None),
        ("Собери новости зарубежных СМИ предыдущей недели.", None),
        ("Show foreign media news since Monday.", None),
        ("Show world news this week.", None),
        ("Привет! дай сводку по новостям СВО за 24 число", None),
        ("Пожалуйста, подготовь новости экономики за 24.08.2026", None),
        ("Можно сводку новостей за 24 число?", None),
        ("Сводку новостей за 24 число, пожалуйста", None),
        ("Что было в новостях 24 числа?", None),
        ("Можешь дать сводку новостей за 24 число?", None),
        ("Подведи итоги новостей за 24 число", None),
        ("Give me news for August 24", None),
        ("Please give me news for August 24", None),
        ("Tell me the news for August 24", None),
        ("Пятница, дай сводку новостей за 24 число", None),
        ("Каковы главные новости за 24 число?", None),
        ("Give me news for August 24, 2026", None),
        ("Дай новости за 2026-08-24", None),
        ("Дай новости России, Украины и мира за 24 августа", None),
        ("Show latest news about OpenAI", "day"),
        ("Расскажи последние новости науки", "day"),
        ("Свежие новости за прошедшие сутки покажешь?", "day"),
    ),
)
def test_simple_news_never_narrows_an_explicit_time_window(
    news_prompt: str,
    expected_freshness: str | None,
) -> None:
    authority = agent_runtime.file_turn_authority(news_prompt)

    assert authority.actions == frozenset({"web"})
    assert agent_runtime._public_news_site_request(authority.speech)  # noqa: SLF001
    assert (  # noqa: SLF001
        agent_runtime._simple_public_news_freshness(authority.speech) == expected_freshness
    )


@pytest.mark.asyncio
async def test_simple_news_prefetch_uses_the_parsed_week_window(settings, storage) -> None:
    prompt = "Сделай сводку новостей за последнюю неделю на зарубежных сайтах"
    kernel = _SyntheticNewsKernel()
    runtime = _runtime(settings, storage, model=_OneShotNewsModel(), kernel=kernel)
    context = AgentContext(
        conversation_id="weekly-news",
        user_id=OWNER,
        person_id=OWNER,
        outward_verdict=("интернет", "world news last week"),
        isolated_outbound_turn=True,
    )
    messages: list[dict[str, Any]] = []
    tools_used: list[str] = []
    tool_evidence: list[dict[str, str]] = []

    await runtime._prefetch_the_web_if_asked(  # noqa: SLF001
        prompt,
        _actor(),
        [_tool("web_research")],
        messages,
        tools_used,
        tool_evidence,
        [],
        context,
    )

    assert kernel.calls == [
        (
            "web_research",
            {
                "query": runtime.web_query_from(prompt),
                "max_sources": 3,
                "freshness": "week",
                "source_class": "foreign",
            },
        )
    ]
    assert context.web_evidence_status == "sourced"
    assert len(tool_evidence) == 1
    assert '"freshness": "week"' in tool_evidence[0]["output"]


class _OrdinaryHistoricalNewsModel:
    enabled = True
    model = "synthetic-ordinary-historical-news"
    total_budget_sec = 360.0

    def __init__(self, prompt: str = "Какие мировые новости вышли вчера?") -> None:
        self.prompt = prompt
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001
        self.calls.append(
            {
                "messages": [dict(item) for item in messages],
                "tools": tools,
                "kwargs": dict(kwargs),
            }
        )
        assert tools == [], "an isolated historical-news turn retained effect authority"
        rendered = json.dumps(messages, ensure_ascii=False)
        assert self.prompt in rendered
        assert PUBLIC_FACT in rendered
        return {
            "content": f"Обычный маршрут: {PUBLIC_FACT}",
            "tool_calls": None,
            "finish_reason": "stop",
        }


@pytest.mark.parametrize(
    ("prompt", "expected_query", "expected_source_class"),
    (
        ("Какие мировые новости вышли вчера?", "Какие мировые новости вышли вчера?", "foreign"),
        (
            "Привет! дай сводку по новостям СВО за 24 число",
            "сводку по новостям СВО за 24 число",
            "",
        ),
        (
            "Пожалуйста, подготовь новости экономики за 24.08.2026",
            "подготовь новости экономики за 24.08.2026",
            "",
        ),
        ("Можно сводку новостей за 24 число?", "Можно сводку новостей за 24 число?", ""),
        (
            "Сводку новостей за 24 число, пожалуйста",
            "Сводку новостей за 24 число",
            "",
        ),
        ("Что было в новостях 24 числа?", "Что было в новостях 24 числа?", ""),
        ("Give me news for August 24", "Give me news for August 24", ""),
        ("Please give me news for August 24", "Please give me news for August 24", ""),
        ("Tell me the news for August 24", "Tell me the news for August 24", ""),
    ),
)
@pytest.mark.asyncio
async def test_unsupported_historical_news_stays_isolated_without_a_day_filter(
    prompt: str,
    expected_query: str,
    expected_source_class: str,
    settings,
    storage,
    monkeypatch,
) -> None:
    private_canary = "PRIVATE-NEWS-HISTORY-MUST-NOT-LEAVE"
    storage.ensure_user(OWNER, preset_key="owner")
    conversation = storage.create_conversation(OWNER, title="private-news-history")
    conversation_id = str(conversation["id"])
    storage.store_message(
        conversation_id,
        OWNER,
        "user",
        private_canary,
        metadata={"had_attachments": True, "private_context_lineage": True},
    )
    kernel = _SyntheticNewsKernel()
    model = _OrdinaryHistoricalNewsModel(prompt)
    runtime = _runtime(
        settings,
        storage,
        model=model,
        kernel=kernel,
        verify_answers=False,
    )

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("historical public-news turn entered ambient context retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)

    reply = await runtime.chat(
        OWNER,
        prompt,
        actor=_actor(),
        conversation_id=conversation_id,
    )

    expected_arguments = {
        "query": expected_query,
        "max_sources": 3,
    }
    assert runtime.web_query_from(prompt) == expected_query
    if expected_source_class:
        expected_arguments["source_class"] = expected_source_class
    assert kernel.calls == [("web_research", expected_arguments)]
    assert len(model.calls) == 1
    assert reply["tools_used"] == ["web_research"]
    assert reply["web_evidence_status"] == "sourced"
    assert "Обычный маршрут" in reply["message"]
    assert private_canary not in json.dumps([model.calls, kernel.calls], ensure_ascii=False)


@pytest.mark.asyncio
async def test_simple_foreign_news_runs_one_fresh_research_then_mandatory_bounded_verifier(
    settings,
    storage,
    monkeypatch,
) -> None:
    kernel = _SyntheticNewsKernel()
    model = _OneShotNewsModel()
    runtime = _runtime(
        settings,
        storage,
        model=model,
        kernel=kernel,
        # The closed factual-news lane verifies independently of the broad
        # preference used for ordinary conversation.
        verify_answers=False,
    )

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    monkeypatch.setattr(runtime, "_repair_once", _forbid_repair)

    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert kernel.calls == [
        (
            "web_research",
            {
                "query": "Russia Ukraine war latest news",
                "max_sources": 3,
                "freshness": "day",
                "source_class": "foreign",
                "topic_class": "russia_ukraine_war_news",
            },
        )
    ]
    assert len(model.calls) == 2
    assert [call["tools"] for call in model.calls] == [[], []]
    verifier_prompt = json.dumps(model.calls[1]["messages"], ensure_ascii=False)
    assert "FRIDAY_VERIFICATION_DATA" in verifier_prompt
    assert PUBLIC_FACT in verifier_prompt
    assert reply["tools_used"] == ["web_research"]
    assert reply["web_evidence_status"] == "sourced"
    assert reply["web_sources"] == [{"url": PUBLIC_URL, "title": PUBLIC_TITLE}]
    assert PUBLIC_FACT in reply["message"]
    assert reply["verified"] is True
    assert reply["verification_status"] == "passed"
    assert PUBLIC_URL in _telegram_body(reply)
    assert "Russia Ukraine war latest news" in reply["web_query_notice"]
    receipt, metadata = _stored_news_receipt(storage, reply)
    assert receipt.outcome.status is CapabilityOutcomeStatus.COMPLETE
    assert "accepted_capability_outcome" in metadata
    assert "accepted_capability_outcome" not in reply
    assert "accepted_capability_outcome" not in _telegram_body(reply)
    private_receipt = json.dumps(metadata["accepted_capability_outcome"], ensure_ascii=False)
    for private_value in (REQUEST, PUBLIC_URL, PUBLIC_TITLE, PUBLIC_FACT):
        assert private_value not in private_receipt
    assert "raw_id" not in private_receipt.casefold()
    assert "inbox" not in private_receipt.casefold()


@pytest.mark.asyncio
async def test_simple_news_disables_production_transport_retries_after_web_effect(
    settings,
    storage,
    monkeypatch,
) -> None:
    kernel = _SyntheticNewsKernel()
    model = _ProductionShapeNewsRouter(settings)
    runtime = _runtime(settings, storage, model=model, kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    monkeypatch.setattr(runtime, "_repair_once", _forbid_repair)

    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert model.retry_flags == [False, False]
    assert len(kernel.calls) == 1
    assert PUBLIC_FACT in reply["message"]


@pytest.mark.asyncio
async def test_simple_foreign_news_cancels_hung_synthesis_and_keeps_source_fallback(
    settings,
    storage,
    monkeypatch,
) -> None:
    kernel = _SyntheticNewsKernel()
    model = _CancellableHangingModel()
    runtime = _runtime(settings, storage, model=model, kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    monkeypatch.setattr(runtime, "_verify_response", _forbid_secondary_stage)
    monkeypatch.setattr(runtime, "_repair_once", _forbid_secondary_stage)
    monkeypatch.setattr(agent_runtime, "_SIMPLE_PUBLIC_NEWS_TIMEOUT_SEC", 0.02)

    reply = await asyncio.wait_for(
        runtime.chat(OWNER, REQUEST, actor=_actor()),
        timeout=1.0,
    )

    assert model.cancelled.is_set(), "route timeout returned without cancelling its model task"
    assert len(model.calls) == 1, "a timeout started a salvage, verifier, or repair model call"
    assert kernel.calls == [
        (
            "web_research",
            {
                "query": "Russia Ukraine war latest news",
                "max_sources": 3,
                "freshness": "day",
                "source_class": "foreign",
                "topic_class": "russia_ukraine_war_news",
            },
        )
    ]
    assert reply["tools_used"] == ["web_research"]
    assert reply["web_evidence_status"] == "sourced"
    assert reply["web_sources"] == [{"url": PUBLIC_URL, "title": PUBLIC_TITLE}]
    fallback = reply["message"].casefold()
    assert "не успела" in fallback or "не удалось" in fallback
    assert "поиск в интернете не удался" not in reply["message"].casefold()
    assert f"- {PUBLIC_TITLE}:" not in reply["message"]
    delivered = _telegram_body(reply)
    assert PUBLIC_TITLE in delivered
    assert PUBLIC_URL in delivered

    stored = storage.get_message(str(reply["message_id"]), OWNER)
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    assert metadata["tools_used"] == ["web_research"]
    assert metadata["web_evidence_status"] == "sourced"
    assert metadata["web_sources"] == reply["web_sources"]
    assert metadata["structural"]["model_spoke"] is False
    assert (
        load_accepted_capability_outcome_receipt(metadata).outcome.status is CapabilityOutcomeStatus.PARTIAL
    )


@pytest.mark.asyncio
async def test_simple_svo_news_rejects_the_airport_lexical_collision_without_synthesis(
    settings,
    storage,
    monkeypatch,
) -> None:
    kernel = _AirportCollisionKernel()
    runtime = _runtime(settings, storage, model=_NeverModel(), kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    monkeypatch.setattr(runtime, "_verify_response", _forbid_secondary_stage)
    monkeypatch.setattr(runtime, "_repair_once", _forbid_secondary_stage)

    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert len(kernel.calls) == 1
    assert reply["tools_used"] == ["web_research"]
    assert reply["web_sources"] == []
    assert reply["web_evidence_status"] == "failed"
    assert "svo.aero" not in _telegram_body(reply)
    assert "не получила проверяемую" in reply["message"].casefold()
    assert _stored_news_receipt(storage, reply)[0].outcome.status is CapabilityOutcomeStatus.UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kernel_type", "legacy_status", "outcome_status"),
    (
        (_EmptyNewsKernel, "empty", CapabilityOutcomeStatus.EMPTY),
        (_UnavailableNewsKernel, "failed", CapabilityOutcomeStatus.UNAVAILABLE),
        (_MalformedTargetNewsKernel, "failed", CapabilityOutcomeStatus.UNAVAILABLE),
        (_MalformedEmptySourceClassNewsKernel, "failed", CapabilityOutcomeStatus.UNAVAILABLE),
        (_OversizedTargetNewsKernel, "failed", CapabilityOutcomeStatus.UNAVAILABLE),
        (_WrongQueryNewsKernel, "failed", CapabilityOutcomeStatus.UNAVAILABLE),
        (_CanonicalDuplicateTargetNewsKernel, "failed", CapabilityOutcomeStatus.UNAVAILABLE),
    ),
)
async def test_simple_news_distinguishes_validated_empty_from_unavailable(
    settings,
    storage,
    monkeypatch,
    kernel_type,
    legacy_status: str,
    outcome_status: CapabilityOutcomeStatus,
) -> None:
    prompt = "Сделай сводку сегодняшних новостей на зарубежных сайтах"
    kernel = kernel_type()
    runtime = _runtime(settings, storage, model=_NeverModel(), kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    reply = await runtime.chat(OWNER, prompt, actor=_actor())

    assert len(kernel.calls) == 1
    assert reply["tools_used"] == ["web_research"]
    assert reply["web_evidence_status"] == legacy_status
    assert reply["web_sources"] == []
    assert "не получила проверяемую" in reply["message"].casefold()
    assert _stored_news_receipt(storage, reply)[0].outcome.status is outcome_status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kernel_type", "legacy_status", "outcome_status"),
    (
        (_TopicMismatchNewsKernel, "empty", CapabilityOutcomeStatus.EMPTY),
        (_MalformedTopicMismatchNewsKernel, "failed", CapabilityOutcomeStatus.UNAVAILABLE),
        (_TransportFailedTopicMismatchNewsKernel, "failed", CapabilityOutcomeStatus.UNAVAILABLE),
    ),
)
async def test_topic_mismatch_empty_cannot_bypass_projector_boundaries(
    settings,
    storage,
    monkeypatch,
    kernel_type,
    legacy_status: str,
    outcome_status: CapabilityOutcomeStatus,
) -> None:
    kernel = kernel_type()
    runtime = _runtime(settings, storage, model=_NeverModel(), kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert len(kernel.calls) == 1
    assert reply["web_evidence_status"] == legacy_status
    assert reply["web_sources"] == []
    assert _stored_news_receipt(storage, reply)[0].outcome.status is outcome_status


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_variant", ("non_json_object", "huge_nested_json"))
async def test_simple_news_malformed_source_metadata_fails_without_aborting_turn(
    settings,
    storage,
    monkeypatch,
    metadata_variant: str,
) -> None:
    kernel = _MalformedSourceMetadataNewsKernel(metadata_variant)
    runtime = _runtime(settings, storage, model=_NeverModel(), kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)

    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert len(kernel.calls) == 1
    assert reply["tools_used"] == ["web_research"]
    assert reply["web_evidence_status"] == "failed"
    assert reply["web_sources"] == []
    assert "не получила проверяемую" in reply["message"].casefold()
    assert _stored_news_receipt(storage, reply)[0].outcome.status is CapabilityOutcomeStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_simple_news_late_revoke_publishes_denied_receipt_and_no_evidence(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user(OWNER, preset_key="owner")
    authorization = AuthorizationService(storage)
    kernel = _SyntheticNewsKernel()
    kernel.authorization = authorization
    model = _RevokingNewsModel(authorization)
    runtime = _runtime(settings, storage, model=model, kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    monkeypatch.setattr(runtime, "_repair_once", _forbid_repair)
    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert len(kernel.calls) == 1
    assert len(model.calls) == 2
    assert model.revoked is True
    assert reply["tools_used"] == ["web_research"]
    assert reply["web_evidence_status"] == "none"
    assert reply["web_sources"] == []
    assert reply["web_query_notice"] == ""
    assert PUBLIC_FACT not in reply["message"]
    assert PUBLIC_URL not in _telegram_body(reply)
    receipt, _metadata = _stored_news_receipt(storage, reply)
    assert receipt.outcome.status is CapabilityOutcomeStatus.DENIED
    rows = storage.execute(
        "SELECT id, role, reply_to FROM messages WHERE conversation_id=?",
        (reply["conversation_id"],),
    ).fetchall()
    assert sorted(str(row["role"]) for row in rows) == ["assistant", "user"]
    user_row = next(row for row in rows if str(row["role"]) == "user")
    assistant_row = next(row for row in rows if str(row["role"]) == "assistant")
    assert assistant_row["reply_to"] == user_row["id"]


@pytest.mark.asyncio
async def test_simple_news_body_change_after_passed_verification_rolls_back_assistant(
    settings,
    storage,
    monkeypatch,
) -> None:
    kernel = _SyntheticNewsKernel()
    model = _OneShotNewsModel()
    runtime = _runtime(settings, storage, model=model, kernel=kernel)
    conversation = storage.create_conversation(OWNER, "typed news body tamper")
    monkeypatch.setattr(agent_runtime, "simple_public_news_content_identity", lambda _body: "a" * 64)

    with pytest.raises(SimplePublicNewsOutcomeError, match="changed after its passed verification"):
        await runtime.chat(
            OWNER,
            REQUEST,
            actor=_actor(),
            conversation_id=str(conversation["id"]),
        )

    assert len(kernel.calls) == 1
    assert len(model.calls) == 2
    roles = [
        str(row["role"])
        for row in storage.execute(
            "SELECT role FROM messages WHERE conversation_id=? ORDER BY created_at, id",
            (conversation["id"],),
        ).fetchall()
    ]
    assert roles == ["user"]


@pytest.mark.asyncio
async def test_simple_news_corrupt_durable_reread_rolls_back_assistant(
    settings,
    storage,
    monkeypatch,
) -> None:
    kernel = _SyntheticNewsKernel()
    model = _OneShotNewsModel()
    runtime = _runtime(settings, storage, model=model, kernel=kernel)
    conversation = storage.create_conversation(OWNER, "typed news receipt tamper")
    original_store = agent_runtime.store_message_in_transaction

    def corrupting_store(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        stored = original_store(*args, **kwargs)
        return {**stored, "metadata_json": "{}"}

    monkeypatch.setattr(agent_runtime, "store_message_in_transaction", corrupting_store)
    with pytest.raises(CapabilityOutcomeError, match="has no receipt"):
        await runtime.chat(
            OWNER,
            REQUEST,
            actor=_actor(),
            conversation_id=str(conversation["id"]),
        )

    assert len(kernel.calls) == 1
    assert len(model.calls) == 2
    roles = [
        str(row["role"])
        for row in storage.execute(
            "SELECT role FROM messages WHERE conversation_id=? ORDER BY created_at, id",
            (conversation["id"],),
        ).fetchall()
    ]
    assert roles == ["user"]


@pytest.mark.asyncio
async def test_mixed_svo_news_keeps_only_relevant_evidence_and_marks_it_partial(
    settings,
    storage,
    monkeypatch,
) -> None:
    kernel = _MixedCollisionKernel()
    model = _MixedOneShotNewsModel()
    runtime = _runtime(settings, storage, model=model, kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    monkeypatch.setattr(runtime, "_repair_once", _forbid_repair)

    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert len(kernel.calls) == 1
    assert len(model.calls) == 2
    assert reply["web_evidence_status"] == "partial"
    assert reply["web_sources"] == [{"url": PUBLIC_URL, "title": PUBLIC_TITLE}]
    delivered = _telegram_body(reply)
    assert PUBLIC_URL in delivered
    assert "svo.aero" not in delivered.casefold()
    assert "airport departures" not in delivered.casefold()
    assert _stored_news_receipt(storage, reply)[0].outcome.status is CapabilityOutcomeStatus.PARTIAL


@pytest.mark.asyncio
async def test_simple_news_provider_overreturn_is_capped_and_partial(
    settings,
    storage,
    monkeypatch,
) -> None:
    kernel = _OverreturningNewsKernel()
    model = _OneShotNewsModel()
    runtime = _runtime(settings, storage, model=model, kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    monkeypatch.setattr(runtime, "_repair_once", _forbid_repair)
    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert len(kernel.calls) == 1
    assert len(model.calls) == 2
    assert reply["web_evidence_status"] == "partial"
    assert len(reply["web_sources"]) == 3
    receipt, _metadata = _stored_news_receipt(storage, reply)
    assert receipt.outcome.status is CapabilityOutcomeStatus.PARTIAL
    assert receipt.outcome.citation_labels == ("A1", "A2", "A3")


@pytest.mark.asyncio
@pytest.mark.parametrize("kernel_type", (_RefilledTargetNewsKernel,))
async def test_simple_news_complete_reports_cross_projector_and_typed_boundary(
    settings,
    storage,
    monkeypatch,
    kernel_type,
) -> None:
    kernel = kernel_type()
    model = _OneShotNewsModel()
    runtime = _runtime(settings, storage, model=model, kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    monkeypatch.setattr(runtime, "_repair_once", _forbid_repair)

    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert reply["web_evidence_status"] == "sourced"
    assert len(reply["web_sources"]) in {2, 3}
    assert _stored_news_receipt(storage, reply)[0].outcome.status is CapabilityOutcomeStatus.COMPLETE


@pytest.mark.asyncio
async def test_fresh_simple_news_rejects_unattested_legacy_direct_overflow(
    settings,
    storage,
    monkeypatch,
) -> None:
    kernel = _LegacyDirectOverflowNewsKernel()
    runtime = _runtime(settings, storage, model=_NeverModel(), kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert len(kernel.calls) == 1
    assert reply["web_evidence_status"] == "failed"
    assert reply["web_sources"] == []
    assert _stored_news_receipt(storage, reply)[0].outcome.status is CapabilityOutcomeStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_topic_filtered_filled_target_remains_partial_without_aborting_turn(
    settings,
    storage,
    monkeypatch,
) -> None:
    kernel = _TargetMixedCollisionKernel()
    model = _OneShotNewsModel()
    runtime = _runtime(settings, storage, model=model, kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    monkeypatch.setattr(runtime, "_repair_once", _forbid_repair)

    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert reply["web_evidence_status"] == "partial"
    assert len(reply["web_sources"]) == 3
    assert all("svo.aero" not in row["url"] for row in reply["web_sources"])
    assert _stored_news_receipt(storage, reply)[0].outcome.status is CapabilityOutcomeStatus.PARTIAL


@pytest.mark.asyncio
@pytest.mark.parametrize("kernel_type", (_DuplicateNewsKernel, _LegacyMissingRowFieldsNewsKernel))
async def test_simple_news_degraded_provider_rows_are_receipted_as_partial(
    settings,
    storage,
    monkeypatch,
    kernel_type,
) -> None:
    kernel = kernel_type()
    model = _OneShotNewsModel()
    runtime = _runtime(settings, storage, model=model, kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    monkeypatch.setattr(runtime, "_repair_once", _forbid_repair)
    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert len(kernel.calls) == 1
    assert len(model.calls) == 2
    assert reply["web_evidence_status"] == "partial"
    assert reply["web_sources"] == [{"url": PUBLIC_URL, "title": PUBLIC_TITLE}]
    assert _stored_news_receipt(storage, reply)[0].outcome.status is CapabilityOutcomeStatus.PARTIAL


@pytest.mark.asyncio
async def test_failed_news_draft_gets_one_repair_with_the_same_full_bounded_evidence(
    settings,
    storage,
    monkeypatch,
) -> None:
    long_source = f"{PUBLIC_FACT} {'bounded filler ' * 300}{LONG_EVIDENCE_TAIL}"
    kernel = _SyntheticNewsKernel(source_text=long_source)
    model = _RepairingNewsModel(final_passes=True)
    runtime = _runtime(settings, storage, model=model, kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)

    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert len(long_source) > agent_runtime._TOOL_EVIDENCE_CHARS  # noqa: SLF001
    assert model.stages == ["synthesis", "verify", "repair", "verify"]
    assert len(model.calls) == 4
    assert UNSUPPORTED_FACT not in reply["message"]
    assert REPAIRED_FACT in reply["message"]
    assert reply["verified"] is True
    assert reply["verification_status"] == "passed"


@pytest.mark.asyncio
async def test_final_failed_news_verdict_discards_model_prose_for_source_only_fallback(
    settings,
    storage,
    monkeypatch,
) -> None:
    long_source = f"{PUBLIC_FACT} {'bounded filler ' * 300}{LONG_EVIDENCE_TAIL}"
    kernel = _SyntheticNewsKernel(source_text=long_source)
    model = _RepairingNewsModel(final_passes=False)
    runtime = _runtime(settings, storage, model=model, kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)

    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert model.stages == ["synthesis", "verify", "repair", "verify"]
    assert UNSUPPORTED_FACT not in reply["message"]
    assert REPAIRED_FACT not in reply["message"]
    assert "только найденные источники" in reply["message"].casefold()
    assert reply["verified"] is False
    assert reply["verification_status"] == "failed"
    assert reply["verification"]["issues"] == []
    assert VERIFIER_PRIVATE_SENTINEL not in reply["verification_caution"]
    delivered = _telegram_body(reply)
    assert PUBLIC_TITLE in delivered
    assert PUBLIC_URL in delivered
    assert VERIFIER_PRIVATE_SENTINEL not in delivered
    stored = storage.get_message(str(reply["message_id"]), OWNER)
    assert VERIFIER_PRIVATE_SENTINEL not in str(stored["metadata_json"] or "")


@pytest.mark.asyncio
async def test_unknown_news_verdict_fails_closed_without_starting_repair(
    settings,
    storage,
    monkeypatch,
) -> None:
    kernel = _SyntheticNewsKernel()
    model = _UnknownVerifierNewsModel()
    runtime = _runtime(settings, storage, model=model, kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    monkeypatch.setattr(runtime, "_repair_once", _forbid_repair)

    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert len(model.calls) == 2
    assert PUBLIC_FACT not in reply["message"]
    assert "только найденные источники" in reply["message"].casefold()
    assert reply["verified"] is False
    assert reply["verification_status"] == "unknown"
    assert PUBLIC_URL in _telegram_body(reply)


@pytest.mark.asyncio
async def test_common_open_search_ceiling_also_discards_an_exhaustive_news_claim(
    settings,
    storage,
    monkeypatch,
) -> None:
    kernel = _SyntheticNewsKernel()
    model = _ExhaustiveNewsModel()
    runtime = _runtime(settings, storage, model=model, kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    monkeypatch.setattr(runtime, "_repair_once", _forbid_repair)

    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert len(model.calls) == 2
    assert "полный обзор" not in reply["message"].casefold()
    assert PUBLIC_FACT not in reply["message"]
    assert "только найденные источники" in reply["message"].casefold()
    assert reply["verification_status"] == "unknown"
    assert PUBLIC_URL in _telegram_body(reply)


def test_latin_svo_uses_the_same_closed_collision_policy_as_cyrillic() -> None:
    prompt = "Show a news roundup about SVO from international media"
    authority = agent_runtime.file_turn_authority(prompt)
    assert authority.actions == frozenset({"web"})
    assert (
        agent_runtime._public_news_search_query(  # noqa: SLF001
            authority.speech,
            prompt,
            "foreign",
        )
        == "Russia Ukraine war latest news"
    )
    airport = {
        "title": "SVO airport departures",
        "text": "Sheremetyevo is the largest airport in Russia and publishes flight schedules.",
    }
    assert not agent_runtime._public_news_item_matches_collision_topic(  # noqa: SLF001
        authority.speech,
        airport,
    )


class _NeverModel:
    enabled = True
    model = "synthetic-compound-never-called"
    total_budget_sec = 360.0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        raise AssertionError("compound route escaped its patched agentic seam")


class _LongQueryNewsRuntime(AgentRuntime):
    @staticmethod
    def web_query_from(message: str) -> str:
        del message
        return "future-query-source-" + ("q" * 250)


@pytest.mark.asyncio
@pytest.mark.parametrize("early_stop", ("expired", "source_effect_denied"))
async def test_simple_news_early_stopped_prefetch_keeps_one_bounded_query_identity(
    settings,
    storage,
    early_stop: str,
) -> None:
    """A future longer query source cannot split plan and fallback identity."""

    storage.ensure_user(OWNER, preset_key="owner")
    kernel = _SyntheticNewsKernel()
    runtime = _LongQueryNewsRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=_NeverModel(),
        kernel=kernel,
    )
    prompt = "Расскажи последние новости про квантовые технологии"
    context = AgentContext(
        conversation_id="bounded-query-identity",
        user_id=OWNER,
        person_id=OWNER,
        conversation_history=[],
        search_query=prompt,
        outward_verdict=("интернет", None),
        isolated_outbound_turn=True,
        turn_deadline=0.0 if early_stop == "expired" else None,
        source_effect_reauth_required=early_stop == "source_effect_denied",
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        prompt,
        _actor(),
        [_tool("web_research")],
        None,
    )

    assert kernel.calls == []
    assert result["_simple_public_news_fallback_owned"] is True
    assert context.simple_public_news_evidence is not None
    assert context.simple_public_news_evidence.status.value == "unavailable"


@pytest.mark.asyncio
async def test_news_plus_reminder_keeps_outbound_web_out_of_the_compound_route(
    settings,
    storage,
    monkeypatch,
) -> None:
    kernel = _SyntheticNewsKernel()
    runtime = _runtime(settings, storage, model=_NeverModel(), kernel=kernel)
    monkeypatch.setattr(runtime, "_prepare_context", _prepare_without_retrieval)
    compound = f"{REQUEST} и напомни мне завтра проверить обновления"
    agentic_calls: list[dict[str, Any]] = []

    async def compound_agentic(
        context,
        message,
        actor,
        tools,
        attachments,
        **kwargs,
    ):  # noqa: ANN001
        del context, actor, attachments, kwargs
        agentic_calls.append(
            {
                "message": str(message),
                "tools": [
                    str((tool.get("function") or {}).get("name") or tool.get("name") or "") for tool in tools
                ],
            }
        )
        return {
            "content": "COMPOUND-AGENTIC-SENTINEL",
            "tools_used": [],
            "tool_evidence": [],
        }

    monkeypatch.setattr(runtime, "_agentic_loop", compound_agentic)

    reply = await runtime.chat(OWNER, compound, actor=_actor())

    assert agentic_calls == [
        {
            "message": compound,
            "tools": ["remind"],
        }
    ]
    assert kernel.calls == []
    assert reply["message"]
