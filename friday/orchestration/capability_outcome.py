"""Closed outcome and completion gate for the first promoted V12 read routes.

The contract is deliberately smaller than the future cross-capability outcome
ledger.  It carries no prose, paths, object identifiers, or evidence bodies.
Only the existing ``file_read`` and ``archive_read`` handlers may produce v1.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from friday.orchestration.contracts import RouteClass
from friday.orchestration.file_read_contract import validate_file_synthesis_answer

CAPABILITY_OUTCOME_SCHEMA = "friday.capability-outcome.v1"
CAPABILITY_OUTCOME_RECEIPT_SCHEMA = "friday.accepted-capability-outcome-receipt.v1"
ACCEPTED_CAPABILITY_OUTCOME_METADATA_KEY = "accepted_capability_outcome"

_DIGEST = re.compile(r"[0-9a-f]{64}")
_CITATION = re.compile(r"A[1-9][0-9]{0,2}")
_MAX_SERIALIZED_BYTES = 4_096
_MAX_RECEIPT_SERIALIZED_BYTES = 8_192
_MAX_ASSISTANT_METADATA_BYTES = 65_536
_MAX_CITATIONS = 32
_PROMOTED_ROUTES = frozenset({RouteClass.FILE_READ, RouteClass.ARCHIVE_READ})


class CapabilityOutcomeError(ValueError):
    """A value is outside the closed capability outcome/gate contract."""


class CapabilityOutcomeStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"


class CapabilityOutcomeReason(StrEnum):
    NONE = "none"
    PARTIAL_COVERAGE = "partial_coverage"
    NO_EVIDENCE = "no_evidence"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    AUTHORITY_DENIED = "authority_denied"


class CompletionGateDecision(StrEnum):
    READY_TO_PUBLISH = "ready_to_publish"
    RETURN_PARTIAL = "return_partial"
    RETURN_EMPTY = "return_empty"
    RETRY = "retry"
    DENY = "deny"


_REASON_BY_STATUS = {
    CapabilityOutcomeStatus.COMPLETE: CapabilityOutcomeReason.NONE,
    CapabilityOutcomeStatus.PARTIAL: CapabilityOutcomeReason.PARTIAL_COVERAGE,
    CapabilityOutcomeStatus.EMPTY: CapabilityOutcomeReason.NO_EVIDENCE,
    CapabilityOutcomeStatus.UNAVAILABLE: CapabilityOutcomeReason.CAPABILITY_UNAVAILABLE,
    CapabilityOutcomeStatus.DENIED: CapabilityOutcomeReason.AUTHORITY_DENIED,
}
_DECISION_BY_STATUS = {
    CapabilityOutcomeStatus.COMPLETE: CompletionGateDecision.READY_TO_PUBLISH,
    CapabilityOutcomeStatus.PARTIAL: CompletionGateDecision.RETURN_PARTIAL,
    CapabilityOutcomeStatus.EMPTY: CompletionGateDecision.RETURN_EMPTY,
    CapabilityOutcomeStatus.UNAVAILABLE: CompletionGateDecision.RETRY,
    CapabilityOutcomeStatus.DENIED: CompletionGateDecision.DENY,
}


def _digest(value: object, *, label: str, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise CapabilityOutcomeError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _boolean(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise CapabilityOutcomeError(f"{label} must be a boolean")
    return value


def _citations(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise CapabilityOutcomeError("citation_labels must be an immutable tuple")
    if (
        len(value) > _MAX_CITATIONS
        or len(set(value)) != len(value)
        or any(not isinstance(label, str) or _CITATION.fullmatch(label) is None for label in value)
    ):
        raise CapabilityOutcomeError("citation_labels are outside the closed contract")
    return value


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CapabilityOutcomeError("capability outcome contains a duplicate object key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class CapabilityOutcome:
    """Immutable structural result of one bounded read capability."""

    route: RouteClass
    status: CapabilityOutcomeStatus
    plan_sha256: str
    evidence_identity_sha256: str | None
    citation_labels: tuple[str, ...]
    authority_rechecked: bool
    verified: bool

    def __post_init__(self) -> None:
        if not isinstance(self.route, RouteClass) or self.route not in _PROMOTED_ROUTES:
            raise CapabilityOutcomeError("route is not admitted by capability outcome v1")
        if not isinstance(self.status, CapabilityOutcomeStatus):
            raise CapabilityOutcomeError("status must be a CapabilityOutcomeStatus")
        _digest(self.plan_sha256, label="plan_sha256")
        _digest(
            self.evidence_identity_sha256,
            label="evidence_identity_sha256",
            optional=True,
        )
        _citations(self.citation_labels)
        _boolean(self.authority_rechecked, label="authority_rechecked")
        _boolean(self.verified, label="verified")
        self._validate_status_shape()

    def _validate_status_shape(self) -> None:
        has_evidence = self.evidence_identity_sha256 is not None
        has_citations = bool(self.citation_labels)
        if self.status in {CapabilityOutcomeStatus.COMPLETE, CapabilityOutcomeStatus.PARTIAL}:
            if not (has_evidence and has_citations and self.authority_rechecked and self.verified):
                raise CapabilityOutcomeError(
                    "complete and partial outcomes require authorized verified cited evidence"
                )
            return
        if self.status is CapabilityOutcomeStatus.EMPTY:
            if not (has_evidence and not has_citations and self.authority_rechecked and self.verified):
                raise CapabilityOutcomeError(
                    "empty outcomes require an authorized verified empty evidence set"
                )
            return
        if self.status is CapabilityOutcomeStatus.UNAVAILABLE:
            if has_evidence or has_citations or self.authority_rechecked or self.verified:
                raise CapabilityOutcomeError(
                    "unavailable outcomes cannot claim evidence, authority recheck, or verification"
                )
            return
        if self.status is CapabilityOutcomeStatus.DENIED and (
            has_evidence or has_citations or not self.authority_rechecked or self.verified
        ):
            raise CapabilityOutcomeError(
                "denied outcomes require a completed authority check and no accepted evidence"
            )

    @property
    def reason_code(self) -> CapabilityOutcomeReason:
        return _REASON_BY_STATUS[self.status]

    @property
    def retryable(self) -> bool:
        # A verified partial/empty result is stable.  Authority denial must never
        # be retried through another path.  Only transient unavailability is
        # retryable in this first read-only contract.
        return self.status is CapabilityOutcomeStatus.UNAVAILABLE

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": CAPABILITY_OUTCOME_SCHEMA,
            "route": self.route.value,
            "status": self.status.value,
            "plan_sha256": self.plan_sha256,
            "evidence_identity_sha256": self.evidence_identity_sha256,
            "citation_labels": list(self.citation_labels),
            "authority_rechecked": self.authority_rechecked,
            "verified": self.verified,
            "retryable": self.retryable,
            "reason_code": self.reason_code.value,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("ascii")).hexdigest()

    @classmethod
    def parse(cls, value: str | Mapping[str, object]) -> CapabilityOutcome:
        if isinstance(value, str):
            try:
                encoded = value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise CapabilityOutcomeError("capability outcome JSON must be valid UTF-8") from exc
            if len(encoded) > _MAX_SERIALIZED_BYTES:
                raise CapabilityOutcomeError("capability outcome JSON is too large")
            try:
                decoded = json.loads(value, object_pairs_hook=_closed_object)
            except json.JSONDecodeError as exc:
                raise CapabilityOutcomeError("capability outcome must be one JSON object") from exc
        else:
            decoded = value
        if not isinstance(decoded, Mapping):
            raise CapabilityOutcomeError("capability outcome must be an object")
        expected = {
            "schema",
            "route",
            "status",
            "plan_sha256",
            "evidence_identity_sha256",
            "citation_labels",
            "authority_rechecked",
            "verified",
            "retryable",
            "reason_code",
        }
        if any(not isinstance(key, str) for key in decoded) or set(decoded) != expected:
            raise CapabilityOutcomeError("capability outcome keys do not match the closed contract")
        if decoded["schema"] != CAPABILITY_OUTCOME_SCHEMA:
            raise CapabilityOutcomeError("capability outcome schema is not supported")
        try:
            route = RouteClass(decoded["route"])
            status = CapabilityOutcomeStatus(decoded["status"])
        except (TypeError, ValueError) as exc:
            raise CapabilityOutcomeError("capability outcome contains an unknown enum value") from exc
        raw_citations = decoded["citation_labels"]
        if not isinstance(raw_citations, list):
            raise CapabilityOutcomeError("citation_labels must be an array")
        outcome = cls(
            route=route,
            status=status,
            plan_sha256=str(_digest(decoded["plan_sha256"], label="plan_sha256")),
            evidence_identity_sha256=_digest(
                decoded["evidence_identity_sha256"],
                label="evidence_identity_sha256",
                optional=True,
            ),
            citation_labels=_citations(tuple(raw_citations)),
            authority_rechecked=_boolean(decoded["authority_rechecked"], label="authority_rechecked"),
            verified=_boolean(decoded["verified"], label="verified"),
        )
        if (
            decoded["retryable"] is not outcome.retryable
            or decoded["reason_code"] != outcome.reason_code.value
        ):
            raise CapabilityOutcomeError("retryability or reason does not match the closed status")
        return outcome


@dataclass(frozen=True, slots=True)
class AcceptedCapabilityOutcomeReceipt:
    """Durable private receipt for one outcome accepted for publication.

    The receipt repeats no evidence body, path, object identifier, or prose.  Its
    digest binds the exact closed outcome payload that was accepted in the same
    transaction as the assistant message.
    """

    outcome: CapabilityOutcome
    outcome_sha256: str

    def __post_init__(self) -> None:
        if type(self.outcome) is not CapabilityOutcome:
            raise CapabilityOutcomeError("accepted outcome receipt requires CapabilityOutcome v1")
        digest = _digest(self.outcome_sha256, label="outcome_sha256")
        if digest != self.outcome.canonical_sha256():
            raise CapabilityOutcomeError("accepted outcome receipt digest does not match its outcome")

    @classmethod
    def from_outcome(cls, outcome: CapabilityOutcome) -> AcceptedCapabilityOutcomeReceipt:
        if type(outcome) is not CapabilityOutcome:
            raise CapabilityOutcomeError("accepted outcome receipt requires CapabilityOutcome v1")
        return cls(outcome=outcome, outcome_sha256=outcome.canonical_sha256())

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": CAPABILITY_OUTCOME_RECEIPT_SCHEMA,
            "outcome": self.outcome.to_payload(),
            "outcome_sha256": self.outcome_sha256,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("ascii")).hexdigest()

    @classmethod
    def parse(
        cls,
        value: str | Mapping[str, object],
    ) -> AcceptedCapabilityOutcomeReceipt:
        if isinstance(value, str):
            try:
                encoded = value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise CapabilityOutcomeError("accepted outcome receipt JSON must be valid UTF-8") from exc
            if len(encoded) > _MAX_RECEIPT_SERIALIZED_BYTES:
                raise CapabilityOutcomeError("accepted outcome receipt JSON is too large")
            try:
                decoded = json.loads(value, object_pairs_hook=_closed_object)
            except json.JSONDecodeError as exc:
                raise CapabilityOutcomeError("accepted outcome receipt must be one JSON object") from exc
        else:
            decoded = value
        if not isinstance(decoded, Mapping):
            raise CapabilityOutcomeError("accepted outcome receipt must be an object")
        expected = {"schema", "outcome", "outcome_sha256"}
        if any(not isinstance(key, str) for key in decoded) or set(decoded) != expected:
            raise CapabilityOutcomeError("accepted outcome receipt keys do not match the closed contract")
        if decoded["schema"] != CAPABILITY_OUTCOME_RECEIPT_SCHEMA:
            raise CapabilityOutcomeError("accepted outcome receipt schema is not supported")
        raw_outcome = decoded["outcome"]
        if not isinstance(raw_outcome, Mapping):
            raise CapabilityOutcomeError("accepted outcome receipt has no outcome object")
        return cls(
            outcome=CapabilityOutcome.parse(raw_outcome),
            outcome_sha256=str(_digest(decoded["outcome_sha256"], label="outcome_sha256")),
        )


def attach_accepted_capability_outcome_receipt(
    metadata: dict[str, Any],
    outcome: CapabilityOutcome,
    *,
    max_serialized_bytes: int = _MAX_ASSISTANT_METADATA_BYTES,
) -> AcceptedCapabilityOutcomeReceipt:
    """Attach one mandatory receipt without overwriting state or exceeding budget."""

    if type(metadata) is not dict:
        raise CapabilityOutcomeError("accepted outcome metadata carrier must be a dictionary")
    if ACCEPTED_CAPABILITY_OUTCOME_METADATA_KEY in metadata:
        raise CapabilityOutcomeError("accepted outcome receipt is already attached")
    if (
        not isinstance(max_serialized_bytes, int)
        or isinstance(max_serialized_bytes, bool)
        or max_serialized_bytes <= 0
        or max_serialized_bytes > _MAX_ASSISTANT_METADATA_BYTES
    ):
        raise CapabilityOutcomeError("accepted outcome metadata budget is outside the closed limit")
    receipt = AcceptedCapabilityOutcomeReceipt.from_outcome(outcome)
    candidate = dict(metadata)
    candidate[ACCEPTED_CAPABILITY_OUTCOME_METADATA_KEY] = receipt.to_payload()
    try:
        encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise CapabilityOutcomeError("accepted outcome metadata cannot be serialized") from exc
    if len(encoded) > max_serialized_bytes:
        raise CapabilityOutcomeError("accepted outcome metadata exceeds the bounded carrier")
    metadata[ACCEPTED_CAPABILITY_OUTCOME_METADATA_KEY] = receipt.to_payload()
    return receipt


def load_accepted_capability_outcome_receipt(
    metadata: object,
    *,
    expected_outcome: CapabilityOutcome | None = None,
) -> AcceptedCapabilityOutcomeReceipt:
    """Load and validate the receipt retained in private assistant metadata."""

    if isinstance(metadata, str):
        try:
            encoded = metadata.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise CapabilityOutcomeError("accepted outcome metadata must be valid UTF-8") from exc
        if len(encoded) > _MAX_ASSISTANT_METADATA_BYTES:
            raise CapabilityOutcomeError("accepted outcome metadata exceeds the bounded carrier")
        try:
            decoded = json.loads(metadata, object_pairs_hook=_closed_object)
        except json.JSONDecodeError as exc:
            raise CapabilityOutcomeError("accepted outcome metadata must be one JSON object") from exc
    else:
        decoded = metadata
    if not isinstance(decoded, Mapping) or any(not isinstance(key, str) for key in decoded):
        raise CapabilityOutcomeError("accepted outcome metadata must be an object")
    if not isinstance(metadata, str):
        try:
            encoded = json.dumps(decoded, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="strict")
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
            raise CapabilityOutcomeError("accepted outcome metadata cannot be serialized") from exc
        if len(encoded) > _MAX_ASSISTANT_METADATA_BYTES:
            raise CapabilityOutcomeError("accepted outcome metadata exceeds the bounded carrier")
    raw_receipt = decoded.get(ACCEPTED_CAPABILITY_OUTCOME_METADATA_KEY)
    if not isinstance(raw_receipt, Mapping):
        raise CapabilityOutcomeError("accepted outcome metadata has no receipt")
    receipt = AcceptedCapabilityOutcomeReceipt.parse(raw_receipt)
    if expected_outcome is not None:
        if type(expected_outcome) is not CapabilityOutcome:
            raise CapabilityOutcomeError("expected accepted outcome must be CapabilityOutcome v1")
        if (
            receipt.outcome != expected_outcome
            or receipt.outcome_sha256 != expected_outcome.canonical_sha256()
        ):
            raise CapabilityOutcomeError("accepted outcome receipt does not match expected outcome")
    return receipt


def evaluate_read_only_completion(
    outcome: CapabilityOutcome,
    *,
    expected_route: RouteClass,
    expected_plan_sha256: str,
    expected_evidence_identity_sha256: str | None,
    expected_citation_labels: tuple[str, ...],
    answer: str,
    authority_rechecked: bool,
    verification_passed: bool,
) -> CompletionGateDecision:
    """Revalidate all code-owned bindings and return one closed gate decision."""

    if type(outcome) is not CapabilityOutcome:  # exact v1, never a widened subclass
        raise CapabilityOutcomeError("completion gate requires CapabilityOutcome v1")
    if expected_route not in _PROMOTED_ROUTES or outcome.route is not expected_route:
        raise CapabilityOutcomeError("completion gate route binding failed")
    expected_plan = _digest(expected_plan_sha256, label="expected_plan_sha256")
    expected_evidence = _digest(
        expected_evidence_identity_sha256,
        label="expected_evidence_identity_sha256",
        optional=True,
    )
    citations = _citations(expected_citation_labels)
    if outcome.plan_sha256 != expected_plan:
        raise CapabilityOutcomeError("completion gate plan binding failed")
    if outcome.evidence_identity_sha256 != expected_evidence:
        raise CapabilityOutcomeError("completion gate evidence binding failed")
    if outcome.citation_labels != citations:
        raise CapabilityOutcomeError("completion gate citation binding failed")
    try:
        validate_file_synthesis_answer(answer, citations)
    except ValueError:
        raise CapabilityOutcomeError("completion gate answer citations do not match the outcome") from None
    if outcome.authority_rechecked is not _boolean(
        authority_rechecked,
        label="authority_rechecked",
    ):
        raise CapabilityOutcomeError("completion gate authority binding failed")
    if outcome.verified is not _boolean(verification_passed, label="verification_passed"):
        raise CapabilityOutcomeError("completion gate verification binding failed")
    return _DECISION_BY_STATUS[outcome.status]


def require_complete_read_only_publication(
    outcome: CapabilityOutcome,
    **gate_inputs: Any,
) -> CapabilityOutcome:
    """Require the only status publishable by the current narrow V12 routes."""

    decision = evaluate_read_only_completion(outcome, **gate_inputs)
    if decision is not CompletionGateDecision.READY_TO_PUBLISH:
        raise CapabilityOutcomeError("capability outcome is not complete for publication")
    return outcome


__all__ = [
    "ACCEPTED_CAPABILITY_OUTCOME_METADATA_KEY",
    "CAPABILITY_OUTCOME_RECEIPT_SCHEMA",
    "CAPABILITY_OUTCOME_SCHEMA",
    "AcceptedCapabilityOutcomeReceipt",
    "CapabilityOutcome",
    "CapabilityOutcomeError",
    "CapabilityOutcomeReason",
    "CapabilityOutcomeStatus",
    "CompletionGateDecision",
    "attach_accepted_capability_outcome_receipt",
    "evaluate_read_only_completion",
    "load_accepted_capability_outcome_receipt",
    "require_complete_read_only_publication",
]
