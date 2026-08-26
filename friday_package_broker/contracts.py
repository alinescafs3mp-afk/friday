"""Closed, bounded contracts for privileged APT planning and receipts."""

from __future__ import annotations

import hmac
import re
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any

from friday.host_control.contracts import (
    MAX_WIRE_BYTES,
    PROTOCOL_VERSION,
    ContractError,
    canonical_digest,
    canonical_json_bytes,
    decode_canonical_json,
)

BROKER_PLAN_SCHEMA_VERSION = 1
BROKER_RECEIPT_SCHEMA_VERSION = 3
BROKER_RECONCILIATION_SCHEMA_VERSION = 1
MAX_PLAN_BYTES = 512 * 1024
MAX_RECEIPT_BYTES = 256 * 1024
MAX_PACKAGE_EVIDENCE_BYTES = 256 * 1024
MAX_PACKAGE_OUTPUT_EVIDENCE_BYTES = 1024 * 1024
MAX_PACKAGE_EVIDENCE_REFS = 8
MAX_SERVICE_UNIT_OBSERVATIONS = 128
EMPTY_PLAN_DIGEST = "0" * 64

_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+:~\-]{0,159}$")
_ARCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,31}$")
_ID = re.compile(r"^[a-z][a-z0-9_]{1,31}_[0-9a-f]{16,64}$")
_ACTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,199}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ED25519_SIGNATURE = re.compile(r"^[0-9a-f]{128}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,199}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EVIDENCE_REF = re.compile(r"^evidence/[0-9a-f]{64}\.(?:json|stderr|stdout)$")
_UNIT_NAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,191}"
    r"\.(?:service|socket|timer|path|mount|automount|target)$"
)


class BrokerContractError(ContractError):
    """A package-broker value is outside the closed protocol."""


class PackageAction(StrEnum):
    INSTALL = "install"
    UPGRADE = "upgrade"
    DOWNGRADE = "downgrade"
    REMOVE = "remove"
    REINSTALL = "reinstall"


class TransactionOutcome(StrEnum):
    COMPLETED = "completed"
    ALREADY_SATISFIED = "already_satisfied"
    FAILED_BEFORE_EFFECT = "failed_before_effect"
    UNKNOWN = "unknown"
    CANCELLED_BEFORE_COMMIT = "cancelled_before_commit"


class PackagePostconditionState(StrEnum):
    DESIRED = "desired"
    PRE_STATE = "pre_state"
    MIXED = "mixed"
    UNAVAILABLE = "unavailable"


class ServiceUnitChange(StrEnum):
    NEWLY_PRESENT = "newly_present"
    ENABLED = "enabled"
    STARTED = "started"
    RESTARTED = "restarted"
    FAILED = "failed"


