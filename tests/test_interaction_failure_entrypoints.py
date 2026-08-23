"""HTTP entrypoints retain body-free failures without changing their response path."""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient


def test_api_chat_failure_is_retained_before_assistant_commit(settings) -> None:
    from friday.server import create_app

    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        client.app.state.agent.chat = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("PRIVATE PROVIDER ERROR 7419")
        )
        response = client.post(
            "/api/chat",
            json={"message": "PRIVATE REQUEST 7419"},
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )

        assert response.status_code == 500
        row = client.app.state.storage.execute(
            "SELECT * FROM interaction_failure_traces ORDER BY created_at DESC,id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["entrypoint"] == "api_chat"
        assert row["route"] == "admission"
        assert "PRIVATE" not in row["trace_json"]


def test_regenerate_failure_keeps_the_existing_conversation_owner(settings) -> None:
    from friday.server import create_app

    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        seeded = client.post("/api/chat", json={"message": "seed turn"}, headers=headers)
        assert seeded.status_code == 200, seeded.text
        conversation_id = seeded.json()["conversation_id"]
        client.app.state.agent.chat = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("PRIVATE REGENERATE ERROR 3682")
        )

        response = client.post(
            "/api/me/regenerate",
            json={"conversation_id": conversation_id},
            headers=headers,
        )

        assert response.status_code == 500
        row = client.app.state.storage.execute(
            """SELECT * FROM interaction_failure_traces
                WHERE entrypoint='regenerate'
                ORDER BY created_at DESC,id DESC LIMIT 1"""
        ).fetchone()
        assert row is not None
        assert row["conversation_id"] == conversation_id
        assert "PRIVATE" not in row["trace_json"]
