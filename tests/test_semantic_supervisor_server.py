from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from typing import Any

from fastapi.testclient import TestClient

from friday import semantic_supervisor_policy
from friday.orchestration.semantic_supervisor_runtime import SemanticSupervisorShadowRuntime
from friday.secondary_brain import ModelWorkload
from friday.security import sign_bridge_request


class _AdmittedShadowScheduler:
    def __init__(self) -> None:
        self.started = 0
        self.closed = 0

    def workload_mode(self, workload: ModelWorkload) -> str:
        assert workload is ModelWorkload.PLAN_CANDIDATE
        return "shadow"

    def start(self) -> None:
        self.started += 1

    async def aclose(self) -> None:
        self.closed += 1


def _signed_bridge_post(client: TestClient, settings: Any, payload: dict[str, Any]) -> Any:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    return client.post(
        "/api/chat",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Friday-Timestamp": str(timestamp),
            "X-Friday-User": "5001",
            "X-Friday-Chat": "5001",
            "X-Friday-Nonce": nonce,
            "X-Friday-Signature": sign_bridge_request(
                settings.telegram_bridge_secret,
                timestamp=timestamp,
                method="POST",
                path="/api/chat",
                external_user_id="5001",
                chat_id="5001",
                nonce=nonce,
                body=body,
            ),
        },
    )


def test_health_exposes_closed_semantic_supervisor_default(settings: Any) -> None:
    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        status = client.get("/api/health").json()["semantic_supervisor"]
        assert status == {
            "schema": "friday.semantic-supervisor-shadow-runtime.v1",
            "installed": False,
            "role": "discarded_advisory_shadow",
            "requested_mode": "off",
            "effective_mode": "off",
            "promotion_admitted": False,
            "runtime_owner": "unchanged",
            "publication_owner": "primary",
            "tools_allowed": False,
            "effects_allowed": False,
            "execution_allowed": False,
            "activation": {
                "schema": "friday.supervisor-assist-activation-status.v1",
                "configured": False,
                "reason": "default_off",
                "requested_mode": "off",
                "source_revision_loaded": False,
                "registry_binding_loaded": False,
                "scheduler_projection_loaded": False,
                "scheduler_runtime_available": False,
                "evidence_loaded": False,
                "evidence_authority": "none",
                "operator_gate_enabled": False,
                "canary_actor_binding_count": 0,
                "promotion_admitted": False,
                "evidence_accepted": False,
                "acceptance_authority": "none",
                "body_free": True,
            },
        }


def test_server_installs_and_closes_non_owning_shadow_without_hiding_router_mode(
    settings: Any,
    monkeypatch: Any,
) -> None:
    import friday.server as server

    scheduler = _AdmittedShadowScheduler()
    monkeypatch.setattr(server, "build_secondary_brain", lambda _settings: scheduler)
    configured = replace(
        settings,
        semantic_supervisor_mode="shadow",
        semantic_supervisor_tasks=(
            "compare_archive_with_current_web",
            "compare_current_file_with_current_web",
        ),
        semantic_supervisor_max_steps=6,
        semantic_supervisor_max_review_rounds=0,
        semantic_supervisor_timeout_sec=12.0,
        secondary_llm_profile=semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
    )
    app = server.create_app(configured)

    with TestClient(app) as client:
        assert isinstance(app.state.agent, SemanticSupervisorShadowRuntime)
        assert app.state.agent is not app.state.orchestration_agent
        payload = client.get("/api/health").json()
        semantic = payload["semantic_supervisor"]
        assert semantic["installed"] is True
        assert semantic["effective_mode"] == "shadow"
        assert semantic["promotion_admitted"] is False
        assert semantic["tools_allowed"] is False
        assert semantic["effects_allowed"] is False
        assert semantic["execution_allowed"] is False
        assert payload["orchestration"]["installed_mode"] == "legacy"
        assert scheduler.started == 1

    assert scheduler.closed == 1
    assert app.state.agent.semantic_supervisor_status()["effective_mode"] == "off"


