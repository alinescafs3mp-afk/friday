"""Exact immutable action plans and approval/drift binding."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from .adapters.base import ActionSpec, AdapterSpec, attest_execution
from .contracts import (
    PLAN_SCHEMA_VERSION,
    ContractError,
    ExecutableAttestation,
    ExecutionProfile,
    RiskClass,
    canonical_digest,
    canonical_json_bytes,
    decode_canonical_json,
)

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_WORKSPACE_PATH = re.compile(r"^(?:input|work|output|evidence)/[A-Za-z0-9_.-]{1,180}$")


@dataclass(frozen=True, slots=True)
class WorkspaceGrant:
    grant_id: str
    actor_own_id: str
    access: str
    relative_path: str
    identity_sha256: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"^grant_[0-9a-f]{16}$", self.grant_id):
            raise ContractError("workspace grant id is invalid")
        if not _ID.fullmatch(self.actor_own_id):
            raise ContractError("workspace grant actor is invalid")
        if self.access not in {"read", "create", "replace"}:
            raise ContractError("workspace grant access is invalid")
        if not _WORKSPACE_PATH.fullmatch(self.relative_path) or ".." in self.relative_path.split("/"):
            raise ContractError("workspace grant path is invalid")
        if self.identity_sha256 is not None and not _DIGEST.fullmatch(self.identity_sha256):
            raise ContractError("workspace grant identity is invalid")
        if self.access in {"read", "replace"} and self.identity_sha256 is None:
            raise ContractError("existing workspace object grant lacks identity")

    def to_payload(self) -> dict[str, Any]:
        return {
            "access": self.access,
            "actor_own_id": self.actor_own_id,
            "grant_id": self.grant_id,
            "identity_sha256": self.identity_sha256,
            "relative_path": self.relative_path,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()

    @classmethod
    def from_payload(cls, value: Any) -> WorkspaceGrant:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ContractError("workspace grant fields are invalid")
        try:
            return cls(**value)
        except TypeError as exc:
            raise ContractError("workspace grant field types are invalid") from exc


@dataclass(frozen=True, slots=True)
class HostActionPlan:
    schema_version: int
    plan_id: str
    actor_user_id: str
    actor_own_id: str
    conversation_id: str
    source_message_id: str
    continuation_work_item_id: str | None
    host_agent_id: str
    idempotency_key: str
    capability_id: str
    adapter_id: str
    adapter_schema_version: int
    implementation_version: int
    adapter_digest: str
    action_id: str
    normalized_arguments_json: bytes
    risk_class: RiskClass
    security_id: str
    execution_profile: ExecutionProfile
    timeout_sec: int
    max_output_bytes: int
    target_snapshot_json: bytes | None
    workspace_grants: tuple[WorkspaceGrant, ...]
    executable_attestation_digest: str
    created_at: int
    expires_at: int

    def __post_init__(self) -> None:
        if self.schema_version != PLAN_SCHEMA_VERSION:
            raise ContractError("unknown host action plan schema")
        for name in (
            "plan_id",
            "actor_user_id",
            "actor_own_id",
            "conversation_id",
            "source_message_id",
            "host_agent_id",
            "idempotency_key",
            "capability_id",
            "adapter_id",
            "action_id",
            "security_id",
        ):
            if not _ID.fullmatch(str(getattr(self, name))):
                raise ContractError(f"host action plan {name} is invalid")
        for name in ("adapter_digest", "executable_attestation_digest"):
            if not _DIGEST.fullmatch(str(getattr(self, name))):
                raise ContractError(f"host action plan {name} is invalid")
        if self.continuation_work_item_id is not None and not _ID.fullmatch(self.continuation_work_item_id):
            raise ContractError("host action continuation id is invalid")
        if self.adapter_schema_version != 1 or self.implementation_version < 1:
            raise ContractError("host action adapter version is invalid")
        arguments = decode_canonical_json(self.normalized_arguments_json)
        if not isinstance(arguments, dict):
            raise ContractError("host action normalized arguments must be an object")
        if self.target_snapshot_json is not None:
            target_snapshot = decode_canonical_json(self.target_snapshot_json)
            if not isinstance(target_snapshot, dict):
                raise ContractError("host action target snapshot must be an object")
        if len(self.workspace_grants) > 32 or len({item.grant_id for item in self.workspace_grants}) != len(
            self.workspace_grants
        ):
            raise ContractError("host action workspace grants are invalid")
        if any(item.actor_own_id != self.actor_own_id for item in self.workspace_grants):
            raise ContractError("host action workspace grant actor mismatch")
        if isinstance(self.timeout_sec, bool) or not 1 <= self.timeout_sec <= 3600:
            raise ContractError("host action timeout is invalid")
        if isinstance(self.max_output_bytes, bool) or not 1024 <= self.max_output_bytes <= 64 * 1024 * 1024:
            raise ContractError("host action output cap is invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (self.created_at, self.expires_at)
        ):
            raise ContractError("host action timestamps are invalid")
        if not self.created_at < self.expires_at <= self.created_at + 3600:
            raise ContractError("host action plan expiry is invalid")

    @property
    def normalized_arguments(self) -> dict[str, Any]:
        value = decode_canonical_json(self.normalized_arguments_json)
        assert isinstance(value, dict)
        return value

    @property
    def target_snapshot(self) -> dict[str, Any] | None:
        if self.target_snapshot_json is None:
            return None
        value = decode_canonical_json(self.target_snapshot_json)
        assert isinstance(value, dict)
        return value

    @property
    def target_snapshot_digest(self) -> str | None:
        value = self.target_snapshot
        return canonical_digest(value) if value is not None else None

    def to_payload(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "actor_own_id": self.actor_own_id,
            "actor_user_id": self.actor_user_id,
            "adapter_digest": self.adapter_digest,
            "adapter_id": self.adapter_id,
            "adapter_schema_version": self.adapter_schema_version,
            "capability_id": self.capability_id,
            "continuation_work_item_id": self.continuation_work_item_id,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at,
            "executable_attestation_digest": self.executable_attestation_digest,
            "execution_profile": self.execution_profile.value,
            "expires_at": self.expires_at,
            "host_agent_id": self.host_agent_id,
            "idempotency_key": self.idempotency_key,
            "implementation_version": self.implementation_version,
            "max_output_bytes": self.max_output_bytes,
            "normalized_arguments": self.normalized_arguments,
            "plan_id": self.plan_id,
            "risk_class": self.risk_class.value,
            "schema_version": self.schema_version,
            "security_id": self.security_id,
            "source_message_id": self.source_message_id,
            "target_snapshot": self.target_snapshot,
            "timeout_sec": self.timeout_sec,
            "workspace_grants": [item.to_payload() for item in self.workspace_grants],
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()

    @classmethod
    def from_payload(cls, value: Any) -> HostActionPlan:
        if not isinstance(value, dict):
            raise ContractError("host action plan must be an object")
        expected = {
            "action_id",
            "actor_own_id",
            "actor_user_id",
            "adapter_digest",
            "adapter_id",
            "adapter_schema_version",
            "capability_id",
            "continuation_work_item_id",
            "conversation_id",
            "created_at",
            "executable_attestation_digest",
            "execution_profile",
            "expires_at",
            "host_agent_id",
            "idempotency_key",
            "implementation_version",
            "max_output_bytes",
            "normalized_arguments",
            "plan_id",
            "risk_class",
            "schema_version",
            "security_id",
            "source_message_id",
            "target_snapshot",
            "timeout_sec",
            "workspace_grants",
        }
        if set(value) != expected or not isinstance(value.get("normalized_arguments"), dict):
            raise ContractError("host action plan fields are invalid")
        target_snapshot = value.get("target_snapshot")
        if target_snapshot is not None and not isinstance(target_snapshot, dict):
            raise ContractError("host action target snapshot is invalid")
        raw_grants = value.get("workspace_grants")
        if not isinstance(raw_grants, list):
            raise ContractError("host action workspace grants are invalid")
        try:
            return cls(
                schema_version=value["schema_version"],
                plan_id=value["plan_id"],
                actor_user_id=value["actor_user_id"],
                actor_own_id=value["actor_own_id"],
                conversation_id=value["conversation_id"],
                source_message_id=value["source_message_id"],
                continuation_work_item_id=value["continuation_work_item_id"],
                host_agent_id=value["host_agent_id"],
                idempotency_key=value["idempotency_key"],
                capability_id=value["capability_id"],
                adapter_id=value["adapter_id"],
                adapter_schema_version=value["adapter_schema_version"],
                implementation_version=value["implementation_version"],
                adapter_digest=value["adapter_digest"],
                action_id=value["action_id"],
                normalized_arguments_json=canonical_json_bytes(value["normalized_arguments"]),
                risk_class=RiskClass(value["risk_class"]),
                security_id=value["security_id"],
                execution_profile=ExecutionProfile(value["execution_profile"]),
                timeout_sec=value["timeout_sec"],
                max_output_bytes=value["max_output_bytes"],
                target_snapshot_json=(
                    canonical_json_bytes(target_snapshot) if target_snapshot is not None else None
                ),
                workspace_grants=tuple(WorkspaceGrant.from_payload(item) for item in raw_grants),
                executable_attestation_digest=value["executable_attestation_digest"],
                created_at=value["created_at"],
                expires_at=value["expires_at"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("host action plan payload is invalid") from exc

    @classmethod
    def from_dict(cls, value: Any) -> HostActionPlan:
        return cls.from_payload(value)

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_payload())


def create_action_plan(
    *,
    plan_id: str,
    actor_user_id: str,
    actor_own_id: str,
    conversation_id: str,
    source_message_id: str,
    host_agent_id: str,
    idempotency_key: str,
    adapter: AdapterSpec,
    action: ActionSpec,
    normalized_arguments: dict[str, Any],
    executable_attestation: ExecutableAttestation,
    target_snapshot: dict[str, Any] | None = None,
    workspace_grants: tuple[WorkspaceGrant, ...] = (),
    continuation_work_item_id: str | None = None,
    now: int | None = None,
    ttl_sec: int = 300,
) -> HostActionPlan:
    """Create a plan from reviewed adapter metadata, never caller risk fields."""

    if action not in adapter.actions:
        raise ContractError("action does not belong to adapter")
    attest_execution(adapter, executable_attestation)
    if isinstance(ttl_sec, bool) or not 1 <= ttl_sec <= 3600:
        raise ContractError("plan TTL is invalid")
    issued = int(time.time()) if now is None else now
    if isinstance(issued, bool) or not isinstance(issued, int):
        raise ContractError("plan issue time is invalid")
    return HostActionPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        plan_id=plan_id,
        actor_user_id=actor_user_id,
        actor_own_id=actor_own_id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        continuation_work_item_id=continuation_work_item_id,
        host_agent_id=host_agent_id,
        idempotency_key=idempotency_key,
        capability_id=action.capability_id,
        adapter_id=adapter.adapter_id,
        adapter_schema_version=adapter.adapter_schema_version,
        implementation_version=adapter.implementation_version,
        adapter_digest=adapter.digest,
        action_id=action.action_id,
        normalized_arguments_json=canonical_json_bytes(normalized_arguments),
        risk_class=action.risk_class,
        security_id=action.security_id,
        execution_profile=action.execution_profile,
        timeout_sec=action.timeout_sec,
        max_output_bytes=action.max_output_bytes,
        target_snapshot_json=canonical_json_bytes(target_snapshot) if target_snapshot is not None else None,
        workspace_grants=workspace_grants,
        executable_attestation_digest=executable_attestation.digest,
        created_at=issued,
        expires_at=issued + ttl_sec,
    )


def assert_plan_current(
    plan: HostActionPlan,
    *,
    adapter: AdapterSpec,
    executable_attestation: ExecutableAttestation,
    target_snapshot: dict[str, Any] | None,
    approved_plan_digest: str,
    now: int | None = None,
) -> None:
    observed_now = int(time.time()) if now is None else now
    if observed_now >= plan.expires_at:
        raise ContractError("host action plan expired")
    if plan.digest != approved_plan_digest:
        raise ContractError("approval is bound to a different host action plan")
    if (
        plan.adapter_id != adapter.adapter_id
        or plan.adapter_schema_version != adapter.adapter_schema_version
        or plan.implementation_version != adapter.implementation_version
        or plan.adapter_digest != adapter.digest
    ):
        raise ContractError("adapter changed after planning")
    if plan.executable_attestation_digest != executable_attestation.digest:
        raise ContractError("executable changed after planning")
    attest_execution(adapter, executable_attestation)
    observed_target_digest = canonical_digest(target_snapshot) if target_snapshot is not None else None
    if plan.target_snapshot_digest != observed_target_digest:
        raise ContractError("network target snapshot changed after planning")


__all__ = ["HostActionPlan", "WorkspaceGrant", "assert_plan_current", "create_action_plan"]