def _bounded_text(value: object, *, field: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8", errors="strict")) > maximum:
        raise BrokerContractError(f"{field} is invalid")
    if (not allow_empty and not value) or value != value.strip() or "\x00" in value:
        raise BrokerContractError(f"{field} is invalid")
    return value


def _digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise BrokerContractError(f"{field} is invalid")
    return value


def _identifier(value: object, *, field: str, actor: bool = False) -> str:
    pattern = _ACTOR_ID if actor else _ID
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise BrokerContractError(f"{field} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class PackageRef:
    name: str
    version: str | None = None
    architecture: str | None = None

    def __post_init__(self) -> None:
        if _PACKAGE_NAME.fullmatch(self.name) is None:
            raise BrokerContractError("package name is invalid")
        if self.version is not None and _VERSION.fullmatch(self.version) is None:
            raise BrokerContractError("package version is invalid")
        if self.architecture is not None and _ARCH.fullmatch(self.architecture) is None:
            raise BrokerContractError("package architecture is invalid")

    def to_payload(self) -> dict[str, Any]:
        return {"architecture": self.architecture, "name": self.name, "version": self.version}

    @classmethod
    def from_payload(cls, value: Any) -> PackageRef:
        if not isinstance(value, dict) or set(value) - {"name", "version", "architecture"}:
            raise BrokerContractError("package reference fields are invalid")
        if "name" not in value:
            raise BrokerContractError("package reference name is missing")
        try:
            return cls(
                name=value["name"],
                version=value.get("version"),
                architecture=value.get("architecture"),
            )
        except TypeError as exc:
            raise BrokerContractError("package reference field types are invalid") from exc


@dataclass(frozen=True, slots=True)
class RepositoryOrigin:
    origin: str
    label: str
    archive: str
    site: str
    component: str
    trusted: bool

    def __post_init__(self) -> None:
        for field in ("origin", "label", "archive", "site", "component"):
            _bounded_text(getattr(self, field), field=f"repository {field}", maximum=160, allow_empty=True)
        if not isinstance(self.trusted, bool):
            raise BrokerContractError("repository trust marker is invalid")

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: Any) -> RepositoryOrigin:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise BrokerContractError("repository origin fields are invalid")
        try:
            return cls(**value)
        except TypeError as exc:
            raise BrokerContractError("repository origin field types are invalid") from exc


@dataclass(frozen=True, slots=True)
class PackageChange:
    action: PackageAction
    name: str
    architecture: str
    from_version: str | None
    to_version: str | None
    download_bytes: int
    installed_delta_bytes: int
    archive_sha256: str | None
    origins: tuple[RepositoryOrigin, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.action, PackageAction):
            raise BrokerContractError("package action is invalid")
        PackageRef(self.name, self.to_version or self.from_version, self.architecture)
        if self.action is PackageAction.REMOVE:
            if self.from_version is None or self.to_version is not None:
                raise BrokerContractError("remove change versions are inconsistent")
            if self.archive_sha256 is not None:
                raise BrokerContractError("remove change cannot name an archive digest")
        elif self.action is PackageAction.INSTALL:
            if self.from_version is not None or self.to_version is None:
                raise BrokerContractError("install change versions are inconsistent")
        elif self.from_version is None or self.to_version is None:
            raise BrokerContractError("package change versions are incomplete")
        elif self.action is PackageAction.REINSTALL and self.from_version != self.to_version:
            raise BrokerContractError("reinstall must preserve the exact package version")
        elif (
            self.action in {PackageAction.UPGRADE, PackageAction.DOWNGRADE}
            and self.from_version == self.to_version
        ):
            raise BrokerContractError("version-changing package action preserves its version")
        if self.action is not PackageAction.REMOVE:
            _digest(self.archive_sha256, field="package archive digest")
        for value, field, maximum in (
            (self.download_bytes, "download bytes", 2**40),
            (self.installed_delta_bytes, "installed delta", 2**40),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not -maximum <= value <= maximum:
                raise BrokerContractError(f"package {field} is invalid")
        if self.download_bytes < 0:
            raise BrokerContractError("package download bytes cannot be negative")
        if (
            not isinstance(self.origins, tuple)
            or len(self.origins) > 16
            or any(not isinstance(item, RepositoryOrigin) for item in self.origins)
        ):
            raise BrokerContractError("package origin set is oversized")
        if self.action is not PackageAction.REMOVE and not self.origins:
            raise BrokerContractError("package candidate lacks configured origins")

    def to_payload(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "architecture": self.architecture,
            "archive_sha256": self.archive_sha256,
            "download_bytes": self.download_bytes,
            "from_version": self.from_version,
            "installed_delta_bytes": self.installed_delta_bytes,
            "name": self.name,
            "origins": [item.to_payload() for item in self.origins],
            "to_version": self.to_version,
        }

    @classmethod
    def from_payload(cls, value: Any) -> PackageChange:
        expected = {
            "action",
            "architecture",
            "archive_sha256",
            "download_bytes",
            "from_version",
            "installed_delta_bytes",
            "name",
            "origins",
            "to_version",
        }
        if not isinstance(value, dict) or set(value) != expected or not isinstance(value["origins"], list):
            raise BrokerContractError("package change fields are invalid")
        try:
            return cls(
                action=PackageAction(value["action"]),
                name=value["name"],
                architecture=value["architecture"],
                archive_sha256=value["archive_sha256"],
                from_version=value["from_version"],
                to_version=value["to_version"],
                download_bytes=value["download_bytes"],
                installed_delta_bytes=value["installed_delta_bytes"],
                origins=tuple(RepositoryOrigin.from_payload(item) for item in value["origins"]),
            )
        except (TypeError, ValueError) as exc:
            raise BrokerContractError("package change field types are invalid") from exc


@dataclass(frozen=True, slots=True)
class AptTransaction:
    schema_version: int
    requested: tuple[PackageRef, ...]
    changes: tuple[PackageChange, ...]
    download_bytes: int
    installed_delta_bytes: int
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise BrokerContractError("unknown APT transaction schema")
        if (
            not isinstance(self.requested, tuple)
            or not self.requested
            or len(self.requested) > 16
            or any(not isinstance(item, PackageRef) for item in self.requested)
        ):
            raise BrokerContractError("APT requested package set is invalid")
        if any(item.version is None or item.architecture is None for item in self.requested):
            raise BrokerContractError("APT transaction requests must be exactly resolved")
        requested_keys = {(item.name, item.architecture) for item in self.requested}
        if len(requested_keys) != len(self.requested):
            raise BrokerContractError("APT requested package set contains duplicates")
        if (
            not isinstance(self.changes, tuple)
            or len(self.changes) > 256
            or any(not isinstance(item, PackageChange) for item in self.changes)
        ):
            raise BrokerContractError("APT transaction change set is oversized")
        change_keys = {(item.name, item.architecture) for item in self.changes}
        if len(change_keys) != len(self.changes):
            raise BrokerContractError("APT transaction change set contains duplicates")
        if isinstance(self.download_bytes, bool) or not 0 <= self.download_bytes <= 2**40:
            raise BrokerContractError("APT download size is invalid")
        if (
            isinstance(self.installed_delta_bytes, bool)
            or not -(2**40) <= self.installed_delta_bytes <= 2**40
        ):
            raise BrokerContractError("APT disk delta is invalid")
        if (
            not isinstance(self.warnings, tuple)
            or len(self.warnings) > 32
            or any(
                not isinstance(item, str) or not item or len(item.encode("utf-8")) > 240 or "\x00" in item
                for item in self.warnings
            )
        ):
            raise BrokerContractError("APT warning set is invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "changes": [item.to_payload() for item in self.changes],
            "download_bytes": self.download_bytes,
            "installed_delta_bytes": self.installed_delta_bytes,
            "manager": "apt",
            "requested": [item.to_payload() for item in self.requested],
            "schema_version": self.schema_version,
            "warnings": list(self.warnings),
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_payload())

    @classmethod
    def from_payload(cls, value: Any) -> AptTransaction:
        expected = {
            "changes",
            "download_bytes",
            "installed_delta_bytes",
            "manager",
            "requested",
            "schema_version",
            "warnings",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value.get("manager") != "apt"
            or not isinstance(value["changes"], list)
            or not isinstance(value["requested"], list)
            or not isinstance(value["warnings"], list)
        ):
            raise BrokerContractError("APT transaction fields are invalid")
        try:
            return cls(
                schema_version=value["schema_version"],
                requested=tuple(PackageRef.from_payload(item) for item in value["requested"]),
                changes=tuple(PackageChange.from_payload(item) for item in value["changes"]),
                download_bytes=value["download_bytes"],
                installed_delta_bytes=value["installed_delta_bytes"],
                warnings=tuple(value["warnings"]),
            )
        except TypeError as exc:
            raise BrokerContractError("APT transaction field types are invalid") from exc


@dataclass(frozen=True, slots=True)
class AptInstallPlan:
    schema_version: int
    plan_id: str
    broker_id: str
    actor_user_id: str
    actor_own_id: str
    original_task_ref: str
    continuation_work_item_id: str
    transaction: AptTransaction
    created_at: int
    expires_at: int

    def __post_init__(self) -> None:
        if self.schema_version != BROKER_PLAN_SCHEMA_VERSION:
            raise BrokerContractError("unknown package plan schema")
        _identifier(self.plan_id, field="package plan id")
        _bounded_text(self.broker_id, field="broker id", maximum=128)
        _identifier(self.actor_user_id, field="actor user id", actor=True)
        _identifier(self.actor_own_id, field="actor own id", actor=True)
        if not isinstance(self.transaction, AptTransaction):
            raise BrokerContractError("package plan transaction is invalid")
        if _REF.fullmatch(self.original_task_ref) is None:
            raise BrokerContractError("original task reference is invalid")
        if _REF.fullmatch(self.continuation_work_item_id) is None:
            raise BrokerContractError("continuation work item reference is invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (self.created_at, self.expires_at)
        ):
            raise BrokerContractError("package plan timestamps are invalid")
        if not self.created_at < self.expires_at <= self.created_at + 3600:
            raise BrokerContractError("package plan expiry is invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "actor_own_id": self.actor_own_id,
            "actor_user_id": self.actor_user_id,
            "broker_id": self.broker_id,
            "continuation_work_item_id": self.continuation_work_item_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "original_task_ref": self.original_task_ref,
            "plan_id": self.plan_id,
            "schema_version": self.schema_version,
            "transaction": self.transaction.to_payload(),
            "transaction_digest": self.transaction.digest,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_payload())

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload(), maximum=MAX_PLAN_BYTES)

    @classmethod
    def from_payload(cls, value: Any) -> AptInstallPlan:
        expected = {
            "actor_own_id",
            "actor_user_id",
            "broker_id",
            "continuation_work_item_id",
            "created_at",
            "expires_at",
            "original_task_ref",
            "plan_id",
            "schema_version",
            "transaction",
            "transaction_digest",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise BrokerContractError("package plan fields are invalid")
        transaction = AptTransaction.from_payload(value["transaction"])
        if not hmac.compare_digest(
            transaction.digest, _digest(value["transaction_digest"], field="transaction digest")
        ):
            raise BrokerContractError("package transaction digest does not match its body")
        try:
            return cls(
                schema_version=value["schema_version"],
                plan_id=value["plan_id"],
                broker_id=value["broker_id"],
                actor_user_id=value["actor_user_id"],
                actor_own_id=value["actor_own_id"],
                original_task_ref=value["original_task_ref"],
                continuation_work_item_id=value["continuation_work_item_id"],
                transaction=transaction,
                created_at=value["created_at"],
                expires_at=value["expires_at"],
            )
        except TypeError as exc:
            raise BrokerContractError("package plan field types are invalid") from exc

    @classmethod
    def from_canonical_bytes(cls, value: bytes) -> AptInstallPlan:
        return cls.from_payload(decode_canonical_json(value, maximum=MAX_PLAN_BYTES))


@dataclass(frozen=True, slots=True)
class InstalledPackage:
    name: str
    version: str
    architecture: str

    def __post_init__(self) -> None:
        PackageRef(self.name, self.version, self.architecture)

    def to_payload(self) -> dict[str, str]:
        return {"architecture": self.architecture, "name": self.name, "version": self.version}

    @classmethod
    def from_payload(cls, value: Any) -> InstalledPackage:
        if not isinstance(value, dict) or set(value) != {"architecture", "name", "version"}:
            raise BrokerContractError("installed package fields are invalid")
        try:
            return cls(**value)
        except TypeError as exc:
            raise BrokerContractError("installed package field types are invalid") from exc


@dataclass(frozen=True, slots=True)
class PackageEvidenceReference:
    """Content-addressed broker-local evidence; raw bytes are never embedded."""

    kind: str
    ref: str
    sha256: str
    size_bytes: int
    media_type: str = "application/json"

    def __post_init__(self) -> None:
        formats = {
            "apt_dpkg_transaction": ("json", "application/json", 1, MAX_PACKAGE_EVIDENCE_BYTES),
            "apt_stderr": (
                "stderr",
                "application/octet-stream",
                0,
                MAX_PACKAGE_OUTPUT_EVIDENCE_BYTES,
            ),
            "apt_stdout": (
                "stdout",
                "application/octet-stream",
                0,
                MAX_PACKAGE_OUTPUT_EVIDENCE_BYTES,
            ),
        }
        if not isinstance(self.kind, str) or self.kind not in formats:
            raise BrokerContractError("package evidence kind is invalid")
        if not isinstance(self.ref, str) or _EVIDENCE_REF.fullmatch(self.ref) is None:
            raise BrokerContractError("package evidence reference is invalid")
        _digest(self.sha256, field="package evidence digest")
        extension, media_type, minimum, maximum = formats[self.kind]
        if self.ref != f"evidence/{self.sha256}.{extension}":
            raise BrokerContractError("package evidence reference is not content-addressed")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not minimum <= self.size_bytes <= maximum
        ):
            raise BrokerContractError("package evidence size is invalid")
        if self.media_type != media_type:
            raise BrokerContractError("package evidence media type is invalid")

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: Any) -> PackageEvidenceReference:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise BrokerContractError("package evidence fields are invalid")
        try:
            return cls(**value)
        except TypeError as exc:
            raise BrokerContractError("package evidence field types are invalid") from exc