def test_promotion_settings_without_loaded_material_remain_discarded_shadow(
    settings: Any,
    monkeypatch: Any,
) -> None:
    import friday.server as server

    scheduler = _AdmittedShadowScheduler()
    activation_loads = 0

    def unavailable_activation(_settings: Any, _scheduler: Any) -> tuple[None, None]:
        nonlocal activation_loads
        activation_loads += 1
        return None, None

    monkeypatch.setattr(server, "build_secondary_brain", lambda _settings: scheduler)
    monkeypatch.setattr(
        server,
        "_load_semantic_supervisor_activation_material",
        unavailable_activation,
    )
    configured = replace(
        settings,
        semantic_supervisor_mode="assist",
        semantic_supervisor_tasks=("compare_current_file_with_current_web",),
        semantic_supervisor_max_steps=6,
        semantic_supervisor_max_review_rounds=0,
        semantic_supervisor_timeout_sec=12.0,
        semantic_supervisor_promotion_enabled=True,
        semantic_supervisor_promotion_evidence_file="/private/evidence.json",
        semantic_supervisor_promotion_evidence_sha256="a" * 64,
        semantic_supervisor_promotion_source_revision_sha256="b" * 64,
        semantic_supervisor_promotion_registry_binding_sha256="c" * 64,
        semantic_supervisor_promotion_canary_actor_bindings=(),
        secondary_llm_profile=semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
    )

    app = server.create_app(configured)
    with TestClient(app) as client:
        payload = client.get("/api/health").json()["semantic_supervisor"]
        assert isinstance(app.state.agent, SemanticSupervisorShadowRuntime)
        assert payload["effective_mode"] == "shadow"
        assert payload["promotion_admitted"] is False
        assert payload["execution_allowed"] is False
        assert payload["activation"] == {
            "schema": "friday.supervisor-assist-activation-status.v1",
            "configured": False,
            "reason": "activation_material_unavailable",
            "requested_mode": "assist",
            "source_revision_loaded": False,
            "registry_binding_loaded": False,
            "scheduler_projection_loaded": False,
            "scheduler_runtime_available": False,
            "evidence_loaded": False,
            "evidence_authority": "none",
            "operator_gate_enabled": False,
            "canary_actor_binding_count": 0,
            "promotion_admitted": False,
            "evidence_accepted": False,
            "acceptance_authority": "none",
            "body_free": True,
        }
    assert activation_loads == 1


def test_server_distinguishes_restored_telegram_mode_from_body_explicit_mode(
    settings: Any,
    monkeypatch: Any,
) -> None:
    import friday.server as server

    scheduler = _AdmittedShadowScheduler()
    monkeypatch.setattr(server, "build_secondary_brain", lambda _settings: scheduler)
    configured = replace(
        settings,
        semantic_supervisor_mode="shadow",
        semantic_supervisor_tasks=("compare_archive_with_current_web",),
        semantic_supervisor_max_steps=6,
        semantic_supervisor_max_review_rounds=0,
        semantic_supervisor_timeout_sec=12.0,
        secondary_llm_profile=semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
    )
    app = server.create_app(configured)

    with TestClient(app) as client:
        assert isinstance(app.state.agent, SemanticSupervisorShadowRuntime)
        calls: list[dict[str, Any]] = []
        storage = app.state.storage

        async def bounded_chat(
            user_id: str,
            message: str,
            *,
            actor: Any,
            conversation_id: str | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            calls.append(
                {
                    "mode": kwargs.get("mode"),
                    "explicit": kwargs.get("_semantic_supervisor_explicit_mode_requested"),
                }
            )
            conversation = (
                storage.get_conversation(conversation_id, actor.own_id) if conversation_id else None
            ) or storage.create_conversation(actor.own_id, title=message)
            conversation_id = str(conversation["id"])
            storage.store_message(conversation_id, actor.own_id, "user", message)
            assistant = storage.store_message(
                conversation_id,
                actor.own_id,
                "assistant",
                "Синтетический ответ.",
            )
            return {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "message_id": assistant["id"],
                "message": assistant["content"],
                "message_format": "markdown",
                "tools_used": [],
                "context": {"interaction_mode": kwargs.get("mode") or "dialogue"},
            }

        monkeypatch.setattr(app.state.agent, "chat", bounded_chat)
        base = {
            "telegram_user": {"id": 5001, "first_name": "Alice"},
            "telegram_message_id": 1001,
        }
        first = _signed_bridge_post(
            client,
            configured,
            {**base, "source_ref": "telegram-update:semantic-mode-1", "message": "Начинаем."},
        )
        assert first.status_code == 200, first.text
        restored = _signed_bridge_post(
            client,
            configured,
            {
                **base,
                "telegram_message_id": 1002,
                "source_ref": "telegram-update:semantic-mode-2",
                "message": "Сравни архив с текущими правилами в интернете.",
            },
        )
        assert restored.status_code == 200, restored.text
        explicit = _signed_bridge_post(
            client,
            configured,
            {
                **base,
                "telegram_message_id": 1003,
                "source_ref": "telegram-update:semantic-mode-3",
                "message": "Сравни архив с текущими правилами в интернете.",
                "mode": "dialogue",
            },
        )
        assert explicit.status_code == 200, explicit.text

    assert calls == [
        {"mode": None, "explicit": False},
        {"mode": "dialogue", "explicit": False},
        {"mode": "dialogue", "explicit": True},
    ]
