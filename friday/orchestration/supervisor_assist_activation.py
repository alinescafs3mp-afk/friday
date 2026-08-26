"""Body-free activation material for the bounded supervisor promotion gate.

This module loads configuration and evidence only.  It never calls the model,
executes a capability, persists state, publishes an answer, or claims that live
evidence has been accepted.  Missing worktree artifacts, malformed operator
settings and unavailable laptop state all remain ordinary typed closed states.

The source identity is the SHA-256 of the exact installed
``artifacts/release-tree.sha256`` bytes, matching the immutable release
operator's existing tree-manifest identity.  A configured hash is only an
expected value; it can never substitute for reading that installed manifest.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from friday import semantic_supervisor_policy
from friday.orchestration.capability_binding import CapabilityBindingSnapshot
from friday.orchestration.supervisor_assist_promotion import (
    SUPERVISOR_ASSIST_OUTCOME_EVIDENCE_SCHEMA,
    SUPERVISOR_ASSIST_PROMOTION_GATE_ID,
    SUPERVISOR_ASSIST_PROMOTION_MAX_REVIEW_ROUNDS,
    SUPERVISOR_ASSIST_PROMOTION_MAX_STEPS,
    SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256,
    SUPERVISOR_ASSIST_PROMOTION_SCHEMA,
    SUPERVISOR_ASSIST_READINESS_EVIDENCE_SCHEMA,
    AssistPromotionCandidate,
    AssistPromotionEvidenceAuthority,
    AssistPromotionLiveEvidence,
    AssistPromotionOperatorGate,
    AssistPromotionOutcomeEvidence,
    AssistPromotionQualityBasis,
    AssistPromotionReadinessEvidence,
    SupervisorSchedulerAdmissionSnapshot,
)
from friday.orchestration.supervisor_contracts import SupervisorMode, TaskClass
from friday.orchestration.supervisor_promoted_product_event import (
    AcceptedSupervisorLatencyBudget,
    PromotedProductEventError,
    load_accepted_supervisor_latency_budget,
)

SUPERVISOR_ASSIST_ACTIVATION_STATUS_SCHEMA = "friday.supervisor-assist-activation-status.v1"

_MAX_EVIDENCE_BYTES = 32 << 10
_MAX_LATENCY_BUDGET_BYTES = 4_096
_MAX_RELEASE_TREE_BYTES = 64 << 20
_MAX_PATH_CHARS = 4_096
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_MANIFEST_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_P1_POLICY_ID = semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_ID
_P1_POLICY_SHA256 = semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_SHA256
_ASSIST_POLICY_ID = semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_ID
_ASSIST_POLICY_SHA256 = semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256
_P1_PROFILE_ID = semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID
_P1_PROFILE_MANIFEST_SHA256 = semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
_P1_WORKLOAD = semantic_supervisor_policy.SUPERVISOR_WORKLOAD

_PUBLIC_STATUS_KEYS = frozenset(
    {
        "schema",
        "role",
        "enabled",
        "configured",
        "mode",
        "state",
        "available",
        "semantic_supervisor",
    }
)
_SUPERVISOR_STATUS_KEYS = frozenset(
    {
        "workload",
        "requested_mode",
        "effective_mode",
        "policy_id",
        "policy_sha256",
        "workload_available",
        "runtime_available",
        "closed_reason",
    }
)
_EVIDENCE_KEYS = frozenset(
    {
        "schema",
        "evidence_id",
        "authority",
        "observed_mode",
        "task_class",
        "source_revision_sha256",
        "promotion_policy_sha256",
        "observed_policy_id",
        "observed_policy_sha256",
        "target_policy_id",
        "target_policy_sha256",
        "runtime_profile_id",
        "runtime_profile_manifest_sha256",
        "registry_binding_sha256",
        "baseline_file_sha256",
        "baseline_report_sha256",
        "operator_attestation_sha256",
        "precursor_assist_promotion_evidence_sha256",
        "max_steps",
        "max_review_rounds",
        "observation_count",
        "joined_trace_count",
        "representative_window_attested",
        "primary_fallback_proven",
        "laptop_unavailable_fallback_proven",
        "final_authority_recheck_proven",
        "primary_publication_owner_proven",
        "hidden_owner_count",
        "duplicate_capability_count",
        "duplicate_effect_count",
        "duplicate_publication_count",
        "false_completion_regression_count",
        "product_evidence",
    }
)
_READINESS_EVIDENCE_KEYS = frozenset(
    {
        "schema",
        "baseline_window_sha256",
        "baseline_observation_count",
        "baseline_complete_count",
        "documented_failure_class_id",
        "documented_failure_class_sha256",
        "baseline_failure_class_count",
        "readiness_witness_sha256",
        "readiness_observation_count",
        "latency_budget_target_mode",
        "latency_budget_source_revision_sha256",
        "latency_budget_ms",
        "latency_budget_sha256",
        "latency_total_ms",
        "latency_max_ms",
        "call_rate_observation_count",
        "supervisor_invocation_count",
        "unnecessary_supervisor_invocation_count",
        "user_visible_observation_count",
        "user_visible_regression_count",
    }
)
_OUTCOME_EVIDENCE_KEYS = frozenset(
    {
        "schema",
        "quality_basis",
        "baseline_window_sha256",
        "promoted_window_sha256",
        "baseline_observation_count",
        "baseline_complete_count",
        "promoted_observation_count",
        "promoted_complete_count",
        "documented_failure_class_id",
        "documented_failure_class_sha256",
        "baseline_failure_class_count",
        "promoted_failure_class_count",
        "latency_budget_target_mode",
        "latency_budget_source_revision_sha256",
        "latency_budget_ms",
        "latency_budget_sha256",
        "latency_observation_count",
        "latency_total_ms",
        "latency_max_ms",
        "call_rate_observation_count",
        "supervisor_invocation_count",
        "unnecessary_supervisor_invocation_count",
        "user_visible_observation_count",
        "user_visible_regression_count",
    }
)
_FORBIDDEN_STATUS_KEYS = frozenset(
    {
        "actor_id",
        "body",
        "content",
        "conversation_id",
        "evidence_body",
        "message",
        "messages",
        "path",
        "prompt",
        "query",
        "raw",
        "response",
        "secret",
    }
)


class AssistPromotionActivationReason(StrEnum):
    MATERIAL_LOADED_NOT_ACCEPTED = "material_loaded_not_accepted"
    DEFAULT_OFF = "default_off"
    RAW_SETTINGS_INVALID = "raw_settings_invalid"
    MODE_NOT_ADMITTED = "mode_not_admitted"
    SOURCE_REVISION_UNAVAILABLE = "source_revision_unavailable"
    SOURCE_REVISION_MISMATCH = "source_revision_mismatch"
    REGISTRY_BINDING_MISMATCH = "registry_binding_mismatch"
    SCHEDULER_PROJECTION_INVALID = "scheduler_projection_invalid"
    SCHEDULER_IDENTITY_MISMATCH = "scheduler_identity_mismatch"
    EVIDENCE_FILE_UNAVAILABLE = "evidence_file_unavailable"
    EVIDENCE_DIGEST_MISMATCH = "evidence_digest_mismatch"
    EVIDENCE_INVALID = "evidence_invalid"
    EVIDENCE_IDENTITY_MISMATCH = "evidence_identity_mismatch"
    LATENCY_BUDGET_FILE_UNAVAILABLE = "latency_budget_file_unavailable"
    LATENCY_BUDGET_DIGEST_MISMATCH = "latency_budget_digest_mismatch"
    LATENCY_BUDGET_INVALID = "latency_budget_invalid"
    LATENCY_BUDGET_IDENTITY_MISMATCH = "latency_budget_identity_mismatch"
    CANARY_ALLOWLIST_INVALID = "canary_allowlist_invalid"


class AssistPromotionActivationError(ValueError):
    """Finite body-free loader failure suitable for a typed closed result."""

    def __init__(self, reason: AssistPromotionActivationReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class RawAssistPromotionActivationSettings:
    """Untrusted raw operator fields; the exact default is promotion-off."""

    enabled: object = False
    requested_mode: object = SupervisorMode.OFF.value
    evidence_file: object = ""
    evidence_sha256: object = ""
    latency_budget_file: object = ""
    latency_budget_sha256: object = ""
    source_revision_sha256: object = ""
    registry_binding_sha256: object = ""
    canary_actor_bindings: object = ()


@dataclass(frozen=True, slots=True)
class LoadedAssistPromotionEvidence:
    """Parsed evidence plus its exact raw-file identity; no bytes are retained."""

    evidence: AssistPromotionLiveEvidence
    file_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, AssistPromotionLiveEvidence):
            raise TypeError("loaded evidence must be typed")
        _typed_digest(self.file_sha256, label="file_sha256")


@dataclass(frozen=True, slots=True)
class AssistPromotionActivationMaterial:
    """Static activation inputs retained for fresh per-turn scheduler checks."""

    configured: bool
    reason: AssistPromotionActivationReason
    requested_mode: SupervisorMode
    source_revision_sha256: str | None
    registry_binding_sha256: str | None
    scheduler_snapshot: SupervisorSchedulerAdmissionSnapshot | None
    loaded_evidence: LoadedAssistPromotionEvidence | None
    accepted_latency_budget: AcceptedSupervisorLatencyBudget | None
    operator_gate: AssistPromotionOperatorGate

    def __post_init__(self) -> None:
        if type(self.configured) is not bool:
            raise TypeError("configured must be boolean")
        if not isinstance(self.reason, AssistPromotionActivationReason):
            raise TypeError("activation reason must be typed")
        if not isinstance(self.requested_mode, SupervisorMode):
            raise TypeError("requested mode must be typed")
        if not isinstance(self.operator_gate, AssistPromotionOperatorGate):
            raise TypeError("operator gate must be typed")
        if self.configured and (
            self.source_revision_sha256 is None
            or self.registry_binding_sha256 is None
            or self.scheduler_snapshot is None
            or self.loaded_evidence is None
            or self.accepted_latency_budget is None
            or not self.operator_gate.enabled
        ):
            raise ValueError("configured activation material is incomplete")
        if self.source_revision_sha256 is not None:
            _typed_digest(self.source_revision_sha256, label="source_revision_sha256")
        if self.registry_binding_sha256 is not None:
            _typed_digest(self.registry_binding_sha256, label="registry_binding_sha256")
        if self.accepted_latency_budget is not None:
            if type(self.accepted_latency_budget) is not AcceptedSupervisorLatencyBudget:
                raise TypeError("accepted latency budget must be exact")
            document = self.accepted_latency_budget.document
            if (
                document.target_mode is not self.requested_mode
                or self.source_revision_sha256 is None
                or not hmac.compare_digest(
                    document.source_revision_sha256,
                    self.source_revision_sha256,
                )
            ):
                raise ValueError("accepted latency budget identity does not match material")

    def public_status(self) -> dict[str, object]:
        """Return a body-free status; loading is explicitly not acceptance."""

        snapshot = self.scheduler_snapshot
        evidence = self.loaded_evidence
        return {
            "schema": SUPERVISOR_ASSIST_ACTIVATION_STATUS_SCHEMA,
            "configured": self.configured,
            "reason": self.reason.value,
            "requested_mode": self.requested_mode.value,
            "source_revision_loaded": self.source_revision_sha256 is not None,
            "registry_binding_loaded": self.registry_binding_sha256 is not None,
            "scheduler_projection_loaded": snapshot is not None,
            "scheduler_runtime_available": bool(snapshot and snapshot.runtime_available),
            "evidence_loaded": evidence is not None,
            "evidence_authority": (evidence.evidence.authority.value if evidence is not None else "none"),
            "operator_gate_enabled": self.operator_gate.enabled,
            "canary_actor_binding_count": len(self.operator_gate.canary_actor_bindings),
            "promotion_admitted": False,
            "evidence_accepted": False,
            "acceptance_authority": "none",
            "body_free": True,
        }

    def fresh_candidate(
        self,
        public_status: Mapping[str, object],
        diagnostics_status: Mapping[str, object],
        binding_snapshot: CapabilityBindingSnapshot,
        *,
        actor_binding_sha256: str | None = None,
    ) -> AssistPromotionCandidate | None:
        """Rebuild dynamic candidate facts after laptop health changes.

        A pre-start ``runtime_available=false`` snapshot never consumes or
        permanently closes this material.  Malformed fresh projections and
        registry drift simply return no candidate for that turn.
        """

        if not self.configured or self.loaded_evidence is None or self.accepted_latency_budget is None:
            return None
        if not isinstance(binding_snapshot, CapabilityBindingSnapshot):
            raise TypeError("binding_snapshot must be typed")
        if binding_snapshot.digest_hex() != self.registry_binding_sha256:
            return None
        try:
            snapshot = scheduler_admission_snapshot_from_status(
                public_status,
                diagnostics_status,
            )
        except AssistPromotionActivationError:
            return None
        if snapshot.requested_mode != self.requested_mode.value:
            return None
        evidence = self.loaded_evidence.evidence
        budget = self.accepted_latency_budget
        if (
            budget.document.target_mode is not self.requested_mode
            or budget.document.source_revision_sha256 != self.source_revision_sha256
            or not _evidence_budget_identity_matches(evidence, budget)
        ):
            return None
        try:
            return AssistPromotionCandidate(
                requested_mode=self.requested_mode,
                task_class=TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
                source_revision_sha256=self.source_revision_sha256 or "",
                expected_registry_binding_sha256=self.registry_binding_sha256 or "",
                binding_snapshot=binding_snapshot,
                scheduler=snapshot,
                max_steps=evidence.max_steps,
                max_review_rounds=evidence.max_review_rounds,
                latency_budget_sha256=budget.document_sha256,
                latency_budget_ms=budget.document.maximum_user_visible_latency_ms,
                actor_binding_sha256=actor_binding_sha256,
            )
        except ValueError:
            return None


def _typed_digest(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be text")
    if _DIGEST_RE.fullmatch(value) is None:
        raise AssistPromotionActivationError(AssistPromotionActivationReason.RAW_SETTINGS_INVALID)
    return value


def _raw_digest(value: object) -> str | None:
    return value if type(value) is str and _DIGEST_RE.fullmatch(value) is not None else None


def _stable_regular_file_bytes(
    path: Path,
    *,
    maximum_bytes: int,
    allowed_modes: frozenset[int],
    reason: AssistPromotionActivationReason,
) -> bytes:
    if not isinstance(path, Path):
        raise TypeError("path must be a pathlib.Path")
    if not path.is_absolute():
        raise AssistPromotionActivationError(reason)
    lexical = Path(os.path.abspath(path))
    if lexical != path or len(str(lexical)) > _MAX_PATH_CHARS:
        raise AssistPromotionActivationError(reason)

    parent_descriptor = -1
    descriptor = -1
    try:
        if lexical.resolve(strict=True) != lexical:
            raise AssistPromotionActivationError(reason)
        parent_descriptor = os.open(
            lexical.parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        parent_before = os.fstat(parent_descriptor)
        descriptor = os.open(
            lexical.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) not in allowed_modes
            or not 0 < before.st_size <= maximum_bytes
        ):
            raise AssistPromotionActivationError(reason)
        chunks: list[bytes] = []
        size = 0
        while size <= maximum_bytes:
            chunk = os.read(descriptor, min(1 << 20, maximum_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        parent_after = os.fstat(parent_descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(raw) != before.st_size
            or len(raw) > maximum_bytes
            or any(getattr(before, name) != getattr(after, name) for name in stable_fields)
            or any(
                getattr(parent_before, name) != getattr(parent_after, name)
                for name in ("st_dev", "st_ino", "st_mode", "st_uid", "st_mtime_ns", "st_ctime_ns")
            )
            or lexical.resolve(strict=True) != lexical
        ):
            raise AssistPromotionActivationError(reason)
        return raw
    except AssistPromotionActivationError:
        raise
    except (OSError, RuntimeError) as exc:
        raise AssistPromotionActivationError(reason) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _release_manifest_is_well_formed(raw: bytes) -> bool:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        return False
    if not text.endswith("\n") or "\x00" in text or "\r" in text:
        return False
    lines = text.splitlines()
    if not lines or len(lines) > 1_000_000:
        return False
    paths: list[str] = []
    for line in lines:
        fields = line.split(" ", 3)
        if len(fields) != 4:
            return False
        kind, mode, digest, relative = fields
        canonical = PurePosixPath(relative).as_posix()
        if (
            kind not in {"D", "F", "L"}
            or len(mode) != 4
            or any(character not in "01234567" for character in mode)
            or _MANIFEST_DIGEST_RE.fullmatch(digest) is None
            or not relative
            or relative.startswith("/")
            or canonical != relative
            or any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts)
            or relative == "artifacts/release-tree.sha256"
            or "__pycache__" in PurePosixPath(relative).parts
        ):
            return False
        paths.append(relative)
    return paths == sorted(paths) and len(paths) == len(set(paths))


def derive_installed_release_tree_sha256(release_root: Path) -> str | None:
    """Return the installed manifest-bytes digest or a fail-closed ``None``."""

    if not isinstance(release_root, Path):
        raise TypeError("release_root must be a pathlib.Path")
    try:
        raw = _stable_regular_file_bytes(
            release_root / "artifacts/release-tree.sha256",
            maximum_bytes=_MAX_RELEASE_TREE_BYTES,
            allowed_modes=frozenset({0o400}),
            reason=AssistPromotionActivationReason.SOURCE_REVISION_UNAVAILABLE,
        )
    except AssistPromotionActivationError:
        return None
    if not _release_manifest_is_well_formed(raw):
        return None
    return hashlib.sha256(raw).hexdigest()


def _reject_json_constant(_value: str) -> Any:
    raise AssistPromotionActivationError(AssistPromotionActivationReason.EVIDENCE_INVALID)


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AssistPromotionActivationError(AssistPromotionActivationReason.EVIDENCE_INVALID)
        result[key] = value
    return result


def parse_assist_promotion_live_evidence(
    raw: bytes,
    expected_sha256: str,
) -> LoadedAssistPromotionEvidence:
    """Strictly parse one exact-hash body-free evidence JSON object."""

    if type(raw) is not bytes or type(expected_sha256) is not str:
        raise TypeError("evidence parser requires bytes and a digest string")
    if not 0 < len(raw) <= _MAX_EVIDENCE_BYTES:
        raise AssistPromotionActivationError(AssistPromotionActivationReason.EVIDENCE_INVALID)
    if _DIGEST_RE.fullmatch(expected_sha256) is None or not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(),
        expected_sha256,
    ):
        raise AssistPromotionActivationError(AssistPromotionActivationReason.EVIDENCE_DIGEST_MISMATCH)
    try:
        decoded = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_closed_json_object,
            parse_constant=_reject_json_constant,
        )
    except AssistPromotionActivationError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise AssistPromotionActivationError(AssistPromotionActivationReason.EVIDENCE_INVALID) from exc
    if type(decoded) is not dict or set(decoded) != _EVIDENCE_KEYS:
        raise AssistPromotionActivationError(AssistPromotionActivationReason.EVIDENCE_INVALID)
    if decoded.get("schema") != SUPERVISOR_ASSIST_PROMOTION_SCHEMA:
        raise AssistPromotionActivationError(AssistPromotionActivationReason.EVIDENCE_INVALID)
    product_payload = decoded.get("product_evidence")
    if type(product_payload) is not dict:
        raise AssistPromotionActivationError(AssistPromotionActivationReason.EVIDENCE_INVALID)
    product_evidence: AssistPromotionReadinessEvidence | AssistPromotionOutcomeEvidence
    try:
        product_schema = product_payload.get("schema")
        if product_schema == SUPERVISOR_ASSIST_READINESS_EVIDENCE_SCHEMA:
            if set(product_payload) != _READINESS_EVIDENCE_KEYS:
                raise ValueError("readiness evidence keys do not match")
            product_evidence = AssistPromotionReadinessEvidence(
                baseline_window_sha256=product_payload["baseline_window_sha256"],
                baseline_observation_count=product_payload["baseline_observation_count"],
                baseline_complete_count=product_payload["baseline_complete_count"],
                documented_failure_class_id=product_payload["documented_failure_class_id"],
                documented_failure_class_sha256=product_payload["documented_failure_class_sha256"],
                baseline_failure_class_count=product_payload["baseline_failure_class_count"],
                readiness_witness_sha256=product_payload["readiness_witness_sha256"],
                readiness_observation_count=product_payload["readiness_observation_count"],
                latency_budget_target_mode=SupervisorMode(product_payload["latency_budget_target_mode"]),
                latency_budget_source_revision_sha256=product_payload[
                    "latency_budget_source_revision_sha256"
                ],
                latency_budget_ms=product_payload["latency_budget_ms"],
                latency_budget_sha256=product_payload["latency_budget_sha256"],
                latency_total_ms=product_payload["latency_total_ms"],
                latency_max_ms=product_payload["latency_max_ms"],
                call_rate_observation_count=product_payload["call_rate_observation_count"],
                supervisor_invocation_count=product_payload["supervisor_invocation_count"],
                unnecessary_supervisor_invocation_count=product_payload[
                    "unnecessary_supervisor_invocation_count"
                ],
                user_visible_observation_count=product_payload["user_visible_observation_count"],
                user_visible_regression_count=product_payload["user_visible_regression_count"],
            )
        elif product_schema == SUPERVISOR_ASSIST_OUTCOME_EVIDENCE_SCHEMA:
            if set(product_payload) != _OUTCOME_EVIDENCE_KEYS:
                raise ValueError("outcome evidence keys do not match")
            product_evidence = AssistPromotionOutcomeEvidence(
                quality_basis=AssistPromotionQualityBasis(product_payload["quality_basis"]),
                baseline_window_sha256=product_payload["baseline_window_sha256"],
                promoted_window_sha256=product_payload["promoted_window_sha256"],
                baseline_observation_count=product_payload["baseline_observation_count"],
                baseline_complete_count=product_payload["baseline_complete_count"],
                promoted_observation_count=product_payload["promoted_observation_count"],
                promoted_complete_count=product_payload["promoted_complete_count"],
                documented_failure_class_id=product_payload["documented_failure_class_id"],
                documented_failure_class_sha256=product_payload["documented_failure_class_sha256"],
                baseline_failure_class_count=product_payload["baseline_failure_class_count"],
                promoted_failure_class_count=product_payload["promoted_failure_class_count"],
                latency_budget_target_mode=SupervisorMode(product_payload["latency_budget_target_mode"]),
                latency_budget_source_revision_sha256=product_payload[
                    "latency_budget_source_revision_sha256"
                ],
                latency_budget_ms=product_payload["latency_budget_ms"],
                latency_budget_sha256=product_payload["latency_budget_sha256"],
                latency_observation_count=product_payload["latency_observation_count"],
                latency_total_ms=product_payload["latency_total_ms"],
                latency_max_ms=product_payload["latency_max_ms"],
                call_rate_observation_count=product_payload["call_rate_observation_count"],
                supervisor_invocation_count=product_payload["supervisor_invocation_count"],
                unnecessary_supervisor_invocation_count=product_payload[
                    "unnecessary_supervisor_invocation_count"
                ],
                user_visible_observation_count=product_payload["user_visible_observation_count"],
                user_visible_regression_count=product_payload["user_visible_regression_count"],
            )
        else:
            raise ValueError("product evidence schema is invalid")
        evidence = AssistPromotionLiveEvidence(
            evidence_id=decoded["evidence_id"],
            authority=AssistPromotionEvidenceAuthority(decoded["authority"]),
            observed_mode=SupervisorMode(decoded["observed_mode"]),
            task_class=TaskClass(decoded["task_class"]),
            source_revision_sha256=decoded["source_revision_sha256"],
            promotion_policy_sha256=decoded["promotion_policy_sha256"],
            observed_policy_id=decoded["observed_policy_id"],
            observed_policy_sha256=decoded["observed_policy_sha256"],
            target_policy_id=decoded["target_policy_id"],
            target_policy_sha256=decoded["target_policy_sha256"],
            runtime_profile_id=decoded["runtime_profile_id"],
            runtime_profile_manifest_sha256=decoded["runtime_profile_manifest_sha256"],
            registry_binding_sha256=decoded["registry_binding_sha256"],
            baseline_file_sha256=decoded["baseline_file_sha256"],
            baseline_report_sha256=decoded["baseline_report_sha256"],
            operator_attestation_sha256=decoded["operator_attestation_sha256"],
            precursor_assist_promotion_evidence_sha256=decoded[
                "precursor_assist_promotion_evidence_sha256"
            ],
            max_steps=decoded["max_steps"],
            max_review_rounds=decoded["max_review_rounds"],
            observation_count=decoded["observation_count"],
            joined_trace_count=decoded["joined_trace_count"],
            representative_window_attested=decoded["representative_window_attested"],
            primary_fallback_proven=decoded["primary_fallback_proven"],
            laptop_unavailable_fallback_proven=decoded["laptop_unavailable_fallback_proven"],
            final_authority_recheck_proven=decoded["final_authority_recheck_proven"],
            primary_publication_owner_proven=decoded["primary_publication_owner_proven"],
            hidden_owner_count=decoded["hidden_owner_count"],
            duplicate_capability_count=decoded["duplicate_capability_count"],
            duplicate_effect_count=decoded["duplicate_effect_count"],
            duplicate_publication_count=decoded["duplicate_publication_count"],
            false_completion_regression_count=decoded["false_completion_regression_count"],
            product_evidence=product_evidence,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AssistPromotionActivationError(AssistPromotionActivationReason.EVIDENCE_INVALID) from exc
    return LoadedAssistPromotionEvidence(evidence=evidence, file_sha256=expected_sha256)


def load_assist_promotion_live_evidence(
    path: Path,
    expected_sha256: str,
) -> LoadedAssistPromotionEvidence:
    """Read a private stable regular evidence file and discard its raw bytes."""

    if not isinstance(path, Path) or type(expected_sha256) is not str:
        raise TypeError("evidence loader requires a pathlib.Path and digest string")
    raw = _stable_regular_file_bytes(
        path,
        maximum_bytes=_MAX_EVIDENCE_BYTES,
        allowed_modes=frozenset({0o400, 0o600}),
        reason=AssistPromotionActivationReason.EVIDENCE_FILE_UNAVAILABLE,
    )
    return parse_assist_promotion_live_evidence(raw, expected_sha256)


def _mapping(value: object, *, reason: AssistPromotionActivationReason) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("scheduler status projections must be mappings")
    if any(type(key) is not str for key in value):
        raise AssistPromotionActivationError(reason)
    return value


def _status_contains_forbidden_field(value: object, *, depth: int = 0) -> bool:
    if depth > 8:
        return True
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str or key.casefold() in _FORBIDDEN_STATUS_KEYS:
                return True
            if _status_contains_forbidden_field(item, depth=depth + 1):
                return True
        return False
    if isinstance(value, (tuple, list)):
        return any(_status_contains_forbidden_field(item, depth=depth + 1) for item in value)
    return False


def scheduler_admission_snapshot_from_status(
    public_status: Mapping[str, object],
    diagnostics_status: Mapping[str, object],
) -> SupervisorSchedulerAdmissionSnapshot:
    """Construct the exact body-free assist/P4 admission projection.

    The runtime-profile manifest digest is code-owned because the current
    scheduler projection exposes only the accepted profile ID and a manifest
    match bit, never an operator-supplied replacement digest.
    """

    public = _mapping(
        public_status,
        reason=AssistPromotionActivationReason.SCHEDULER_PROJECTION_INVALID,
    )
    diagnostics = _mapping(
        diagnostics_status,
        reason=AssistPromotionActivationReason.SCHEDULER_PROJECTION_INVALID,
    )
    reason = AssistPromotionActivationReason.SCHEDULER_PROJECTION_INVALID
    if (
        set(public) != _PUBLIC_STATUS_KEYS
        or _status_contains_forbidden_field(public)
        or _status_contains_forbidden_field(diagnostics)
    ):
        raise AssistPromotionActivationError(reason)
    supervisor = _mapping(public.get("semantic_supervisor"), reason=reason)
    diagnostic_supervisor = _mapping(diagnostics.get("semantic_supervisor"), reason=reason)
    workloads = _mapping(diagnostics.get("workloads"), reason=reason)
    plan_workload = _mapping(workloads.get(_P1_WORKLOAD), reason=reason)
    if set(supervisor) != _SUPERVISOR_STATUS_KEYS or dict(diagnostic_supervisor) != dict(supervisor):
        raise AssistPromotionActivationError(reason)
    for key in _PUBLIC_STATUS_KEYS:
        if key == "semantic_supervisor":
            continue
        if diagnostics.get(key) != public.get(key):
            raise AssistPromotionActivationError(reason)
    runtime_available = supervisor.get("runtime_available")
    if (
        public.get("schema") != "friday.optional-secondary-health.v1"
        or public.get("role") != "optional_advisory"
        or public.get("enabled") is not True
        or public.get("configured") is not True
        or public.get("mode") != "assist"
        or type(public.get("available")) is not bool
        or public.get("state") not in {"probing", "healthy", "degraded", "cooldown"}
        or supervisor.get("workload") != _P1_WORKLOAD
        or supervisor.get("requested_mode") not in {"assist", "canary"}
        or supervisor.get("effective_mode") != "shadow"
        or supervisor.get("policy_id") != _ASSIST_POLICY_ID
        or supervisor.get("policy_sha256") != _ASSIST_POLICY_SHA256
        or supervisor.get("workload_available") is not True
        or type(runtime_available) is not bool
        or supervisor.get("closed_reason") != "admitted"
        or diagnostics.get("profile") != _P1_PROFILE_ID
        or diagnostics.get("profile_admission") != "accepted"
        or type(diagnostics.get("profile_manifest_match")) is not bool
        or type(diagnostics.get("served_model_match")) is not bool
        or plan_workload.get("routing_mode") != "shadow"
        or plan_workload.get("available") is not True
        or plan_workload.get("closed_reason") != "admitted"
        or runtime_available is not public.get("available")
    ):
        raise AssistPromotionActivationError(AssistPromotionActivationReason.SCHEDULER_IDENTITY_MISMATCH)
    if runtime_available and (
        public.get("state") != "healthy"
        or diagnostics.get("profile_manifest_match") is not True
        or diagnostics.get("served_model_match") is not True
    ):
        raise AssistPromotionActivationError(AssistPromotionActivationReason.SCHEDULER_IDENTITY_MISMATCH)
    return SupervisorSchedulerAdmissionSnapshot(
        workload=_P1_WORKLOAD,
        requested_mode=str(supervisor["requested_mode"]),
        effective_mode=SupervisorMode.SHADOW.value,
        policy_id=_ASSIST_POLICY_ID,
        policy_sha256=_ASSIST_POLICY_SHA256,
        runtime_profile_id=_P1_PROFILE_ID,
        runtime_profile_manifest_sha256=_P1_PROFILE_MANIFEST_SHA256,
        profile_admission="accepted",
        closed_reason="admitted",
        workload_available=True,
        runtime_available=bool(runtime_available),
    )


def _closed_material(
    reason: AssistPromotionActivationReason,
    *,
    requested_mode: SupervisorMode = SupervisorMode.OFF,
    source_revision_sha256: str | None = None,
    registry_binding_sha256: str | None = None,
    scheduler_snapshot: SupervisorSchedulerAdmissionSnapshot | None = None,
    loaded_evidence: LoadedAssistPromotionEvidence | None = None,
    accepted_latency_budget: AcceptedSupervisorLatencyBudget | None = None,
) -> AssistPromotionActivationMaterial:
    return AssistPromotionActivationMaterial(
        configured=False,
        reason=reason,
        requested_mode=requested_mode,
        source_revision_sha256=source_revision_sha256,
        registry_binding_sha256=registry_binding_sha256,
        scheduler_snapshot=scheduler_snapshot,
        loaded_evidence=loaded_evidence,
        accepted_latency_budget=accepted_latency_budget,
        operator_gate=AssistPromotionOperatorGate(),
    )


def _validated_raw_settings(
    raw: RawAssistPromotionActivationSettings,
) -> tuple[SupervisorMode, Path, str, Path, str, str, str, tuple[str, ...]]:
    if raw.enabled is not True:
        raise AssistPromotionActivationError(AssistPromotionActivationReason.RAW_SETTINGS_INVALID)
    if type(raw.requested_mode) is not str:
        raise AssistPromotionActivationError(AssistPromotionActivationReason.RAW_SETTINGS_INVALID)
    try:
        mode = SupervisorMode(raw.requested_mode)
    except ValueError as exc:
        raise AssistPromotionActivationError(AssistPromotionActivationReason.MODE_NOT_ADMITTED) from exc
    if mode not in {SupervisorMode.ASSIST, SupervisorMode.CANARY}:
        raise AssistPromotionActivationError(AssistPromotionActivationReason.MODE_NOT_ADMITTED)
    if (
        type(raw.evidence_file) is not str
        or not raw.evidence_file
        or len(raw.evidence_file) > _MAX_PATH_CHARS
        or any(character in raw.evidence_file for character in "\x00\r\n")
    ):
        raise AssistPromotionActivationError(AssistPromotionActivationReason.RAW_SETTINGS_INVALID)
    evidence_path = Path(raw.evidence_file)
    if not evidence_path.is_absolute() or str(evidence_path) != raw.evidence_file:
        raise AssistPromotionActivationError(AssistPromotionActivationReason.RAW_SETTINGS_INVALID)
    if (
        type(raw.latency_budget_file) is not str
        or not raw.latency_budget_file
        or len(raw.latency_budget_file) > _MAX_PATH_CHARS
        or any(character in raw.latency_budget_file for character in "\x00\r\n")
    ):
        raise AssistPromotionActivationError(AssistPromotionActivationReason.RAW_SETTINGS_INVALID)
    latency_budget_path = Path(raw.latency_budget_file)
    if not latency_budget_path.is_absolute() or str(latency_budget_path) != raw.latency_budget_file:
        raise AssistPromotionActivationError(AssistPromotionActivationReason.RAW_SETTINGS_INVALID)
    evidence_sha256 = _raw_digest(raw.evidence_sha256)
    latency_budget_sha256 = _raw_digest(raw.latency_budget_sha256)
    source_sha256 = _raw_digest(raw.source_revision_sha256)
    registry_sha256 = _raw_digest(raw.registry_binding_sha256)
    if (
        evidence_sha256 is None
        or latency_budget_sha256 is None
        or source_sha256 is None
        or registry_sha256 is None
    ):
        raise AssistPromotionActivationError(AssistPromotionActivationReason.RAW_SETTINGS_INVALID)
    if type(raw.canary_actor_bindings) is not tuple:
        raise AssistPromotionActivationError(AssistPromotionActivationReason.CANARY_ALLOWLIST_INVALID)
    actors = raw.canary_actor_bindings
    if (
        len(actors) > 32
        or any(_raw_digest(value) is None for value in actors)
        or len(set(actors)) != len(actors)
        or (mode is SupervisorMode.ASSIST and bool(actors))
        or (mode is SupervisorMode.CANARY and not actors)
    ):
        raise AssistPromotionActivationError(AssistPromotionActivationReason.CANARY_ALLOWLIST_INVALID)
    return (
        mode,
        evidence_path,
        evidence_sha256,
        latency_budget_path,
        latency_budget_sha256,
        source_sha256,
        registry_sha256,
        actors,
    )


def _evidence_budget_identity_matches(
    evidence: AssistPromotionLiveEvidence,
    budget: AcceptedSupervisorLatencyBudget,
) -> bool:
    product = evidence.product_evidence
    document = budget.document
    return bool(
        product.latency_budget_target_mode is document.target_mode
        and hmac.compare_digest(
            product.latency_budget_source_revision_sha256,
            document.source_revision_sha256,
        )
        and hmac.compare_digest(product.latency_budget_sha256, budget.document_sha256)
        and product.latency_budget_ms == document.maximum_user_visible_latency_ms
    )


def _evidence_identity_matches(
    evidence: AssistPromotionLiveEvidence,
    *,
    requested_mode: SupervisorMode,
    source_revision_sha256: str,
    registry_binding_sha256: str,
    accepted_latency_budget: AcceptedSupervisorLatencyBudget,
) -> bool:
    expected_predecessor = (
        SupervisorMode.SHADOW if requested_mode is SupervisorMode.ASSIST else SupervisorMode.ASSIST
    )
    expected_observed_policy_id, expected_observed_policy_sha256 = (
        (_P1_POLICY_ID, _P1_POLICY_SHA256)
        if expected_predecessor is SupervisorMode.SHADOW
        else (_ASSIST_POLICY_ID, _ASSIST_POLICY_SHA256)
    )
    return bool(
        evidence.observed_mode is expected_predecessor
        and (
            (
                requested_mode is SupervisorMode.ASSIST
                and isinstance(evidence.product_evidence, AssistPromotionReadinessEvidence)
            )
            or (
                requested_mode is SupervisorMode.CANARY
                and isinstance(evidence.product_evidence, AssistPromotionOutcomeEvidence)
            )
        )
        and evidence.task_class is TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB
        and evidence.source_revision_sha256 == source_revision_sha256
        and evidence.promotion_policy_sha256 == SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256
        and evidence.observed_policy_id == expected_observed_policy_id
        and evidence.observed_policy_sha256 == expected_observed_policy_sha256
        and evidence.target_policy_id == _ASSIST_POLICY_ID
        and evidence.target_policy_sha256 == _ASSIST_POLICY_SHA256
        and evidence.runtime_profile_id == _P1_PROFILE_ID
        and evidence.runtime_profile_manifest_sha256 == _P1_PROFILE_MANIFEST_SHA256
        and evidence.registry_binding_sha256 == registry_binding_sha256
        and evidence.max_steps == SUPERVISOR_ASSIST_PROMOTION_MAX_STEPS
        and evidence.max_review_rounds == SUPERVISOR_ASSIST_PROMOTION_MAX_REVIEW_ROUNDS
        and _evidence_budget_identity_matches(evidence, accepted_latency_budget)
    )


def load_assist_promotion_activation(
    raw: RawAssistPromotionActivationSettings,
    *,
    installed_release_root: Path,
    scheduler_public_status: Mapping[str, object],
    scheduler_diagnostics_status: Mapping[str, object],
    binding_snapshot: CapabilityBindingSnapshot,
) -> AssistPromotionActivationMaterial:
    """Load static material without making a promotion or acceptance decision."""

    if not isinstance(raw, RawAssistPromotionActivationSettings):
        raise TypeError("raw settings must be typed")
    if not isinstance(installed_release_root, Path):
        raise TypeError("installed_release_root must be a pathlib.Path")
    if not isinstance(scheduler_public_status, Mapping) or not isinstance(
        scheduler_diagnostics_status,
        Mapping,
    ):
        raise TypeError("scheduler status projections must be mappings")
    if not isinstance(binding_snapshot, CapabilityBindingSnapshot):
        raise TypeError("binding_snapshot must be typed")

    if raw.enabled is False:
        return _closed_material(AssistPromotionActivationReason.DEFAULT_OFF)
    try:
        (
            mode,
            evidence_path,
            expected_evidence_sha256,
            latency_budget_path,
            expected_latency_budget_sha256,
            expected_source_sha256,
            expected_registry_sha256,
            actor_bindings,
        ) = _validated_raw_settings(raw)
    except AssistPromotionActivationError as exc:
        return _closed_material(exc.reason)

    source_sha256 = derive_installed_release_tree_sha256(installed_release_root)
    if source_sha256 is None:
        return _closed_material(
            AssistPromotionActivationReason.SOURCE_REVISION_UNAVAILABLE,
            requested_mode=mode,
        )
    if not hmac.compare_digest(source_sha256, expected_source_sha256):
        return _closed_material(
            AssistPromotionActivationReason.SOURCE_REVISION_MISMATCH,
            requested_mode=mode,
            source_revision_sha256=source_sha256,
        )
    try:
        latency_budget_raw = _stable_regular_file_bytes(
            latency_budget_path,
            maximum_bytes=_MAX_LATENCY_BUDGET_BYTES,
            allowed_modes=frozenset({0o400, 0o600}),
            reason=AssistPromotionActivationReason.LATENCY_BUDGET_FILE_UNAVAILABLE,
        )
    except AssistPromotionActivationError as exc:
        return _closed_material(
            exc.reason,
            requested_mode=mode,
            source_revision_sha256=source_sha256,
        )
    if not hmac.compare_digest(
        hashlib.sha256(latency_budget_raw).hexdigest(),
        expected_latency_budget_sha256,
    ):
        return _closed_material(
            AssistPromotionActivationReason.LATENCY_BUDGET_DIGEST_MISMATCH,
            requested_mode=mode,
            source_revision_sha256=source_sha256,
        )
    try:
        accepted_latency_budget = load_accepted_supervisor_latency_budget(
            latency_budget_raw,
            expected_document_sha256=expected_latency_budget_sha256,
        )
    except (PromotedProductEventError, TypeError):
        return _closed_material(
            AssistPromotionActivationReason.LATENCY_BUDGET_INVALID,
            requested_mode=mode,
            source_revision_sha256=source_sha256,
        )
    if accepted_latency_budget.document.target_mode is not mode or not hmac.compare_digest(
        accepted_latency_budget.document.source_revision_sha256,
        source_sha256,
    ):
        return _closed_material(
            AssistPromotionActivationReason.LATENCY_BUDGET_IDENTITY_MISMATCH,
            requested_mode=mode,
            source_revision_sha256=source_sha256,
            accepted_latency_budget=None,
        )
    current_registry_sha256 = binding_snapshot.digest_hex()
    if not hmac.compare_digest(current_registry_sha256, expected_registry_sha256):
        return _closed_material(
            AssistPromotionActivationReason.REGISTRY_BINDING_MISMATCH,
            requested_mode=mode,
            source_revision_sha256=source_sha256,
            accepted_latency_budget=accepted_latency_budget,
            registry_binding_sha256=current_registry_sha256,
        )
    try:
        scheduler_snapshot = scheduler_admission_snapshot_from_status(
            scheduler_public_status,
            scheduler_diagnostics_status,
        )
    except AssistPromotionActivationError as exc:
        return _closed_material(
            exc.reason,
            requested_mode=mode,
            source_revision_sha256=source_sha256,
            accepted_latency_budget=accepted_latency_budget,
            registry_binding_sha256=current_registry_sha256,
        )
    if scheduler_snapshot.requested_mode != mode.value:
        return _closed_material(
            AssistPromotionActivationReason.SCHEDULER_IDENTITY_MISMATCH,
            requested_mode=mode,
            source_revision_sha256=source_sha256,
            accepted_latency_budget=accepted_latency_budget,
            registry_binding_sha256=current_registry_sha256,
            scheduler_snapshot=scheduler_snapshot,
        )
    try:
        loaded_evidence = load_assist_promotion_live_evidence(
            evidence_path,
            expected_evidence_sha256,
        )
    except AssistPromotionActivationError as exc:
        return _closed_material(
            exc.reason,
            requested_mode=mode,
            source_revision_sha256=source_sha256,
            registry_binding_sha256=current_registry_sha256,
            scheduler_snapshot=scheduler_snapshot,
        )
    if not _evidence_identity_matches(
        loaded_evidence.evidence,
        requested_mode=mode,
        source_revision_sha256=source_sha256,
        registry_binding_sha256=current_registry_sha256,
        accepted_latency_budget=accepted_latency_budget,
    ):
        return _closed_material(
            AssistPromotionActivationReason.EVIDENCE_IDENTITY_MISMATCH,
            requested_mode=mode,
            source_revision_sha256=source_sha256,
            registry_binding_sha256=current_registry_sha256,
            scheduler_snapshot=scheduler_snapshot,
            loaded_evidence=loaded_evidence,
            accepted_latency_budget=accepted_latency_budget,
        )
    try:
        gate = AssistPromotionOperatorGate(
            enabled=True,
            gate_id=SUPERVISOR_ASSIST_PROMOTION_GATE_ID,
            promotion_policy_sha256=SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256,
            target_mode=mode,
            task_class=TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
            source_revision_sha256=source_sha256,
            registry_binding_sha256=current_registry_sha256,
            accepted_evidence_sha256=loaded_evidence.evidence.canonical_sha256(),
            canary_actor_bindings=actor_bindings,
        )
    except (TypeError, ValueError):
        return _closed_material(
            AssistPromotionActivationReason.RAW_SETTINGS_INVALID,
            requested_mode=mode,
            source_revision_sha256=source_sha256,
            registry_binding_sha256=current_registry_sha256,
            scheduler_snapshot=scheduler_snapshot,
            loaded_evidence=loaded_evidence,
            accepted_latency_budget=accepted_latency_budget,
        )
    return AssistPromotionActivationMaterial(
        configured=True,
        reason=AssistPromotionActivationReason.MATERIAL_LOADED_NOT_ACCEPTED,
        requested_mode=mode,
        source_revision_sha256=source_sha256,
        registry_binding_sha256=current_registry_sha256,
        scheduler_snapshot=scheduler_snapshot,
        loaded_evidence=loaded_evidence,
        accepted_latency_budget=accepted_latency_budget,
        operator_gate=gate,
    )


__all__ = [
    "AssistPromotionActivationError",
    "AssistPromotionActivationMaterial",
    "AssistPromotionActivationReason",
    "LoadedAssistPromotionEvidence",
    "RawAssistPromotionActivationSettings",
    "SUPERVISOR_ASSIST_ACTIVATION_STATUS_SCHEMA",
    "derive_installed_release_tree_sha256",
    "load_assist_promotion_activation",
    "load_assist_promotion_live_evidence",
    "parse_assist_promotion_live_evidence",
    "scheduler_admission_snapshot_from_status",
]
