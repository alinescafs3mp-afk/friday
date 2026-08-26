from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import replace

import pytest

from friday.orchestration.effect_outcome import EffectAction, EffectCapability, EffectOutcomeV1
from friday.orchestration.supervisor_contracts import CapabilityEffectClass
from friday.orchestration.supervisor_effect_intent import (
    SUPERVISOR_EFFECT_INTENT_SCHEMA,
    BoundAdvisoryEffectIntent,
    EffectIntentError,
    EffectIntentGateReason,
    EffectIntentReason,
    EffectIntentV1,
    EffectLifecycle,
    FreshEffectGateState,
    PreparedEffectBinding,
    gate_supervisor_effect_intent,
    prepare_obsidian_effect_binding,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


_DIGESTS = {
    label: _digest(label)
    for label in (
        "actor",
        "conversation",
        "request",
        "source",
        "authorization",
        "idempotency",
        "registry",
        "policy",
        "manifest",
        "proposal",
        "confirmation",
        "other",
    )
}


def _intent(*, action: EffectAction = EffectAction.CREATE) -> EffectIntentV1:
    return EffectIntentV1.parse(
        {
            "schema": SUPERVISOR_EFFECT_INTENT_SCHEMA,
            "capability": EffectCapability.OBSIDIAN_NOTE_MUTATION.value,
            "action": action.value,
            "manifest_digest": _DIGESTS["manifest"],
            "proposal_digest": _DIGESTS["proposal"],
            "reason": EffectIntentReason.EXPLICIT_USER_REQUEST.value,
        }
    )


def _binding(*, action: EffectAction = EffectAction.CREATE) -> PreparedEffectBinding:
    tool = {
        EffectAction.CREATE: "obsidian_create_note",
        EffectAction.APPEND: "obsidian_append_note",
    }[action]
    return prepare_obsidian_effect_binding(
        capability=EffectCapability.OBSIDIAN_NOTE_MUTATION,
        action=action,
        resolved_tool_name=tool,
        resolved_security_id="obsidian.write",
        resolved_effect_class=CapabilityEffectClass.WRITE,
        resolved_tool_risk="mutate",
        actor_binding_digest=_DIGESTS["actor"],
        conversation_binding_digest=_DIGESTS["conversation"],
        request_digest=_DIGESTS["request"],
        source_revision_digest=_DIGESTS["source"],
        authorization_basis_digest=_DIGESTS["authorization"],
        idempotency_key_digest=_DIGESTS["idempotency"],
        registry_digest=_DIGESTS["registry"],
        policy_digest=_DIGESTS["policy"],
        manifest_digest=_DIGESTS["manifest"],
        proposal_digest=_DIGESTS["proposal"],
        confirmation_digest=_DIGESTS["confirmation"],
    )


def _current(binding: PreparedEffectBinding) -> FreshEffectGateState:
    return FreshEffectGateState(
        resolved_tool_name=binding.tool_name,
        resolved_security_id=binding.security_id,
        resolved_effect_class=binding.effect_class,
        resolved_tool_risk=binding.tool_risk,
        actor_binding_digest=binding.actor_binding_digest,
        conversation_binding_digest=binding.conversation_binding_digest,
        request_digest=binding.request_digest,
        source_revision_digest=binding.source_revision_digest,
        authorization_basis_digest=binding.authorization_basis_digest,
        idempotency_key_digest=binding.idempotency_key_digest,
        registry_digest=binding.registry_digest,
        policy_digest=binding.policy_digest,
        manifest_digest=binding.manifest_digest,
        proposal_digest=binding.proposal_digest,
        permission_allowed=True,
        source_authorized=True,
        confirmation_present=True,
        confirmation_digest=binding.confirmation_digest,
        lifecycle=EffectLifecycle.NOT_STARTED,
    )


def test_model_intent_is_a_closed_six_field_round_trip() -> None:
    intent = _intent()

    assert EffectIntentV1.parse(intent.to_json()) == intent
    assert set(intent.to_payload()) == {
        "schema",
        "capability",
        "action",
        "manifest_digest",
        "proposal_digest",
        "reason",
    }
    assert not (
        {
            "args",
            "arguments",
            "body",
            "path",
            "tool",
            "security_id",
            "risk",
            "permission",
            "confirmation",
            "idempotency_key",
            "authority",
        }
        & set(intent.to_payload())
    )


def test_model_json_rejects_duplicate_and_non_closed_keys() -> None:
    payload = _intent().to_payload()
    duplicate = _intent().to_json().replace(
        '"action":"create"',
        '"action":"create","action":"append"',
    )

    with pytest.raises(EffectIntentError, match="duplicate"):
        EffectIntentV1.parse(duplicate)
    with pytest.raises(EffectIntentError, match="keys"):
        EffectIntentV1.parse({**payload, "unknown": "field"})
    with pytest.raises(EffectIntentError, match="one JSON object"):
        EffectIntentV1.parse("[]")


@pytest.mark.parametrize(
    "forged_key,forged_value",
    [
        ("args", {"path": "private.md"}),
        ("body", "private note bytes"),
        ("path", "private.md"),
        ("tool_name", "obsidian_create_note"),
        ("security_id", "obsidian.write"),
        ("risk", "observe"),
        ("permission_allowed", True),
        ("confirmation_present", True),
        ("idempotency_key_digest", _DIGESTS["idempotency"]),
        ("authority", "owner"),
        ("execution_authorized", True),
    ],
)
def test_model_cannot_forge_arguments_risk_or_authority(
    forged_key: str,
    forged_value: object,
) -> None:
    with pytest.raises(EffectIntentError, match="keys"):
        EffectIntentV1.parse({**_intent().to_payload(), forged_key: forged_value})


@pytest.mark.parametrize(
    "capability,action",
    [
        ("host_scan_local", "nmap"),
        ("host.install", "install"),
        ("shell", "create"),
        ("network", "append"),
        ("obsidian_note_mutation", "delete"),
        ("obsidian_note_mutation", "nmap"),
        ("obsidian_note_mutation", "install"),
    ],
)
def test_host_shell_delete_network_and_unknown_symbols_are_unavailable(
    capability: str,
    action: str,
) -> None:
    payload = _intent().to_payload()
    payload.update(capability=capability, action=action)

    with pytest.raises(EffectIntentError, match="unavailable"):
        EffectIntentV1.parse(payload)


@pytest.mark.parametrize(
    "override",
    [
        {"resolved_tool_name": "obsidian_append_note"},
        {"resolved_tool_name": "obsidian_prepend_note"},
        {"resolved_security_id": "obsidian.read"},
        {"resolved_effect_class": CapabilityEffectClass.HIGH},
        {"resolved_tool_risk": "observe"},
    ],
)
def test_binding_factory_accepts_only_the_real_create_append_contract(
    override: dict[str, object],
) -> None:
    kwargs: dict[str, object] = {
        "capability": EffectCapability.OBSIDIAN_NOTE_MUTATION,
        "action": EffectAction.CREATE,
        "resolved_tool_name": "obsidian_create_note",
        "resolved_security_id": "obsidian.write",
        "resolved_effect_class": CapabilityEffectClass.WRITE,
        "resolved_tool_risk": "mutate",
        "actor_binding_digest": _DIGESTS["actor"],
        "conversation_binding_digest": _DIGESTS["conversation"],
        "request_digest": _DIGESTS["request"],
        "source_revision_digest": _DIGESTS["source"],
        "authorization_basis_digest": _DIGESTS["authorization"],
        "idempotency_key_digest": _DIGESTS["idempotency"],
        "registry_digest": _DIGESTS["registry"],
        "policy_digest": _DIGESTS["policy"],
        "manifest_digest": _DIGESTS["manifest"],
        "proposal_digest": _DIGESTS["proposal"],
        "confirmation_digest": _DIGESTS["confirmation"],
    }
    kwargs.update(override)

    with pytest.raises(EffectIntentError, match="unavailable or has drifted"):
        prepare_obsidian_effect_binding(**kwargs)  # type: ignore[arg-type]


def test_model_parser_cannot_mint_or_reseal_prepared_binding() -> None:
    binding = _binding()

    assert type(EffectIntentV1.parse(_intent().to_json())) is EffectIntentV1
    assert not hasattr(PreparedEffectBinding, "parse")
    with pytest.raises(EffectIntentError, match="process-owned"):
        replace(binding, registry_digest=_DIGESTS["other"])


def test_pure_gate_returns_only_a_non_authorizing_body_free_advisory() -> None:
    intent = _intent()
    binding = _binding()

    decision = gate_supervisor_effect_intent(intent, binding, _current(binding))

    assert decision.bound is True
    assert decision.reason is EffectIntentGateReason.ADVISORY_BOUND
    assert decision.execution_authorized is False
    assert decision.publication_authorized is False
    advisory = decision.advisory
    assert type(advisory) is BoundAdvisoryEffectIntent
    assert advisory.execution_authorized is False
    assert advisory.publication_authorized is False
    assert not isinstance(advisory, EffectOutcomeV1)
    assert set(inspect.signature(gate_supervisor_effect_intent).parameters) == {
        "intent",
        "binding",
        "current",
    }
    for forbidden in (
        "args",
        "arguments",
        "body",
        "path",
        "tool",
        "security_id",
        "risk",
        "permission",
        "confirmation",
        "idempotency_key",
        "authority",
        "execute",
        "handler",
        "kernel",
        "outcome",
    ):
        assert not hasattr(advisory, forbidden)


def test_symbolic_action_drift_rejects_before_binding() -> None:
    binding = _binding(action=EffectAction.CREATE)
    decision = gate_supervisor_effect_intent(
        _intent(action=EffectAction.APPEND),
        binding,
        _current(binding),
    )

    assert decision.bound is False
    assert decision.reason is EffectIntentGateReason.SYMBOLIC_DRIFT
    assert decision.advisory is None


@pytest.mark.parametrize(
    "field,reason",
    [
        ("resolved_tool_name", EffectIntentGateReason.TOOL_CONTRACT_DRIFT),
        ("resolved_security_id", EffectIntentGateReason.SECURITY_CONTRACT_DRIFT),
        ("resolved_effect_class", EffectIntentGateReason.EFFECT_CONTRACT_DRIFT),
        ("resolved_tool_risk", EffectIntentGateReason.EFFECT_CONTRACT_DRIFT),
        ("manifest_digest", EffectIntentGateReason.MANIFEST_DRIFT),
        ("proposal_digest", EffectIntentGateReason.PROPOSAL_DRIFT),
        ("actor_binding_digest", EffectIntentGateReason.ACTOR_DRIFT),
        ("conversation_binding_digest", EffectIntentGateReason.CONVERSATION_DRIFT),
        ("request_digest", EffectIntentGateReason.REQUEST_DRIFT),
        ("source_revision_digest", EffectIntentGateReason.SOURCE_REVISION_DRIFT),
        ("authorization_basis_digest", EffectIntentGateReason.AUTHORIZATION_DRIFT),
        ("idempotency_key_digest", EffectIntentGateReason.IDEMPOTENCY_DRIFT),
        ("registry_digest", EffectIntentGateReason.REGISTRY_DRIFT),
        ("policy_digest", EffectIntentGateReason.POLICY_DRIFT),
        ("confirmation_digest", EffectIntentGateReason.CONFIRMATION_DRIFT),
    ],
)
def test_fresh_exact_identity_drift_is_measurable(
    field: str,
    reason: EffectIntentGateReason,
) -> None:
    binding = _binding()
    current = _current(binding)
    if field == "resolved_tool_name":
        changed: object = "obsidian_append_note"
    elif field == "resolved_security_id":
        changed = "knowledge.edit"
    elif field == "resolved_effect_class":
        changed = CapabilityEffectClass.HIGH
    elif field == "resolved_tool_risk":
        changed = "high"
    else:
        changed = _DIGESTS["other"]

    decision = gate_supervisor_effect_intent(
        _intent(),
        binding,
        replace(current, **{field: changed}),
    )

    assert decision.bound is False
    assert decision.reason is reason
    assert decision.execution_authorized is False
    assert decision.publication_authorized is False


def test_model_manifest_and_proposal_drift_reject() -> None:
    binding = _binding()
    current = _current(binding)

    manifest = gate_supervisor_effect_intent(
        replace(_intent(), manifest_digest=_DIGESTS["other"]),
        binding,
        current,
    )
    proposal = gate_supervisor_effect_intent(
        replace(_intent(), proposal_digest=_DIGESTS["other"]),
        binding,
        current,
    )

    assert manifest.reason is EffectIntentGateReason.MANIFEST_DRIFT
    assert proposal.reason is EffectIntentGateReason.PROPOSAL_DRIFT


def test_permission_source_and_confirmation_fail_closed() -> None:
    binding = _binding()
    current = _current(binding)

    denied = gate_supervisor_effect_intent(
        _intent(),
        binding,
        replace(current, permission_allowed=False),
    )
    source_denied = gate_supervisor_effect_intent(
        _intent(),
        binding,
        replace(current, source_authorized=False),
    )
    absent = gate_supervisor_effect_intent(
        _intent(),
        binding,
        replace(current, confirmation_present=False, confirmation_digest=None),
    )

    assert denied.reason is EffectIntentGateReason.PERMISSION_DENIED
    assert source_denied.reason is EffectIntentGateReason.SOURCE_NOT_AUTHORIZED
    assert absent.reason is EffectIntentGateReason.CONFIRMATION_REQUIRED
    assert denied.advisory is source_denied.advisory is absent.advisory is None


@pytest.mark.parametrize(
    "lifecycle,reason",
    [
        (EffectLifecycle.STARTED, EffectIntentGateReason.EFFECT_ALREADY_STARTED),
        (EffectLifecycle.UNCERTAIN, EffectIntentGateReason.OUTCOME_UNCERTAIN),
    ],
)
def test_started_or_uncertain_effect_never_rebinds(
    lifecycle: EffectLifecycle,
    reason: EffectIntentGateReason,
) -> None:
    binding = _binding()

    decision = gate_supervisor_effect_intent(
        _intent(),
        binding,
        replace(_current(binding), lifecycle=lifecycle),
    )

    assert decision.bound is False
    assert decision.reason is reason
    assert decision.execution_authorized is False
    assert decision.publication_authorized is False


def test_gate_detects_post_construction_binding_tamper() -> None:
    binding = _binding()
    current = _current(binding)
    object.__setattr__(binding, "registry_digest", _DIGESTS["other"])

    decision = gate_supervisor_effect_intent(_intent(), binding, current)

    assert decision.bound is False
    assert decision.reason is EffectIntentGateReason.INVALID_BINDING
    assert decision.advisory is None


def test_intent_json_size_and_utf8_are_bounded() -> None:
    payload = _intent().to_payload()
    with pytest.raises(EffectIntentError, match="too large"):
        EffectIntentV1.parse(json.dumps({**payload, "padding": "x" * 3_000}))
    with pytest.raises(EffectIntentError, match="valid UTF-8"):
        EffectIntentV1.parse("\ud800")
