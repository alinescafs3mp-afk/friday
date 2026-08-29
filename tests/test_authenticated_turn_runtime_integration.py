from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.orchestration.turn_context import AuthenticatedTurnContext
from friday.orchestration.turn_context_runtime import current_primary_authenticated_turn_context
from friday.permissions import LEGACY_OWNER_USER_ID

_PUBLICATION_KEY = "authenticated_turn_publication"


def test_claimed_existing_scalar_turn_keeps_one_context_through_publication(
    settings: Any,
    monkeypatch: Any,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    captured: list[AuthenticatedTurnContext] = []
    ingested: list[AuthenticatedTurnContext] = []

    with TestClient(app) as client:
        assert type(app.state.agent) is AgentRuntime
        seeded = client.post(
            "/api/chat",
            headers=headers,
            json={"message": "Создай исходный разговор.", "enable_tools": False},
        )
        assert seeded.status_code == 200, seeded.text
        conversation_id = str(seeded.json()["conversation_id"])

        original_ingest_text = app.state.ingestion.ingest_text

        async def exact_ingest(*args: Any, **kwargs: Any) -> dict[str, Any]:
            exact = current_primary_authenticated_turn_context()
            assert type(exact) is AuthenticatedTurnContext
            ingested.append(exact)
            return await original_ingest_text(*args, **kwargs)

        monkeypatch.setattr(app.state.ingestion, "ingest_text", exact_ingest)

        async def exact_response(
            context: AgentContext,
            _message: str,
            _attachments: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            exact = context._authenticated_turn_context
            assert type(exact) is AuthenticatedTurnContext
            assert current_primary_authenticated_turn_context(exact) is exact
            captured.append(exact)
            return {
                "content": "Контекст принят.",
                "tools_used": [],
                "_model_generated": True,
            }

        monkeypatch.setattr(app.state.agent, "_generate_response", exact_response)
        response = client.post(
            "/api/chat",
            headers=headers,
            json={
                "message": "Сформулируй краткий нейтральный ответ о текущем состоянии.",
                "conversation_id": conversation_id,
                "source_ref": "authenticated-scalar-runtime-1",
                "enable_tools": False,
            },
        )
        assert response.status_code == 200, response.text

        rows = app.state.storage.get_conversation_messages(
            conversation_id,
            user_id=LEGACY_OWNER_USER_ID,
            limit=10,
        )

    assert len(captured) == len(ingested) == 1
    exact = captured[0]
    assert ingested[0] is exact
    assert exact.authority.conversation_id == conversation_id
    assert exact.model_input.message == "Сформулируй краткий нейтральный ответ о текущем состоянии."
    current_rows = rows[-2:]
    assert [str(row["role"]) for row in current_rows] == ["user", "assistant"]
    projections = [json.loads(str(row["metadata_json"]))[_PUBLICATION_KEY] for row in current_rows]
    assert [projection["publication_role"] for projection in projections] == ["user", "assistant"]
    assert {projection["turn_id"] for projection in projections} == {exact.turn_id}
    assert {projection["context_authority_sha256"] for projection in projections} == {
        exact.context_authority_sha256
    }
    assert {projection["request_effect_binding_sha256"] for projection in projections} == {
        exact.effect_fence.request_effect_binding_sha256
    }
    assert all(_PUBLICATION_KEY not in json.loads(str(row["metadata_json"])) for row in rows[:-2])


@pytest.mark.parametrize(
    ("message", "source_ref"),
    [
        ("Не запоминай: это приватная временная реплика.", "authenticated-no-save"),
        ("x" * 16_001, "authenticated-overlong-scalar"),
        ("Обычная совместимая реплика.", "abc\nxyz"),
        ("Обычная совместимая реплика.", "😀" * 300),
    ],
)
def test_non_exact_scalar_surfaces_remain_on_the_legacy_compatibility_path(
    settings: Any,
    monkeypatch: Any,
    message: str,
    source_ref: str,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    carried: list[object] = []

    with TestClient(app) as client:
        conversation = app.state.storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title="authenticated compatibility limit",
        )

        async def compatibility_primary(
            _user_id: str,
            _message: str,
            **kwargs: Any,
        ) -> dict[str, Any]:
            carried.append(kwargs.get("_authenticated_turn_context"))
            return {
                "conversation_id": conversation["id"],
                "message": "legacy compatibility completed",
                "answer": "legacy compatibility completed",
                "context": {"interaction_mode": "dialogue"},
            }

        monkeypatch.setattr(app.state.agent, "chat", compatibility_primary)
        response = client.post(
            "/api/chat",
            headers=headers,
            json={
                "message": message,
                "conversation_id": conversation["id"],
                "source_ref": source_ref,
            },
        )

    assert response.status_code == 200, response.text
    assert carried == [None]


@pytest.mark.parametrize(
    "configured",
    [
        lambda settings: replace(settings, llm_max_tokens=1_000_001),
        lambda settings: replace(settings, llm_timeout_sec=1_201.0),
    ],
)
def test_unsupported_context_budget_settings_remain_on_the_legacy_path(
    settings: Any,
    monkeypatch: Any,
    configured: Any,
) -> None:
    from friday.server import create_app

    tuned = configured(settings)
    app = create_app(tuned)
    headers = {"Authorization": f"Bearer {tuned.api_token}"}
    carried: list[object] = []

    with TestClient(app) as client:
        conversation = app.state.storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title="unsupported context budget compatibility",
        )

        async def compatibility_primary(
            _user_id: str,
            _message: str,
            **kwargs: Any,
        ) -> dict[str, Any]:
            carried.append(kwargs.get("_authenticated_turn_context"))
            return {
                "conversation_id": conversation["id"],
                "message": "legacy compatibility completed",
                "answer": "legacy compatibility completed",
                "context": {"interaction_mode": "dialogue"},
            }

        monkeypatch.setattr(app.state.agent, "chat", compatibility_primary)
        response = client.post(
            "/api/chat",
            headers=headers,
            json={
                "message": "Обычная реплика с поддерживаемой legacy-конфигурацией.",
                "conversation_id": conversation["id"],
                "source_ref": "unsupported-context-budget",
            },
        )

    assert response.status_code == 200, response.text
    assert carried == [None]


def test_concurrent_mode_change_cannot_upgrade_an_authenticated_turn(
    settings: Any,
    monkeypatch: Any,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    generated: list[bool] = []

    with TestClient(app, raise_server_exceptions=False) as client:
        assert type(app.state.agent) is AgentRuntime
        conversation = app.state.storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title="sealed dialogue mode",
        )
        original_chat = app.state.agent.chat

        async def forbidden_generation(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            generated.append(True)
            return {"content": "must not run", "tools_used": []}

        async def mutate_then_enter_runtime(*args: Any, **kwargs: Any) -> dict[str, Any]:
            app.state.storage.set_conversation_mode(
                str(conversation["id"]),
                LEGACY_OWNER_USER_ID,
                "engineer",
            )
            return await original_chat(*args, **kwargs)

        monkeypatch.setattr(app.state.agent, "_generate_response", forbidden_generation)
        monkeypatch.setattr(app.state.agent, "chat", mutate_then_enter_runtime)
        response = client.post(
            "/api/chat",
            headers=headers,
            json={
                "message": "Ответь нейтрально без инструментов.",
                "conversation_id": conversation["id"],
                "source_ref": "authenticated-mode-race",
                "enable_tools": False,
            },
        )

    assert response.status_code == 500
    assert generated == []


@pytest.mark.asyncio
async def test_provably_pre_effect_retry_gets_a_new_attempt_root(settings: Any) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    payload_source = "authenticated-safe-retry-1"

    async with app.router.lifespan_context(app):
        conversation = app.state.storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title="authenticated safe retry",
        )
        entered = asyncio.Event()
        never_release = asyncio.Event()
        contexts: list[AuthenticatedTurnContext] = []

        async def primary_once(
            _user_id: str,
            _message: str,
            **kwargs: Any,
        ) -> dict[str, Any]:
            exact = kwargs.get("_authenticated_turn_context")
            assert type(exact) is AuthenticatedTurnContext
            assert current_primary_authenticated_turn_context(exact) is exact
            contexts.append(exact)
            if len(contexts) == 1:
                entered.set()
                await never_release.wait()
            return {
                "conversation_id": conversation["id"],
                "message": "safe retry completed",
                "answer": "safe retry completed",
                "context": {"interaction_mode": "dialogue"},
            }

        app.state.agent.chat = primary_once
        transport = httpx.ASGITransport(app=app, client=("198.51.100.57", 8357))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            payload = {
                "message": "Найди в интернете свежую нейтральную новость.",
                "conversation_id": conversation["id"],
                "source_ref": payload_source,
            }
            first = asyncio.create_task(client.post("/api/chat", headers=headers, json=payload))
            await asyncio.wait_for(entered.wait(), timeout=2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            retry = await client.post("/api/chat", headers=headers, json=payload)

    assert retry.status_code == 200, retry.text
    assert retry.json()["message"] == "safe retry completed"
    assert len(contexts) == 2
    first_context, retried_context = contexts
    assert first_context.turn_id != retried_context.turn_id
    assert first_context.authority.ingress_issued_token != retried_context.authority.ingress_issued_token
    assert first_context.authority.update_id == retried_context.authority.update_id == payload_source
    assert first_context.model_input == retried_context.model_input
