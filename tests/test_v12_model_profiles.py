from __future__ import annotations

import inspect
from dataclasses import replace
from typing import Any

import pytest

import friday.model_profiles as model_profiles_module
from friday.model_profiles import (
    QWEN36_27B_V12_PROFILE,
    V12_MODEL_PROFILES,
    ModelCapability,
    ModelEffect,
    ModelGateReason,
    ModelProfileLease,
    ModelRequirements,
    V12LiveAttestation,
    V12ModelGate,
    v12_model_profile_for,
)

_ENDPOINT_BINDING = "a" * 64
_PROCESS_EPOCH_SHA256 = "c" * 64


def _attestation(**changes: Any) -> V12LiveAttestation:
    profile = QWEN36_27B_V12_PROFILE
    value = V12LiveAttestation(
        profile_id=profile.profile_id,
        planner_contract_sha256=profile.planner_contract_sha256,
        probe_suite_sha256=profile.probe_suite_sha256,
        endpoint_binding_sha256=_ENDPOINT_BINDING,
        process_epoch_sha256=_PROCESS_EPOCH_SHA256,
        capabilities=profile.required_capabilities,
        verified_context_tokens=profile.minimum_context_tokens,
        max_prepared_evidence_items=profile.max_prepared_evidence_items,
        max_tool_steps=profile.max_tool_steps,
        allowed_effects=profile.allowed_effects,
        verifier_required=True,
    )
    return replace(value, **changes)


def _requirements(**changes: Any) -> ModelRequirements:
    value = ModelRequirements(
        capabilities=frozenset(
            {
                ModelCapability.TURN_PLAN_V1,
                ModelCapability.RU_PLANNING,
                ModelCapability.PREPARED_EVIDENCE_2,
                ModelCapability.CONTEXT_8K,
                ModelCapability.REMOTE_CANCELLATION,
            }
        ),
        required_context_tokens=8192,
        prepared_evidence_items=2,
        max_tool_steps=0,
        effect=ModelEffect.READ,
        verifier_required=True,
    )
    return replace(value, **changes)


def _ready_gate() -> V12ModelGate:
    gate = V12ModelGate(QWEN36_27B_V12_PROFILE, endpoint_binding_sha256=_ENDPOINT_BINDING)
    assert gate.install_live(_attestation()) is True
    return gate


def test_current_profile_is_exact_code_owned_read_only_candidate() -> None:
    profile = QWEN36_27B_V12_PROFILE

    assert v12_model_profile_for("qwen36-27b-nvfp4-nvidia", "dispatcher") is profile
    assert v12_model_profile_for(" qwen36-27b-nvfp4-nvidia ", "dispatcher") is None
    assert v12_model_profile_for("qwen36-27b-nvfp4-nvidia", "Dispatcher") is None
    assert profile.max_prepared_evidence_items == 2
    assert profile.minimum_context_tokens == profile.max_context_tokens == 8192
    assert profile.max_tool_steps == 0
    assert profile.allowed_effects == frozenset({ModelEffect.READ})
    assert profile.verifier_required is True
    assert ModelCapability.RAW_VISION not in profile.allowed_capabilities
    assert ModelCapability.NATIVE_TOOL_CALLS not in profile.allowed_capabilities
    with pytest.raises(TypeError):
        V12_MODEL_PROFILES[("forged", "dispatcher")] = profile  # type: ignore[index]


def test_gate_starts_as_shadow_candidate_without_canary_authority() -> None:
    gate = V12ModelGate(QWEN36_27B_V12_PROFILE, endpoint_binding_sha256=_ENDPOINT_BINDING)

    assert gate.shadow_allowed() is True
    assert gate.lease(_requirements(), process_epoch_sha256=_PROCESS_EPOCH_SHA256) is None
    assert gate.public_status() == {
        "schema": "friday.v12-model-profile.v1",
        "profile_id": QWEN36_27B_V12_PROFILE.profile_id,
        "status": "shadow_candidate",
        "reason_code": "awaiting_live_attestation",
        "planner_contract_sha256": QWEN36_27B_V12_PROFILE.planner_contract_sha256,
        "probe_suite_sha256": QWEN36_27B_V12_PROFILE.probe_suite_sha256,
        "attestation_sha256": "",
        "capabilities": [],
        "verified_context_tokens": 0,
        "max_prepared_evidence_items": 0,
        "max_tool_steps": 0,
        "allowed_effects": [],
        "verifier_required": True,
    }


