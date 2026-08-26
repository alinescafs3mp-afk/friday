from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday import semantic_supervisor_policy
from friday.orchestration.semantic_supervisor_runtime import SemanticSupervisorShadowRuntime
from friday.orchestration.supervisor_assist_activation import AssistPromotionActivationMaterial
from friday.orchestration.supervisor_contracts import SupervisorMode
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
                "representative_window_verified": False,
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


def test_server_closes_semantic_runtime_when_worker_startup_fails(
    settings: Any,
    monkeypatch: Any,
) -> None:
    import friday.server as server

    scheduler = _AdmittedShadowScheduler()

    class Runtime:
        def __init__(self, primary: Any) -> None:
            self.primary = primary
            self.closed = 0

        def __getattr__(self, name: str) -> Any:
            return getattr(self.primary, name)

        async def close(self) -> None:
            self.closed += 1

    runtime: Runtime | None = None

    def build_runtime(_settings: Any, primary: Any, _scheduler: Any) -> Runtime:
        nonlocal runtime
        runtime = Runtime(primary)
        return runtime

    async def fail_start(_workers: Any) -> None:
        raise RuntimeError("synthetic worker startup failure")

    monkeypatch.setattr(server, "build_secondary_brain", lambda _settings: scheduler)
    monkeypatch.setattr(
        server,
        "_load_semantic_supervisor_activation_material",
        lambda *_args: (None, None),
    )
    monkeypatch.setattr(server, "build_semantic_supervisor_runtime", build_runtime)
    monkeypatch.setattr(server.WorkersManager, "start", fail_start)

    app = server.create_app(replace(settings, semantic_supervisor_mode="shadow"))
    with pytest.raises(RuntimeError, match="synthetic worker startup failure"), TestClient(app):
        pass

    assert runtime is not None
    assert runtime.closed == 1
    assert scheduler.closed == 1


