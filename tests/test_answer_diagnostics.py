"""Why an answer came out the way it did — reachable by the owner, not only an admin.

`_exclusion_reason` computes `identifier_mismatch`, `insufficient_evidence` and
`deprecated_weak` on EVERY query and throws them away unless `explain` is set, and
`explain` had exactly one call site: an admin route behind `admin.all_data.read`.

So the owner asking from Telegram, told «в базе нет записей об этом» about a note
they know they saved, had no way to learn that the note *was* in the candidate pool
and was dropped — short of opening the admin panel on the host and retyping an
approximation of a query the agent had already rewritten. A different run.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import KnowledgeObject, RawObject, new_id


def _knowledge(storage, user_id: str, text: str, **fields) -> str:
    storage.ensure_user(user_id)
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        title=text[:60],
        summary=text[:120],
        **fields,
    )
    storage.store_knowledge_object(ko)
    return ko.id


def test_the_owner_can_ask_the_search_why(settings):
    """`explain` on the tenant route: the trace is the caller's own data."""
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        _knowledge(storage, LEGACY_OWNER_USER_ID, "Заметка про аренду помещения и залог")
        owner = {"Authorization": f"Bearer {settings.api_token}"}

        plain = client.get("/api/search", params={"q": "аренда"}, headers=owner)
        assert plain.status_code == 200
        assert "trace" not in plain.json(), "the trace must be opt-in"

        explained = client.get("/api/search", params={"q": "аренда", "explain": "true"}, headers=owner)
        assert explained.status_code == 200
        trace = explained.json()["trace"]
        assert trace, "explain returned no trace"
        assert {"id", "status", "reason", "score"} <= set(trace[0])


@pytest.mark.asyncio
async def test_a_discarded_candidate_carries_its_reason(storage):
    """The reason is real output, not a label invented for the diagnostic."""
    from friday.retrieval import HybridSearcher

    _knowledge(storage, "owner", "Совершенно посторонний документ о ремонте велосипеда")
    result = await HybridSearcher(storage).search("owner", "квартальная отчётность", explain=True)

    discarded = [item for item in result["trace"] if item.get("reason")]
    assert discarded, "nothing was discarded, so this proves nothing"
    assert discarded[0]["reason"] in {
        "identifier_mismatch",
        "insufficient_evidence",
        "deprecated_weak",
    }


def test_the_agent_captures_the_trace_at_answer_time():
    """It must be captured WITH the answer, not recomputed afterwards.

    The agent rewrites the user's message into `search_query`; re-running that by
    hand later is a different query against a database that has moved on, which is
    exactly why the admin explain route was not an answer to "why did it say there
    is nothing".
    """
    import ast
    import inspect

    from friday.agent_runtime import AgentContext, AgentRuntime

    assert "retrieval_trace" in AgentContext.__dataclass_fields__

    source = inspect.getsource(AgentRuntime)
    tree = ast.parse(source.lstrip())
    explained = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "search"
        and any(keyword.arg == "explain" for keyword in node.keywords)
    ]
    assert explained, "the agent stopped asking the searcher to explain itself"
    assert '"retrieval_trace": context.retrieval_trace' in source


def test_the_channel_why_route_reports_the_stored_diagnosis(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        storage.ensure_user(LEGACY_OWNER_USER_ID)
        owner = {"Authorization": f"Bearer {settings.api_token}"}

        # No conversation yet: an honest 404, not an empty success.
        empty = client.get(
            "/api/conversations/channel/why",
            params={"channel": "api", "channel_id": "c1"},
            headers=owner,
        )
        assert empty.status_code == 404

        conversation_id = storage.create_conversation(LEGACY_OWNER_USER_ID, title="Проба")
        conversation_id = conversation_id if isinstance(conversation_id, str) else conversation_id["id"]
        storage.set_channel_conversation(LEGACY_OWNER_USER_ID, "api", "c1", conversation_id)
        knowledge_id = _knowledge(storage, LEGACY_OWNER_USER_ID, "Договор аренды")
        storage.store_message(
            conversation_id,
            LEGACY_OWNER_USER_ID,
            "assistant",
            "В базе нет записей об этом.",
            metadata={
                "search_query": "аренда помещения залог",
                "answer_mode": "personal_knowledge_missing",
                "knowledge_hits": 0,
                "retrieval_trace": [
                    {
                        "id": knowledge_id,
                        "title": "Договор аренды",
                        "score": 0.31,
                        "status": "discarded",
                        "reason": "insufficient_evidence",
                    }
                ],
            },
        )

        response = client.get(
            "/api/conversations/channel/why",
            params={"channel": "api", "channel_id": "c1"},
            headers=owner,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["search_query"] == "аренда помещения залог"
        assert body["knowledge_hits"] == 0
        assert body["trace"][0]["reason"] == "insufficient_evidence"
        assert body["trace"][0]["title"] == "Договор аренды"
