"""Host-side validation using the backend's shared adapter contracts."""

from __future__ import annotations

import hmac
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from friday.host_control.adapters.base import (
    AdapterSpec,
    ExecutionSpec,
    HostAdapter,
    attest_execution,
)
from friday.host_control.contracts import ContractError, ExecutableAttestation, canonical_digest
from friday.host_control.plans import HostActionPlan, assert_plan_current
from friday.host_control.policy import (
    NetworkPolicy,
    NetworkTargetSnapshot,
    assert_target_snapshot_current,
)

from .inventory import ExecutableInventory


class AdapterValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ValidatedAction:
    plan: HostActionPlan
    adapter: AdapterSpec
    implementation: HostAdapter
    action_id: str
    normalized_arguments: dict[str, Any]
    execution: ExecutionSpec
    executable: ExecutableAttestation
    plan_digest: str

    def to_dict(self) -> dict[str, Any]:
        action = self.adapter.action(self.action_id)
        return {
            "action": action.to_payload(),
            "adapter_digest": self.adapter.digest,
            "adapter_id": self.adapter.adapter_id,
            "adapter_schema_version": self.adapter.adapter_schema_version,
            "arguments": self.normalized_arguments,
            "executable": self.executable.to_payload(),
            "execution": {
                "argv": list(self.execution.argv),
                "argv_sha256": self.execution.argv_sha256,
                "environment": [list(item) for item in self.execution.environment],
                "executable": self.execution.executable,
                "max_output_bytes": self.execution.max_output_bytes,
                "profile": self.execution.profile.value,
                "timeout_sec": self.execution.timeout_sec,
                "working_directory_ref": self.execution.working_directory_ref,
            },
            "implementation_version": self.adapter.implementation_version,
            "plan_digest": self.plan_digest,
            "plan": self.plan.to_payload(),
        }


class AdapterRegistry:
    def __init__(
        self,
        adapters: tuple[HostAdapter, ...],
        *,
        inventory: ExecutableInventory,
        network_policy: NetworkPolicy | Callable[[], NetworkPolicy] | None = None,
    ) -> None:
        self._adapters: dict[str, HostAdapter] = {}
        self._inventory = inventory
        if network_policy is None:
            default_policy = NetworkPolicy(connected_cidrs=())
            self._network_policy_source = lambda: default_policy
        elif isinstance(network_policy, NetworkPolicy):
            self._network_policy_source = lambda: network_policy
        else:
            self._network_policy_source = network_policy
        for adapter in adapters:
            if adapter.spec.adapter_id in self._adapters:
                raise AdapterValidationError("adapter id is already registered")
            self._adapters[adapter.spec.adapter_id] = adapter

    @property
    def adapters(self) -> Mapping[str, HostAdapter]:
        return MappingProxyType(self._adapters)

    def catalog_digest(self) -> str:
        return canonical_digest(
            [
                {"adapter_digest": adapter.spec.digest, "adapter_id": adapter.spec.adapter_id}
                for adapter in sorted(self._adapters.values(), key=lambda item: item.spec.adapter_id)
            ]
        )

    def network_policy_digest(self) -> str:
        """Return the current agent-owned policy identity for the signed handshake."""

        return self._current_network_policy().digest

    def assert_target_policy_current(self, plan: HostActionPlan) -> None:
        """Re-normalize exact network targets under native operator authority."""

        if plan.adapter_id != "network.nmap":
            return
        payload = plan.target_snapshot
        if payload is None:
            raise AdapterValidationError("network action lacks an exact target snapshot")
        try:
            snapshot = NetworkTargetSnapshot.from_payload(payload)
            if snapshot.digest != plan.target_snapshot_digest:
                raise ContractError("network target snapshot digest changed")
            assert_target_snapshot_current(snapshot, self._current_network_policy())
        except ContractError as exc:
            raise AdapterValidationError("network target is outside the current host-agent policy") from exc

    def _current_network_policy(self) -> NetworkPolicy:
        try:
            policy = self._network_policy_source()
        except (OSError, RuntimeError, ValueError) as exc:
            raise AdapterValidationError("host-agent network policy is unavailable") from exc
        if not isinstance(policy, NetworkPolicy):
            raise AdapterValidationError("host-agent network policy source is invalid")
        return policy

    def validate_action(
        self,
        *,
        plan_payload: dict[str, Any],
        approved_plan_digest: str,
        now: int | None = None,
    ) -> ValidatedAction:
        try:
            plan = HostActionPlan.from_payload(plan_payload)
        except ContractError as exc:
            raise AdapterValidationError("host action plan payload is invalid") from exc
        adapter = self._adapters.get(plan.adapter_id)
        if adapter is None:
            raise AdapterValidationError("adapter is not registered")
        spec = adapter.spec
        if (
            plan.adapter_schema_version != spec.adapter_schema_version
            or plan.implementation_version != spec.implementation_version
        ):
            raise AdapterValidationError("adapter version changed after planning")
        try:
            action = spec.action(plan.action_id)
            inventory = self._inventory.inspect(plan.adapter_id)
            if inventory.state != "available" or inventory.attestation is None:
                raise AdapterValidationError("adapter executable is not package-attested")
            self.assert_target_policy_current(plan)
            assert_plan_current(
                plan,
                adapter=spec,
                executable_attestation=inventory.attestation,
                target_snapshot=plan.target_snapshot,
                approved_plan_digest=approved_plan_digest,
                now=now,
            )
            execution = adapter.build_execution(plan, inventory.attestation)
            attest_execution(spec, inventory.attestation)
        except ContractError as exc:
            raise AdapterValidationError("adapter action violates the shared contract") from exc
        if execution.executable != inventory.attestation.canonical_path:
            raise AdapterValidationError("execution does not use the attested executable")
        if (
            action.security_id != plan.security_id
            or action.risk_class != plan.risk_class
            or action.execution_profile != plan.execution_profile
            or execution.profile != plan.execution_profile
            or execution.timeout_sec != plan.timeout_sec
            or execution.max_output_bytes != plan.max_output_bytes
        ):
            raise AdapterValidationError("execution contract drifted from the signed plan")
        return ValidatedAction(
            plan=plan,
            adapter=spec,
            implementation=adapter,
            action_id=plan.action_id,
            normalized_arguments=plan.normalized_arguments,
            execution=execution,
            executable=inventory.attestation,
            plan_digest=plan.digest,
        )


def require_plan_digest(action: ValidatedAction, expected: str) -> None:
    if not hmac.compare_digest(action.plan_digest, expected):
        raise AdapterValidationError("validated action does not match the approved plan")


__all__ = ["AdapterRegistry", "AdapterValidationError", "ValidatedAction", "require_plan_digest"]
