from __future__ import annotations

import dataclasses

import pytest

from friday.host_control.adapters.jq import JQ_SPEC, JqAdapter
from friday.host_control.contracts import (
    PROTOCOL_VERSION,
    ContractError,
    ExecutableAttestation,
    RequestEnvelope,
    WireRequest,
    body_sha256,
    canonical_json_bytes,
)
from friday.host_control.plans import HostActionPlan, WorkspaceGrant, assert_plan_current, create_action_plan


def attestation(adapter_id: str = "data.jq", path: str = "/usr/bin/jq") -> ExecutableAttestation:
    return ExecutableAttestation(
        schema_version=1,
        canonical_path=path,
        device=8,
        inode=42,
        mode=0o755,
        owner_uid=0,
        owner_gid=0,
        size_bytes=1234,
        mtime_ns=100,
        sha256="a" * 64,
        package_name=path.rsplit("/", 1)[-1],
        package_version="1.7.1-3ubuntu0.24.04.1",
        architecture="amd64",
        observed_version="jq-1.7.1",
        adapter_id=adapter_id,
        adapter_schema_version=1,
        implementation_version=1,
    )


def jq_plan() -> HostActionPlan:
    observed = attestation()
    grant = WorkspaceGrant(
        grant_id="grant_0123456789abcdef",
        actor_own_id="owner",
        access="read",
        relative_path="input/document.json",
        identity_sha256="b" * 64,
    )
    normalized = JqAdapter().normalize_arguments(
        "extract_fields",
        {"input_grant": grant.grant_id, "fields": ["person.name"], "compact": True},
    )
    return create_action_plan(
        plan_id="plan_0123456789abcdef",
        actor_user_id="owner",
        actor_own_id="owner",
        conversation_id="conv_0123456789abcdef",
        source_message_id="msg_0123456789abcdef",
        host_agent_id="agent_0123456789abcdef",
        idempotency_key="idem_0123456789abcdef",
        adapter=JQ_SPEC,
        action=JQ_SPEC.action("extract_fields"),
        normalized_arguments=normalized,
        executable_attestation=observed,
        workspace_grants=(grant,),
        now=1_700_000_000,
    )


def test_canonical_wire_contract_is_utf8_stable_and_body_bound() -> None:
    body = {"z": 2, "a": "привет"}
    assert canonical_json_bytes(body) == '{"a":"привет","z":2}'.encode()
    envelope = RequestEnvelope(
        protocol_version=PROTOCOL_VERSION,
        request_id="request:1",
        agent_id="agent:1",
        sequence=7,
        issued_at=100,
        expires_at=130,
        method="RunAction",
        job_id="job:1",
        actor_id="owner",
        own_id="owner",
        idempotency_key="idem:1",
        plan_digest="c" * 64,
        approval_receipt_id=None,
        body_sha256=body_sha256(body),
        signature="d" * 64,
    )
    request = WireRequest.create(envelope, body)
    assert WireRequest.decode(request.encode()) == request
    tampered = request.encode().replace("привет".encode(), "пока".encode())
    with pytest.raises(ContractError, match="body digest mismatch"):
        WireRequest.decode(tampered)


def test_unknown_wire_protocol_and_unknown_fields_are_rejected() -> None:
    payload = {
        "protocol_version": "2.0",
        "request_id": "request:1",
        "agent_id": "agent:1",
        "sequence": 1,
        "issued_at": 1,
        "expires_at": 2,
        "method": "RunAction",
        "job_id": "job:1",
        "actor_id": "owner",
        "own_id": "owner",
        "idempotency_key": "idem:1",
        "plan_digest": "a" * 64,
        "approval_receipt_id": None,
        "body_sha256": "b" * 64,
        "signature": "c" * 64,
    }
    with pytest.raises(ContractError, match="unsupported"):
        RequestEnvelope.from_payload(payload)
    payload["extra"] = True
    with pytest.raises(ContractError, match="fields"):
        RequestEnvelope.from_payload(payload)


def test_executable_attestation_is_exact_immutable_round_trip() -> None:
    observed = attestation()
    assert ExecutableAttestation.from_payload(observed.to_payload()) == observed
    assert ExecutableAttestation.from_payload(observed.to_payload()).digest == observed.digest
    with pytest.raises(dataclasses.FrozenInstanceError):
        observed.inode = 99  # type: ignore[misc]
    changed = {**observed.to_payload(), "mode": 0o775}
    assert ExecutableAttestation.from_payload(changed).digest != observed.digest
    with pytest.raises(ContractError, match="canonical"):
        ExecutableAttestation.from_payload({**observed.to_payload(), "canonical_path": "/usr/../bin/jq"})


def test_host_action_plan_has_exact_inverse_and_one_canonical_digest() -> None:
    plan = jq_plan()
    restored = HostActionPlan.from_dict(plan.to_dict())
    assert restored == plan
    assert restored.digest == plan.digest
    assert restored.workspace_grants[0].relative_path == "input/document.json"
    assert restored.source_message_id == "msg_0123456789abcdef"
    changed_payload = plan.to_dict()
    changed_payload["normalized_arguments"] = {
        **changed_payload["normalized_arguments"],
        "compact": False,
    }
    assert HostActionPlan.from_dict(changed_payload).digest != plan.digest


def test_approval_and_executable_drift_fail_closed() -> None:
    plan = jq_plan()
    observed = attestation()
    assert_plan_current(
        plan,
        adapter=JQ_SPEC,
        executable_attestation=observed,
        target_snapshot=None,
        approved_plan_digest=plan.digest,
        now=plan.created_at + 1,
    )
    drifted = ExecutableAttestation.from_payload({**observed.to_payload(), "inode": 43})
    with pytest.raises(ContractError, match="executable changed"):
        assert_plan_current(
            plan,
            adapter=JQ_SPEC,
            executable_attestation=drifted,
            target_snapshot=None,
            approved_plan_digest=plan.digest,
            now=plan.created_at + 1,
        )
    with pytest.raises(ContractError, match="different"):
        assert_plan_current(
            plan,
            adapter=JQ_SPEC,
            executable_attestation=observed,
            target_snapshot=None,
            approved_plan_digest="f" * 64,
            now=plan.created_at + 1,
        )


def test_workspace_grants_reject_traversal_and_unbound_existing_files() -> None:
    with pytest.raises(ContractError):
        WorkspaceGrant("grant_0123456789abcdef", "owner", "read", "input/..", "a" * 64)
    with pytest.raises(ContractError, match="lacks identity"):
        WorkspaceGrant("grant_0123456789abcdef", "owner", "read", "input/data.json")
