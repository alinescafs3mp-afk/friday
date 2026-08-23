from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from friday.file_evidence import stamp_current_turn_file_reference
from friday.model_profiles import (
    ModelProfileLease,
    ModelRequirements,
)
from friday.orchestration import (
    OrchestrationRouter,
    RouteClass,
    RouterMode,
    ToolEffect,
    TurnInput,
    TurnPlan,
    TurnPlanError,
    build_orchestrated_agent,
)
from friday.orchestration.capability_outcome import CapabilityOutcome, CapabilityOutcomeStatus
from friday.orchestration.file_read_contract import file_read_plan_supports_attachment_count
from friday.orchestration.planner import V12Planner
from friday.orchestration.router import (
    ReadOnlyRoutePreparation,
    ReadOnlyRouteRequest,
    ReadOnlyRouteResult,
)
from friday.permissions import ActorContext


def _plan_payload(
    *,
    route: str = "file_read",
    tool_intents: list[dict[str, Any]] | None = None,
    evidence_requests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if evidence_requests is None:
        evidence_requests = (
            [
                {
                    "kind": "attached_files",
                    "query": "сравнить два документа",
                    "max_items": 4,
                    "required": True,
                }
            ]
            if route == "file_read"
            else []
        )
    return {
        "schema": "friday.turn-plan.v1",
        "route": route,
        "objective": "Сравнить документы и вернуть один связный ответ.",
        "evidence_requests": evidence_requests,
        "tool_intents": list(tool_intents or []),
        "output": {
            "format": "text",
            "language": "ru",
            "require_citations": route in {"file_read", "archive_read", "web_read"},
            "one_message": True,
        },
        "confidence": 0.91,
        "fallback": "legacy",
        "reason_code": "two_attached_documents",
    }


class _Runtime:
    def __init__(self, label: str = "legacy", *, error: Exception | None = None) -> None:
        self.label = label
        self.error = error
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.feedback_marker = object()

    async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((user_id, message, kwargs))
        if self.error is not None:
            raise self.error
        return {"message": self.label, "conversation_id": "c-1"}


class _Handler:
    effect = ToolEffect.READ

    def __init__(
        self,
        route: RouteClass = RouteClass.FILE_READ,
        label: str = "v12",
        *,
        error: Exception | None = None,
        delay: float = 0.0,
        current: bool = True,
    ) -> None:
        self.route = route
        self.label = label
        self.error = error
        self.delay = delay
        self.current = current
        self.calls: list[tuple[ReadOnlyRouteRequest, TurnInput, TurnPlan]] = []
        self.prepared: list[tuple[ReadOnlyRouteRequest, TurnInput, TurnPlan]] = []

    async def prepare(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
    ) -> ReadOnlyRoutePreparation | None:
        self.prepared.append((request, turn, plan))
        return ReadOnlyRoutePreparation(
            route=plan.route,
            plan_sha256=plan.canonical_sha256(),
            evidence_identity_sha256="1" * 64,
            private_payload=object(),
        )

    async def preparation_is_current(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        preparation: ReadOnlyRoutePreparation,
    ) -> bool:
        del request, turn, plan, preparation
        return self.current

    async def handle(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        preparation: ReadOnlyRoutePreparation,
    ) -> ReadOnlyRouteResult:
        self.calls.append((request, turn, plan))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return ReadOnlyRouteResult(
            message=f"{self.label} [A1]",
            conversation_id="conv_0000000000000001",
            message_id="msg_0000000000000001",
            evidence_identity_sha256=preparation.evidence_identity_sha256,
            citation_labels=("A1",),
            verified=True,
            outcome=CapabilityOutcome(
                route=plan.route,
                status=CapabilityOutcomeStatus.COMPLETE,
                plan_sha256=plan.canonical_sha256(),
                evidence_identity_sha256=preparation.evidence_identity_sha256,
                citation_labels=("A1",),
                authority_rechecked=True,
                verified=True,
            ),
        )


class _Planner:
    def __init__(self, plan: TurnPlan | None = None, *, error: Exception | None = None) -> None:
        self.returned_plan = plan or TurnPlan.parse(_plan_payload())
        self.error = error
        self.calls: list[tuple[TurnInput, float | None]] = []

    async def plan(self, turn: TurnInput, *, turn_deadline: float | None = None) -> TurnPlan:
        self.calls.append((turn, turn_deadline))
        if self.error is not None:
            raise self.error
        return self.returned_plan

    async def plan_attested(
        self,
        turn: TurnInput,
        *,
        turn_deadline: float | None = None,
    ) -> TurnPlan:
        return await self.plan(turn, turn_deadline=turn_deadline)


class _CurrentTurnCarrier(dict[str, Any]):
    pass


def _current_attachment(
    raw_id: str = "raw_0123456789abcdef",
    *,
    filename: str = "/private/customer/contracts/scan.pdf",
) -> _CurrentTurnCarrier:
    attachment = _CurrentTurnCarrier(
        {
            "filename": filename,
            "mime_type": "application/pdf",
            "size_bytes": 12345,
            "raw_object_id": raw_id,
            "persisted": True,
            "current_turn_only": True,
            "local_path": "/do/not/expose",
        }
    )
    stamp_current_turn_file_reference(
        attachment,
        {
            "id": raw_id,
            "source": "telegram",
            "source_ref": f"telegram-file:{raw_id}",
            "content_type": "file",
            "received_at": "2026-08-18T00:00:00Z",
            "content_hash": "2" * 64,
            "raw_content": "private body",
            "metadata_json": "{}",
        },
    )
    return attachment


def _chat_kwargs() -> dict[str, Any]:
    return {
        "actor": ActorContext("owner", "owner", "test"),
        "conversation_id": "conversation-private-id",
        "attachments": [_current_attachment()],
        "enable_tools": True,
        "reply_to": "Проверь обе страницы.",
        "quoted_attachment_reference": False,
        "turn_deadline": time.monotonic() + 60.0,
    }


def test_router_mode_unknown_is_fail_closed() -> None:
    assert RouterMode.fail_closed("legacy") is RouterMode.LEGACY
    assert RouterMode.fail_closed("V12") is RouterMode.V12
    assert RouterMode.fail_closed("typo-enables-new-code") is RouterMode.LEGACY
    assert RouterMode.fail_closed(None) is RouterMode.LEGACY


def test_turn_input_is_bounded_and_drops_ids_and_private_paths() -> None:
    turn = TurnInput.from_chat(
        message="сравни документы",
        actor=SimpleNamespace(is_owner=True, shared_tenant=True, user_id="secret-user-id"),
        conversation_id="secret-conversation-id",
        attachments=_chat_kwargs()["attachments"],
        enable_tools=True,
        synthetic_document_notice=False,
        mode=None,
        reply_to="ответ на документ",
        quoted_attachment_reference=True,
        reply_assistant_reference=False,
    )
    serialized = json.dumps(turn.model_payload(), ensure_ascii=False, sort_keys=True)
    assert turn.attachments[0].name == "attachment-1"
    assert "secret-user-id" not in serialized
    assert "secret-conversation-id" not in serialized
    assert "raw_0123456789abcdef" not in serialized
    assert "/private/customer" not in serialized
    assert "/do/not/expose" not in serialized

    production_shape = TurnInput.from_chat(
        message="прочитай",
        actor=SimpleNamespace(is_owner=True, shared_tenant=False),
        conversation_id=None,
        attachments=[{"filename": "scan.jpg", "transient_text": "OCR body"}],
        enable_tools=True,
        synthetic_document_notice=True,
        mode=None,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )
    assert production_shape.attachments[0].extracted_text_available is True

    invalid_unicode = TurnInput.from_chat(
        message="прочитай",
        actor=SimpleNamespace(is_owner=True, shared_tenant=False),
        conversation_id=None,
        attachments=[{"filename": "scan.jpg", "mime_type": "x/\ud800"}],
        enable_tools=True,
        synthetic_document_notice=False,
        mode="\ud800",
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )
    json.dumps(invalid_unicode.model_payload(), ensure_ascii=False).encode("utf-8")
    assert "\ud800" not in invalid_unicode.attachments[0].media_type
    assert "\ud800" not in invalid_unicode.conversation_mode


def test_planner_projection_neutralizes_filenames_and_synthetic_upload_message() -> None:
    secret = "jrc_DO_NOT_FORWARD_THIS_FILENAME_CREDENTIAL_1234567890"
    turn = TurnInput.from_chat(
        message=f"Загружен документ: {secret}.txt",
        actor=SimpleNamespace(is_owner=True, shared_tenant=False),
        conversation_id=None,
        attachments=[{"filename": f"/private/{secret}.txt", "mime_type": f"text/{secret}"}],
        enable_tools=True,
        synthetic_document_notice=True,
        mode=None,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )

    serialized = json.dumps(turn.model_payload(), ensure_ascii=False, sort_keys=True)
    assert turn.message == "Загружен документ."
    assert turn.attachments[0].name == "attachment-1"
    assert turn.attachments[0].media_type == "text"
    assert secret not in serialized
    assert "/private/" not in serialized


def test_turn_plan_is_closed_and_canonical() -> None:
    plan = TurnPlan.parse(json.dumps(_plan_payload(), ensure_ascii=False))
    assert plan.route is RouteClass.FILE_READ
    assert plan.read_only is True
    assert TurnPlan.parse(plan.payload()) == plan
    assert len(plan.canonical_sha256()) == 64

    with pytest.raises(TurnPlanError, match="extra"):
        TurnPlan.parse({**_plan_payload(), "hidden_reasoning": "do not accept"})
    with pytest.raises(TurnPlanError, match="without surrounding text"):
        TurnPlan.parse("```json\n{}\n```")
    with pytest.raises(TurnPlanError, match="confidence"):
        TurnPlan.parse({**_plan_payload(), "confidence": float("nan")})
    nested_nan = _plan_payload(
        route="effect",
        evidence_requests=[],
        tool_intents=[
            {
                "name": "example.write",
                "arguments": {"value": float("nan")},
                "effect": "write",
                "purpose": "test",
            }
        ],
    )
    with pytest.raises(TurnPlanError, match="non-finite"):
        TurnPlan.parse(nested_nan)
    with pytest.raises(TurnPlanError, match="invalid number"):
        TurnPlan.parse(json.dumps(nested_nan).replace("NaN", "Infinity"))
    with pytest.raises(TurnPlanError, match="duplicate key"):
        TurnPlan.parse(
            json.dumps(_plan_payload()).replace(
                '"route": "file_read"', '"route": "file_read", "route": "web_read"'
            )
        )
    with pytest.raises(TurnPlanError, match="valid UTF-8"):
        TurnPlan.parse({**_plan_payload(), "objective": "\ud800"})
    invalid_key = _plan_payload(
        route="effect",
        evidence_requests=[],
        tool_intents=[
            {
                "name": "example.write",
                "arguments": {"\ud800": 1},
                "effect": "write",
                "purpose": "test",
            }
        ],
    )
    with pytest.raises(TurnPlanError, match="keys must be valid UTF-8"):
        TurnPlan.parse(invalid_key)
    with pytest.raises(TurnPlanError, match="keys must be valid UTF-8"):
        TurnPlan.parse(json.dumps(invalid_key))


def test_turn_plan_relationships_reject_semantic_authority_escalation() -> None:
    mutating = {
        "name": "archive.delete",
        "arguments": {"object": "anything"},
        "effect": "write",
        "purpose": "remove a record",
    }
    with pytest.raises(TurnPlanError, match="route=effect"):
        TurnPlan.parse(_plan_payload(tool_intents=[mutating]))
    with pytest.raises(TurnPlanError, match="file_read requires"):
        TurnPlan.parse(_plan_payload(evidence_requests=[]))
    with pytest.raises(TurnPlanError, match="small_talk"):
        TurnPlan.parse(
            _plan_payload(route="small_talk", evidence_requests=[_plan_payload()["evidence_requests"][0]])
        )
    with pytest.raises(TurnPlanError, match="conversation evidence only"):
        TurnPlan.parse(
            _plan_payload(
                route="ordinary_dialogue",
                evidence_requests=[{"kind": "web", "query": "news", "max_items": 2, "required": True}],
            )
        )
    with pytest.raises(TurnPlanError, match="archive_read requires"):
        TurnPlan.parse(_plan_payload(route="archive_read", evidence_requests=[]))
    optional_source = _plan_payload()
    optional_source["evidence_requests"][0]["required"] = False
    with pytest.raises(TurnPlanError, match="file_read requires"):
        TurnPlan.parse(optional_source)
    with pytest.raises(TurnPlanError, match="cannot request archive"):
        TurnPlan.parse(
            _plan_payload(
                route="file_read",
                evidence_requests=[
                    {"kind": "archive", "query": "old", "max_items": 2, "required": True},
                    {
                        "kind": "attached_files",
                        "query": "current",
                        "max_items": 2,
                        "required": True,
                    },
                ],
            )
        )
    no_citations = _plan_payload()
    no_citations["output"] = {**no_citations["output"], "require_citations": False}
    with pytest.raises(TurnPlanError, match="require citations"):
        TurnPlan.parse(no_citations)
    multiple_publications = _plan_payload()
    multiple_publications["output"] = {**multiple_publications["output"], "one_message": False}
    with pytest.raises(TurnPlanError, match="exactly one"):
        TurnPlan.parse(multiple_publications)
    document_output = _plan_payload()
    document_output["output"] = {**document_output["output"], "format": "document"}
    with pytest.raises(TurnPlanError, match="requires route=effect"):
        TurnPlan.parse(document_output)


def test_turn_plan_tool_arguments_are_deeply_immutable() -> None:
    payload = _plan_payload(
        route="effect",
        evidence_requests=[],
        tool_intents=[
            {
                "name": "example.write",
                "arguments": {"outer": {"items": ["first"]}},
                "effect": "write",
                "purpose": "test",
            }
        ],
    )
    plan = TurnPlan.parse(payload)
    original_sha = plan.canonical_sha256()
    payload["tool_intents"][0]["arguments"]["outer"]["items"].append("second")
    assert plan.canonical_sha256() == original_sha
    with pytest.raises(TypeError):
        plan.tool_intents[0].arguments["new"] = "forbidden"  # type: ignore[index]


@pytest.mark.parametrize(
    ("output_key", "value"),
    [("format", "table"), ("language", "en")],
)
def test_phase_one_file_canary_accepts_only_russian_text_output(
    output_key: str,
    value: str,
) -> None:
    payload = _plan_payload()
    payload["output"][output_key] = value

    assert not file_read_plan_supports_attachment_count(TurnPlan.parse(payload), 1)


def test_configuration_defaults_to_legacy_and_invalid_env_is_legacy(settings, monkeypatch) -> None:
    assert settings.router_mode == "legacy"
    assert settings.router_canary_routes == ("file_read",)
    assert settings.public_dict()["orchestration"] == {
        "mode": "legacy",
        "canary_routes": ["file_read"],
        "canary_user_count": 0,
        "plan_timeout_sec": 12.0,
    }

    monkeypatch.setenv("FRIDAY_ROUTER_MODE", "spelling-error")
    from friday.config import load_settings

    assert load_settings().router_mode == "legacy"


def test_builder_returns_the_same_legacy_object_without_a_planner(settings) -> None:
    legacy = _Runtime()

    class _ModelThatMustNotBeTouched:
        async def chat(self, *args: Any, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
            raise AssertionError("legacy mode constructed or called a planner")

    assert build_orchestrated_agent(settings, legacy, _ModelThatMustNotBeTouched()) is legacy
    assert (
        build_orchestrated_agent(
            replace(settings, router_mode="invalid"), legacy, _ModelThatMustNotBeTouched()
        )
        is legacy
    )
    assert (
        build_orchestrated_agent(
            replace(settings, router_mode="canary"),
            legacy,
            _ModelThatMustNotBeTouched(),
            route_handlers={RouteClass.FILE_READ: _Handler()},
        )
        is legacy
    )


def test_server_wiring_keeps_legacy_exact_and_shadow_handlerless(settings) -> None:
    from fastapi.testclient import TestClient

    from friday.agent_runtime import AgentRuntime
    from friday.server import create_app

    legacy_app = create_app(settings)
    with TestClient(legacy_app):
        assert type(legacy_app.state.agent) is AgentRuntime

    shadow_app = create_app(replace(settings, router_mode="shadow"))
    with TestClient(shadow_app):
        assert type(shadow_app.state.agent) is OrchestrationRouter
        assert shadow_app.state.agent._route_handlers == {}  # noqa: SLF001


def test_server_registers_file_read_only_after_live_attestation(
    settings,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    import friday.server as server_module

    class _AttestedRuntime:
        def __init__(self) -> None:
            self.attest_calls = 0

        async def attest(self, *, absolute_deadline: float) -> object:
            assert absolute_deadline > time.monotonic()
            self.attest_calls += 1
            return object()

        def public_status(self) -> dict[str, object]:
            return {
                "status": "canary_ready",
                "reason_code": "live_attestation_clear",
                "attestation_sha256": "a" * 64,
            }

    runtime = _AttestedRuntime()
    monkeypatch.setattr(server_module, "AttestedV12ModelRuntime", _AttestedRuntime)
    monkeypatch.setattr(
        server_module,
        "create_attested_v12_model_runtime",
        lambda _llm: runtime,
    )
    app = server_module.create_app(
        replace(
            settings,
            router_mode="canary",
            router_canary_routes=("file_read",),
            router_canary_user_ids=("owner",),
        )
    )

    with TestClient(app) as client:
        assert type(app.state.agent) is OrchestrationRouter
        assert tuple(app.state.agent._route_handlers) == (RouteClass.FILE_READ,)  # noqa: SLF001
        assert runtime.attest_calls == 1
        health = client.get("/health").json()["orchestration"]
        assert health["configured_mode"] == "canary"
        assert health["installed_mode"] == "canary"
        assert health["registered_routes"] == ["file_read"]
        assert health["model_gate"]["status"] == "canary_ready"


def test_server_wires_bounded_dynamic_model_gate_status_to_organs(
    settings,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    import friday.server as server_module

    captured_contexts: list[Any] = []

    class _Registry:
        def capabilities(self):
            return ()

        def routers(self):
            return ()

        def tools(self, _ctx):
            return ()

        def workers(self, ctx):
            captured_contexts.append(ctx)
            return ()

    class _AttestedRuntime:
        def __init__(self) -> None:
            self.gate = {
                "status": "canary_ready",
                "reason_code": "live_attestation_clear",
                "private_epoch": "must-not-cross-the-organ-boundary",
            }

        async def attest(self, *, absolute_deadline: float) -> object:
            assert absolute_deadline > time.monotonic()
            return object()

        def public_status(self) -> dict[str, object]:
            return dict(self.gate)

    runtime = _AttestedRuntime()
    monkeypatch.setattr(server_module, "AttestedV12ModelRuntime", _AttestedRuntime)
    monkeypatch.setattr(
        server_module,
        "create_attested_v12_model_runtime",
        lambda _llm: runtime,
    )
    monkeypatch.setattr(server_module, "build_registry", lambda _settings: _Registry())
    app = server_module.create_app(replace(settings, router_mode="canary"))

    with TestClient(app):
        assert len(captured_contexts) == 1
        provider = captured_contexts[0].model_gate_status
        assert provider is not None
        assert provider() == {
            "status": "canary_ready",
            "reason_code": "live_attestation_clear",
        }
        runtime.gate = {
            "status": "revoked",
            "reason_code": "private transport response /srv/secret",
            "private_epoch": "still-must-not-cross",
        }
        assert provider() == {"status": "revoked", "reason_code": "unknown"}
        runtime.gate = {"status": "private-status", "reason_code": "private-reason"}
        assert provider() == {
            "status": "unavailable",
            "reason_code": "observer_unavailable",
        }
        runtime.gate = {"status": "private-status", "reason_code": "epoch_invalid"}
        assert provider() == {
            "status": "unavailable",
            "reason_code": "observer_unavailable",
        }


def test_server_attestation_failure_is_observable_and_keeps_exact_legacy(
    settings,
    monkeypatch,
) -> None:
    from fastapi.testclient import TestClient

    import friday.server as server_module
    from friday.agent_runtime import AgentRuntime

    class _RejectedRuntime:
        async def attest(self, *, absolute_deadline: float) -> object:
            assert absolute_deadline > time.monotonic()
            raise RuntimeError("private transport detail")

        def public_status(self) -> dict[str, object]:
            return {
                "status": "revoked",
                "reason_code": "attestation_rejected",
                "attestation_sha256": "",
            }

    runtime = _RejectedRuntime()
    monkeypatch.setattr(server_module, "AttestedV12ModelRuntime", _RejectedRuntime)
    monkeypatch.setattr(
        server_module,
        "create_attested_v12_model_runtime",
        lambda _llm: runtime,
    )
    app = server_module.create_app(replace(settings, router_mode="canary"))

    with TestClient(app) as client:
        assert type(app.state.agent) is AgentRuntime
        health = client.get("/health").json()["orchestration"]
        assert health["configured_mode"] == "canary"
        assert health["installed_mode"] == "legacy"
        assert health["registered_routes"] == []
        assert health["model_gate"]["status"] == "revoked"
        assert "private" not in json.dumps(health)


def test_router_config_is_forwarded_by_every_operator_template() -> None:
    root = Path(__file__).resolve().parents[1]
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    cli = (root / "friday" / "cli.py").read_text(encoding="utf-8")
    expected_defaults = {
        "FRIDAY_ROUTER_MODE": "legacy",
        "FRIDAY_ROUTER_CANARY_ROUTES": "file_read",
        "FRIDAY_ROUTER_CANARY_USER_IDS": "",
        "FRIDAY_ROUTER_PLAN_TIMEOUT_SEC": "12",
    }
    for key, value in expected_defaults.items():
        assert f"{key}={value}" in env_example
        assert f"{key}={value}" in cli
        assert f"{key}: ${{{key}:-{value}}}" in compose


@pytest.mark.asyncio
async def test_shadow_plans_once_but_legacy_alone_owns_the_answer() -> None:
    legacy = _Runtime()
    planner = _Planner()
    router = OrchestrationRouter(legacy, planner, mode="shadow", allowed_routes=("file_read",))

    result = await router.chat("private-person-id", "сравни два файла", **_chat_kwargs())
    await router.drain_shadow()

    assert result == {"message": "legacy", "conversation_id": "c-1"}
    assert len(planner.calls) == 1
    assert len(legacy.calls) == 1
    assert router.observations[-1].selected_runtime == "legacy"
    assert router.observations[-1].route == "file_read"
    assert "private-person-id" not in repr(router.observations)
    assert "scan.pdf" not in repr(router.observations)
    assert router.feedback_marker is legacy.feedback_marker


@pytest.mark.asyncio
async def test_shadow_diagnostics_never_copy_model_reason_or_source_names() -> None:
    payload = _plan_payload()
    payload["reason_code"] = "secret_scan_filename"
    router = OrchestrationRouter(
        _Runtime(),
        _Planner(TurnPlan.parse(payload)),
        mode="shadow",
    )

    await router.chat("person", "сравни", **_chat_kwargs())
    await router.drain_shadow()

    diagnostic = repr(router.observations)
    assert "secret_scan_filename" not in diagnostic
    assert "scan.pdf" not in diagnostic
    assert router.observations[-1].reason_code == "planned"


@pytest.mark.asyncio
async def test_shadow_planner_failure_cannot_change_or_suppress_legacy() -> None:
    legacy = _Runtime()
    planner = _Planner(error=RuntimeError("private model body"))
    router = OrchestrationRouter(legacy, planner, mode="shadow")

    assert await router.chat("person", "привет", **_chat_kwargs()) == {
        "message": "legacy",
        "conversation_id": "c-1",
    }
    await router.drain_shadow()
    assert len(legacy.calls) == 1
    assert router.observations[-1].status == "planner_rejected"


@pytest.mark.asyncio
async def test_shadow_returns_before_blocked_planner_and_close_drains_it() -> None:
    class _BlockedPlanner(_Planner):
        started = asyncio.Event()
        cancelled = False

        async def plan(self, turn: TurnInput, *, turn_deadline: float | None = None) -> TurnPlan:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("unreachable")

    legacy = _Runtime()
    planner = _BlockedPlanner()
    router = OrchestrationRouter(legacy, planner, mode="shadow")

    result = await asyncio.wait_for(
        router.chat("person", "привет", **_chat_kwargs()),
        timeout=0.2,
    )
    await asyncio.wait_for(planner.started.wait(), timeout=0.2)
    assert result["message"] == "legacy"
    await router.close()
    assert planner.cancelled is True


@pytest.mark.asyncio
async def test_shadow_has_bounded_backpressure() -> None:
    class _BlockedPlanner(_Planner):
        async def plan(self, turn: TurnInput, *, turn_deadline: float | None = None) -> TurnPlan:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    router = OrchestrationRouter(_Runtime(), _BlockedPlanner(), mode="shadow")
    for index in range(5):
        await router.chat(f"person-{index}", "привет", **_chat_kwargs())
    assert router.observations[-1].status == "shadow_dropped_backpressure"
    await router.close()


@pytest.mark.asyncio
async def test_canary_falls_back_before_dispatch_when_route_is_not_registered() -> None:
    legacy = _Runtime()
    planner = _Planner()
    router = OrchestrationRouter(
        legacy,
        planner,
        mode="canary",
        allowed_routes=("file_read",),
        canary_user_ids=("owner",),
    )

    result = await router.chat("person", "сравни", **_chat_kwargs())

    assert result["message"] == "legacy"
    assert len(legacy.calls) == 1
    assert router.observations[-1].status == "legacy_fallback"


@pytest.mark.asyncio
async def test_canary_reserves_legacy_budget_before_calling_planner() -> None:
    legacy = _Runtime()
    planner = _Planner()
    router = OrchestrationRouter(
        legacy,
        planner,
        mode="canary",
        allowed_routes=("file_read",),
        canary_user_ids=("owner",),
        planner_timeout_sec=1,
    )
    kwargs = _chat_kwargs()
    kwargs["turn_deadline"] = time.monotonic() + 5

    result = await router.chat("person", "сравни", **kwargs)

    assert result["message"] == "legacy"
    assert planner.calls == []
    assert router.observations[-1].status == "legacy_budget_reserved"


@pytest.mark.asyncio
async def test_voice_turn_never_enters_canary_planning_or_prepare() -> None:
    legacy = _Runtime()
    planner = _Planner()
    handler = _Handler()
    router = OrchestrationRouter(
        legacy,
        planner,
        mode="canary",
        allowed_routes=("file_read",),
        canary_user_ids=("owner",),
        route_handlers={RouteClass.FILE_READ: handler},
    )
    kwargs = _chat_kwargs()
    kwargs["answer_with_voice"] = True

    result = await router.chat("person", "сравни", **kwargs)

    assert result["message"] == "legacy"
    assert planner.calls == []
    assert handler.prepared == []
    assert router.observations[-1].status == "voice_not_in_canary"


@pytest.mark.asyncio
async def test_canary_requires_an_explicit_actor_allowlist() -> None:
    legacy = _Runtime()
    planner = _Planner()
    v12 = _Handler()
    router = OrchestrationRouter(
        legacy,
        planner,
        mode="canary",
        allowed_routes=("file_read",),
        canary_user_ids=(),
        route_handlers={RouteClass.FILE_READ: v12},
    )

    result = await router.chat("person", "сравни", **_chat_kwargs())

    assert result["message"] == "legacy"
    assert len(legacy.calls) == 1
    assert v12.calls == []
    assert planner.calls == []


@pytest.mark.asyncio
async def test_canary_selects_exactly_one_registered_read_only_runtime() -> None:
    legacy = _Runtime()
    v12 = _Handler()
    router = OrchestrationRouter(
        legacy,
        _Planner(),
        mode="canary",
        allowed_routes=("file_read",),
        canary_user_ids=("owner",),
        route_handlers={RouteClass.FILE_READ: v12},
    )

    result = await router.chat("person", "сравни", **_chat_kwargs())

    assert result["message"] == "v12 [A1]"
    assert legacy.calls == []
    assert len(v12.calls) == 1
    assert v12.calls[0][2].route is RouteClass.FILE_READ
    assert v12.calls[0][0].attachments[0].name == "attachment-1"
    assert v12.calls[0][0].turn_deadline is not None
    assert router.observations[-1].selected_runtime == "v12"
    assert router.observations[-1].status == "completed"


@pytest.mark.asyncio
async def test_route_authority_loss_before_selection_falls_back_once() -> None:
    legacy = _Runtime()
    handler = _Handler(current=False)
    router = OrchestrationRouter(
        legacy,
        _Planner(),
        mode="v12",
        allowed_routes=("file_read",),
        route_handlers={RouteClass.FILE_READ: handler},
    )

    result = await router.chat("person", "сравни", **_chat_kwargs())

    assert result["message"] == "legacy"
    assert len(legacy.calls) == 1
    assert handler.calls == []
    assert router.observations[-1].status == "prepare_authority_rejected"
    assert all(item.status != "selected" for item in router.observations)


@pytest.mark.asyncio
async def test_preplanner_attachment_snapshot_cannot_be_swapped_by_the_planner() -> None:
    kwargs = _chat_kwargs()
    original = kwargs["attachments"][0]

    class _MutatingPlanner(_Planner):
        async def plan(self, turn: TurnInput, *, turn_deadline: float | None = None) -> TurnPlan:
            original["filename"] = "replacement.pdf"
            original["raw_object_id"] = "raw_fedcba9876543210"
            return await super().plan(turn, turn_deadline=turn_deadline)

    handler = _Handler()
    router = OrchestrationRouter(
        _Runtime(),
        _MutatingPlanner(),
        mode="v12",
        allowed_routes=("file_read",),
        route_handlers={RouteClass.FILE_READ: handler},
    )

    result = await router.chat("person", "сравни", **kwargs)

    assert result["message"] == "v12 [A1]"
    reference = handler.prepared[0][0].attachments[0]
    assert reference.raw_object_id == "raw_0123456789abcdef"
    assert reference.name == "attachment-1"


@pytest.mark.asyncio
async def test_current_file_admission_is_process_owned_and_fail_closed() -> None:
    mutations: list[dict[str, Any]] = []
    for field, value in (
        ("raw_object_id", "raw_fedcba9876543210"),
        ("persisted", False),
        ("current_turn_only", False),
    ):
        kwargs = _chat_kwargs()
        kwargs["attachments"][0][field] = value
        mutations.append(kwargs)
    forged = _chat_kwargs()
    forged["attachments"] = [dict(forged["attachments"][0])]
    mutations.append(forged)
    duplicate = _chat_kwargs()
    duplicate["attachments"] = [duplicate["attachments"][0], duplicate["attachments"][0]]
    mutations.append(duplicate)
    too_many = _chat_kwargs()
    too_many["attachments"] = [
        _current_attachment(f"raw_{index:016x}", filename=f"scan-{index}.pdf") for index in range(13)
    ]
    mutations.append(too_many)
    for field in (
        "quoted_attachment_reference",
        "reply_assistant_reference",
        "synthetic_document_notice",
    ):
        kwargs = _chat_kwargs()
        kwargs[field] = True
        mutations.append(kwargs)

    for kwargs in mutations:
        legacy = _Runtime()
        handler = _Handler()
        router = OrchestrationRouter(
            legacy,
            _Planner(),
            mode="v12",
            allowed_routes=("file_read",),
            route_handlers={RouteClass.FILE_READ: handler},
        )
        result = await router.chat("person", "сравни", **kwargs)
        assert result["message"] == "legacy"
        assert len(legacy.calls) == 1
        assert handler.prepared == []
        assert handler.calls == []


@pytest.mark.asyncio
async def test_prepare_must_bind_plan_before_selection_or_fall_back_once() -> None:
    class _PrepareVariant(_Handler):
        def __init__(self, variant: str) -> None:
            super().__init__()
            self.variant = variant

        async def prepare(
            self,
            request: ReadOnlyRouteRequest,
            turn: TurnInput,
            plan: TurnPlan,
        ) -> ReadOnlyRoutePreparation | None:
            self.prepared.append((request, turn, plan))
            if self.variant == "none":
                return None
            if self.variant == "raise":
                raise RuntimeError("private admission failure")
            if self.variant == "slow":
                await asyncio.sleep(10)
            return ReadOnlyRoutePreparation(
                route=RouteClass.WEB_READ if self.variant == "route" else plan.route,
                plan_sha256="2" * 64 if self.variant == "plan" else plan.canonical_sha256(),
                evidence_identity_sha256="1" * 64,
                private_payload=object(),
            )

    for variant in ("none", "raise", "slow", "route", "plan"):
        legacy = _Runtime()
        handler = _PrepareVariant(variant)
        router = OrchestrationRouter(
            legacy,
            _Planner(),
            mode="v12",
            allowed_routes=("file_read",),
            route_handlers={RouteClass.FILE_READ: handler},
            preparation_timeout_sec=0.01,
        )
        result = await router.chat("person", "сравни", **_chat_kwargs())
        assert result["message"] == "legacy"
        assert len(legacy.calls) == 1
        assert handler.calls == []
        assert all(observation.status != "selected" for observation in router.observations)


@pytest.mark.asyncio
async def test_source_result_must_match_prepared_evidence_and_citations() -> None:
    class _ResultVariant(_Handler):
        def __init__(self, variant: str) -> None:
            super().__init__()
            self.variant = variant

        async def handle(
            self,
            request: ReadOnlyRouteRequest,
            turn: TurnInput,
            plan: TurnPlan,
            preparation: ReadOnlyRoutePreparation,
        ) -> ReadOnlyRouteResult:
            result = await super().handle(request, turn, plan, preparation)
            if self.variant == "digest":
                return replace(result, evidence_identity_sha256="2" * 64)
            if self.variant == "verified":
                return replace(result, verified=False)
            if self.variant == "empty":
                return replace(result, citation_labels=())
            if self.variant == "extra":
                return replace(result, message="answer [A1], invented [A2]")
            if self.variant == "outcome_partial":
                return replace(
                    result,
                    outcome=replace(result.outcome, status=CapabilityOutcomeStatus.PARTIAL),
                )
            if self.variant == "outcome_plan":
                return replace(result, outcome=replace(result.outcome, plan_sha256="f" * 64))
            if self.variant == "outcome_evidence":
                return replace(
                    result,
                    outcome=replace(result.outcome, evidence_identity_sha256="f" * 64),
                )
            if self.variant == "outcome_citations":
                return replace(
                    result,
                    outcome=replace(result.outcome, citation_labels=("A2",)),
                )
            if self.variant == "outcome_route":
                return replace(
                    result,
                    outcome=replace(result.outcome, route=RouteClass.ARCHIVE_READ),
                )
            return replace(result, message="answer without marker")

    for variant in (
        "digest",
        "verified",
        "empty",
        "extra",
        "marker",
        "outcome_partial",
        "outcome_plan",
        "outcome_evidence",
        "outcome_citations",
        "outcome_route",
    ):
        legacy = _Runtime()
        router = OrchestrationRouter(
            legacy,
            _Planner(),
            mode="v12",
            allowed_routes=("file_read",),
            route_handlers={RouteClass.FILE_READ: _ResultVariant(variant)},
        )
        with pytest.raises(TypeError, match="invalid result"):
            await router.chat("person", "сравни", **_chat_kwargs())
        assert legacy.calls == []
        assert router.observations[-1].status == "handler_invalid_result"


def test_read_only_result_is_deeply_immutable_and_citations_are_unique() -> None:
    outcome = CapabilityOutcome(
        route=RouteClass.FILE_READ,
        status=CapabilityOutcomeStatus.COMPLETE,
        plan_sha256="2" * 64,
        evidence_identity_sha256="1" * 64,
        citation_labels=("A1",),
        authority_rechecked=True,
        verified=True,
    )
    with pytest.raises(ValueError, match="immutable tuple"):
        ReadOnlyRouteResult(
            message="answer [A1]",
            conversation_id="conv_0000000000000001",
            message_id="msg_0000000000000001",
            evidence_identity_sha256="1" * 64,
            citation_labels=["A1"],  # type: ignore[arg-type]
            verified=True,
            outcome=outcome,
        )
    with pytest.raises(ValueError, match="unique"):
        ReadOnlyRouteResult(
            message="answer [A1]",
            conversation_id="conv_0000000000000001",
            message_id="msg_0000000000000001",
            evidence_identity_sha256="1" * 64,
            citation_labels=("A1", "A1"),
            verified=True,
            outcome=outcome,
        )


@pytest.mark.asyncio
async def test_handler_declaration_and_turn_facts_are_fail_closed() -> None:
    legacy = _Runtime()
    wrong_route = _Handler(RouteClass.WEB_READ)
    wrong_effect = _Handler()
    wrong_effect.effect = ToolEffect.WRITE

    for handler in (wrong_route, wrong_effect):
        router = OrchestrationRouter(
            legacy,
            _Planner(),
            mode="v12",
            allowed_routes=("file_read",),
            route_handlers={RouteClass.FILE_READ: handler},
        )
        result = await router.chat("person", "сравни", **_chat_kwargs())
        assert result["message"] == "legacy"
        assert handler.calls == []

    missing_source_kwargs = _chat_kwargs()
    missing_source_kwargs["attachments"] = []
    missing_source_kwargs["quoted_attachment_reference"] = False
    router = OrchestrationRouter(
        legacy,
        _Planner(),
        mode="v12",
        allowed_routes=("file_read",),
        route_handlers={RouteClass.FILE_READ: _Handler()},
    )
    result = await router.chat("person", "сравни", **missing_source_kwargs)
    assert result["message"] == "legacy"

    truncated_quote_kwargs = _chat_kwargs()
    truncated_quote_kwargs["reply_to"] = "x" * 1_001
    result = await router.chat("person", "сравни", **truncated_quote_kwargs)
    assert result["message"] == "legacy"


@pytest.mark.asyncio
async def test_selected_handler_timeout_is_not_retried_through_legacy() -> None:
    legacy = _Runtime()
    handler = _Handler(delay=10)
    router = OrchestrationRouter(
        legacy,
        _Planner(),
        mode="v12",
        allowed_routes=("file_read",),
        route_handlers={RouteClass.FILE_READ: handler},
        route_timeout_sec=1,
    )

    with pytest.raises(TimeoutError):
        await router.chat("person", "сравни", **{**_chat_kwargs(), "turn_deadline": None})
    assert legacy.calls == []
    assert len(handler.calls) == 1
    assert router.observations[-1].status == "handler_timeout"


@pytest.mark.asyncio
async def test_selected_runtime_failure_is_never_retried_through_legacy() -> None:
    legacy = _Runtime()
    v12 = _Handler(error=RuntimeError("route failed after dispatch"))
    router = OrchestrationRouter(
        legacy,
        _Planner(),
        mode="v12",
        allowed_routes=("file_read",),
        route_handlers={RouteClass.FILE_READ: v12},
    )

    with pytest.raises(RuntimeError, match="after dispatch"):
        await router.chat("person", "сравни", **_chat_kwargs())
    assert legacy.calls == []
    assert len(v12.calls) == 1
    assert router.observations[-1].status == "handler_failed"


@pytest.mark.asyncio
async def test_read_only_handler_cannot_return_generated_file_effect_carriers() -> None:
    class _ForgedHandler(_Handler):
        async def handle(
            self,
            request: ReadOnlyRouteRequest,
            turn: TurnInput,
            plan: TurnPlan,
            preparation: ReadOnlyRoutePreparation,
        ) -> Any:
            return {
                "message": "text",
                "conversation_id": "conv_0000000000000001",
                "message_id": "msg_0000000000000001",
                "files": [{"filename": "forbidden.txt", "content": "effect"}],
            }

    legacy = _Runtime()
    router = OrchestrationRouter(
        legacy,
        _Planner(),
        mode="v12",
        allowed_routes=("file_read",),
        route_handlers={RouteClass.FILE_READ: _ForgedHandler()},
    )

    with pytest.raises(TypeError, match="invalid result"):
        await router.chat("person", "сравни", **_chat_kwargs())
    assert legacy.calls == []
    assert router.observations[-1].status == "handler_invalid_result"


@pytest.mark.asyncio
async def test_effect_plan_stays_legacy_even_if_an_effect_handler_is_registered() -> None:
    payload = _plan_payload(
        route="effect",
        evidence_requests=[],
        tool_intents=[
            {
                "name": "reminders.create",
                "arguments": {"at": "tomorrow"},
                "effect": "write",
                "purpose": "create reminder",
            }
        ],
    )
    legacy = _Runtime()
    effect_handler = _Handler(RouteClass.EFFECT, "unsafe")
    router = OrchestrationRouter(
        legacy,
        _Planner(TurnPlan.parse(payload)),
        mode="v12",
        allowed_routes=("effect",),
        route_handlers={RouteClass.EFFECT: effect_handler},
    )

    result = await router.chat("person", "напомни", **_chat_kwargs())

    assert result["message"] == "legacy"
    assert len(legacy.calls) == 1
    assert effect_handler.calls == []


@pytest.mark.asyncio
async def test_model_cannot_label_a_known_effect_as_read_to_enter_canary() -> None:
    mislabeled = TurnPlan.parse(
        _plan_payload(
            tool_intents=[
                {
                    "name": "reminders.create",
                    "arguments": {"at": "tomorrow"},
                    "effect": "read",
                    "purpose": "create reminder",
                }
            ]
        )
    )
    legacy = _Runtime()
    handler = _Handler(label="unsafe")
    router = OrchestrationRouter(
        legacy,
        _Planner(mislabeled),
        mode="v12",
        allowed_routes=("file_read",),
        route_handlers={RouteClass.FILE_READ: handler},
    )

    result = await router.chat("person", "сделай", **_chat_kwargs())

    assert result["message"] == "legacy"
    assert len(legacy.calls) == 1
    assert handler.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["conversation", "max_items"])
async def test_file_canary_requires_one_fully_satisfied_attachment_request(mutation: str) -> None:
    payload = _plan_payload()
    kwargs = _chat_kwargs()
    if mutation == "conversation":
        payload["evidence_requests"].append(
            {"kind": "conversation", "query": "earlier context", "max_items": 2, "required": True}
        )
    else:
        payload["evidence_requests"][0]["max_items"] = 1
        kwargs["attachments"] = [
            _current_attachment("raw_0123456789abcdef"),
            _current_attachment("raw_fedcba9876543210"),
        ]
    legacy = _Runtime()
    handler = _Handler()
    router = OrchestrationRouter(
        legacy,
        _Planner(TurnPlan.parse(payload)),
        mode="v12",
        allowed_routes=("file_read",),
        route_handlers={RouteClass.FILE_READ: handler},
    )

    result = await router.chat("person", "прочитай всё", **kwargs)

    assert result["message"] == "legacy"
    assert len(legacy.calls) == 1
    assert handler.prepared == []


def _planner_lease(requirements: ModelRequirements, authority: object) -> ModelProfileLease:
    return ModelProfileLease(
        profile_id="v12-planner-test:dispatcher",
        attestation_sha256="a" * 64,
        requirements_sha256=requirements.canonical_sha256(),
        capabilities=requirements.capabilities,
        required_context_tokens=requirements.required_context_tokens,
        prepared_evidence_items=requirements.prepared_evidence_items,
        max_tool_steps=requirements.max_tool_steps,
        effect=requirements.effect,
        verifier_required=requirements.verifier_required,
        process_epoch_sha256="b" * 64,
        _gate_authority=authority,
        _gate_generation=1,
    )


@pytest.mark.asyncio
async def test_attested_planner_never_uses_the_raw_shadow_model() -> None:
    class _RawModel:
        calls = 0

        async def chat(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            raise AssertionError("CANARY planner touched the raw model")

    class _AttestedRuntime:
        def __init__(self) -> None:
            self.calls = 0
            self.lease: ModelProfileLease | None = None

        async def acquire_lease(
            self,
            requirements: ModelRequirements,
            *,
            absolute_deadline: float,
        ) -> ModelProfileLease:
            assert absolute_deadline > time.monotonic()
            self.lease = _planner_lease(requirements, self)
            return self.lease

        async def complete(
            self,
            lease: ModelProfileLease,
            requirements: ModelRequirements,
            messages: list[dict[str, Any]],
            **kwargs: Any,
        ) -> dict[str, Any]:
            assert lease is self.lease
            assert lease.requirements_sha256 == requirements.canonical_sha256()
            assert kwargs["priority"] == "background"
            assert kwargs["max_tokens"] == 512
            assert messages
            self.calls += 1
            return {
                "content": json.dumps(_plan_payload(), ensure_ascii=False),
                "finish_reason": "stop",
                "tool_calls": [],
            }

    raw = _RawModel()
    runtime = _AttestedRuntime()
    planner = V12Planner(raw, timeout_sec=5, attested_runtime=runtime)
    turn = TurnInput.from_chat(
        message="сравни два файла",
        actor=SimpleNamespace(is_owner=True, shared_tenant=False),
        conversation_id=None,
        attachments=_chat_kwargs()["attachments"],
        enable_tools=True,
        synthetic_document_notice=False,
        mode="dialogue",
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )

    plan = await planner.plan_attested(turn)

    assert plan.route is RouteClass.FILE_READ
    assert runtime.calls == 1
    assert raw.calls == 0


@pytest.mark.asyncio
async def test_attested_planner_without_runtime_fails_before_raw_model() -> None:
    class _RawModel:
        calls = 0

        async def chat(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            raise AssertionError("unattested planner touched the raw model")

    raw = _RawModel()
    planner = V12Planner(raw, timeout_sec=5)
    turn = TurnInput.from_chat(
        message="сравни",
        actor=SimpleNamespace(is_owner=True, shared_tenant=False),
        conversation_id=None,
        attachments=_chat_kwargs()["attachments"],
        enable_tools=True,
        synthetic_document_notice=False,
        mode=None,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )

    with pytest.raises(RuntimeError, match="runtime is unavailable"):
        await planner.plan_attested(turn)
    assert raw.calls == 0


@pytest.mark.asyncio
async def test_real_planner_uses_one_bounded_schema_only_model_call() -> None:
    class _Model:
        def __init__(self) -> None:
            self.calls: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []

        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            self.calls.append((messages, kwargs))
            return {
                "content": json.dumps(_plan_payload(), ensure_ascii=False),
                "finish_reason": "stop",
                "tool_calls": None,
            }

    model = _Model()
    planner = V12Planner(model, timeout_sec=5)
    turn = TurnInput.from_chat(
        message="сравни два файла",
        actor=SimpleNamespace(is_owner=True, shared_tenant=True, own_id="do-not-send"),
        conversation_id=None,
        attachments=_chat_kwargs()["attachments"],
        enable_tools=True,
        synthetic_document_notice=False,
        mode="dialogue",
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )

    plan = await planner.plan(turn)

    assert plan.route is RouteClass.FILE_READ
    assert len(model.calls) == 1
    messages, kwargs = model.calls[0]
    assert kwargs["priority"] == "background"
    assert kwargs["tools"] is None
    assert kwargs["allow_retries"] is False
    assert kwargs["max_tokens"] == 512
    assert kwargs["require_full_context"] is True
    serialized = json.dumps(messages, ensure_ascii=False)
    assert "do-not-send" not in serialized
    assert "/private/customer" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"finish_reason": "length", "tool_calls": None},
        {"finish_reason": "stop", "tool_calls": [{"name": "forbidden"}]},
    ],
)
async def test_real_planner_rejects_incomplete_or_effectful_protocol_response(
    mutation: dict[str, Any],
) -> None:
    class _Model:
        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            del messages, kwargs
            return {
                "content": json.dumps(_plan_payload(), ensure_ascii=False),
                **mutation,
            }

    planner = V12Planner(_Model(), timeout_sec=5)
    turn = TurnInput.from_chat(
        message="сравни",
        actor=SimpleNamespace(is_owner=True, shared_tenant=False),
        conversation_id=None,
        attachments=_chat_kwargs()["attachments"],
        enable_tools=True,
        synthetic_document_notice=False,
        mode=None,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )

    with pytest.raises(ValueError, match="incomplete or effectful"):
        await planner.plan(turn)


@pytest.mark.asyncio
async def test_real_planner_rejects_unattested_context_before_the_model_call() -> None:
    class _Model:
        calls = 0

        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            del messages, kwargs
            self.calls += 1
            raise AssertionError("oversized planner input reached the model")

    model = _Model()
    planner = V12Planner(model, timeout_sec=5)
    turn = TurnInput.from_chat(
        message="Ж" * 10_000,
        actor=SimpleNamespace(is_owner=True, shared_tenant=False),
        conversation_id=None,
        attachments=(),
        enable_tools=True,
        synthetic_document_notice=False,
        mode=None,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )

    with pytest.raises(ValueError, match="attested context"):
        await planner.plan(turn)
    assert model.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["message", "reply_quote"])
async def test_real_planner_never_projects_a_current_runtime_secret(
    monkeypatch,
    location: str,
) -> None:
    secret = "sk-friday-v12-planner-secret-1234567890"
    monkeypatch.setenv("FRIDAY_API_TOKEN", secret)

    class _Model:
        calls = 0

        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            del messages, kwargs
            self.calls += 1
            raise AssertionError("secret-bearing planner input reached the model")

    model = _Model()
    planner = V12Planner(model, timeout_sec=5)
    turn = TurnInput.from_chat(
        message=secret if location == "message" else "проверь ответ",
        actor=SimpleNamespace(is_owner=True, shared_tenant=False),
        conversation_id=None,
        attachments=(),
        enable_tools=True,
        synthetic_document_notice=False,
        mode=None,
        reply_to=secret if location == "reply_quote" else None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )

    with pytest.raises(ValueError, match="secret projection"):
        await planner.plan(turn)
    assert model.calls == 0


@pytest.mark.asyncio
async def test_planner_enforces_its_own_deadline_when_model_ignores_the_hint() -> None:
    class _IgnoringModel:
        cancelled = False

        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return {"content": json.dumps(_plan_payload())}

    model = _IgnoringModel()
    planner = V12Planner(model, timeout_sec=1)
    turn = TurnInput.from_chat(
        message="сравни",
        actor=SimpleNamespace(is_owner=True, shared_tenant=False),
        conversation_id=None,
        attachments=_chat_kwargs()["attachments"],
        enable_tools=True,
        synthetic_document_notice=False,
        mode=None,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )

    with pytest.raises(TimeoutError):
        await planner.plan(turn)
    assert model.cancelled is True
