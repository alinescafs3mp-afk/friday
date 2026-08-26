"""Closed adapter metadata and execution-spec contracts.

Adapters construct an absolute executable plus an ``argv`` tuple.  This module
intentionally contains no runner, shell, environment inheritance, or PATH lookup.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ..contracts import ContractError, ExecutableAttestation, ExecutionProfile, RiskClass, canonical_digest

if TYPE_CHECKING:
    from ..plans import HostActionPlan
    from ..policy import NetworkTargetSnapshot

_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


@dataclass(frozen=True, slots=True)
class PackageRequirement:
    manager: str
    name: str

    def __post_init__(self) -> None:
        if self.manager != "apt" or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]{0,127}", self.name):
            raise ContractError("adapter package requirement is invalid")


@dataclass(frozen=True, slots=True)
class ExecutableRequirement:
    logical_name: str
    package_name: str
    allowed_paths: tuple[str, ...]
    require_sha256: bool = True

    def __post_init__(self) -> None:
        if not _TOKEN.fullmatch(self.logical_name):
            raise ContractError("executable logical name is invalid")
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]{0,127}", self.package_name):
            raise ContractError("executable package owner is invalid")
        if not self.allowed_paths or len(self.allowed_paths) > 8:
            raise ContractError("executable allowed path set is invalid")
        for path in self.allowed_paths:
            if not path.startswith("/") or "\x00" in path or "/../" in f"{path}/" or len(path) > 512:
                raise ContractError("executable allowed path is invalid")


@dataclass(frozen=True, slots=True)
class ActionSpec:
    action_id: str
    capability_id: str
    summary: str
    security_id: str
    risk_class: RiskClass
    execution_profile: ExecutionProfile
    input_schema_id: str
    output_parser_id: str
    timeout_sec: int
    max_output_bytes: int
    approval_required: bool = False

    def __post_init__(self) -> None:
        for value, field in (
            (self.action_id, "action_id"),
            (self.capability_id, "capability_id"),
            (self.security_id, "security_id"),
            (self.input_schema_id, "input_schema_id"),
            (self.output_parser_id, "output_parser_id"),
        ):
            if not _TOKEN.fullmatch(value):
                raise ContractError(f"adapter {field} is invalid")
        if not self.summary or len(self.summary) > 240:
            raise ContractError("adapter action summary is invalid")
        if isinstance(self.timeout_sec, bool) or not 1 <= self.timeout_sec <= 3600:
            raise ContractError("adapter timeout is invalid")
        if isinstance(self.max_output_bytes, bool) or not 1024 <= self.max_output_bytes <= 64 * 1024 * 1024:
            raise ContractError("adapter output cap is invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "approval_required": self.approval_required,
            "capability_id": self.capability_id,
            "execution_profile": self.execution_profile.value,
            "input_schema_id": self.input_schema_id,
            "max_output_bytes": self.max_output_bytes,
            "output_parser_id": self.output_parser_id,
            "risk_class": self.risk_class.value,
            "security_id": self.security_id,
            "summary": self.summary,
            "timeout_sec": self.timeout_sec,
        }


@dataclass(frozen=True, slots=True)
class AdapterSpec:
    adapter_id: str
    adapter_schema_version: int
    implementation_version: int
    summary: str
    categories: tuple[str, ...]
    supported_platforms: tuple[str, ...]
    packages: tuple[PackageRequirement, ...]
    executable: ExecutableRequirement
    actions: tuple[ActionSpec, ...]

    def __post_init__(self) -> None:
        if not _TOKEN.fullmatch(self.adapter_id):
            raise ContractError("adapter id is invalid")
        if self.adapter_schema_version != 1 or self.implementation_version < 1:
            raise ContractError("adapter version is invalid")
        if not self.summary or len(self.summary) > 240:
            raise ContractError("adapter summary is invalid")
        if not self.categories or len(self.categories) > 8:
            raise ContractError("adapter categories are invalid")
        if self.supported_platforms != ("ubuntu",):
            raise ContractError("initial adapter platform contract is ubuntu")
        if not self.packages or not self.actions or len(self.actions) > 16:
            raise ContractError("adapter requirements/actions are empty or oversized")
        if len({item.action_id for item in self.actions}) != len(self.actions):
            raise ContractError("adapter action ids are not unique")

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "actions": [item.to_payload() for item in self.actions],
                "adapter_id": self.adapter_id,
                "adapter_schema_version": self.adapter_schema_version,
                "categories": list(self.categories),
                "executable": {
                    "allowed_paths": list(self.executable.allowed_paths),
                    "logical_name": self.executable.logical_name,
                    "package_name": self.executable.package_name,
                    "require_sha256": self.executable.require_sha256,
                },
                "implementation_version": self.implementation_version,
                "packages": [{"manager": item.manager, "name": item.name} for item in self.packages],
                "summary": self.summary,
                "supported_platforms": list(self.supported_platforms),
            }
        )

    def action(self, action_id: str) -> ActionSpec:
        found = next((item for item in self.actions if item.action_id == action_id), None)
        if found is None:
            raise ContractError("adapter action is not supported")
        return found


@dataclass(frozen=True, slots=True)
class ExecutionSpec:
    executable: str
    argv: tuple[str, ...]
    profile: ExecutionProfile
    timeout_sec: int
    max_output_bytes: int
    working_directory_ref: str = "job_work"
    environment: tuple[tuple[str, str], ...] = (("LC_ALL", "C.UTF-8"),)

    def __post_init__(self) -> None:
        if not self.executable.startswith("/") or "\x00" in self.executable:
            raise ContractError("execution executable is not absolute")
        if not self.argv or self.argv[0] != self.executable or len(self.argv) > 256:
            raise ContractError("execution argv is invalid")
        if any("\x00" in item or len(item) > 4096 for item in self.argv):
            raise ContractError("execution argv item is invalid")
        if sum(len(item.encode("utf-8")) + 1 for item in self.argv) > 128 * 1024:
            raise ContractError("execution argv exceeds byte limit")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.working_directory_ref):
            raise ContractError("execution working-directory reference is invalid")
        allowed_environment = {"LANG", "LC_ALL", "TZ"}
        if len(self.environment) > 8 or any(
            key not in allowed_environment or "\x00" in value or len(value) > 128
            for key, value in self.environment
        ):
            raise ContractError("execution environment is not allowlisted")

    @property
    def argv_sha256(self) -> str:
        framed = b"".join(len(item.encode()).to_bytes(4, "big") + item.encode() for item in self.argv)
        return hashlib.sha256(framed).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "environment": [list(item) for item in self.environment],
            "executable": self.executable,
            "max_output_bytes": self.max_output_bytes,
            "profile": self.profile.value,
            "timeout_sec": self.timeout_sec,
            "working_directory_ref": self.working_directory_ref,
        }

    @classmethod
    def from_payload(cls, value: Any) -> ExecutionSpec:
        expected = {
            "argv",
            "environment",
            "executable",
            "max_output_bytes",
            "profile",
            "timeout_sec",
            "working_directory_ref",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ContractError("execution spec fields are invalid")
        argv = value.get("argv")
        environment = value.get("environment")
        if (
            not isinstance(argv, list)
            or not isinstance(environment, list)
            or any(not isinstance(item, list) or len(item) != 2 for item in environment)
        ):
            raise ContractError("execution spec collections are invalid")
        try:
            return cls(
                executable=value["executable"],
                argv=tuple(argv),
                profile=ExecutionProfile(value["profile"]),
                timeout_sec=value["timeout_sec"],
                max_output_bytes=value["max_output_bytes"],
                working_directory_ref=value["working_directory_ref"],
                environment=tuple((item[0], item[1]) for item in environment),
            )
        except (TypeError, ValueError) as exc:
            raise ContractError("execution spec payload is invalid") from exc


def attest_execution(spec: AdapterSpec, observed: ExecutableAttestation) -> None:
    requirement = spec.executable
    if (
        observed.adapter_id != spec.adapter_id
        or observed.adapter_schema_version != spec.adapter_schema_version
        or observed.implementation_version != spec.implementation_version
    ):
        raise ContractError("executable attestation adapter identity mismatch")
    if observed.canonical_path not in requirement.allowed_paths:
        raise ContractError("executable path is outside adapter allowlist")
    if observed.package_name != requirement.package_name:
        raise ContractError("executable package owner mismatch")
    if observed.owner_uid != 0:
        raise ContractError("package executable is not root-owned")
    if observed.mode & 0o022:
        raise ContractError("package executable is group/world writable")
    if not observed.mode & 0o111:
        raise ContractError("package executable is not executable")
    if requirement.require_sha256 and not observed.sha256:
        raise ContractError("executable digest is required")


def attest_plan(spec: AdapterSpec, plan: HostActionPlan, observed: ExecutableAttestation) -> ActionSpec:
    """Recheck the complete adapter/executable identity at the execution seam."""

    attest_execution(spec, observed)
    action = spec.action(plan.action_id)
    if (
        plan.adapter_id != spec.adapter_id
        or plan.adapter_schema_version != spec.adapter_schema_version
        or plan.implementation_version != spec.implementation_version
        or plan.adapter_digest != spec.digest
        or plan.capability_id != action.capability_id
        or plan.security_id != action.security_id
        or plan.risk_class is not action.risk_class
        or plan.execution_profile is not action.execution_profile
        or plan.timeout_sec != action.timeout_sec
        or plan.max_output_bytes != action.max_output_bytes
    ):
        raise ContractError("host action plan drifted from the reviewed adapter")
    if plan.executable_attestation_digest != observed.digest:
        raise ContractError("host action executable changed after planning")
    return action


class HostAdapter(Protocol):
    spec: AdapterSpec

    def normalize_arguments(
        self,
        action_id: str,
        arguments: dict[str, Any],
        *,
        target_snapshot: NetworkTargetSnapshot | None = None,
    ) -> dict[str, Any]: ...

    def build_execution(
        self,
        plan: HostActionPlan,
        attestation: ExecutableAttestation,
    ) -> ExecutionSpec: ...


__all__ = [
    "ActionSpec",
    "AdapterSpec",
    "ExecutableRequirement",
    "ExecutionSpec",
    "HostAdapter",
    "PackageRequirement",
    "attest_execution",
    "attest_plan",
]
