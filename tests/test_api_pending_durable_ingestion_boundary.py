"""HTTP intake admits durable replies before ingestion or V12 planning."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday.orchestration import OrchestrationRouter
from friday.permissions import LEGACY_OWNER_USER_ID

_CONVERSATION_ID = "conv_pending_durable_api"


def _result() -> dict[str, Any]:
    return {
        "conversation_id": _CONVERSATION_ID,
        "message_id": "msg_0123456789abcdef",
        "message": "durable candidate response",
        "message_format": "plain",
        "tools_used": [],
        "files": [],
        "voice": None,
        "context": {"interaction_mode": "dialogue"},
    }


@pytest.mark.parametrize("admission", ["owned", "exception", "awaitable"])
def test_pending_durable_api_reply_suppresses_ingestion_planner_and_model(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    admission: str,
) -> None:
    from friday.server import create_app

    app = create_app(replace(settings, router_mode="shadow"))
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    admission_calls: list[tuple[str, str, str | None]] = []
    legacy_calls: list[dict[str, Any]] = []
    planner_calls: list[str] = []

    async def forbidden_ingest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("pending durable reply reached ingest_text")

    async def legacy_chat(_user_id: str, _message: str, **kwargs: Any) -> dict[str, Any]:
        legacy_calls.append(kwargs)
        return _result()

    async def forbidden_plan(*_args: Any, **_kwargs: Any) -> Any:
        planner_calls.append("called")
        raise AssertionError("pending durable reply reached the planner")

    with TestClient(app) as client:
        router = app.state.agent
        assert isinstance(router, OrchestrationRouter)
        legacy = router._legacy  # noqa: SLF001 - production composition regression

        def owner_check(
            person_id: str,
            message: str,
            *,
            actor: Any,
            conversation_id: str | None,
        ) -> object:
            del actor
            admission_calls.append((person_id, message, conversation_id))
            if admission == "exception":
                raise RuntimeError("private lookup failed")
            if admission == "awaitable":
                async def result() -> bool:
                    return True

                return result()
            return True

        monkeypatch.setattr(legacy, "pending_durable_turn_admission", owner_check)
        monkeypatch.setattr(legacy, "chat", legacy_chat)
        monkeypatch.setattr(router._planner, "plan", forbidden_plan)  # noqa: SLF001
        monkeypatch.setattr(router._planner, "plan_attested", forbidden_plan)  # noqa: SLF001
        monkeypatch.setattr(app.state.ingestion, "ingest_text", forbidden_ingest)
        response = client.post(
            "/api/chat",
            json={"message": "2", "conversation_id": _CONVERSATION_ID},
            headers=headers,
        )

    assert response.status_code == 200, response.text
    assert len(admission_calls) == 1
    assert all(call == (LEGACY_OWNER_USER_ID, "2", _CONVERSATION_ID) for call in admission_calls)
    assert len(legacy_calls) == 1
    assert legacy_calls[0]["ingestion_result"] is None
    assert legacy_calls[0]["_pending_durable_admission"] is not None
    assert planner_calls == []


def test_pre_ingestion_owned_receipt_survives_router_race_without_second_admission(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.server import create_app

    app = create_app(replace(settings, router_mode="shadow"))
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    ownership = {"current": True}
    admission_calls: list[bool] = []
    planner_calls: list[str] = []
    legacy_calls: list[dict[str, Any]] = []

    def owner_check(*_args: Any, **_kwargs: Any) -> bool:
        admission_calls.append(ownership["current"])
        return ownership["current"]

    async def forbidden_ingest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("owned race reached ingest_text")

    async def forbidden_plan(*_args: Any, **_kwargs: Any) -> Any:
        planner_calls.append("called")
        raise AssertionError("owned race reached planner")

    async def legacy_chat(_user_id: str, _message: str, **kwargs: Any) -> dict[str, Any]:
        legacy_calls.append(kwargs)
        return _result()

    with TestClient(app) as client:
        router = app.state.agent
        assert isinstance(router, OrchestrationRouter)
        legacy = router._legacy  # noqa: SLF001 - production composition regression
        original_router_chat = router.chat

        async def complete_between_preflight_and_router(*args: Any, **kwargs: Any) -> dict[str, Any]:
            ownership["current"] = False
            return await original_router_chat(*args, **kwargs)

        monkeypatch.setattr(legacy, "pending_durable_turn_admission", owner_check)
        monkeypatch.setattr(legacy, "chat", legacy_chat)
        monkeypatch.setattr(router, "chat", complete_between_preflight_and_router)
        monkeypatch.setattr(router._planner, "plan", forbidden_plan)  # noqa: SLF001
        monkeypatch.setattr(router._planner, "plan_attested", forbidden_plan)  # noqa: SLF001
        monkeypatch.setattr(app.state.ingestion, "ingest_text", forbidden_ingest)
        response = client.post(
            "/api/chat",
            json={"message": "2", "conversation_id": _CONVERSATION_ID},
            headers=headers,
        )

    assert response.status_code == 200, response.text
    assert admission_calls == [True]
    assert planner_calls == []
    assert len(legacy_calls) == 1
    assert legacy_calls[0]["ingestion_result"] is None
    assert legacy_calls[0]["_pending_durable_admission"].is_owned


@pytest.mark.parametrize(
    "override",
    [
        {"enable_tools": False},
        {"mode": "research"},
        {"reply_to": "explicit reply carrier"},
        {"force_knowledge": True},
    ],
)
def test_explicit_api_surface_does_not_claim_pending_durable_turn(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, Any],
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    admission_calls: list[str] = []
    ingest_calls: list[str] = []
    agent_calls: list[dict[str, Any]] = []

    def owner_check(*_args: Any, **_kwargs: Any) -> bool:
        admission_calls.append("called")
        return True

    async def ingest_text(_user_id: str, content: str, **_kwargs: Any) -> dict[str, Any]:
        ingest_calls.append(content)
        return {
            "promoted": False,
            "queued_for_review": True,
            "action": "queued",
            "category": "note",
            "reason": "ordinary explicit surface",
        }

    async def capture_chat(_user_id: str, _message: str, **kwargs: Any) -> dict[str, Any]:
        agent_calls.append(kwargs)
        return _result()

    with TestClient(app) as client:
        monkeypatch.setattr(app.state.agent, "owns_pending_durable_turn", owner_check)
        monkeypatch.setattr(app.state.agent, "chat", capture_chat)
        monkeypatch.setattr(app.state.ingestion, "ingest_text", ingest_text)
        response = client.post(
            "/api/chat",
            json={"message": "2", "conversation_id": _CONVERSATION_ID, **override},
            headers=headers,
        )

    assert response.status_code == 200, response.text
    assert admission_calls == []
    assert ingest_calls == ["2"]
    assert len(agent_calls) == 1
    assert agent_calls[0]["ingestion_result"] is not None
