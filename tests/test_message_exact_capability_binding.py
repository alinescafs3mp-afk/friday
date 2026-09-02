"""Focused activation binding tests for the internal exact-message lane."""

from __future__ import annotations

from dataclasses import replace

import pytest

import friday.orchestration.capability_binding as capability_binding_module
import friday.retrieval.message_exact_internal as message_exact_internal
from friday.orchestration.capability_binding import (
    CapabilityBindingSnapshot,
    OperationalCapabilityBinding,
    operational_capability_snapshot,
)
from friday.orchestration.supervisor_contracts import (
    CONVERSATION_WINDOW_READ_ID,
    CapabilityEffectClass,
    canonical_sha256,
)
from friday.permissions import AuthorizationService, CapabilityDefinition
from friday.retrieval.message_exact_internal import (
    MESSAGE_EXACT_ADAPTER_BINDING,
    MESSAGE_EXACT_INTERNAL_ADAPTER_ID,
    MESSAGE_EXACT_SECURITY_IDS,
    MessageExactInternalAdapter,
)


def _window_binding(snapshot: CapabilityBindingSnapshot) -> OperationalCapabilityBinding:
    binding = snapshot.binding_for(CONVERSATION_WINDOW_READ_ID)
    assert binding is not None
    return binding


def _qualified_name(value: object) -> str:
    module = str(getattr(value, "__module__", "") or "")
    name = str(getattr(value, "__qualname__", "") or "")
    return f"{module}.{name}" if module and name else ""


def _foreign_adapter_method(*_args: object, **_kwargs: object) -> None:
    return None


class _DriftedBindingWitness:
    """Expose one drifted binding field without invoking closed dataclass validation."""

    def __init__(self, field: str, value: object) -> None:
        self._field = field
        self._value = value

    def __getattr__(self, name: str) -> object:
        if name == self._field:
            return self._value
        return getattr(MESSAGE_EXACT_ADAPTER_BINDING, name)

    def payload(self) -> dict[str, object]:
        payload = MESSAGE_EXACT_ADAPTER_BINDING.payload()
        payload[self._field] = self._value
        return payload

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())


def test_conversation_window_exact_adapter_is_available_and_code_owned() -> None:
    snapshot = operational_capability_snapshot()
    binding = _window_binding(snapshot)

    assert binding.available is True
    assert binding.permission_registered is True
    assert binding.adapter_registered is True
    assert binding.effect_class is CapabilityEffectClass.READ
    assert binding.supervisor_capability_id == CONVERSATION_WINDOW_READ_ID
    assert binding.security_id == "conversations.read"
    assert binding.tool_id == CONVERSATION_WINDOW_READ_ID == "conversation.window.read"
    assert (
        binding.adapter_id
        == MESSAGE_EXACT_INTERNAL_ADAPTER_ID
        == "friday.retrieval.message_exact_internal.MessageExactInternalAdapter"
    )
    assert MESSAGE_EXACT_ADAPTER_BINDING.capability_id == CONVERSATION_WINDOW_READ_ID
    assert MESSAGE_EXACT_ADAPTER_BINDING.adapter_id == MESSAGE_EXACT_INTERNAL_ADAPTER_ID
    assert MESSAGE_EXACT_ADAPTER_BINDING.security_ids == (
        "conversations.read",
        "search.use",
    )
    assert MESSAGE_EXACT_ADAPTER_BINDING.security_ids == MESSAGE_EXACT_SECURITY_IDS
    assert MESSAGE_EXACT_ADAPTER_BINDING.effect_class == "read"
    assert MESSAGE_EXACT_ADAPTER_BINDING.model_visible is False
    for method in (
        "prepare_in_transaction",
        "project_for_model",
        "reauthorize_for_publication_in_transaction",
    ):
        implementation = getattr(MessageExactInternalAdapter, method, None)
        assert callable(implementation)
        assert _qualified_name(implementation) == f"{MESSAGE_EXACT_INTERNAL_ADAPTER_ID}.{method}"