_LOAD_STATES = frozenset(
    {"bad_setting", "error", "loaded", "masked", "merged", "not_found", "stub", "unknown"}
)
_UNIT_FILE_STATES = frozenset(
    {
        "alias",
        "bad",
        "disabled",
        "enabled",
        "enabled_runtime",
        "generated",
        "indirect",
        "linked",
        "linked_runtime",
        "masked",
        "masked_runtime",
        "static",
        "transient",
        "unknown",
    }
)
_ACTIVE_STATES = frozenset(
    {
        "active",
        "activating",
        "deactivating",
        "failed",
        "inactive",
        "maintenance",
        "refreshing",
        "reloading",
        "unknown",
    }
)
_SUB_STATE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")


@dataclass(frozen=True, slots=True)
class ServiceUnitState:
    load_state: str
    unit_file_state: str
    active_state: str
    sub_state: str
    active_enter_timestamp_monotonic: int

    def __post_init__(self) -> None:
        if self.load_state not in _LOAD_STATES:
            raise BrokerContractError("service unit load state is invalid")
        if self.unit_file_state not in _UNIT_FILE_STATES:
            raise BrokerContractError("service unit file state is invalid")
        if self.active_state not in _ACTIVE_STATES:
            raise BrokerContractError("service unit active state is invalid")
        if not isinstance(self.sub_state, str) or _SUB_STATE.fullmatch(self.sub_state) is None:
            raise BrokerContractError("service unit sub-state is invalid")
        if (
            isinstance(self.active_enter_timestamp_monotonic, bool)
            or not isinstance(self.active_enter_timestamp_monotonic, int)
            or not 0 <= self.active_enter_timestamp_monotonic <= 2**63 - 1
        ):
            raise BrokerContractError("service unit timestamp is invalid")

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: Any) -> ServiceUnitState:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise BrokerContractError("service unit state fields are invalid")
        try:
            return cls(**value)
        except TypeError as exc:
            raise BrokerContractError("service unit state field types are invalid") from exc


