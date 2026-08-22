"""HTTP admission must treat a direct Obsidian command as an effect request."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

_LIVE_PROMPT = (
    "Создай в Obsidian заметку Projects/Friday Test.md. "
    "Заголовок: «Тест интеграции Friday». Внутри напиши, что заметка создана "
    "через Telegram, и добавь текущую дату."
)
_COMPOUND_PROMPT = (
    "Можно ли развернуть на qnap TVS-675 nextcloud? Создай заметку в obsidian по результатам этой задачи"
)


@pytest.mark.parametrize(
    "prompt",
    [_LIVE_PROMPT, _COMPOUND_PROMPT],
    ids=["direct-note", "compound-public-result-note"],
)
def test_live_obsidian_command_bypasses_ingestion_and_reaches_agent_as_transient(
    settings,
    prompt: str,
) -> None:
    # Import after the settings fixture has installed the isolated FRIDAY_HOME;
    # importing create_app at module collection time binds process-global env.
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    ingest_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    agent_calls: list[tuple[str, str, dict[str, Any]]] = []

    async def forbidden_ingest(*args: Any, **kwargs: Any) -> dict[str, Any]:
        ingest_calls.append((args, kwargs))
        raise AssertionError("a direct Obsidian command reached ingest_text")

    async def capture_chat(user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        agent_calls.append((user_id, message, kwargs))
        return {
            "conversation_id": "conv_obsidian_ingestion_boundary",
            "message_id": "msg_0123456789abcdef",
            "message": "synthetic agent receipt",
            "message_format": "plain",
            "tools_used": [],
            "files": [],
            "voice": None,
            "context": {"interaction_mode": "dialogue"},
        }

    with TestClient(app) as client:
        app.state.ingestion.ingest_text = forbidden_ingest
        app.state.agent.chat = capture_chat
        inbox_before = int(
            app.state.storage.execute("SELECT COUNT(*) AS count FROM inbox").fetchone()["count"]
        )
        raw_before = int(
            app.state.storage.execute("SELECT COUNT(*) AS count FROM raw_objects").fetchone()["count"]
        )

        response = client.post(
            "/api/chat",
            json={
                "message": prompt,
                # Caller promotion preference must not turn an effect request
                # into knowledge or a review card.
                "force_knowledge": True,
            },
            headers=headers,
        )

        inbox_after = int(
            app.state.storage.execute("SELECT COUNT(*) AS count FROM inbox").fetchone()["count"]
        )
        raw_after = int(
            app.state.storage.execute("SELECT COUNT(*) AS count FROM raw_objects").fetchone()["count"]
        )

    assert response.status_code == 200, response.text
    assert ingest_calls == []
    assert inbox_after == inbox_before
    assert raw_after == raw_before
    assert len(agent_calls) == 1
    _user_id, forwarded_message, forwarded = agent_calls[0]
    assert forwarded_message == prompt
    assert forwarded["ingestion_result"] == {
        "promoted": False,
        "queued_for_review": False,
        "action": "transient",
        "category": "obsidian_request",
        "reason": "явная команда Obsidian — действие, а не материал",
    }
    assert response.json()["ingestion"]["action"] == "transient"
    assert response.json()["ingestion"]["category"] == "obsidian_request"
    assert response.json()["ingestion"]["reason"] == "явная команда Obsidian — действие, а не материал"
    assert response.json()["ingestion"]["promoted"] is False
    assert response.json()["ingestion"]["queued_for_review"] is False