def test_server_composes_mature_effect_shadow_after_registry_and_closes_it_first(
    settings: Any,
    monkeypatch: Any,
) -> None:
    import friday.server as server

    sequence: list[str] = []
    original_registry_assert = server.ExecutionKernel.assert_risk_declarations_agree
    original_effect_binding = server.operational_effect_capability_snapshot

    class Scheduler(_AdmittedShadowScheduler):
        def start(self) -> None:
            sequence.append("secondary_started")
            super().start()

        async def aclose(self) -> None:
            sequence.append("secondary_closed")
            await super().aclose()

    class EffectRuntime:
        def __init__(self, primary: Any) -> None:
            self.primary = primary
            self.closed = 0

        def __getattr__(self, name: str) -> Any:
            return getattr(self.primary, name)

        def semantic_supervisor_effect_status(self) -> dict[str, object]:
            return {
                "schema": "friday.semantic-supervisor-effect-shadow-runtime.v1",
                "installed": True,
                "requested_mode": "shadow",
                "effective_mode": "shadow" if not self.closed else "off",
                "maturity_accepted": not self.closed,
                "evidence_sha256": "a" * 64 if not self.closed else "",
                "maturity_facts_sha256": "b" * 64 if not self.closed else "",
                "source_revision_sha256": "c" * 64 if not self.closed else "",
                "registry_binding_sha256": "d" * 64 if not self.closed else "",
                "effect_registry_binding_sha256": "e" * 64 if not self.closed else "",
                "policy_id": semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_ID,
                "policy_sha256": semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256,
                "workload": "effect_planning",
                "runtime_owner": "unchanged",
                "publication_owner": "primary",
                "primary_result_unchanged": True,
                "tools_allowed": False,
                "effects_allowed": False,
                "execution_authorized": False,
                "publication_authorized": False,
                "max_pending": 4,
                "pending": 0,
                "dedupe_retention": "process_lifetime",
                "dedupe_algorithm": "fixed_hmac_sha256_bloom_v1",
                "dedupe_identity": "accepted_effect_id_and_outcome_sha256_v1",
                "dedupe_identity_count": 2,
                "dedupe_memory_bounded": True,
                "dedupe_memory_bytes": 512 * 1_024,
                "dedupe_bit_capacity": 512 * 1_024 * 8,
                "dedupe_hash_count": 7,
                "dedupe_bit_probes_per_receipt": 14,
                "dedupe_insert_total": 0,
                "dispatch_total": 0,
                "observation_total": 0,
                "agreements": {},
                "skip_reasons": {},
                "body_free": True,
            }

        async def close(self) -> None:
            sequence.append("effect_closed")
            self.closed += 1

    def registry_assert(kernel: Any) -> None:
        original_registry_assert(kernel)
        sequence.append("registry_closed")

    witness = object()
    scheduler = Scheduler()
    effect_runtime: EffectRuntime | None = None
    effect_binding_digest = ""

    def effect_binding(**kwargs: Any) -> object:
        nonlocal effect_binding_digest
        assert sequence[-1] == "registry_closed"
        snapshot = original_effect_binding(**kwargs)
        assert [item.tool_id for item in snapshot.bindings] == [
            "obsidian_create_note",
            "obsidian_append_note",
        ]
        effect_binding_digest = snapshot.digest_hex()
        sequence.append("effect_binding_loaded")
        return snapshot

    def load_maturity(*_args: Any, **kwargs: Any) -> tuple[object, dict[str, object]]:
        assert sequence[-1] == "effect_binding_loaded"
        assert kwargs["binding_snapshot"].digest_hex() != effect_binding_digest
        assert kwargs["effect_binding_snapshot"].digest_hex() == effect_binding_digest
        sequence.append("maturity_loaded")
        return witness, {"maturity_accepted": True}

    def build_effect(
        _settings: Any,
        primary: Any,
        received_scheduler: Any,
        storage: Any,
        received_witness: Any,
    ) -> EffectRuntime:
        nonlocal effect_runtime
        assert received_scheduler is scheduler
        assert storage is not None
        assert received_witness is witness
        assert sequence[-1] == "maturity_loaded"
        sequence.append("effect_composed")
        effect_runtime = EffectRuntime(primary)
        return effect_runtime

    monkeypatch.setattr(server, "build_secondary_brain", lambda _settings: scheduler)
    monkeypatch.setattr(
        server,
        "_load_semantic_supervisor_activation_material",
        lambda *_args: (None, None),
    )
    monkeypatch.setattr(server.ExecutionKernel, "assert_risk_declarations_agree", registry_assert)
    monkeypatch.setattr(server, "operational_effect_capability_snapshot", effect_binding)
    monkeypatch.setattr(server, "load_configured_supervisor_effect_maturity", load_maturity)
    monkeypatch.setattr(server, "build_supervisor_effect_intent_runtime", build_effect)
    configured = replace(
        settings,
        semantic_supervisor_mode="off",
        semantic_supervisor_effect_mode="shadow",
        semantic_supervisor_effect_evidence_file="/private/maturity.json",
        semantic_supervisor_effect_evidence_sha256="a" * 64,
        obsidian_enabled=True,
    )

    app = server.create_app(configured)
    with TestClient(app) as client:
        assert effect_runtime is not None
        assert app.state.agent is effect_runtime
        assert app.state.semantic_supervisor_effect_runtime is effect_runtime
        effect = client.get("/api/health").json()["semantic_supervisor_effect"]
        assert effect == {
            "schema": "friday.semantic-supervisor-effect-shadow-health.v1",
            "installed": True,
            "requested_mode": "shadow",
            "effective_mode": "shadow",
            "maturity_accepted": True,
            "evidence_sha256": "a" * 64,
            "maturity_facts_sha256": "b" * 64,
            "source_revision_sha256": "c" * 64,
            "registry_binding_sha256": "d" * 64,
            "effect_registry_binding_sha256": "e" * 64,
            "policy_id": semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_ID,
            "policy_sha256": semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256,
            "execution_authorized": False,
            "publication_authorized": False,
        }
        maturity_index = sequence.index("maturity_loaded")
        assert sequence[maturity_index - 2 : maturity_index + 3] == [
            "registry_closed",
            "effect_binding_loaded",
            "maturity_loaded",
            "effect_composed",
            "secondary_started",
        ]

    assert effect_runtime.closed == 1
    assert sequence.index("effect_closed") < sequence.index("secondary_closed")


