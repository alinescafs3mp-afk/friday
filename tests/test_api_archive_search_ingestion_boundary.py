"""HTTP intake keeps private-archive read commands transient."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID


@pytest.mark.parametrize(
    "prompt",
    (
        "Найди договор Альфа в моём личном архиве.",
        "Не ищи в интернете, найди договор в моём архиве.",
        "Найди договор в моём архиве, но не ищи в интернете.",
    ),
)
@pytest.mark.parametrize("search_denied", [False, True], ids=["available", "denied"])
def test_archive_search_command_never_enters_raw_inbox_or_knowledge(
    settings: Any,
    search_denied: bool,
    prompt: str,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    ingest_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    agent_calls: list[dict[str, Any]] = []

    async def forbidden_ingest(*args: Any, **kwargs: Any) -> dict[str, Any]:
        ingest_calls.append((args, kwargs))
        raise AssertionError("private archive command reached ingest_text")

    async def capture_chat(_user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        assert message == prompt
        agent_calls.append(kwargs)
        return {
            "conversation_id": "conv_archive_ingestion_boundary",
            "message_id": "msg_0123456789abcdef",
            "message": "synthetic archive receipt",
            "message_format": "plain",
            "tools_used": [],
            "files": [],
            "voice": None,
            "context": {"interaction_mode": "dialogue"},
        }

    with TestClient(app) as client:
        if search_denied:
            app.state.storage.set_permission_override(
                LEGACY_OWNER_USER_ID,
                "search.use",
                "deny",
            )
        app.state.ingestion.ingest_text = forbidden_ingest
        app.state.agent.chat = capture_chat
        before = {
            table: int(
                app.state.storage.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            )
            for table in ("raw_objects", "inbox", "knowledge_objects")
        }
        response = client.post(
            "/api/chat",
            json={"message": prompt, "force_knowledge": True},
            headers=headers,
        )
        after = {
            table: int(
                app.state.storage.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            )
            for table in before
        }

    assert response.status_code == 200, response.text
    assert ingest_calls == []
    assert after == before
    assert len(agent_calls) == 1
    expected = {
        "promoted": False,
        "queued_for_review": False,
        "action": "transient",
        "category": "archive_search_request",
        "reason": "явная просьба прочитать личный архив — команда, а не материал",
    }
    assert agent_calls[0]["ingestion_result"] == expected
    public_ingestion = response.json()["ingestion"]
    assert public_ingestion == {
        "promoted": False,
        "queued_for_review": False,
        "action": "transient",
    }