@dataclass(frozen=True, slots=True)
class ServiceUnitObservation:
    package_name: str
    package_architecture: str
    unit_name: str
    before: ServiceUnitState | None
    after: ServiceUnitState | None
    changes: tuple[ServiceUnitChange, ...]

    def __post_init__(self) -> None:
        PackageRef(self.package_name, architecture=self.package_architecture)
        if not isinstance(self.unit_name, str) or _UNIT_NAME.fullmatch(self.unit_name) is None:
            raise BrokerContractError("service unit name is invalid")
        if self.before is not None and not isinstance(self.before, ServiceUnitState):
            raise BrokerContractError("service unit before-state is invalid")
        if self.after is not None and not isinstance(self.after, ServiceUnitState):
            raise BrokerContractError("service unit after-state is invalid")
        if self.before is None and self.after is None:
            raise BrokerContractError("service unit observation has no state")
        if (
            not isinstance(self.changes, tuple)
            or not self.changes
            or len(self.changes) > len(ServiceUnitChange)
            or any(not isinstance(item, ServiceUnitChange) for item in self.changes)
            or len(set(self.changes)) != len(self.changes)
            or tuple(sorted(self.changes, key=lambda item: item.value)) != self.changes
        ):
            raise BrokerContractError("service unit change set is invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "after": None if self.after is None else self.after.to_payload(),
            "before": None if self.before is None else self.before.to_payload(),
            "changes": [item.value for item in self.changes],
            "package_architecture": self.package_architecture,
            "package_name": self.package_name,
            "unit_name": self.unit_name,
        }

    @classmethod
    def from_payload(cls, value: Any) -> ServiceUnitObservation:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise BrokerContractError("service unit observation fields are invalid")
        if not isinstance(value["changes"], list):
            raise BrokerContractError("service unit observation changes are invalid")
        try:
            return cls(
                package_name=value["package_name"],
                package_architecture=value["package_architecture"],
                unit_name=value["unit_name"],
                before=(None if value["before"] is None else ServiceUnitState.from_payload(value["before"])),
                after=None if value["after"] is None else ServiceUnitState.from_payload(value["after"]),
                changes=tuple(ServiceUnitChange(item) for item in value["changes"]),
            )
        except (TypeError, ValueError) as exc:
            raise BrokerContractError("service unit observation field types are invalid") from exc


