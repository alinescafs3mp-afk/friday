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
from friday.execution_kernel import ToolResult
from friday.permissions import ActorContext

OWNER = "simple_public_news_owner"
REQUEST = "Сделай сводку по новостям СВО на зарубежных сайтах"
PUBLIC_URL = "https://foreign.synthetic.example.com/svo-update"
PUBLIC_TITLE = "Synthetic foreign bulletin"
PUBLIC_FACT = "Synthetic foreign bulletin reports a confirmed dated Ukraine development."


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

    def __init__(self, *, include_reminder: bool = True) -> None:
        self.include_reminder = include_reminder
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
        definitions = [_tool("web_research")]
        if self.include_reminder:
            definitions.append(_tool("remind"))
        return definitions

    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        assert tool == "web_research", "the simple route selected a second capability"
        self.calls.append((str(tool), dict(params)))
        return ToolResult(
            tool,
            True,
            {
                "outbound_attempted": True,
                "query": str(params.get("query") or ""),
                "sources": [
                    {
                        "url": PUBLIC_URL,
                        "title": PUBLIC_TITLE,
                        "text": PUBLIC_FACT,
                        "text_length": len(PUBLIC_FACT),
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
        airport = "Sheremetyevo is the largest airport in Russia; departure board and terminal schedule."
        return ToolResult(
            tool,
            True,
            {
                "outbound_attempted": True,
                "query": str(params.get("query") or ""),
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
        airport = "Sheremetyevo is the largest airport in Russia; departure board and terminal schedule."
        return ToolResult(
            tool,
            True,
            {
                "outbound_attempted": True,
                "query": str(params.get("query") or ""),
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
        assert tools == [], "simple news synthesis was offered an agentic tool schema"
        rendered = json.dumps(messages, ensure_ascii=False)
        assert REQUEST in rendered
        assert PUBLIC_FACT in rendered
        assert PUBLIC_TITLE in rendered
        assert PUBLIC_URL in rendered
        assert "svo.aero" not in rendered.casefold()
        assert "airport departures" not in rendered.casefold()
        return {
            "content": f"{PUBLIC_FACT}\n\nИсточник: [{PUBLIC_TITLE}]({PUBLIC_URL})",
            "tool_calls": None,
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
        assert '"completed_sources": 1' in rendered, rendered
        assert '"failed_sources": 1' in rendered, rendered
        assert '"requested_sources": 2' in rendered, rendered
        assert "Accepted 1 readable public source." in rendered, rendered
        assert '"completed_sources": 2' not in rendered, rendered
        return await super().chat(messages, tools=tools, **kwargs)


async def _forbid_second_model_stage(*args, **kwargs):  # noqa: ANN002, ANN003
    raise AssertionError("simple public-news route entered verifier or repair")


def _runtime(settings, storage, *, model: Any, kernel: Any) -> AgentRuntime:
    storage.ensure_user(OWNER, preset_key="owner")
    return AgentRuntime(
        replace(
            settings,
            verify_answers=True,
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


@pytest.mark.asyncio
async def test_simple_foreign_news_runs_one_research_then_one_tool_free_synthesis(
    settings,
    storage,
    monkeypatch,
) -> None:
    kernel = _SyntheticNewsKernel()
    model = _OneShotNewsModel()
    runtime = _runtime(settings, storage, model=model, kernel=kernel)

    async def forbidden_context(*args: Any, **kwargs: Any) -> AgentContext:
        del args, kwargs
        raise AssertionError("simple public-news turn entered ambient context/classifier retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    monkeypatch.setattr(runtime, "_verify_response", _forbid_second_model_stage)
    monkeypatch.setattr(runtime, "_repair_once", _forbid_second_model_stage)

    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert kernel.calls == [
        (
            "web_research",
            {
                "query": "Russia Ukraine war latest news",
                "max_sources": 3,
                "source_class": "foreign",
                "topic_class": "russia_ukraine_war_news",
            },
        )
    ]
    assert len(model.calls) == 1
    assert model.calls[0]["tools"] == []
    assert reply["tools_used"] == ["web_research"]
    assert reply["web_evidence_status"] == "sourced"
    assert reply["web_sources"] == [{"url": PUBLIC_URL, "title": PUBLIC_TITLE}]
    assert PUBLIC_FACT in reply["message"]
    assert PUBLIC_URL in _telegram_body(reply)
    assert "Russia Ukraine war latest news" in reply["web_query_notice"]


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
    monkeypatch.setattr(runtime, "_verify_response", _forbid_second_model_stage)
    monkeypatch.setattr(runtime, "_repair_once", _forbid_second_model_stage)
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
    monkeypatch.setattr(runtime, "_verify_response", _forbid_second_model_stage)
    monkeypatch.setattr(runtime, "_repair_once", _forbid_second_model_stage)

    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert len(kernel.calls) == 1
    assert reply["tools_used"] == ["web_research"]
    assert reply["web_sources"] == []
    assert reply["web_evidence_status"] == "failed"
    assert "svo.aero" not in _telegram_body(reply)
    assert "не получила проверяемую" in reply["message"].casefold()


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
    monkeypatch.setattr(runtime, "_verify_response", _forbid_second_model_stage)
    monkeypatch.setattr(runtime, "_repair_once", _forbid_second_model_stage)

    reply = await runtime.chat(OWNER, REQUEST, actor=_actor())

    assert len(kernel.calls) == 1
    assert len(model.calls) == 1
    assert reply["web_evidence_status"] == "partial"
    assert reply["web_sources"] == [{"url": PUBLIC_URL, "title": PUBLIC_TITLE}]
    delivered = _telegram_body(reply)
    assert PUBLIC_URL in delivered
    assert "svo.aero" not in delivered.casefold()
    assert "airport departures" not in delivered.casefold()


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


@pytest.mark.asyncio
async def test_news_plus_reminder_remains_on_the_compound_agentic_route(
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
            "tools": ["web_research", "remind"],
        }
    ]
    assert kernel.calls == []
    assert reply["message"]