def test_valid_live_attestation_issues_an_exact_least_privilege_lease() -> None:
    gate = _ready_gate()
    requirements = _requirements(
        capabilities=frozenset({ModelCapability.TURN_PLAN_V1, ModelCapability.RU_PLANNING}),
        required_context_tokens=4096,
        prepared_evidence_items=1,
    )

    lease = gate.lease(requirements, process_epoch_sha256=_PROCESS_EPOCH_SHA256)

    assert isinstance(lease, ModelProfileLease)
    assert lease.capabilities == requirements.capabilities
    assert lease.required_context_tokens == 4096
    assert lease.prepared_evidence_items == 1
    assert lease.effect is ModelEffect.READ
    assert lease.requirements_sha256 == requirements.canonical_sha256()
    assert len(lease.attestation_sha256) == 64
    assert gate.public_status()["status"] == "canary_ready"
    assert gate.public_status()["reason_code"] == "live_attestation_clear"
    assert (
        gate.validate_lease(
            lease,
            requirements,
            process_epoch_sha256=_PROCESS_EPOCH_SHA256,
        )
        is True
    )


def test_lease_is_live_authority_not_a_stale_bearer_snapshot() -> None:
    gate = _ready_gate()
    requirements = _requirements()
    old_lease = gate.lease(requirements, process_epoch_sha256=_PROCESS_EPOCH_SHA256)
    assert old_lease is not None

    gate.revoke(ModelGateReason.EXPLICIT_REVOCATION)
    assert (
        gate.validate_lease(
            old_lease,
            requirements,
            process_epoch_sha256=_PROCESS_EPOCH_SHA256,
        )
        is False
    )

    assert gate.install_live(_attestation()) is True
    assert (
        gate.validate_lease(
            old_lease,
            requirements,
            process_epoch_sha256=_PROCESS_EPOCH_SHA256,
        )
        is False
    )
    current_lease = gate.lease(requirements, process_epoch_sha256=_PROCESS_EPOCH_SHA256)
    assert current_lease is not None
    assert (
        gate.validate_lease(
            current_lease,
            requirements,
            process_epoch_sha256=_PROCESS_EPOCH_SHA256,
        )
        is True
    )

    forged = replace(current_lease, _gate_generation=current_lease._gate_generation + 1)  # noqa: SLF001
    assert (
        gate.validate_lease(
            forged,
            requirements,
            process_epoch_sha256=_PROCESS_EPOCH_SHA256,
        )
        is False
    )


@pytest.mark.parametrize(
    "requirements",
    [
        _requirements(capabilities=frozenset({ModelCapability.RAW_VISION})),
        _requirements(capabilities=frozenset({ModelCapability.NATIVE_TOOL_CALLS})),
        _requirements(required_context_tokens=8193),
        _requirements(prepared_evidence_items=3),
        _requirements(max_tool_steps=1),
        _requirements(effect=ModelEffect.WRITE),
        _requirements(effect=ModelEffect.HIGH),
        _requirements(verifier_required=False),
    ],
)
def test_lease_denies_every_requirement_outside_the_attested_subset(
    requirements: ModelRequirements,
) -> None:
    gate = _ready_gate()

    assert gate.lease(requirements, process_epoch_sha256=_PROCESS_EPOCH_SHA256) is None
    assert gate.public_status()["status"] == "canary_ready"


@pytest.mark.parametrize(
    "attestation",
    [
        _attestation(profile_id="another:dispatcher:v12.1"),
        _attestation(planner_contract_sha256="b" * 64),
        _attestation(probe_suite_sha256="b" * 64),
        _attestation(endpoint_binding_sha256="b" * 64),
        _attestation(capabilities=frozenset({ModelCapability.TURN_PLAN_V1})),
        _attestation(
            capabilities=frozenset(
                {*QWEN36_27B_V12_PROFILE.required_capabilities, ModelCapability.RAW_VISION}
            )
        ),
        _attestation(verified_context_tokens=8191),
        _attestation(verified_context_tokens=8193),
        _attestation(max_prepared_evidence_items=1),
        _attestation(max_tool_steps=1),
        _attestation(allowed_effects=frozenset({ModelEffect.READ, ModelEffect.WRITE})),
        _attestation(verifier_required=False),
    ],
)
def test_live_install_rejects_unpinned_or_overbroad_claims(
    attestation: V12LiveAttestation,
) -> None:
    gate = V12ModelGate(QWEN36_27B_V12_PROFILE, endpoint_binding_sha256=_ENDPOINT_BINDING)

    assert gate.install_live(attestation) is False
    assert gate.lease(_requirements(), process_epoch_sha256=_PROCESS_EPOCH_SHA256) is None
    assert gate.public_status()["status"] == "revoked"
    assert gate.public_status()["reason_code"] == "attestation_rejected"


