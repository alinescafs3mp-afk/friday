"""Closed exact-host outcome, continuation and hidden-profile contracts."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from friday.agent_runtime import (
    _ENGINEER_HOST_OUTCOME_METADATA_KEY,
    _ENGINEER_MODEL_TOOL_NAMES,
    AgentRuntime,
    _accepted_engineer_host_receipt,
    _engineer_host_owned_outcome,
    _engineer_host_private_digest,
    _EngineerHostOutcome,
    _render_engineer_host_outcome,
    _render_engineer_nmap_capability_truth,
)
from friday.execution_kernel import ToolResult
from friday.interaction_control_plane.legacy_trace import CapabilityStatus
from friday.interaction_control_plane.runtime_trace import load_trace_namespace_key
from friday.organs.engineer import authority, hosts, local_binaries
from friday.organs.engineer.targets import PinnedTarget, target_source_sha256
from friday.organs.engineer.tools import build_engineer_tools
from friday.permissions import ActorContext


def _actor() -> ActorContext:
    return ActorContext("alice", "owner", "test")


def _host_outcome(
    *,
    source_user_message_id: str,
    request: str,
    profile: str = "services",
    namespace_key: bytes = b"test-engineer-host-binding-key-32",
    conversation_id: str = "conv_test",
) -> _EngineerHostOutcome:
    token = "192.168.1.120"
    return _EngineerHostOutcome(
        status=CapabilityStatus.SUCCEEDED,
        profile=profile,  # type: ignore[arg-type]
        target=token,
        address=token,
        open_ports=(139, 2376, 5000),
        services=((139, "unknown", None), (2376, "unknown", 7), (5000, "web", 8)),
        findings=(
            ("file_sharing_port_reachable", 139),
            ("container_control_port_reachable", 2376),
            ("alternate_web_port_reachable", 5000),
        )
        if profile == "vulnerabilities"
        else (),
        active_probes_status="sent",
        evidence_sha256="a" * 64,
        reason_code="none",
        tool_started=True,
        source_user_message_id=source_user_message_id,
        request_binding_sha256=_engineer_host_private_digest(
            namespace_key,
            "request",
            "alice",
            "alice",
            conversation_id,
            source_user_message_id,
            request,
            token,
        ),
        target_identity_sha256=_engineer_host_private_digest(
            namespace_key,
            "target",
            "alice",
            "alice",
            token,
            token,
        ),
        tenant_sha256=_engineer_host_private_digest(namespace_key, "tenant", "alice"),
        person_sha256=_engineer_host_private_digest(namespace_key, "person", "alice"),
    )


def _generic_receipt() -> dict[str, Any]:
    return {
        "schema": "friday.engineer-receipt.v1",
        "dossier_sha256": "b" * 64,
        "target_count": 1,
        "artifact_count": 0,
        "active_probes_sent": True,
        "active_probes_status": "sent",
        "active_probes_uncertain": False,
        "exploit_payloads_sent": False,
        "sandboxed": False,
        "tool_versions": {},
    }


def _store_accepted_scan(storage, *, capability_gap: bool = False):  # noqa: ANN001, ANN202
    actor = _actor()
    storage.ensure_user(actor.own_id, preset_key="owner")
    conversation = storage.create_conversation(actor.user_id, mode="engineer")
    conversation_id = str(conversation["id"])
    scan_request = "просканируй хост 192.168.1.120"
    source = storage.store_message(conversation_id, actor.user_id, "user", scan_request)
    outcome = _host_outcome(
        source_user_message_id=str(source["id"]),
        request=scan_request,
        namespace_key=load_trace_namespace_key(storage.conn),
        conversation_id=conversation_id,
    )
    assistant = storage.store_message(
        conversation_id,
        actor.user_id,
        "assistant",
        "structural scan result",
        metadata={
            "engineer_receipt": _generic_receipt(),
            _ENGINEER_HOST_OUTCOME_METADATA_KEY: outcome.receipt(),
        },
        reply_to=str(source["id"]),
    )
    parent_id = str(assistant["id"])
    if capability_gap:
        capability_user = storage.store_message(
            conversation_id,
            actor.user_id,
            "user",
            "у тебя должен быть доступ к nmap",
            reply_to=parent_id,
        )
        capability_assistant = storage.store_message(
            conversation_id,
            actor.user_id,
            "assistant",
            "closed capability truth",
            metadata={},
            reply_to=str(capability_user["id"]),
        )
        parent_id = str(capability_assistant["id"])
    current = storage.store_message(
        conversation_id,
        actor.user_id,
        "user",
        "какие у него есть уязвимости?",
        reply_to=parent_id,
    )
    return actor, conversation_id, source, assistant, current


def test_host_outcome_receipt_is_content_free_and_renderer_ignores_remote_strings() -> None:
    request = "проверь 192.168.1.120 на уязвимости"
    outcome = _host_outcome(
        source_user_message_id="msg_0123456789abcdef",
        request=request,
        profile="vulnerabilities",
    )

    rendered = _render_engineer_host_outcome(outcome)
    receipt = outcome.receipt()

    assert "139" in rendered and "2376" in rendered and "5000" in rendered
    assert "CVE-проверка не выполнялась" in rendered
    assert "подтверждённых CVE-утверждений нет" in rendered
    assert "фактический сервис и его конфигурация не подтверждены" in rendered
    assert "порт управления контейнерами" not in rendered
    assert "порт файлового обмена" not in rendered
    serialized = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    assert "192.168.1.120" not in serialized
    assert "139" not in serialized
    plain_target_digest = hashlib.sha256(
        json.dumps(
            {"address": "192.168.1.120", "host": "192.168.1.120"},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    assert receipt["outcome"]["target_identity_sha256"] != plain_target_digest  # type: ignore[index]
    assert receipt["outcome"]["open_port_count"] == 3  # type: ignore[index]
    assert _accepted_engineer_host_receipt(receipt) is None, "vulnerability receipts cannot continue"


def test_partial_vulnerability_renderer_does_not_claim_service_phase_completed() -> None:
    complete = _host_outcome(
        source_user_message_id="msg_0123456789abcdef",
        request="проверь 192.168.1.120 на уязвимости",
        profile="vulnerabilities",
    )
    partial = replace(
        complete,
        status=CapabilityStatus.PARTIAL,
        reason_code="scan_unavailable",
    )

    rendered = _render_engineer_host_outcome(partial)

    assert "bounded TCP discovery" in rendered
    assert "полное покрытие light service/version не подтверждено" in rendered
    assert "Проверены фиксированные TCP-порты и закрытый" not in rendered


def test_zero_open_port_renderer_does_not_claim_nmap_service_phase_ran() -> None:
    outcome = replace(
        _host_outcome(
            source_user_message_id="msg_0123456789abcdef",
            request="проверь 192.168.1.120 на уязвимости",
            profile="vulnerabilities",
        ),
        open_ports=(),
        services=(),
        findings=(),
        evidence_sha256="",
    )

    rendered = _render_engineer_host_outcome(outcome)

    assert "открытых портов для запуска light service/version profile не обнаружено" in rendered
    assert "Проверены фиксированные TCP-порты и закрытый" not in rendered


def test_host_outcome_rejects_noncanonical_or_type_confused_rows() -> None:
    outcome = _host_outcome(
        source_user_message_id="msg_0123456789abcdef",
        request="проверь 192.168.1.120 на уязвимости",
        profile="vulnerabilities",
    )

    with pytest.raises(ValueError, match="services are invalid"):
        replace(outcome, services=tuple(reversed(outcome.services)))
    with pytest.raises(ValueError, match="findings are invalid"):
        replace(outcome, findings=tuple(reversed(outcome.findings)))
    with pytest.raises(ValueError, match="findings are invalid"):
        replace(
            outcome,
            open_ports=(1,),
            services=(),
            findings=(("administration_port_reachable", True),),
        )


def test_owned_projection_rejects_multi_address_target_without_crashing() -> None:
    dossier = {
        "_named_host_action_requested": True,
        "_named_host_profile": "services",
        "_named_host_source_user_message_id": "msg_0123456789abcdef",
        "_named_host_request_sha256": "a" * 64,
        "_named_host_target_identity_sha256": "e" * 64,
        "_named_host_tenant_sha256": "b" * 64,
        "_named_host_person_sha256": "c" * 64,
        "target_error": "exact_single_host_required",
        "targets": [
            {
                "host": "multi.example",
                "addresses": ["192.168.1.120", "192.168.1.121"],
                "source_sha256": "d" * 64,
            }
        ],
    }

    outcome = _engineer_host_owned_outcome(dossier)

    assert outcome is not None
    assert outcome.status is CapabilityStatus.DENIED
    assert outcome.target == ""
    assert outcome.address == ""


@pytest.mark.parametrize("capability_gap", (False, True))
def test_continuation_restores_exact_receipt_across_only_capability_truth(
    settings,
    storage,
    capability_gap: bool,
) -> None:
    actor, conversation_id, source, assistant, current = _store_accepted_scan(
        storage,
        capability_gap=capability_gap,
    )
    runtime = AgentRuntime(replace(settings, engineer_mode_enabled=True), storage)

    with storage.transaction() as conn:
        continuation = runtime._engineer_host_continuation(  # noqa: SLF001
            conn,
            actor=actor,
            conversation_id=conversation_id,
            current_user_message_id=str(current["id"]),
        )

    assert continuation is not None
    assert continuation.target == PinnedTarget(
        host="192.168.1.120",
        addresses=("192.168.1.120",),
        implied_port=None,
        source_token="192.168.1.120",
        source_sha256=target_source_sha256(str(source["content"]), "192.168.1.120"),
    )
    assert continuation.source_assistant_message_id == assistant["id"]


def test_continuation_fails_closed_after_displacing_user_turn(settings, storage) -> None:
    actor, conversation_id, _source, _assistant, current = _store_accepted_scan(storage)
    displacement = storage.store_message(
        conversation_id,
        actor.user_id,
        "assistant",
        "displacing answer",
        metadata={},
        reply_to=str(current["id"]),
    )
    next_user = storage.store_message(
        conversation_id,
        actor.user_id,
        "user",
        "какие у него есть уязвимости?",
        reply_to=str(displacement["id"]),
    )
    runtime = AgentRuntime(replace(settings, engineer_mode_enabled=True), storage)

    with storage.transaction() as conn:
        continuation = runtime._engineer_host_continuation(  # noqa: SLF001
            conn,
            actor=actor,
            conversation_id=conversation_id,
            current_user_message_id=str(next_user["id"]),
        )

    assert continuation is None


@pytest.mark.asyncio
@pytest.mark.parametrize("capability_gap", (False, True))
async def test_live_receipt_followup_executes_hidden_profile_on_restored_exact_target(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
    capability_gap: bool,
) -> None:
    actor, conversation_id, _source, assistant, current = _store_accepted_scan(
        storage,
        capability_gap=capability_gap,
    )
    runtime = AgentRuntime(
        replace(
            settings,
            engineer_mode_enabled=True,
            host_allowed_cidrs=("192.168.1.0/24",),
        ),
        storage,
    )
    executed: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current_actor, _cap: current_actor)
    monkeypatch.setattr(local_binaries, "inventory", lambda: {"nmap": "/usr/bin/nmap"})

    async def execute(name, arguments, *, actor):  # noqa: ANN001
        executed.append((name, dict(arguments)))
        return ToolResult(
            name,
            True,
            data={
                "ok": True,
                "assessment_status": "complete",
                "profile": "vulnerabilities",
                "service_profile": "tcp_connect_then_nmap_selected_ports_version_light",
                "ports_checked": 64,
                "open_ports": [139, 2376, 5000],
                "services": [
                    {"port": 139, "service_class": "unknown", "confidence": None},
                    {"port": 2376, "service_class": "unknown", "confidence": 7},
                    {"port": 5000, "service_class": "web", "confidence": 8},
                ],
                "findings": [
                    {"code": "file_sharing_port_reachable", "port": 139},
                    {"code": "container_control_port_reachable", "port": 2376},
                    {"code": "alternate_web_port_reachable", "port": 5000},
                ],
                "nmap": {
                    "ok": True,
                    "used": True,
                    "parser_status": "complete",
                    "coverage": {
                        "grade": "complete",
                        "requested": 1,
                        "accounted": 1,
                        "skipped": 0,
                    },
                    "evidence": [{"sha256": "a" * 64}],
                },
                "active_probes_sent": True,
                "active_probes": ["bounded_tcp_connect", "nmap_selected_ports_version_light"],
                "exploit_payloads_sent": False,
                "cve_assessment_performed": False,
                "verified_vulnerability_claims": False,
            },
            handler_entered=True,
            work_started=True,
        )

    monkeypatch.setattr(runtime.kernel, "execute", execute)
    dossier = await runtime._engineer_autohunt(  # noqa: SLF001
        str(current["content"]),
        [],
        actor=actor,
        turn_deadline=time.monotonic() + 10,
        enable_tools=True,
        conversation_id=conversation_id,
        source_user_message_id=str(current["id"]),
    )

    assert [name for name, _arguments in executed] == ["engineer_assess_host_vulnerabilities"]
    assert executed[0][1]["host"] == "192.168.1.120"
    verified = authority.verify_target_ticket(
        str(executed[0][1]["target_ticket"]),
        actor_id=actor.own_id,
        exact_host="192.168.1.120",
    )
    assert verified.target.addresses == ("192.168.1.120",)
    outcome = _engineer_host_owned_outcome(dossier)
    assert outcome is not None
    assert outcome.status is CapabilityStatus.SUCCEEDED
    assert outcome.continuation_source_assistant_id == assistant["id"]
    assert "CVE-проверка не выполнялась" in _render_engineer_host_outcome(outcome)


def test_capability_truth_uses_attested_registry_projection() -> None:
    available = _render_engineer_nmap_capability_truth({"environment": {"tools": {"nmap": True}}})
    unavailable = _render_engineer_nmap_capability_truth({"environment": {"tools": {"nmap": False}}})

    assert "Проверенный nmap доступен" in available
    assert "закрытые code-owned профили" in available
    assert "Произвольный shell" in available
    assert "не прошёл attestation или недоступен" in unavailable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    (
        "какие у него есть уязвимости?",
        "проверь сервер на уязвимости",
        "проверь 192.168.1.120 и 192.168.1.121 на уязвимости",
        "какие уязвимости у 192.168.1.120 и 192.168.1.121?",
    ),
)
async def test_unbound_or_ambiguous_vulnerability_request_is_terminal_before_tool(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    actor = _actor()
    storage.ensure_user(actor.own_id, preset_key="owner")
    runtime = AgentRuntime(
        replace(
            settings,
            engineer_mode_enabled=True,
            host_allowed_cidrs=("192.168.1.0/24",),
        ),
        storage,
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current_actor, _cap: current_actor)
    monkeypatch.setattr(local_binaries, "inventory", lambda: {"nmap": "/usr/bin/nmap"})
    monkeypatch.setattr(
        runtime.kernel,
        "execute",
        lambda *_args, **_kwargs: pytest.fail("unbound target reached a tool"),
    )

    dossier = await runtime._engineer_autohunt(  # noqa: SLF001
        message,
        [],
        actor=actor,
        turn_deadline=time.monotonic() + 10,
        enable_tools=True,
        source_user_message_id="msg_0123456789abcdef",
    )
    outcome = _engineer_host_owned_outcome(dossier)

    assert outcome is not None
    assert outcome.status is CapabilityStatus.DENIED
    assert outcome.tool_started is False


@pytest.mark.asyncio
async def test_multi_address_hostname_is_denied_before_hidden_profile(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _actor()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, host_allowed_cidrs=("192.168.1.0/24",)),
        storage,
    )
    target = PinnedTarget(
        host="multi.example",
        addresses=("192.168.1.120", "192.168.1.121"),
        implied_port=None,
        source_token="multi.example",
        source_sha256=target_source_sha256(
            "проверь multi.example на уязвимости",
            "multi.example",
        ),
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current_actor, _cap: current_actor)
    monkeypatch.setattr(local_binaries, "inventory", lambda: {"nmap": "/usr/bin/nmap"})
    monkeypatch.setattr(hosts, "pin_target_from_speech", lambda *_args, **_kwargs: target)
    monkeypatch.setattr(
        runtime.kernel,
        "execute",
        lambda *_args, **_kwargs: pytest.fail("multi-address target reached a tool"),
    )

    dossier = await runtime._engineer_autohunt(  # noqa: SLF001
        "проверь multi.example на уязвимости",
        [],
        actor=actor,
        turn_deadline=time.monotonic() + 10,
        enable_tools=True,
        source_user_message_id="msg_0123456789abcdef",
    )
    outcome = _engineer_host_owned_outcome(dossier)

    assert dossier["target_error"] == "exact_single_host_required"
    assert outcome is not None and outcome.status is CapabilityStatus.DENIED


@pytest.mark.asyncio
async def test_direct_vulnerability_request_executes_only_hidden_profile(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _actor()
    storage.ensure_user(actor.own_id, preset_key="owner")
    runtime = AgentRuntime(
        replace(
            settings,
            engineer_mode_enabled=True,
            host_allowed_cidrs=("192.168.1.0/24",),
        ),
        storage,
    )
    executed: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)
    monkeypatch.setattr(local_binaries, "inventory", lambda: {"nmap": "/usr/bin/nmap"})

    async def execute(name, arguments, *, actor):  # noqa: ANN001
        del actor
        executed.append((name, dict(arguments)))
        return ToolResult(
            name,
            True,
            data={
                "ok": True,
                "assessment_status": "complete",
                "profile": "vulnerabilities",
                "service_profile": "tcp_connect_then_nmap_selected_ports_version_light",
                "ports_checked": 64,
                "open_ports": [139, 2376, 5000],
                "services": [
                    {"port": 139, "protocol": "tcp", "service_class": "unknown", "confidence": None},
                    {"port": 2376, "protocol": "tcp", "service_class": "unknown", "confidence": 7},
                    {"port": 5000, "protocol": "tcp", "service_class": "web", "confidence": 8},
                ],
                "findings": [
                    {"code": "file_sharing_port_reachable", "port": 139},
                    {"code": "container_control_port_reachable", "port": 2376},
                    {"code": "alternate_web_port_reachable", "port": 5000},
                ],
                "nmap": {
                    "ok": True,
                    "used": True,
                    "parser_status": "complete",
                    "coverage": {
                        "grade": "complete",
                        "requested": 1,
                        "accounted": 1,
                        "skipped": 0,
                    },
                    "evidence": [{"sha256": "a" * 64}],
                },
                "active_probes_sent": True,
                "active_probes": ["bounded_tcp_connect", "nmap_selected_ports_version_light"],
                "exploit_payloads_sent": False,
                "cve_assessment_performed": False,
                "verified_vulnerability_claims": False,
            },
            handler_entered=True,
            work_started=True,
        )

    monkeypatch.setattr(runtime.kernel, "execute", execute)
    message = "проверь 192.168.1.120 на уязвимости"
    dossier = await runtime._engineer_autohunt(  # noqa: SLF001
        message,
        [],
        actor=actor,
        turn_deadline=time.monotonic() + 10,
        enable_tools=True,
        source_user_message_id="msg_0123456789abcdef",
    )

    assert [name for name, _arguments in executed] == ["engineer_assess_host_vulnerabilities"]
    assert set(executed[0][1]) == {"host", "target_ticket"}
    outcome = _engineer_host_owned_outcome(dossier)
    assert outcome is not None
    assert outcome.status is CapabilityStatus.SUCCEEDED
    assert outcome.profile == "vulnerabilities"
    assert outcome.open_ports == (139, 2376, 5000)


def test_hidden_vulnerability_tool_is_not_model_visible(settings, storage) -> None:
    tools = build_engineer_tools(SimpleNamespace(settings=settings, storage=storage, auth=None))
    registered = next(
        (item for item in tools if item.name == "engineer_assess_host_vulnerabilities"),
        None,
    )

    assert registered is not None
    assert registered.model_visible is False
    assert registered.security_id == "engineer.host.audit"
    assert "engineer_assess_host_vulnerabilities" not in _ENGINEER_MODEL_TOOL_NAMES


def test_publication_reauth_requires_owner_use_audit_and_exact_private_policy(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _actor()
    storage.ensure_user(actor.own_id, preset_key="owner")
    conversation = storage.create_conversation(actor.user_id, mode="engineer")
    conversation_id = str(conversation["id"])
    request = "проверь 192.168.1.120 на уязвимости"
    current = storage.store_message(conversation_id, actor.user_id, "user", request)
    outcome = _host_outcome(
        source_user_message_id=str(current["id"]),
        request=request,
        profile="vulnerabilities",
        namespace_key=load_trace_namespace_key(storage.conn),
        conversation_id=conversation_id,
    )
    dossier = {
        "_named_host_action_requested": True,
        "_named_host_profile": "vulnerabilities",
        "_named_host_tool_started": True,
        "_named_host_source_user_message_id": str(current["id"]),
        "_named_host_tenant_sha256": outcome.tenant_sha256,
        "_named_host_person_sha256": outcome.person_sha256,
        "_named_host_request_sha256": outcome.request_binding_sha256,
        "_named_host_target_identity_sha256": outcome.target_identity_sha256,
        "targets": [
            {
                "host": "192.168.1.120",
                "addresses": ["192.168.1.120"],
                "implied_port": None,
                "source_sha256": target_source_sha256(request, "192.168.1.120"),
            }
        ],
        "host_vulnerability_assessment": {
            "ok": True,
            "assessment_status": "complete",
            "profile": "vulnerabilities",
            "service_profile": "tcp_connect_then_nmap_selected_ports_version_light",
            "ports_checked": 64,
            "open_ports": [139, 2376, 5000],
            "services": [
                {"port": 139, "service_class": "unknown", "confidence": None},
                {"port": 2376, "service_class": "unknown", "confidence": 7},
                {"port": 5000, "service_class": "web", "confidence": 8},
            ],
            "findings": [
                {"code": "file_sharing_port_reachable", "port": 139},
                {"code": "container_control_port_reachable", "port": 2376},
                {"code": "alternate_web_port_reachable", "port": 5000},
            ],
            "nmap": {
                "ok": True,
                "used": True,
                "parser_status": "complete",
                "coverage": {
                    "grade": "complete",
                    "requested": 1,
                    "accounted": 1,
                    "skipped": 0,
                },
                "evidence": [{"sha256": "a" * 64}],
            },
            "active_probes_sent": True,
            "exploit_payloads_sent": False,
            "cve_assessment_performed": False,
            "verified_vulnerability_claims": False,
        },
    }
    runtime = AgentRuntime(
        replace(
            settings,
            engineer_mode_enabled=True,
            host_allowed_cidrs=("192.168.1.0/24",),
        ),
        storage,
    )
    monkeypatch.setattr(
        runtime.kernel,
        "get_tool",
        lambda name: SimpleNamespace(
            name=name,
            security_id="engineer.host.audit",
            risk="observe",
            model_visible=False,
        ),
    )
    monkeypatch.setattr(
        runtime.kernel,
        "authorization",
        SimpleNamespace(authorize=lambda *_args, **_kwargs: SimpleNamespace(allowed=True)),
    )

    with storage.transaction() as conn:
        allowed = runtime._engineer_host_outcome_publication_authorized(  # noqa: SLF001
            conn,
            actor=actor,
            conversation_id=conversation_id,
            current_user_message_id=str(current["id"]),
            request=request,
            dossier=dossier,
            outcome=outcome,
        )

    assert allowed is True
    storage.execute("UPDATE users SET status='suspended' WHERE id=?", (actor.own_id,))
    storage.conn.commit()
    with storage.transaction() as conn:
        denied = runtime._engineer_host_outcome_publication_authorized(  # noqa: SLF001
            conn,
            actor=actor,
            conversation_id=conversation_id,
            current_user_message_id=str(current["id"]),
            request=request,
            dossier=dossier,
            outcome=outcome,
        )
    assert denied is False