def test_effect_shadow_default_off_never_loads_optional_activation(
    settings: Any,
    monkeypatch: Any,
) -> None:
    import friday.server as server

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("default-off effect activation must stay lazy")

    monkeypatch.setattr(
        server,
        "_load_semantic_supervisor_activation_material",
        lambda *_args: (None, None),
    )
    monkeypatch.setattr(server, "operational_capability_snapshot", forbidden)
    monkeypatch.setattr(server, "operational_effect_capability_snapshot", forbidden)
    monkeypatch.setattr(server, "load_configured_supervisor_effect_maturity", forbidden)
    monkeypatch.setattr(server, "build_supervisor_effect_intent_runtime", forbidden)

    app = server.create_app(replace(settings, semantic_supervisor_effect_mode="off"))
    with TestClient(app) as client:
        status = client.get("/api/health").json()["semantic_supervisor_effect"]
        assert app.state.semantic_supervisor_effect_runtime is None
        assert status["installed"] is False
        assert status["requested_mode"] == "off"
        assert status["effective_mode"] == "off"
        assert status["maturity_accepted"] is False


def test_effect_shadow_with_disabled_obsidian_fails_off_without_loading_evidence(
    settings: Any,
    monkeypatch: Any,
) -> None:
    import friday.server as server

    monkeypatch.setattr(
        server,
        "_load_semantic_supervisor_activation_material",
        lambda *_args: (None, None),
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("disabled Obsidian must close before maturity loading")

    monkeypatch.setattr(server, "load_configured_supervisor_effect_maturity", forbidden)
    configured = replace(
        settings,
        semantic_supervisor_effect_mode="shadow",
        semantic_supervisor_effect_evidence_file="/private/maturity.json",
        semantic_supervisor_effect_evidence_sha256="a" * 64,
        obsidian_enabled=False,
    )

    app = server.create_app(configured)
    with TestClient(app) as client:
        status = client.get("/api/health").json()["semantic_supervisor_effect"]
        assert app.state.semantic_supervisor_effect_runtime is None
        assert status["requested_mode"] == "shadow"
        assert status["effective_mode"] == "off"
        assert status["maturity_accepted"] is False
        assert status["registry_binding_sha256"] == ""
        assert status["effect_registry_binding_sha256"] == ""


@pytest.mark.parametrize("failure_point", ["binding", "maturity", "composition"])
def test_effect_shadow_activation_failure_never_blocks_server_startup(
    settings: Any,
    monkeypatch: Any,
    failure_point: str,
) -> None:
    import friday.server as server

    monkeypatch.setattr(
        server,
        "_load_semantic_supervisor_activation_material",
        lambda *_args: (None, None),
    )

    def binding(**_kwargs: Any) -> object:
        if failure_point == "binding":
            raise RuntimeError("synthetic binding failure")
        return object()

    def maturity(*_args: Any, **_kwargs: Any) -> tuple[object, dict[str, object]]:
        if failure_point == "maturity":
            raise RuntimeError("synthetic maturity failure")
        return object(), {
            "installed": False,
            "requested_mode": "shadow",
            "effective_mode": "off",
            "maturity_accepted": True,
        }

    def composition(*_args: Any, **_kwargs: Any) -> Any:
        if failure_point == "composition":
            raise RuntimeError("synthetic composition failure")
        raise AssertionError("composition is unreachable before its selected failure")

    monkeypatch.setattr(server, "operational_effect_capability_snapshot", binding)
    monkeypatch.setattr(server, "load_configured_supervisor_effect_maturity", maturity)
    monkeypatch.setattr(server, "build_supervisor_effect_intent_runtime", composition)
    configured = replace(
        settings,
        semantic_supervisor_effect_mode="shadow",
        semantic_supervisor_effect_evidence_file="/private/maturity.json",
        semantic_supervisor_effect_evidence_sha256="a" * 64,
    )

    app = server.create_app(configured)
    with TestClient(app) as client:
        status = client.get("/api/health").json()["semantic_supervisor_effect"]
        assert app.state.semantic_supervisor_effect_runtime is None
        assert status["installed"] is False
        assert status["requested_mode"] == "shadow"
        assert status["effective_mode"] == "off"
        assert status["maturity_accepted"] is False


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


def test_promoted_server_schedules_restart_recovery_and_keeps_model_attestation_lazy(
    settings: Any,
    monkeypatch: Any,
) -> None:
    import friday.server as server

    sequence: list[str] = []

    class Scheduler(_AdmittedShadowScheduler):
        def start(self) -> None:
            sequence.append("secondary_started")
            super().start()

        async def aclose(self) -> None:
            sequence.append("secondary_closed")
            await super().aclose()

    class GraphAdapter:
        def __init__(self, _storage: Any) -> None:
            sequence.append("graph_adapter_created")

        def reconcile_all_active_after_restart(self, **_kwargs: Any) -> tuple[Any, ...]:
            raise AssertionError("promoted restart graphs must be rebound, not drained")

    class LazyModel:
        def __init__(self) -> None:
            self.attest_calls = 0

        async def attest(self, **_kwargs: Any) -> None:
            self.attest_calls += 1
            raise AssertionError("assist model was attested at startup")

    class PromotedRuntime:
        def __init__(self) -> None:
            self.closed = 0
            self.restart_calls = 0

        def start_restart_recovery(self) -> None:
            sequence.append("restart_recovery_started")
            self.restart_calls += 1

        async def close(self) -> None:
            sequence.append("promoted_closed")
            self.closed += 1

    material = object.__new__(AssistPromotionActivationMaterial)
    object.__setattr__(material, "configured", True)
    object.__setattr__(material, "requested_mode", SupervisorMode.ASSIST)
    scheduler = Scheduler()
    model = LazyModel()
    promoted = PromotedRuntime()
    composition_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(server, "SupervisorAssistGraphAdapter", GraphAdapter)
    monkeypatch.setattr(server, "build_secondary_brain", lambda _settings: scheduler)
    monkeypatch.setattr(
        server,
        "_load_semantic_supervisor_activation_material",
        lambda _settings, _scheduler: (material, None),
    )
    monkeypatch.setattr(server, "create_attested_v12_model_runtime", lambda _llm: model)

    def build_promoted(**kwargs: Any) -> PromotedRuntime:
        composition_calls.append(kwargs)
        sequence.append("promoted_composed")
        return promoted

    monkeypatch.setattr(server, "build_supervisor_assist_production_runtime", build_promoted)
    monkeypatch.setattr(
        server,
        "build_semantic_supervisor_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("shadow was built after promoted composition")
        ),
    )
    configured = replace(
        settings,
        semantic_supervisor_mode="assist",
        semantic_supervisor_tasks=("compare_current_file_with_current_web",),
        semantic_supervisor_max_steps=6,
        semantic_supervisor_max_review_rounds=1,
        semantic_supervisor_timeout_sec=12.0,
    )

    app = server.create_app(configured)
    with TestClient(app):
        assert app.state.agent is promoted
        assert app.state.semantic_supervisor_runtime is promoted
        assert app.state.v12_model_runtime is model
        assert model.attest_calls == 0
        assert promoted.restart_calls == 1
        assert len(composition_calls) == 1
        assert composition_calls[0]["primary_model_runtime"] is model
        assert sequence.index("promoted_composed") < sequence.index("secondary_started")
        assert sequence.index("secondary_started") < sequence.index("restart_recovery_started")
        assert app.state.semantic_supervisor_restart_reconciliation == {
            "retired": 0,
            "retained": 0,
            "resume_scheduled": True,
        }

    assert promoted.closed == 1
    assert sequence.index("promoted_closed") < sequence.index("secondary_closed")


def test_server_aborts_before_runtime_exposure_when_restart_drain_is_uncertain(
    settings: Any,
    monkeypatch: Any,
) -> None:
    import friday.server as server

    secondary_built = False

    class BrokenGraphAdapter:
        def __init__(self, _storage: Any) -> None:
            pass

        def reconcile_all_active_after_restart(self, **_kwargs: Any) -> None:
            raise RuntimeError("synthetic restart uncertainty")

    def build_secondary(_settings: Any) -> object:
        nonlocal secondary_built
        secondary_built = True
        return object()

    monkeypatch.setattr(server, "SupervisorAssistGraphAdapter", BrokenGraphAdapter)
    monkeypatch.setattr(server, "build_secondary_brain", build_secondary)
    app = server.create_app(settings)

    with pytest.raises(RuntimeError, match="synthetic restart uncertainty"), TestClient(app):
        pass
    assert secondary_built is False


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
