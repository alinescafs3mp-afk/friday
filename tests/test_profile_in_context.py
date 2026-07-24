"""User model injection into the agent context — personalization.

The derived profile (people/projects/interests) now rides in the untrusted
JERICHO_CONTEXT_DATA envelope so answers can be personal. These tests pin: the
compact payload shape (even with zero retrieval hits — pure dialogue), the
off-switch, silence on an empty base, and the SYSTEM_PROMPT ground rule that
the model is background, not a citable source.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from jericho.agent_runtime import SYSTEM_PROMPT, AgentRuntime
from jericho.knowledge_graph import KnowledgeGraph
from jericho.permissions import ActorContext
from jericho.storage.models import EntityType, KnowledgeObject, RawObject, new_id


class _FakeSearcher:
    async def search(self, user_id, query, **kwargs):
        del user_id, query, kwargs
        return {"results": [], "entity_matches": []}


class _CapturingLLM:
    enabled = True
    model = "capture"

    def __init__(self):
        self.context_payload = None

    async def chat(self, messages, **kwargs):
        del kwargs
        for item in messages:
            content = str(item.get("content") or "")
            if "JERICHO_CONTEXT_DATA" in content and "{" in content:
                self.context_payload = json.loads(content[content.index("{") :])
        return {"content": "Хорошо."}


def _seed_knowledge(storage, user_id: str, title: str, tags: list[str]) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=title,
        content_type="text",
        content_hash=hashlib.sha256(new_id("h").encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=title,
        content_type="text",
        title=title,
        summary=title,
        tags_json=tags,
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _seed_model(storage) -> None:
    graph = KnowledgeGraph(storage)
    storage.ensure_user("alice")
    k1 = _seed_knowledge(storage, "alice", "Встреча с Иваном", ["работа"])
    k2 = _seed_knowledge(storage, "alice", "Ещё про Ивана", ["работа", "orion"])
    person = graph.create_entity("alice", "Иван Петров", EntityType.PERSON)
    graph.link_knowledge_to_entity(k1, person["id"], "alice", status="accepted", reviewed_by="alice")
    graph.link_knowledge_to_entity(k2, person["id"], "alice", status="accepted", reviewed_by="alice")
    box = graph.create_container("alice", "Orion", kind="project")
    graph.link_knowledge_to_entity(k2, box["id"], "alice", status="accepted", reviewed_by="alice")


async def _chat(settings, storage, llm):
    runtime = AgentRuntime(settings, storage, llm=llm)
    await runtime.chat(
        "alice",
        "привет, как дела?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        enable_tools=False,
        hybrid_searcher=_FakeSearcher(),
    )


@pytest.mark.asyncio
async def test_user_model_reaches_the_prompt_even_without_hits(settings, storage):
    _seed_model(storage)
    llm = _CapturingLLM()
    await _chat(settings, storage, llm)

    # Zero retrieval hits, yet the context block exists because of the model:
    # personalization works in pure dialogue too.
    assert llm.context_payload is not None
    model = llm.context_payload["user_model"]
    assert "Иван Петров" in model["people"]
    assert "Orion" in model["projects"]
    assert "работа" in model["interests"]
    assert model["recent_30d"] >= 2


@pytest.mark.asyncio
async def test_user_model_absent_when_disabled(settings, storage):
    from dataclasses import replace

    _seed_model(storage)
    llm = _CapturingLLM()
    await _chat(replace(settings, profile_in_context=False), storage, llm)
    payload = llm.context_payload
    assert payload is None or "user_model" not in payload


@pytest.mark.asyncio
async def test_user_model_silent_on_empty_base(settings, storage):
    storage.ensure_user("alice")
    llm = _CapturingLLM()
    await _chat(settings, storage, llm)
    payload = llm.context_payload
    assert payload is None or "user_model" not in payload


def test_system_prompt_frames_user_model_as_background():
    assert "user_model" in SYSTEM_PROMPT
    assert "не цитируй user_model как [K#]" in SYSTEM_PROMPT