def test_a_mapping_or_failed_replacement_can_never_self_attest() -> None:
    gate = _ready_gate()
    forged = {
        "profile_id": QWEN36_27B_V12_PROFILE.profile_id,
        "capabilities": [item.value for item in QWEN36_27B_V12_PROFILE.required_capabilities],
        "attested": True,
    }

    assert gate.install_live(forged) is False
    assert gate.lease(_requirements(), process_epoch_sha256=_PROCESS_EPOCH_SHA256) is None
    assert gate.public_status()["status"] == "revoked"


@pytest.mark.parametrize("observed_epoch_sha256", ["", "not-a-digest", "C" * 64])
def test_invalid_epoch_revokes_future_leases(observed_epoch_sha256: str) -> None:
    gate = _ready_gate()

    assert gate.lease(_requirements(), process_epoch_sha256=observed_epoch_sha256) is None
    assert gate.public_status()["status"] == "revoked"
    assert gate.public_status()["reason_code"] == "epoch_invalid"
    assert gate.lease(_requirements(), process_epoch_sha256=_PROCESS_EPOCH_SHA256) is None


def test_changed_model_process_epoch_revokes_future_leases() -> None:
    gate = _ready_gate()

    assert gate.lease(_requirements(), process_epoch_sha256="d" * 64) is None
    assert gate.public_status()["status"] == "revoked"
    assert gate.public_status()["reason_code"] == "epoch_changed"
    assert gate.lease(_requirements(), process_epoch_sha256=_PROCESS_EPOCH_SHA256) is None


def test_explicit_revoke_never_copies_arbitrary_private_text_to_status() -> None:
    gate = _ready_gate()
    private_text = "private-hostname and bearer-secret"

    gate.revoke(private_text)

    public = gate.public_status()
    assert public["status"] == "revoked"
    assert public["reason_code"] == ModelGateReason.EXPLICIT_REVOCATION.value
    assert private_text not in repr(public)
    assert gate.lease(_requirements(), process_epoch_sha256=_PROCESS_EPOCH_SHA256) is None


def test_public_status_and_repr_never_expose_endpoint_binding_or_epoch() -> None:
    gate = _ready_gate()
    attestation = _attestation()
    lease = gate.lease(_requirements(), process_epoch_sha256=_PROCESS_EPOCH_SHA256)
    assert lease is not None

    exposed = "\n".join((repr(attestation), repr(lease), repr(gate.public_status())))

    assert _ENDPOINT_BINDING not in exposed
    assert _PROCESS_EPOCH_SHA256 not in exposed
    assert "base_url" not in exposed
    assert "api_key" not in exposed
    assert "prompt" not in exposed
    assert "response" not in exposed


def test_gate_and_contract_inputs_are_strictly_typed_and_immutable() -> None:
    with pytest.raises(ValueError, match="immutable capability"):
        ModelRequirements(capabilities={ModelCapability.TURN_PLAN_V1})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-negative"):
        ModelRequirements(capabilities=frozenset(), required_context_tokens=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        V12ModelGate(QWEN36_27B_V12_PROFILE, endpoint_binding_sha256="not-a-digest")
    with pytest.raises(ValueError, match="attestation hashes"):
        replace(_attestation(), process_epoch_sha256="C" * 64)
    with pytest.raises(ValueError, match="registered code-owned"):
        V12ModelGate(replace(QWEN36_27B_V12_PROFILE), endpoint_binding_sha256=_ENDPOINT_BINDING)


def test_profile_authority_has_no_environment_file_or_network_loader() -> None:
    source = inspect.getsource(model_profiles_module)

    assert "os.environ" not in source
    assert "pathlib" not in source
    assert "httpx" not in source
    assert "from friday.orchestration" not in source
    assert "import friday.orchestration" not in source