@dataclass(frozen=True, slots=True)
class PackageTransactionReceipt:
    schema_version: int
    protocol_version: str
    broker_id: str
    broker_build_id: str
    package_manager: str
    package_manager_version: str
    transaction_id: str
    plan_id: str
    approved_plan_digest: str
    executed_transaction_digest: str
    approval_receipt_id: str
    idempotency_key: str
    outcome: TransactionOutcome
    effect_boundary_crossed: bool
    started_at: int
    finished_at: int
    exit_code: int | None
    lock_state: str
    before: tuple[InstalledPackage, ...]
    after: tuple[InstalledPackage, ...]
    output_capture_status: str
    stdout_sha256: str | None
    stdout_size_bytes: int | None
    stderr_sha256: str | None
    stderr_size_bytes: int | None
    output_truncated: bool
    reboot_required: bool
    stdout_total_size_bytes: int | None = None
    stderr_total_size_bytes: int | None = None
    stdout_total_size_complete: bool = False
    stderr_total_size_complete: bool = False
    evidence_refs: tuple[PackageEvidenceReference, ...] = ()
    service_unit_observation_status: str = "unavailable"
    service_unit_observations: tuple[ServiceUnitObservation, ...] = ()
    error_code: str | None = None
    signature: str = ""

    def __post_init__(self) -> None:
        if self.schema_version not in {1, 2, BROKER_RECEIPT_SCHEMA_VERSION} or (
            self.protocol_version != PROTOCOL_VERSION
        ):
            raise BrokerContractError("package receipt version is invalid")
        for value, field in (
            (self.transaction_id, "transaction id"),
            (self.plan_id, "plan id"),
        ):
            _identifier(value, field=field)
        _bounded_text(self.broker_id, field="broker id", maximum=128)
        _bounded_text(self.broker_build_id, field="broker build id", maximum=160)
        if self.package_manager != "apt":
            raise BrokerContractError("package receipt manager is invalid")
        _bounded_text(self.package_manager_version, field="package manager version", maximum=160)
        if _REF.fullmatch(self.approval_receipt_id) is None or _REF.fullmatch(self.idempotency_key) is None:
            raise BrokerContractError("package receipt approval/idempotency reference is invalid")
        _digest(self.approved_plan_digest, field="approved plan digest")
        _digest(self.executed_transaction_digest, field="executed transaction digest")
        if not isinstance(self.outcome, TransactionOutcome):
            raise BrokerContractError("package receipt outcome is invalid")
        if not isinstance(self.effect_boundary_crossed, bool):
            raise BrokerContractError("package receipt effect marker is invalid")
        if (
            self.outcome
            in {
                TransactionOutcome.ALREADY_SATISFIED,
                TransactionOutcome.FAILED_BEFORE_EFFECT,
                TransactionOutcome.CANCELLED_BEFORE_COMMIT,
            }
            and self.effect_boundary_crossed
        ):
            raise BrokerContractError("package receipt outcome contradicts its effect marker")
        if self.outcome is TransactionOutcome.COMPLETED and not self.effect_boundary_crossed:
            raise BrokerContractError("completed package receipt lacks its effect marker")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (self.started_at, self.finished_at)
        ):
            raise BrokerContractError("package receipt numeric fields are invalid")
        if self.started_at > self.finished_at:
            raise BrokerContractError("package receipt timing is invalid")
        if self.output_capture_status not in {"captured", "not_applicable", "unavailable"}:
            raise BrokerContractError("package receipt output capture status is invalid")
        if self.output_capture_status != "captured":
            if (
                any(
                    value is not None
                    for value in (
                        self.stdout_sha256,
                        self.stdout_size_bytes,
                        self.stdout_total_size_bytes,
                        self.stderr_sha256,
                        self.stderr_size_bytes,
                        self.stderr_total_size_bytes,
                    )
                )
                or self.stdout_total_size_complete
                or self.stderr_total_size_complete
                or self.output_truncated
            ):
                raise BrokerContractError("uncaptured package output cannot claim evidence")
        else:
            _digest(self.stdout_sha256, field="stdout digest")
            _digest(self.stderr_sha256, field="stderr digest")
            for size_value, field in (
                (self.stdout_size_bytes, "stdout"),
                (self.stderr_size_bytes, "stderr"),
            ):
                if (
                    isinstance(size_value, bool)
                    or not isinstance(size_value, int)
                    or not 0 <= size_value <= MAX_PACKAGE_OUTPUT_EVIDENCE_BYTES
                ):
                    raise BrokerContractError(f"package receipt {field} size is invalid")
            for retained, total, complete, field in (
                (
                    self.stdout_size_bytes,
                    self.stdout_total_size_bytes,
                    self.stdout_total_size_complete,
                    "stdout",
                ),
                (
                    self.stderr_size_bytes,
                    self.stderr_total_size_bytes,
                    self.stderr_total_size_complete,
                    "stderr",
                ),
            ):
                if self.schema_version < 3:
                    if total is not None or complete:
                        raise BrokerContractError("legacy package receipt contains v3 output totals")
                    continue
                if (
                    isinstance(retained, bool)
                    or not isinstance(retained, int)
                    or isinstance(total, bool)
                    or not isinstance(total, int)
                    or not isinstance(complete, bool)
                    or total < retained
                    or total > 2**63 - 1
                ):
                    raise BrokerContractError(f"package receipt {field} total size is invalid")
                if (not complete or total > retained) and not self.output_truncated:
                    raise BrokerContractError("package receipt output completeness is contradictory")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not -255 <= self.exit_code <= 255
        ):
            raise BrokerContractError("package receipt exit code is invalid")
        if self.lock_state not in {"not_started", "held", "released", "unknown"}:
            raise BrokerContractError("package receipt lock state is invalid")
        if (
            not isinstance(self.before, tuple)
            or not isinstance(self.after, tuple)
            or len(self.before) > 256
            or len(self.after) > 256
            or any(not isinstance(item, InstalledPackage) for item in self.before + self.after)
        ):
            raise BrokerContractError("package receipt state snapshot is oversized")
        for snapshot in (self.before, self.after):
            keys = {(item.name, item.architecture) for item in snapshot}
            if len(keys) != len(snapshot):
                raise BrokerContractError("package receipt state snapshot contains duplicates")
        if not isinstance(self.output_truncated, bool) or not isinstance(self.reboot_required, bool):
            raise BrokerContractError("package receipt boolean fields are invalid")
        if (
            not isinstance(self.evidence_refs, tuple)
            or len(self.evidence_refs) > MAX_PACKAGE_EVIDENCE_REFS
            or any(not isinstance(item, PackageEvidenceReference) for item in self.evidence_refs)
            or len({item.ref for item in self.evidence_refs}) != len(self.evidence_refs)
        ):
            raise BrokerContractError("package receipt evidence references are invalid")
        if self.service_unit_observation_status not in {
            "captured",
            "not_applicable",
            "partial",
            "unavailable",
        }:
            raise BrokerContractError("package receipt service observation status is invalid")
        if (
            not isinstance(self.service_unit_observations, tuple)
            or len(self.service_unit_observations) > MAX_SERVICE_UNIT_OBSERVATIONS
            or any(not isinstance(item, ServiceUnitObservation) for item in self.service_unit_observations)
        ):
            raise BrokerContractError("package receipt service observations are invalid")
        observation_keys = {
            (item.package_name, item.package_architecture, item.unit_name)
            for item in self.service_unit_observations
        }
        if len(observation_keys) != len(self.service_unit_observations):
            raise BrokerContractError("package receipt service observations contain duplicates")
        if self.service_unit_observations and self.service_unit_observation_status not in {
            "captured",
            "partial",
        }:
            raise BrokerContractError("package receipt service observations contradict capture status")
        if self.schema_version in {1, 2} and (
            self.stdout_total_size_bytes is not None
            or self.stderr_total_size_bytes is not None
            or self.stdout_total_size_complete
            or self.stderr_total_size_complete
        ):
            raise BrokerContractError("legacy package receipt contains v3 output totals")
        if self.schema_version == 1:
            if (
                self.evidence_refs
                or self.service_unit_observations
                or self.service_unit_observation_status != "unavailable"
            ):
                raise BrokerContractError("legacy package receipt contains v2 evidence")
        elif self.schema_version == 2 and self.outcome is TransactionOutcome.COMPLETED:
            if self.output_capture_status != "captured" or not self.evidence_refs:
                raise BrokerContractError("completed package receipt lacks bounded APT evidence")
            if self.service_unit_observation_status not in {"captured", "partial"}:
                raise BrokerContractError("completed package receipt lacks service observations")
        elif self.schema_version == 3:
            evidence_by_kind = {item.kind: item for item in self.evidence_refs}
            expected_kinds = {"apt_dpkg_transaction", "apt_stderr", "apt_stdout"}
            if self.evidence_refs and (
                set(evidence_by_kind) != expected_kinds or len(evidence_by_kind) != len(self.evidence_refs)
            ):
                raise BrokerContractError("package receipt has incomplete raw-output evidence")
            if evidence_by_kind:
                stdout_ref = evidence_by_kind["apt_stdout"]
                stderr_ref = evidence_by_kind["apt_stderr"]
                if (
                    stdout_ref.sha256 != self.stdout_sha256
                    or stdout_ref.size_bytes != self.stdout_size_bytes
                    or stderr_ref.sha256 != self.stderr_sha256
                    or stderr_ref.size_bytes != self.stderr_size_bytes
                ):
                    raise BrokerContractError("package receipt raw-output evidence mismatches capture")
            if self.outcome is TransactionOutcome.COMPLETED:
                if self.output_capture_status != "captured" or not evidence_by_kind:
                    raise BrokerContractError("completed package receipt lacks bounded APT evidence")
                if self.service_unit_observation_status not in {"captured", "partial"}:
                    raise BrokerContractError("completed package receipt lacks service observations")
        if self.error_code is not None and _ERROR_CODE.fullmatch(self.error_code) is None:
            raise BrokerContractError("package receipt error code is invalid")
        if self.signature and _ED25519_SIGNATURE.fullmatch(self.signature) is None:
            raise BrokerContractError("package receipt signature is invalid")

    def unsigned_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature")
        payload["outcome"] = self.outcome.value
        payload["before"] = [item.to_payload() for item in self.before]
        payload["after"] = [item.to_payload() for item in self.after]
        if self.schema_version < 3:
            payload.pop("stdout_total_size_bytes")
            payload.pop("stderr_total_size_bytes")
            payload.pop("stdout_total_size_complete")
            payload.pop("stderr_total_size_complete")
        if self.schema_version == 1:
            payload.pop("evidence_refs")
            payload.pop("service_unit_observation_status")
            payload.pop("service_unit_observations")
        else:
            payload["evidence_refs"] = [item.to_payload() for item in self.evidence_refs]
            payload["service_unit_observations"] = [
                item.to_payload() for item in self.service_unit_observations
            ]
        return payload

    def to_payload(self) -> dict[str, Any]:
        return {**self.unsigned_payload(), "signature": self.signature}

    def canonical_bytes_for_signing(self) -> bytes:
        return canonical_json_bytes(self.unsigned_payload(), maximum=MAX_RECEIPT_BYTES)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload(), maximum=MAX_RECEIPT_BYTES)

    @classmethod
    def from_payload(cls, value: Any) -> PackageTransactionReceipt:
        if not isinstance(value, dict):
            raise BrokerContractError("package receipt fields are invalid")
        version = value.get("schema_version")
        expected = set(cls.__dataclass_fields__)
        if version in {1, 2}:
            expected -= {
                "stderr_total_size_bytes",
                "stderr_total_size_complete",
                "stdout_total_size_bytes",
                "stdout_total_size_complete",
            }
        if version == 1:
            expected -= {
                "evidence_refs",
                "service_unit_observation_status",
                "service_unit_observations",
            }
        if set(value) != expected:
            raise BrokerContractError("package receipt fields are invalid")
        if not isinstance(value["before"], list) or not isinstance(value["after"], list):
            raise BrokerContractError("package receipt snapshots are invalid")
        if version != 1 and (
            not isinstance(value.get("evidence_refs"), list)
            or not isinstance(value.get("service_unit_observations"), list)
        ):
            raise BrokerContractError("package receipt evidence fields are invalid")
        try:
            return cls(
                **{
                    **value,
                    "outcome": TransactionOutcome(value["outcome"]),
                    "before": tuple(InstalledPackage.from_payload(item) for item in value["before"]),
                    "after": tuple(InstalledPackage.from_payload(item) for item in value["after"]),
                    "stdout_total_size_bytes": (
                        None if version in {1, 2} else value["stdout_total_size_bytes"]
                    ),
                    "stderr_total_size_bytes": (
                        None if version in {1, 2} else value["stderr_total_size_bytes"]
                    ),
                    "stdout_total_size_complete": (
                        False if version in {1, 2} else value["stdout_total_size_complete"]
                    ),
                    "stderr_total_size_complete": (
                        False if version in {1, 2} else value["stderr_total_size_complete"]
                    ),
                    "evidence_refs": (
                        ()
                        if version == 1
                        else tuple(
                            PackageEvidenceReference.from_payload(item) for item in value["evidence_refs"]
                        )
                    ),
                    "service_unit_observation_status": (
                        "unavailable" if version == 1 else value["service_unit_observation_status"]
                    ),
                    "service_unit_observations": (
                        ()
                        if version == 1
                        else tuple(
                            ServiceUnitObservation.from_payload(item)
                            for item in value["service_unit_observations"]
                        )
                    ),
                }
            )
        except (TypeError, ValueError) as exc:
            raise BrokerContractError("package receipt field types are invalid") from exc

    def with_signature(self, signature: str) -> PackageTransactionReceipt:
        return replace(self, signature=signature)


