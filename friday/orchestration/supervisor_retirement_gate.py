"""Fail-closed P6 review over repository-bound retirement artifacts.

Source inspection can accept exact Git objects, but source code cannot prove a
joined production window or that a release rollback was exercised.  Therefore
the factories in this module issue source-only evidence and rollback witnesses;
the gate records what is still missing and never grants deletion authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from friday.orchestration.supervisor_contracts import (
    TaskClass,
    canonical_dumps,
    canonical_sha256,
)
from friday.orchestration.supervisor_retirement_repository import (
    AcceptedRepositoryRetirementCandidate,
    AcceptedRepositoryRetirementSurface,
    ExactRepositoryFile,
    RetirementSurfaceClass,
    accepted_repository_candidate_is_current,
    accepted_repository_file_is_current,
    accepted_repository_surface_is_current,
    inspect_repository_retirement_surface,
    read_exact_repository_file,
    repository_commit_is_ancestor,
)

SUPERVISOR_RETIREMENT_EVIDENCE_SCHEMA = "friday.supervisor-retirement-source-evidence.v2"
SUPERVISOR_RETIREMENT_ROLLBACK_SCHEMA = "friday.supervisor-retirement-source-rollback.v2"
SUPERVISOR_RETIREMENT_GATE_VERSION = "semantic-supervisor-retirement-gate-v2"

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ID_RE = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")
_MAX_EVIDENCE_BYTES = 32 * 1024
_EVIDENCE_PATH_PREFIX = "outer_sol/"

_PROCESS_AUTHORITY = object()
_PROCESS_SEAL_KEY = secrets.token_bytes(32)


class RetirementGateError(ValueError):
    """An artifact is malformed, unbound, or not accepted by this process."""


class RetirementEvidenceAuthority(StrEnum):
    SOURCE_BOUND_ONLY = "source_bound_only"
    PRODUCTION_JOINED = "production_joined"


class RetirementRollbackAuthority(StrEnum):
    SOURCE_PREIMAGE_ONLY = "source_preimage_only"
    SEALED_RELEASE_REHEARSAL = "sealed_release_rehearsal"


class RetirementGateReason(StrEnum):
    ADMITTED = "admitted"
    CANDIDATE_IDENTITY_MISMATCH = "candidate_identity_mismatch"
    PRODUCTION_EVIDENCE_REQUIRED = "production_evidence_required"
    SEALED_RELEASE_ROLLBACK_REQUIRED = "sealed_release_rollback_required"
    TRACE_COVERAGE_INCOMPLETE = "trace_coverage_incomplete"
    HIDDEN_OWNER_OBSERVED = "hidden_owner_observed"
    DUPLICATE_OPERATION_OBSERVED = "duplicate_operation_observed"
    FALSE_COMPLETION_REGRESSION = "false_completion_regression"
    USER_VISIBLE_REGRESSION = "user_visible_regression"


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise RetirementGateError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _safe_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise RetirementGateError(f"{label} is invalid")
    return value


def _count(value: object, *, label: str, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise RetirementGateError(f"{label} must be an integer >= {minimum}")
    return value


def _seal(kind: str, payload: dict[str, object]) -> str:
    envelope = canonical_dumps({"kind": kind, "payload": payload}).encode("utf-8")
    return hmac.new(_PROCESS_SEAL_KEY, envelope, hashlib.sha256).hexdigest()


def _closed_json(raw: bytes) -> dict[str, Any]:
    if not 0 < len(raw) <= _MAX_EVIDENCE_BYTES:
        raise RetirementGateError("retirement evidence exceeds its byte budget")

    def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise RetirementGateError("retirement evidence contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_pairs)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RetirementGateError("retirement evidence must be canonical UTF-8 JSON") from exc
    if type(value) is not dict:
        raise RetirementGateError("retirement evidence must be a JSON object")
    if raw != canonical_dumps(value).encode("utf-8"):
        raise RetirementGateError("retirement evidence must use canonical JSON bytes")
    return value


_EVIDENCE_KEYS = {
    "schema",
    "evidence_id",
    "candidate_sha256",
    "journey",
    "deletion_commit",
    "shadow_bundle_sha256",
    "canary_bundle_sha256",
    "promoted_journey_sha256",
    "primary_fallback_sha256",
    "production_trace_set_sha256",
    "observation_count",
    "joined_trace_count",
    "hidden_owner_count",
    "duplicate_capability_count",
    "duplicate_effect_count",
    "duplicate_publication_count",
    "false_completion_regression_count",
    "user_visible_regression_count",
}


@dataclass(frozen=True, slots=True)
class AcceptedSourceRetirementEvidence:
    """Canonical Git artifact; explicitly not a production attestation."""

    evidence_id: str
    candidate_sha256: str
    journey: TaskClass
    deletion_commit: str
    shadow_bundle_sha256: str
    canary_bundle_sha256: str
    promoted_journey_sha256: str
    primary_fallback_sha256: str
    production_trace_set_sha256: str
    observation_count: int
    joined_trace_count: int
    hidden_owner_count: int
    duplicate_capability_count: int
    duplicate_effect_count: int
    duplicate_publication_count: int
    false_completion_regression_count: int
    user_visible_regression_count: int
    repository_artifact: ExactRepositoryFile
    authority: RetirementEvidenceAuthority = field(init=False)
    _process_authority: object = field(repr=False, compare=False)
    _process_seal_sha256: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "authority", RetirementEvidenceAuthority.SOURCE_BOUND_ONLY)
        payload = self.payload()
        if (
            not accepted_repository_file_is_current(self.repository_artifact)
            or not self.repository_artifact.source_path.startswith(_EVIDENCE_PATH_PREFIX)
            or not self.repository_artifact.source_path.endswith(".json")
            or self._process_authority is not _PROCESS_AUTHORITY
            or type(self._process_seal_sha256) is not str
            or not hmac.compare_digest(
                self._process_seal_sha256,
                _seal("source-evidence", payload),
            )
        ):
            raise RetirementGateError("retirement evidence was not accepted by this process")
        _safe_id(self.evidence_id, label="evidence_id")
        if self.journey is not TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB:
            raise RetirementGateError("retirement evidence journey is not promotable")
        for label in (
            "candidate_sha256",
            "shadow_bundle_sha256",
            "canary_bundle_sha256",
            "promoted_journey_sha256",
            "primary_fallback_sha256",
            "production_trace_set_sha256",
        ):
            _digest(getattr(self, label), label=label)
        _count(self.observation_count, label="observation_count", positive=True)
        _count(self.joined_trace_count, label="joined_trace_count")
        for label in (
            "hidden_owner_count",
            "duplicate_capability_count",
            "duplicate_effect_count",
            "duplicate_publication_count",
            "false_completion_regression_count",
            "user_visible_regression_count",
        ):
            _count(getattr(self, label), label=label)
        if self.joined_trace_count > self.observation_count:
            raise RetirementGateError("joined_trace_count exceeds observation_count")

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_RETIREMENT_EVIDENCE_SCHEMA,
            "evidence_id": self.evidence_id,
            "candidate_sha256": self.candidate_sha256,
            "journey": self.journey.value,
            "deletion_commit": self.deletion_commit,
            "shadow_bundle_sha256": self.shadow_bundle_sha256,
            "canary_bundle_sha256": self.canary_bundle_sha256,
            "promoted_journey_sha256": self.promoted_journey_sha256,
            "primary_fallback_sha256": self.primary_fallback_sha256,
            "production_trace_set_sha256": self.production_trace_set_sha256,
            "observation_count": self.observation_count,
            "joined_trace_count": self.joined_trace_count,
            "hidden_owner_count": self.hidden_owner_count,
            "duplicate_capability_count": self.duplicate_capability_count,
            "duplicate_effect_count": self.duplicate_effect_count,
            "duplicate_publication_count": self.duplicate_publication_count,
            "false_completion_regression_count": self.false_completion_regression_count,
            "user_visible_regression_count": self.user_visible_regression_count,
            "repository_artifact": self.repository_artifact.payload(),
            "authority": self.authority.value,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())


def accepted_source_evidence_is_current(value: object) -> bool:
    if (
        type(value) is not AcceptedSourceRetirementEvidence
        or value._process_authority is not _PROCESS_AUTHORITY
        or value.authority is not RetirementEvidenceAuthority.SOURCE_BOUND_ONLY
        or not accepted_repository_file_is_current(value.repository_artifact)
    ):
        return False
    expected = _seal("source-evidence", value.payload())
    return type(value._process_seal_sha256) is str and hmac.compare_digest(
        value._process_seal_sha256,
        expected,
    )


def accept_source_retirement_evidence(
    repository_root: str | Path,
    *,
    candidate: AcceptedRepositoryRetirementCandidate,
    evidence_commit: str,
    evidence_path: str,
    expected_file_sha256: str,
) -> AcceptedSourceRetirementEvidence:
    """Structurally accept a canonical evidence artifact from one Git commit.

    The returned authority remains ``SOURCE_BOUND_ONLY``.  A future release
    component must join and attest production records; this loader cannot.
    """

    if not accepted_repository_candidate_is_current(candidate):
        raise TypeError("candidate must be accepted by this process")
    _digest(expected_file_sha256, label="expected_file_sha256")
    if not evidence_path.startswith(_EVIDENCE_PATH_PREFIX) or not evidence_path.endswith(".json"):
        raise RetirementGateError("evidence_path must be a repository evidence JSON path")
    if not repository_commit_is_ancestor(
        repository_root,
        ancestor_commit=candidate.deletion_commit,
        descendant_commit=evidence_commit,
    ):
        raise RetirementGateError("evidence_commit must descend from the deletion commit")
    artifact = read_exact_repository_file(
        repository_root,
        source_commit=evidence_commit,
        source_path=evidence_path,
    )
    if not hmac.compare_digest(artifact.file_sha256, expected_file_sha256):
        raise RetirementGateError("retirement evidence file digest mismatch")
    raw = _closed_json(artifact.raw_bytes())
    if set(raw) != _EVIDENCE_KEYS:
        raise RetirementGateError("retirement evidence keys are not closed")
    if raw.get("schema") != SUPERVISOR_RETIREMENT_EVIDENCE_SCHEMA:
        raise RetirementGateError("retirement evidence schema is invalid")
    candidate_sha256 = candidate.canonical_sha256()
    if raw.get("candidate_sha256") != candidate_sha256:
        raise RetirementGateError("retirement evidence candidate identity mismatch")
    if raw.get("journey") != candidate.journey.value:
        raise RetirementGateError("retirement evidence journey mismatch")
    if raw.get("deletion_commit") != candidate.deletion_commit:
        raise RetirementGateError("retirement evidence deletion commit mismatch")

    evidence_id = _safe_id(raw.get("evidence_id"), label="evidence_id")
    digests = {
        label: _digest(raw.get(label), label=label)
        for label in (
            "shadow_bundle_sha256",
            "canary_bundle_sha256",
            "promoted_journey_sha256",
            "primary_fallback_sha256",
            "production_trace_set_sha256",
        )
    }
    counts = {
        "observation_count": _count(
            raw.get("observation_count"),
            label="observation_count",
            positive=True,
        ),
        "joined_trace_count": _count(raw.get("joined_trace_count"), label="joined_trace_count"),
        **{
            label: _count(raw.get(label), label=label)
            for label in (
                "hidden_owner_count",
                "duplicate_capability_count",
                "duplicate_effect_count",
                "duplicate_publication_count",
                "false_completion_regression_count",
                "user_visible_regression_count",
            )
        },
    }
    if counts["joined_trace_count"] > counts["observation_count"]:
        raise RetirementGateError("joined_trace_count exceeds observation_count")
    fields: dict[str, object] = {
        "schema": SUPERVISOR_RETIREMENT_EVIDENCE_SCHEMA,
        "evidence_id": evidence_id,
        "candidate_sha256": candidate_sha256,
        "journey": candidate.journey.value,
        "deletion_commit": candidate.deletion_commit,
        **digests,
        **counts,
        "repository_artifact": artifact.payload(),
        "authority": RetirementEvidenceAuthority.SOURCE_BOUND_ONLY.value,
    }
    return AcceptedSourceRetirementEvidence(
        evidence_id=evidence_id,
        candidate_sha256=candidate_sha256,
        journey=candidate.journey,
        deletion_commit=candidate.deletion_commit,
        shadow_bundle_sha256=digests["shadow_bundle_sha256"],
        canary_bundle_sha256=digests["canary_bundle_sha256"],
        promoted_journey_sha256=digests["promoted_journey_sha256"],
        primary_fallback_sha256=digests["primary_fallback_sha256"],
        production_trace_set_sha256=digests["production_trace_set_sha256"],
        observation_count=counts["observation_count"],
        joined_trace_count=counts["joined_trace_count"],
        hidden_owner_count=counts["hidden_owner_count"],
        duplicate_capability_count=counts["duplicate_capability_count"],
        duplicate_effect_count=counts["duplicate_effect_count"],
        duplicate_publication_count=counts["duplicate_publication_count"],
        false_completion_regression_count=counts["false_completion_regression_count"],
        user_visible_regression_count=counts["user_visible_regression_count"],
        repository_artifact=artifact,
        _process_authority=_PROCESS_AUTHORITY,
        _process_seal_sha256=_seal("source-evidence", fields),
    )


@dataclass(frozen=True, slots=True)
class AcceptedSourceRollbackWitness:
    """Exact restored AST preimage; explicitly not a live release rehearsal."""

    candidate_sha256: str
    rollback_surface: AcceptedRepositoryRetirementSurface
    deletion_commit: str
    authority: RetirementRollbackAuthority = field(init=False)
    _process_authority: object = field(repr=False, compare=False)
    _process_seal_sha256: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "authority", RetirementRollbackAuthority.SOURCE_PREIMAGE_ONLY)
        payload = self.payload()
        if (
            not accepted_repository_surface_is_current(self.rollback_surface)
            or self._process_authority is not _PROCESS_AUTHORITY
            or type(self._process_seal_sha256) is not str
            or not hmac.compare_digest(
                self._process_seal_sha256,
                _seal("source-rollback", payload),
            )
        ):
            raise RetirementGateError("rollback witness was not accepted by this process")
        _digest(self.candidate_sha256, label="candidate_sha256")

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_RETIREMENT_ROLLBACK_SCHEMA,
            "candidate_sha256": self.candidate_sha256,
            "deletion_commit": self.deletion_commit,
            "rollback_surface": self.rollback_surface.payload(),
            "authority": self.authority.value,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())


def accepted_source_rollback_is_current(value: object) -> bool:
    if (
        type(value) is not AcceptedSourceRollbackWitness
        or value._process_authority is not _PROCESS_AUTHORITY
        or value.authority is not RetirementRollbackAuthority.SOURCE_PREIMAGE_ONLY
        or not accepted_repository_surface_is_current(value.rollback_surface)
    ):
        return False
    expected = _seal("source-rollback", value.payload())
    return type(value._process_seal_sha256) is str and hmac.compare_digest(
        value._process_seal_sha256,
        expected,
    )


def accept_source_rollback_witness(
    repository_root: str | Path,
    *,
    candidate: AcceptedRepositoryRetirementCandidate,
    rollback_commit: str,
) -> AcceptedSourceRollbackWitness:
    """Accept an exact post-deletion commit restoring the original AST node."""

    if not accepted_repository_candidate_is_current(candidate):
        raise TypeError("candidate must be accepted by this process")
    if not repository_commit_is_ancestor(
        repository_root,
        ancestor_commit=candidate.deletion_commit,
        descendant_commit=rollback_commit,
    ):
        raise RetirementGateError("rollback_commit must descend from the deletion commit")
    restored = inspect_repository_retirement_surface(
        repository_root,
        source_commit=rollback_commit,
        candidate_id=candidate.candidate_id,
    )
    predecessor = candidate.predecessor_surface
    if (
        restored.descriptor != predecessor.descriptor
        or restored.source_node_kind != predecessor.source_node_kind
        or not hmac.compare_digest(restored.source_node_sha256, predecessor.source_node_sha256)
    ):
        raise RetirementGateError("rollback commit did not restore the exact AST preimage")
    fields: dict[str, object] = {
        "schema": SUPERVISOR_RETIREMENT_ROLLBACK_SCHEMA,
        "candidate_sha256": candidate.canonical_sha256(),
        "deletion_commit": candidate.deletion_commit,
        "rollback_surface": restored.payload(),
        "authority": RetirementRollbackAuthority.SOURCE_PREIMAGE_ONLY.value,
    }
    return AcceptedSourceRollbackWitness(
        candidate_sha256=candidate.canonical_sha256(),
        rollback_surface=restored,
        deletion_commit=candidate.deletion_commit,
        _process_authority=_PROCESS_AUTHORITY,
        _process_seal_sha256=_seal("source-rollback", fields),
    )


@dataclass(frozen=True, slots=True)
class HeuristicRetirementDecision:
    admitted: bool
    reason: RetirementGateReason
    candidate_id: str
    candidate_sha256: str
    evidence_sha256: str | None = None
    rollback_sha256: str | None = None


def _reject(
    candidate: AcceptedRepositoryRetirementCandidate,
    reason: RetirementGateReason,
) -> HeuristicRetirementDecision:
    return HeuristicRetirementDecision(
        admitted=False,
        reason=reason,
        candidate_id=candidate.candidate_id,
        candidate_sha256=candidate.canonical_sha256(),
    )


def evaluate_heuristic_retirement(
    candidate: AcceptedRepositoryRetirementCandidate,
    evidence: AcceptedSourceRetirementEvidence,
    rollback: AcceptedSourceRollbackWitness,
) -> HeuristicRetirementDecision:
    """Evaluate source-complete P6 inputs without granting release authority."""

    if not accepted_repository_candidate_is_current(candidate):
        raise TypeError("candidate must be accepted by this process")
    if not accepted_source_evidence_is_current(evidence):
        raise TypeError("evidence must be accepted by this process")
    if not accepted_source_rollback_is_current(rollback):
        raise TypeError("rollback must be accepted by this process")
    candidate_sha256 = candidate.canonical_sha256()
    if (
        not hmac.compare_digest(evidence.candidate_sha256, candidate_sha256)
        or not hmac.compare_digest(rollback.candidate_sha256, candidate_sha256)
        or evidence.deletion_commit != candidate.deletion_commit
        or rollback.deletion_commit != candidate.deletion_commit
    ):
        return _reject(candidate, RetirementGateReason.CANDIDATE_IDENTITY_MISMATCH)

    # Source files cannot attest joined production observations.  There is no
    # source factory for PRODUCTION_JOINED and no constructor path that can
    # upgrade this accepted object's authority.
    if evidence.authority is not RetirementEvidenceAuthority.PRODUCTION_JOINED:
        return _reject(candidate, RetirementGateReason.PRODUCTION_EVIDENCE_REQUIRED)
    if rollback.authority is not RetirementRollbackAuthority.SEALED_RELEASE_REHEARSAL:
        return _reject(candidate, RetirementGateReason.SEALED_RELEASE_ROLLBACK_REQUIRED)

    checks = (
        (
            evidence.joined_trace_count == evidence.observation_count,
            RetirementGateReason.TRACE_COVERAGE_INCOMPLETE,
        ),
        (evidence.hidden_owner_count == 0, RetirementGateReason.HIDDEN_OWNER_OBSERVED),
        (
            evidence.duplicate_capability_count == 0
            and evidence.duplicate_effect_count == 0
            and evidence.duplicate_publication_count == 0,
            RetirementGateReason.DUPLICATE_OPERATION_OBSERVED,
        ),
        (
            evidence.false_completion_regression_count == 0,
            RetirementGateReason.FALSE_COMPLETION_REGRESSION,
        ),
        (
            evidence.user_visible_regression_count == 0,
            RetirementGateReason.USER_VISIBLE_REGRESSION,
        ),
    )
    for accepted, reason in checks:
        if not accepted:
            return _reject(candidate, reason)
    return HeuristicRetirementDecision(
        admitted=True,
        reason=RetirementGateReason.ADMITTED,
        candidate_id=candidate.candidate_id,
        candidate_sha256=candidate_sha256,
        evidence_sha256=evidence.canonical_sha256(),
        rollback_sha256=rollback.canonical_sha256(),
    )


__all__ = [
    "AcceptedSourceRetirementEvidence",
    "AcceptedSourceRollbackWitness",
    "HeuristicRetirementDecision",
    "RetirementEvidenceAuthority",
    "RetirementGateError",
    "RetirementGateReason",
    "RetirementRollbackAuthority",
    "RetirementSurfaceClass",
    "SUPERVISOR_RETIREMENT_EVIDENCE_SCHEMA",
    "SUPERVISOR_RETIREMENT_GATE_VERSION",
    "SUPERVISOR_RETIREMENT_ROLLBACK_SCHEMA",
    "accept_source_retirement_evidence",
    "accept_source_rollback_witness",
    "accepted_source_evidence_is_current",
    "accepted_source_rollback_is_current",
    "evaluate_heuristic_retirement",
]
