"""Source citations must reach the user, and ungrounded personal answers are flagged.

The [K#] → Knowledge Object map was built internally but never left the runtime:
the /api/chat response omitted it and Telegram never rendered it. These tests pin
the source legend on the response, the honest ungrounded-answer notice, and the
Telegram rendering.
"""

from __future__ import annotations

import hashlib

import pytest

from jericho.agent_runtime import AgentRuntime, _citation_notice, _citation_sort_key
from jericho.permissions import ActorContext
from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _store(storage, user_id: str, content: str, title: str) -> dict:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title=title,
        summary=content,
    )
    storage.store_knowledge_object(ko)
    return storage.get_knowledge_object(ko.id, user_id) or {}


class _FakeSearcher:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, user_id, query, **kwargs):
        del user_id, query, kwargs
        return {"results": self._hits, "entity_matches": []}


class _StaticLLM:
    enabled = True
    model = "citation-test"

    def __init__(self, answer: str):
        self._answer = answer

    async def chat(self, messages, **kwargs):
        del kwargs
        if any(
            "Проверь ответ" in str(item.get("content") or "")
            for item in messages
            if item.get("role") == "system"
        ):
            return {"content": '{"ok": true, "score": 1.0, "issues": []}'}
        return {"content": self._answer}


def _actor():
    return ActorContext(user_id="alice", preset_key="owner", source="test")


def test_citation_sort_orders_labels_numerically_then_unlabelled():
    items = [{"label": "K10"}, {"label": "K2"}, {"label": ""}, {"label": "K1"}]
    items.sort(key=lambda item: _citation_sort_key(item["label"]))
    assert [item["label"] for item in items] == ["K1", "K2", "K10", ""]


def test_citation_notice_legend_and_ungrounded_and_silent():
    grounded = _citation_notice([{"label": "K1", "knowledge_id": "a", "title": "База"}], True)
    assert grounded == "📎 Источники: [K1] База"
    assert _citation_notice([], False).startswith("ℹ️")
    assert _citation_notice([], None) == ""


@pytest.mark.asyncio
async def test_answer_surfaces_resolved_source_legend(settings, storage):
    storage.ensure_user("alice")
    first = _store(storage, "alice", "Atlas использует PostgreSQL 16.", "Atlas база")
    second = _store(storage, "alice", "Atlas использует Redis.", "Atlas кэш")
    hits = [
        {**first, "_score": 0.91, "_entities": []},
        {**second, "_score": 0.86, "_entities": []},
    ]
    llm = _StaticLLM("Atlas использует PostgreSQL [K1] и Redis [K2].")
    runtime = AgentRuntime(settings, storage, llm=llm)

    result = await runtime.chat(
        "alice",
        "Что известно про Atlas?",
        actor=_actor(),
        enable_tools=False,
        hybrid_searcher=_FakeSearcher(hits),
    )

    assert result["answer_grounded"] is True
    legend = {c["label"]: c for c in result["citations"]}
    assert [c["label"] for c in result["citations"]] == ["K1", "K2"]
    assert legend["K1"]["title"] == "Atlas база"
    assert legend["K1"]["knowledge_id"] == first["id"]
    assert legend["K2"]["title"] == "Atlas кэш"
    assert result["citation_notice"].startswith("📎 Источники:")
    assert "Atlas база" in result["citation_notice"]


@pytest.mark.asyncio
async def test_ungrounded_personal_answer_is_flagged(settings, storage):
    storage.ensure_user("alice")
    first = _store(storage, "alice", "Atlas использует PostgreSQL 16.", "Atlas база")
    second = _store(storage, "alice", "Atlas использует Redis.", "Atlas кэш")
    hits = [
        {**first, "_score": 0.9, "_entities": []},
        {**second, "_score": 0.85, "_entities": []},
    ]
    # No [K#] markers, two hits so the single-hit fallback cannot apply.
    llm = _StaticLLM("Atlas — это ваш основной проект.")
    runtime = AgentRuntime(settings, storage, llm=llm)

    result = await runtime.chat(
        "alice",
        "что у меня по Atlas?",
        actor=_actor(),
        enable_tools=False,
        hybrid_searcher=_FakeSearcher(hits),
    )

    assert result["answer_grounded"] is False
    assert result["citations"] == []
    assert result["citation_notice"].startswith("ℹ️")


@pytest.mark.asyncio
async def test_general_answer_has_no_citation_notice(settings, storage):
    storage.ensure_user("alice")
    llm = _StaticLLM("Привет! Чем могу помочь?")
    runtime = AgentRuntime(settings, storage, llm=llm)

    result = await runtime.chat(
        "alice",
        "привет",
        actor=_actor(),
        enable_tools=False,
        hybrid_searcher=_FakeSearcher([]),
    )

    assert result["answer_grounded"] is None
    assert result["citations"] == []
    assert result["citation_notice"] == ""


def test_telegram_appends_citation_legend():
    from jericho.telegram_bridge import TelegramBridge

    out = TelegramBridge._format_response_message(
        {
            "message": "Atlas использует PostgreSQL [K1].",
            "citation_notice": "📎 Источники: [K1] Atlas база",
            "verification_caution": "",
            "context": {},
        }
    )
    assert "📎 Источники" in out
    assert out.rstrip().endswith("[K1] Atlas база")