@dataclass(frozen=True, slots=True)
class PackageReconciliationReceipt:
    """Signed read-only state evidence; never a claim about transaction outcome."""

    schema_version: int
    protocol_version: str
    broker_id: str
    broker_build_id: str
    reconciliation_id: str
    transaction_id: str
    plan_id: str
    plan_digest: str
    transaction_digest: str
    approval_receipt_id: str
    actor_user_id: str
    actor_own_id: str
    continuation_work_item_id: str
    reconciliation_idempotency_key: str
    transaction_outcome: TransactionOutcome
    postcondition_state: PackagePostconditionState
    postcondition_satisfied: bool
    safe_to_replan: bool
    observed_at: int
    installed: tuple[InstalledPackage, ...]
    error_code: str | None = None
    signature: str = ""

    def __post_init__(self) -> None:
        if (
            self.schema_version != BROKER_RECONCILIATION_SCHEMA_VERSION
            or self.protocol_version != PROTOCOL_VERSION
        ):
            raise BrokerContractError("package reconciliation version is invalid")
        _bounded_text(self.broker_id, field="broker id", maximum=128)
        _bounded_text(self.broker_build_id, field="broker build id", maximum=160)
        _identifier(self.reconciliation_id, field="package reconciliation id")
        _identifier(self.transaction_id, field="package transaction id")
        _identifier(self.plan_id, field="package plan id")
        _digest(self.plan_digest, field="package plan digest")
        _digest(self.transaction_digest, field="package transaction digest")
        if _REF.fullmatch(self.approval_receipt_id) is None:
            raise BrokerContractError("package reconciliation approval reference is invalid")
        _identifier(self.actor_user_id, field="actor user id", actor=True)
        _identifier(self.actor_own_id, field="actor own id", actor=True)
        if (
            _REF.fullmatch(self.continuation_work_item_id) is None
            or _REF.fullmatch(self.reconciliation_idempotency_key) is None
        ):
            raise BrokerContractError("package reconciliation continuation binding is invalid")
        if self.transaction_outcome is not TransactionOutcome.UNKNOWN:
            raise BrokerContractError("package reconciliation cannot resolve transaction outcome")
        if not isinstance(self.postcondition_state, PackagePostconditionState):
            raise BrokerContractError("package reconciliation postcondition state is invalid")
        if not isinstance(self.postcondition_satisfied, bool) or not isinstance(self.safe_to_replan, bool):
            raise BrokerContractError("package reconciliation booleans are invalid")
        if self.postcondition_satisfied != (
            self.postcondition_state is PackagePostconditionState.DESIRED
        ) or self.safe_to_replan != (self.postcondition_state is PackagePostconditionState.PRE_STATE):
            raise BrokerContractError("package reconciliation claims contradictory postconditions")
        if isinstance(self.observed_at, bool) or not isinstance(self.observed_at, int):
            raise BrokerContractError("package reconciliation observation time is invalid")
        if (
            not isinstance(self.installed, tuple)
            or len(self.installed) > 256
            or any(not isinstance(item, InstalledPackage) for item in self.installed)
            or len({(item.name, item.architecture) for item in self.installed}) != len(self.installed)
        ):
            raise BrokerContractError("package reconciliation snapshot is invalid")
        expected_error = {
            PackagePostconditionState.DESIRED: None,
            PackagePostconditionState.PRE_STATE: None,
            PackagePostconditionState.MIXED: "package_state_mixed",
            PackagePostconditionState.UNAVAILABLE: "package_state_unavailable",
        }[self.postcondition_state]
        if self.error_code != expected_error:
            raise BrokerContractError("package reconciliation error state is inconsistent")
        if self.signature and _ED25519_SIGNATURE.fullmatch(self.signature) is None:
            raise BrokerContractError("package reconciliation signature is invalid")

    def unsigned_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature")
        payload["installed"] = [item.to_payload() for item in self.installed]
        payload["postcondition_state"] = self.postcondition_state.value
        payload["transaction_outcome"] = self.transaction_outcome.value
        return payload

    def to_payload(self) -> dict[str, Any]:
        return {**self.unsigned_payload(), "signature": self.signature}

    def canonical_bytes_for_signing(self) -> bytes:
        return canonical_json_bytes(self.unsigned_payload(), maximum=MAX_RECEIPT_BYTES)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload(), maximum=MAX_RECEIPT_BYTES)

    @classmethod
    def from_payload(cls, value: Any) -> PackageReconciliationReceipt:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise BrokerContractError("package reconciliation fields are invalid")
        if not isinstance(value["installed"], list):
            raise BrokerContractError("package reconciliation snapshot is invalid")
        try:
            return cls(
                **{
                    **value,
                    "installed": tuple(InstalledPackage.from_payload(item) for item in value["installed"]),
                    "postcondition_state": PackagePostconditionState(value["postcondition_state"]),
                    "transaction_outcome": TransactionOutcome(value["transaction_outcome"]),
                }
            )
        except (TypeError, ValueError) as exc:
            raise BrokerContractError("package reconciliation field types are invalid") from exc

    def with_signature(self, signature: str) -> PackageReconciliationReceipt:
        return replace(self, signature=signature)