@pytest.mark.parametrize("security_id", MESSAGE_EXACT_SECURITY_IDS)
def test_each_message_permission_definition_affects_window_binding_identity(
    monkeypatch: pytest.MonkeyPatch,
    security_id: str,
) -> None:
    baseline_snapshot = operational_capability_snapshot()
    baseline = _window_binding(baseline_snapshot)
    assert baseline.available is True
    original = AuthorizationService.list_capabilities

    def drifted(service: AuthorizationService) -> list[CapabilityDefinition]:
        return [
            replace(definition, description=f"{definition.description} [identity drift]")
            if definition.security_id == security_id
            else definition
            for definition in original(service)
        ]

    monkeypatch.setattr(AuthorizationService, "list_capabilities", drifted)
    drifted_snapshot = operational_capability_snapshot()
    drifted_binding = _window_binding(drifted_snapshot)

    assert drifted_binding.available is True
    assert drifted_binding.adapter_identity_sha256 != baseline.adapter_identity_sha256
    if security_id == "conversations.read":
        assert drifted_binding.permission_identity_sha256 != baseline.permission_identity_sha256
    else:
        assert drifted_binding.permission_identity_sha256 == baseline.permission_identity_sha256
    assert drifted_binding.identity_payload() != baseline.identity_payload()
    assert drifted_snapshot.digest_hex() != baseline_snapshot.digest_hex()


@pytest.mark.parametrize("security_id", MESSAGE_EXACT_SECURITY_IDS)
def test_each_missing_message_permission_makes_window_binding_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    security_id: str,
) -> None:
    baseline_snapshot = operational_capability_snapshot()
    baseline = _window_binding(baseline_snapshot)
    assert baseline.available is True
    original = AuthorizationService.list_capabilities

    def without_definition(service: AuthorizationService) -> list[CapabilityDefinition]:
        return [
            definition
            for definition in original(service)
            if definition.security_id != security_id
        ]

    monkeypatch.setattr(AuthorizationService, "list_capabilities", without_definition)
    missing_snapshot = operational_capability_snapshot()
    missing = _window_binding(missing_snapshot)

    assert missing.permission_registered is (security_id != "conversations.read")
    assert missing.adapter_registered is False
    assert missing.available is False
    assert missing.identity_payload() != baseline.identity_payload()
    assert missing_snapshot.digest_hex() != baseline_snapshot.digest_hex()


@pytest.mark.parametrize(
    "drift",
    (
        "adapter_constant",
        "security_ids_constant",
        "adapter_id",
        "capability_id",
        "model_visible",
        "prepare_in_transaction",
        "project_for_model",
        "reauthorize_for_publication_in_transaction",
    ),
)
def test_message_exact_adapter_surface_drift_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    baseline_snapshot = operational_capability_snapshot()
    baseline = _window_binding(baseline_snapshot)
    assert baseline.available is True

    if drift == "adapter_constant":
        monkeypatch.setattr(
            message_exact_internal,
            "MESSAGE_EXACT_INTERNAL_ADAPTER_ID",
            "foreign.message.Adapter",
        )
    elif drift == "security_ids_constant":
        monkeypatch.setattr(
            message_exact_internal,
            "MESSAGE_EXACT_SECURITY_IDS",
            ("search.use", "conversations.read"),
        )
    elif drift in {"adapter_id", "capability_id", "model_visible"}:
        if drift == "adapter_id":
            replacement = "foreign.message.Adapter"
        elif drift == "capability_id":
            replacement = "foreign.message.read"
        else:
            replacement = True
        drifted_binding = _DriftedBindingWitness(drift, replacement)
        monkeypatch.setattr(
            message_exact_internal,
            "MESSAGE_EXACT_ADAPTER_BINDING",
            drifted_binding,
        )
        monkeypatch.setattr(
            capability_binding_module,
            "MESSAGE_EXACT_ADAPTER_BINDING",
            drifted_binding,
            raising=False,
        )
    else:
        monkeypatch.setattr(MessageExactInternalAdapter, drift, _foreign_adapter_method)

    drifted_snapshot = operational_capability_snapshot()
    drifted = _window_binding(drifted_snapshot)

    assert drifted.adapter_registered is False
    assert drifted.available is False
    assert drifted.adapter_identity_sha256 != baseline.adapter_identity_sha256
    assert drifted_snapshot.digest_hex() != baseline_snapshot.digest_hex()