@dataclass(frozen=True, slots=True)
class BrokerWireResponse:
    response_schema_version: int
    protocol_version: str
    broker_id: str
    build_id: str
    request_id: str
    server_time: int
    ok: bool
    result_json: bytes
    signature: str = ""

    def __post_init__(self) -> None:
        if self.response_schema_version != 1 or self.protocol_version != PROTOCOL_VERSION:
            raise BrokerContractError("broker response version is invalid")
        _bounded_text(self.broker_id, field="response broker id", maximum=128)
        _bounded_text(self.build_id, field="response build id", maximum=160)
        if _WIRE_ID.fullmatch(self.request_id) is None:
            raise BrokerContractError("broker response request id is invalid")
        if isinstance(self.server_time, bool) or not isinstance(self.server_time, int):
            raise BrokerContractError("broker response time is invalid")
        if not isinstance(self.ok, bool):
            raise BrokerContractError("broker response outcome is invalid")
        result = decode_canonical_json(self.result_json, maximum=MAX_WIRE_BYTES)
        if not isinstance(result, dict):
            raise BrokerContractError("broker response result must be an object")
        if not self.ok and (
            set(result) != {"error_code"}
            or not isinstance(result["error_code"], str)
            or _ERROR_CODE.fullmatch(result["error_code"]) is None
        ):
            raise BrokerContractError("broker error response is not closed")
        if self.signature and _ED25519_SIGNATURE.fullmatch(self.signature) is None:
            raise BrokerContractError("broker response signature is invalid")

    @property
    def result(self) -> dict[str, Any]:
        value = decode_canonical_json(self.result_json, maximum=MAX_WIRE_BYTES)
        assert isinstance(value, dict)
        return value

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "broker_id": self.broker_id,
            "build_id": self.build_id,
            "ok": self.ok,
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "response_schema_version": self.response_schema_version,
            "result": self.result,
            "server_time": self.server_time,
        }

    def signing_bytes(self) -> bytes:
        return canonical_json_bytes(self.unsigned_payload(), maximum=MAX_WIRE_BYTES)

    def to_payload(self) -> dict[str, Any]:
        return {**self.unsigned_payload(), "signature": self.signature}

    def encode(self) -> bytes:
        return canonical_json_bytes(self.to_payload(), maximum=MAX_WIRE_BYTES)

    def with_signature(self, signature: str) -> BrokerWireResponse:
        return replace(self, signature=signature)

    @classmethod
    def create(
        cls,
        *,
        broker_id: str,
        build_id: str,
        request_id: str,
        server_time: int,
        ok: bool,
        result: dict[str, Any],
    ) -> BrokerWireResponse:
        return cls(
            response_schema_version=1,
            protocol_version=PROTOCOL_VERSION,
            broker_id=broker_id,
            build_id=build_id,
            request_id=request_id,
            server_time=server_time,
            ok=ok,
            result_json=canonical_json_bytes(result, maximum=MAX_WIRE_BYTES),
        )

    @classmethod
    def decode(cls, raw: bytes) -> BrokerWireResponse:
        value = decode_canonical_json(raw, maximum=MAX_WIRE_BYTES)
        expected = {
            "broker_id",
            "build_id",
            "ok",
            "protocol_version",
            "request_id",
            "response_schema_version",
            "result",
            "server_time",
            "signature",
        }
        if not isinstance(value, dict) or set(value) != expected or not isinstance(value["result"], dict):
            raise BrokerContractError("broker response fields are invalid")
        return cls(
            response_schema_version=value["response_schema_version"],
            protocol_version=value["protocol_version"],
            broker_id=value["broker_id"],
            build_id=value["build_id"],
            request_id=value["request_id"],
            server_time=value["server_time"],
            ok=value["ok"],
            result_json=canonical_json_bytes(value["result"], maximum=MAX_WIRE_BYTES),
            signature=value["signature"],
        )


__all__ = [
    "BROKER_PLAN_SCHEMA_VERSION",
    "BROKER_RECEIPT_SCHEMA_VERSION",
    "BROKER_RECONCILIATION_SCHEMA_VERSION",
    "EMPTY_PLAN_DIGEST",
    "MAX_PACKAGE_EVIDENCE_BYTES",
    "MAX_PACKAGE_EVIDENCE_REFS",
    "MAX_PACKAGE_OUTPUT_EVIDENCE_BYTES",
    "MAX_SERVICE_UNIT_OBSERVATIONS",
    "AptInstallPlan",
    "AptTransaction",
    "BrokerContractError",
    "BrokerWireResponse",
    "InstalledPackage",
    "PackageAction",
    "PackageChange",
    "PackageEvidenceReference",
    "PackagePostconditionState",
    "PackageReconciliationReceipt",
    "PackageRef",
    "PackageTransactionReceipt",
    "RepositoryOrigin",
    "ServiceUnitChange",
    "ServiceUnitObservation",
    "ServiceUnitState",
    "TransactionOutcome",
]
