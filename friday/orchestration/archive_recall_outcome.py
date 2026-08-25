"""Body-free accepted outcome for the legacy federated archive-search lane.

This is deliberately not ``CapabilityOutcome`` v1.  Archive search already has
its own exact multi-page model ledger and phase-2 publication attestation.  The
contract below projects only the immutable digests, honest coverage grade,
used citation labels and an optional single-source body-free evidence identity
which that attestation sealed.  It never carries a query, title or excerpt.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from friday.retrieval.archive_evidence_replay import (
    ArchiveEvidenceReplayCoverageGrade,
    ArchiveEvidenceReplayResult,
    ArchiveEvidenceReplayStatus,
)
from friday.retrieval.archive_evidence_snapshot import (
    archive_selected_evidence_snapshot_sha256,
)
from friday.retrieval.archive_search_authority import (
    ARCHIVE_AUTHORITY_MAX_CANDIDATES,
    ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES,
    ArchiveSearchCoverageGrade,
    ArchiveSearchPublicationAttestation,
    ArchiveSearchSelectedEvidence,
)

ARCHIVE_RECALL_OUTCOME_SCHEMA = "friday.archive-recall-outcome.v2"
_ARCHIVE_RECALL_OUTCOME_SCHEMA_V1 = "friday.archive-recall-outcome.v1"
ARCHIVE_RECALL_OUTCOME_RECEIPT_SCHEMA = "friday.accepted-archive-recall-outcome-receipt.v1"
ACCEPTED_ARCHIVE_RECALL_OUTCOME_METADATA_KEY = "accepted_archive_recall_outcome"

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CITATION = re.compile(r"A([1-9][0-9]{0,2})(?:\.[1-8])?\Z")
_MAX_OUTCOME_BYTES = 49_152
_MAX_RECEIPT_BYTES = 57_344
_MAX_METADATA_BYTES = 65_536
_MAX_CITATIONS = ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES * ARCHIVE_AUTHORITY_MAX_CANDIDATES * 9


class ArchiveRecallOutcomeError(ValueError):
    """A value is outside the closed accepted archive-recall contract."""


class ArchiveRecallLane(StrEnum):
    FEDERATED_SEARCH = "federated_search"
    SELECTED_EVIDENCE_REPLAY = "selected_evidence_replay"


class ArchiveRecallStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    INCOMPLETE_EMPTY = "incomplete_empty"
    DENIED = "denied"
    DRIFTED = "drifted"
    UNAVAILABLE = "unavailable"


ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE = (
    "Не могу безопасно перечитать выбранный источник: доступ или версия изменились. Повторите поиск."
)


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArchiveRecallOutcomeError("archive recall JSON contains a duplicate object key")
        result[key] = value
    return result


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ArchiveRecallOutcomeError("archive recall value is not canonical JSON") from None


def _digest(value: object, *, label: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ArchiveRecallOutcomeError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _count(value: object, *, label: str, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ArchiveRecallOutcomeError(f"{label} is outside the closed limit")
    return value


def _labels(value: object) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise ArchiveRecallOutcomeError("archive recall citation labels must be immutable")
    if (
        len(value) > _MAX_CITATIONS
        or len(value) != len(set(value))
        or any(
            type(item) is not str or (match := _CITATION.fullmatch(item)) is None or int(match.group(1)) > 640
            for item in value
        )
    ):
        raise ArchiveRecallOutcomeError("archive recall citation labels are invalid")
    return value


def _expected_status(
    grade: ArchiveSearchCoverageGrade,
    candidate_count: int,
) -> ArchiveRecallStatus:
    if candidate_count:
        return (
            ArchiveRecallStatus.COMPLETE
            if grade is ArchiveSearchCoverageGrade.COMPLETE
            else ArchiveRecallStatus.PARTIAL
        )
    return (
        ArchiveRecallStatus.EMPTY
        if grade is ArchiveSearchCoverageGrade.COMPLETE
        else ArchiveRecallStatus.INCOMPLETE_EMPTY
    )


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveRecallOutcome:
    """Durable body-free truth projected from one accepted phase-2 attestation."""

    lane: ArchiveRecallLane
    status: ArchiveRecallStatus
    plan_sha256: str
    evidence_sha256: str
    coverage_sha256: str
    coverage_grade: ArchiveSearchCoverageGrade
    candidate_count: int
    used_citation_labels: tuple[str, ...]
    selected_evidence: ArchiveSearchSelectedEvidence | None
    publication_attested: bool
    semantic_verified: bool
    answer_sha256: str
    candidate_projection_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.lane) is not ArchiveRecallLane:
            raise ArchiveRecallOutcomeError("archive recall lane is not supported")
        if type(self.status) is not ArchiveRecallStatus:
            raise ArchiveRecallOutcomeError("archive recall status is invalid")
        for label, value in (
            ("plan_sha256", self.plan_sha256),
            ("evidence_sha256", self.evidence_sha256),
            ("coverage_sha256", self.coverage_sha256),
            ("answer_sha256", self.answer_sha256),
        ):
            _digest(value, label=label)
        if type(self.coverage_grade) is not ArchiveSearchCoverageGrade:
            raise ArchiveRecallOutcomeError("archive recall coverage grade is invalid")
        count = _count(
            self.candidate_count,
            label="candidate_count",
            maximum=ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES * ARCHIVE_AUTHORITY_MAX_CANDIDATES,
        )
        labels = _labels(self.used_citation_labels)
        if self.lane is ArchiveRecallLane.FEDERATED_SEARCH:
            if self.candidate_projection_sha256 is not None:
                _digest(
                    self.candidate_projection_sha256,
                    label="candidate_projection_sha256",
                )
            if self.status is not _expected_status(self.coverage_grade, count):
                raise ArchiveRecallOutcomeError("archive recall status contradicts coverage and count")
            if self.selected_evidence is not None and (
                type(self.selected_evidence) is not ArchiveSearchSelectedEvidence or count == 0 or not labels
            ):
                raise ArchiveRecallOutcomeError("archive selected evidence is not supported by the outcome")
            if self.semantic_verified is not False:
                raise ArchiveRecallOutcomeError("federated archive recall cannot claim semantic verification")
        elif self.candidate_projection_sha256 is not None:
            raise ArchiveRecallOutcomeError("archive replay cannot carry a candidate projection")
        elif self.status in {ArchiveRecallStatus.COMPLETE, ArchiveRecallStatus.PARTIAL}:
            if (
                self.status is not _expected_status(self.coverage_grade, 1)
                or count != 1
                or not labels
                or type(self.selected_evidence) is not ArchiveSearchSelectedEvidence
                or self.semantic_verified is not True
            ):
                raise ArchiveRecallOutcomeError("archive replay success is internally inconsistent")
        elif (
            self.status
            not in {
                ArchiveRecallStatus.DENIED,
                ArchiveRecallStatus.DRIFTED,
                ArchiveRecallStatus.UNAVAILABLE,
            }
            or count != 0
            or labels
            or self.selected_evidence is not None
            or self.semantic_verified is not False
        ):
            raise ArchiveRecallOutcomeError("archive replay failure is internally inconsistent")
        if self.publication_attested is not True:
            raise ArchiveRecallOutcomeError("archive recall outcome requires publication attestation")

    def __repr__(self) -> str:
        return (
            "<ArchiveRecallOutcome body-free "
            f"status={self.status.value!r} candidate_count={self.candidate_count}>"
        )

    @classmethod
    def from_publication_attestation(
        cls,
        attestation: ArchiveSearchPublicationAttestation,
    ) -> ArchiveRecallOutcome:
        if type(attestation) is not ArchiveSearchPublicationAttestation:
            raise ArchiveRecallOutcomeError("archive recall requires an exact publication attestation")
        try:
            grade = attestation.coverage_grade
            candidate_count = attestation.candidate_count
            return cls(
                lane=ArchiveRecallLane.FEDERATED_SEARCH,
                status=_expected_status(grade, candidate_count),
                plan_sha256=attestation.plan_sha256,
                evidence_sha256=attestation.evidence_sha256,
                coverage_sha256=attestation.coverage_sha256,
                coverage_grade=grade,
                candidate_count=candidate_count,
                used_citation_labels=attestation.used_citation_labels,
                selected_evidence=attestation.selected_evidence,
                publication_attested=True,
                semantic_verified=False,
                answer_sha256=attestation.answer_sha256,
                candidate_projection_sha256=attestation.candidate_projection.canonical_sha256,
            )
        except ArchiveRecallOutcomeError:
            raise
        except Exception:
            raise ArchiveRecallOutcomeError("archive publication attestation is unavailable") from None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "answer_sha256": self.answer_sha256,
            "candidate_count": self.candidate_count,
            "coverage_grade": self.coverage_grade.value,
            "coverage_sha256": self.coverage_sha256,
            "evidence_sha256": self.evidence_sha256,
            "lane": self.lane.value,
            "plan_sha256": self.plan_sha256,
            "publication_attested": self.publication_attested,
            "schema": (
                ARCHIVE_RECALL_OUTCOME_SCHEMA
                if self.candidate_projection_sha256 is not None
                else _ARCHIVE_RECALL_OUTCOME_SCHEMA_V1
            ),
            "selected_evidence": (
                None if self.selected_evidence is None else self.selected_evidence.to_private_payload()
            ),
            "semantic_verified": self.semantic_verified,
            "status": self.status.value,
            "used_citation_count": len(self.used_citation_labels),
            "used_citation_labels": list(self.used_citation_labels),
        }
        if self.candidate_projection_sha256 is not None:
            payload["candidate_projection_sha256"] = self.candidate_projection_sha256
        return payload

    def to_json(self) -> str:
        value = _canonical_json(self.to_payload())
        if len(value.encode("ascii")) > _MAX_OUTCOME_BYTES:
            raise ArchiveRecallOutcomeError("archive recall outcome exceeds its closed byte limit")
        return value

    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("ascii")).hexdigest()

    @classmethod
    def parse(cls, value: str | Mapping[str, object]) -> ArchiveRecallOutcome:
        serialized: str | None = None
        if isinstance(value, str):
            serialized = value
            try:
                encoded = value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise ArchiveRecallOutcomeError("archive recall outcome must be valid UTF-8") from exc
            if len(encoded) > _MAX_OUTCOME_BYTES:
                raise ArchiveRecallOutcomeError("archive recall outcome exceeds its closed byte limit")
            try:
                decoded = json.loads(
                    value,
                    object_pairs_hook=_closed_object,
                    parse_constant=lambda _item: (_ for _ in ()).throw(
                        ArchiveRecallOutcomeError("archive recall outcome contains a non-finite number")
                    ),
                )
            except ArchiveRecallOutcomeError:
                raise
            except (ValueError, TypeError, RecursionError) as exc:
                raise ArchiveRecallOutcomeError("archive recall outcome must be one JSON object") from exc
        else:
            decoded = value
        if not isinstance(decoded, Mapping):
            raise ArchiveRecallOutcomeError("archive recall outcome must be one object")
        schema = decoded.get("schema")
        expected = {
            "answer_sha256",
            "candidate_count",
            "coverage_grade",
            "coverage_sha256",
            "evidence_sha256",
            "lane",
            "plan_sha256",
            "publication_attested",
            "schema",
            "selected_evidence",
            "semantic_verified",
            "status",
            "used_citation_count",
            "used_citation_labels",
        }
        if schema == ARCHIVE_RECALL_OUTCOME_SCHEMA:
            expected.add("candidate_projection_sha256")
        elif schema != _ARCHIVE_RECALL_OUTCOME_SCHEMA_V1:
            raise ArchiveRecallOutcomeError("archive recall outcome schema is unsupported")
        if any(type(key) is not str for key in decoded) or frozenset(decoded) != frozenset(expected):
            raise ArchiveRecallOutcomeError("archive recall outcome keys do not match the contract")
        raw_labels = decoded["used_citation_labels"]
        if type(raw_labels) is not list:
            raise ArchiveRecallOutcomeError("archive recall citation labels must be an array")
        if len(raw_labels) > _MAX_CITATIONS:
            raise ArchiveRecallOutcomeError("archive recall citation labels are invalid")
        labels = tuple(raw_labels)
        used_count = _count(
            decoded["used_citation_count"],
            label="used_citation_count",
            maximum=_MAX_CITATIONS,
        )
        if used_count != len(labels):
            raise ArchiveRecallOutcomeError("archive recall citation count is inconsistent")
        raw_selected = decoded["selected_evidence"]
        try:
            selected = (
                None
                if raw_selected is None
                else ArchiveSearchSelectedEvidence.from_private_payload(raw_selected)
            )
            outcome = cls(
                lane=ArchiveRecallLane(decoded["lane"]),
                status=ArchiveRecallStatus(decoded["status"]),
                plan_sha256=_digest(decoded["plan_sha256"], label="plan_sha256"),
                evidence_sha256=_digest(decoded["evidence_sha256"], label="evidence_sha256"),
                coverage_sha256=_digest(decoded["coverage_sha256"], label="coverage_sha256"),
                coverage_grade=ArchiveSearchCoverageGrade(decoded["coverage_grade"]),
                candidate_count=_count(
                    decoded["candidate_count"],
                    label="candidate_count",
                    maximum=ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES * ARCHIVE_AUTHORITY_MAX_CANDIDATES,
                ),
                used_citation_labels=labels,
                selected_evidence=selected,
                publication_attested=decoded["publication_attested"],
                semantic_verified=decoded["semantic_verified"],
                answer_sha256=_digest(decoded["answer_sha256"], label="answer_sha256"),
                candidate_projection_sha256=(
                    _digest(
                        decoded["candidate_projection_sha256"],
                        label="candidate_projection_sha256",
                    )
                    if schema == ARCHIVE_RECALL_OUTCOME_SCHEMA
                    else None
                ),
            )
        except ArchiveRecallOutcomeError:
            raise
        except (TypeError, ValueError):
            raise ArchiveRecallOutcomeError("archive recall outcome contains invalid closed values") from None
        canonical = outcome.to_json()
        if serialized is not None and serialized != canonical:
            raise ArchiveRecallOutcomeError("archive recall outcome JSON is not canonical")
        return outcome


@dataclass(frozen=True, slots=True)
class AcceptedArchiveRecallOutcomeReceipt:
    outcome: ArchiveRecallOutcome
    outcome_sha256: str

    def __post_init__(self) -> None:
        if type(self.outcome) is not ArchiveRecallOutcome:
            raise ArchiveRecallOutcomeError("archive recall receipt requires the exact outcome contract")
        digest = _digest(self.outcome_sha256, label="outcome_sha256")
        if digest != self.outcome.canonical_sha256():
            raise ArchiveRecallOutcomeError("archive recall receipt digest does not match its outcome")

    @classmethod
    def from_outcome(cls, outcome: ArchiveRecallOutcome) -> AcceptedArchiveRecallOutcomeReceipt:
        if type(outcome) is not ArchiveRecallOutcome:
            raise ArchiveRecallOutcomeError("archive recall receipt requires the exact outcome contract")
        return cls(outcome, outcome.canonical_sha256())

    def to_payload(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.to_payload(),
            "outcome_sha256": self.outcome_sha256,
            "schema": ARCHIVE_RECALL_OUTCOME_RECEIPT_SCHEMA,
        }

    def to_json(self) -> str:
        value = _canonical_json(self.to_payload())
        if len(value.encode("ascii")) > _MAX_RECEIPT_BYTES:
            raise ArchiveRecallOutcomeError("archive recall receipt exceeds its closed byte limit")
        return value

    @classmethod
    def parse(
        cls,
        value: str | Mapping[str, object],
    ) -> AcceptedArchiveRecallOutcomeReceipt:
        serialized: str | None = None
        if isinstance(value, str):
            serialized = value
            try:
                encoded = value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise ArchiveRecallOutcomeError("archive recall receipt must be valid UTF-8") from exc
            if len(encoded) > _MAX_RECEIPT_BYTES:
                raise ArchiveRecallOutcomeError("archive recall receipt exceeds its closed byte limit")
            try:
                decoded = json.loads(value, object_pairs_hook=_closed_object)
            except ArchiveRecallOutcomeError:
                raise
            except (ValueError, TypeError, RecursionError) as exc:
                raise ArchiveRecallOutcomeError("archive recall receipt must be one JSON object") from exc
        else:
            decoded = value
        if (
            not isinstance(decoded, Mapping)
            or any(type(key) is not str for key in decoded)
            or frozenset(decoded) != frozenset({"outcome", "outcome_sha256", "schema"})
        ):
            raise ArchiveRecallOutcomeError("archive recall receipt keys do not match the contract")
        if decoded["schema"] != ARCHIVE_RECALL_OUTCOME_RECEIPT_SCHEMA:
            raise ArchiveRecallOutcomeError("archive recall receipt schema is unsupported")
        raw_outcome = decoded["outcome"]
        if not isinstance(raw_outcome, Mapping):
            raise ArchiveRecallOutcomeError("archive recall receipt has no outcome object")
        receipt = cls(
            outcome=ArchiveRecallOutcome.parse(raw_outcome),
            outcome_sha256=_digest(decoded["outcome_sha256"], label="outcome_sha256"),
        )
        if serialized is not None and serialized != receipt.to_json():
            raise ArchiveRecallOutcomeError("archive recall receipt JSON is not canonical")
        return receipt


def archive_recall_outcome_from_attestation(
    attestation: ArchiveSearchPublicationAttestation,
) -> ArchiveRecallOutcome:
    """Build the sole durable archive-recall outcome from a live sealed attestation."""

    return ArchiveRecallOutcome.from_publication_attestation(attestation)


def accept_archive_evidence_replay(
    *,
    request: str,
    result: ArchiveEvidenceReplayResult,
    selected_evidence: ArchiveSearchSelectedEvidence,
    coverage_sha256: str,
    coverage_grade: ArchiveSearchCoverageGrade,
) -> tuple[str, ArchiveRecallOutcome]:
    """Render and accept one exact replay or one source-free terminal failure."""

    if type(request) is not str or not request or len(request.encode("utf-8")) > 256:
        raise ArchiveRecallOutcomeError("archive replay request is invalid")
    if type(result) is not ArchiveEvidenceReplayResult or not result.is_valid():
        raise ArchiveRecallOutcomeError("archive replay result is unavailable")
    if type(selected_evidence) is not ArchiveSearchSelectedEvidence:
        raise ArchiveRecallOutcomeError("archive replay selected evidence is invalid")
    coverage_digest = _digest(coverage_sha256, label="coverage_sha256")
    if type(coverage_grade) is not ArchiveSearchCoverageGrade:
        raise ArchiveRecallOutcomeError("archive replay coverage grade is invalid")
    expected_replay_grade = ArchiveEvidenceReplayCoverageGrade(coverage_grade.value)
    if result.corpus is not selected_evidence.corpus:
        raise ArchiveRecallOutcomeError("archive replay corpus changed")

    selected_sha256 = hashlib.sha256(selected_evidence.to_private_json().encode("ascii")).hexdigest()
    plan_sha256 = hashlib.sha256(
        _canonical_json(
            {
                "request_sha256": hashlib.sha256(request.encode("utf-8")).hexdigest(),
                "schema": "friday.selected-archive-evidence-replay-plan.v1",
                "selected_evidence_sha256": selected_sha256,
            }
        ).encode("ascii")
    ).hexdigest()

    replay_status = result.status
    if replay_status is ArchiveEvidenceReplayStatus.EXACT:
        resolved = result.resolved_source
        excerpts = result.excerpts
        if (
            result.coverage_grade is not expected_replay_grade
            or resolved is None
            or tuple(item.passage_ref for item in excerpts) != selected_evidence.passage_refs
            or tuple(item.citation_label for item in excerpts)
            != tuple(f"A1.{index}" for index in range(1, len(excerpts) + 1))
            or archive_selected_evidence_snapshot_sha256(
                resolved,
                selected_evidence.passage_refs,
                tuple(item.text for item in excerpts),
            )
            != selected_evidence.resolved_snapshot_sha256
        ):
            raise ArchiveRecallOutcomeError("archive replay exact evidence changed")
        lines = ["В выбранном источнике:"]
        if coverage_grade is ArchiveSearchCoverageGrade.PARTIAL:
            lines.append("Охват исходного поиска был частичным.")
        lines.extend(f"[{item.citation_label}] {item.text}" for item in excerpts)
        content = "\n\n".join(lines)
        status = _expected_status(coverage_grade, 1)
        evidence_sha256 = hashlib.sha256(result.model_visible_bytes).hexdigest()
        candidate_count = 1
        labels = tuple(item.citation_label for item in excerpts)
        retained_selection: ArchiveSearchSelectedEvidence | None = selected_evidence
        semantic_verified = True
    else:
        content = ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE
        status = {
            ArchiveEvidenceReplayStatus.DENIED: ArchiveRecallStatus.DENIED,
            ArchiveEvidenceReplayStatus.DRIFTED: ArchiveRecallStatus.DRIFTED,
            ArchiveEvidenceReplayStatus.UNAVAILABLE: ArchiveRecallStatus.UNAVAILABLE,
        }[replay_status]
        evidence_sha256 = hashlib.sha256(
            _canonical_json(
                {
                    "schema": "friday.selected-archive-evidence-replay-source-free.v1",
                    "status": status.value,
                }
            ).encode("ascii")
        ).hexdigest()
        candidate_count = 0
        labels = ()
        retained_selection = None
        semantic_verified = False

    outcome = ArchiveRecallOutcome(
        lane=ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY,
        status=status,
        plan_sha256=plan_sha256,
        evidence_sha256=evidence_sha256,
        coverage_sha256=coverage_digest,
        coverage_grade=coverage_grade,
        candidate_count=candidate_count,
        used_citation_labels=labels,
        selected_evidence=retained_selection,
        publication_attested=True,
        semantic_verified=semantic_verified,
        answer_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )
    return content, outcome


def attach_accepted_archive_recall_outcome_receipt(
    metadata: dict[str, Any],
    outcome: ArchiveRecallOutcome,
    *,
    max_serialized_bytes: int = _MAX_METADATA_BYTES,
) -> AcceptedArchiveRecallOutcomeReceipt:
    """Attach one closed receipt without overwriting metadata or exceeding its budget."""

    if type(metadata) is not dict:
        raise ArchiveRecallOutcomeError("archive recall metadata carrier must be a dictionary")
    if ACCEPTED_ARCHIVE_RECALL_OUTCOME_METADATA_KEY in metadata:
        raise ArchiveRecallOutcomeError("archive recall receipt is already attached")
    if type(max_serialized_bytes) is not int or not 1 <= max_serialized_bytes <= _MAX_METADATA_BYTES:
        raise ArchiveRecallOutcomeError("archive recall metadata budget is outside the closed limit")
    receipt = AcceptedArchiveRecallOutcomeReceipt.from_outcome(outcome)
    candidate = dict(metadata)
    candidate[ACCEPTED_ARCHIVE_RECALL_OUTCOME_METADATA_KEY] = receipt.to_payload()
    try:
        encoded = json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ArchiveRecallOutcomeError("archive recall metadata cannot be serialized") from exc
    if len(encoded) > max_serialized_bytes:
        raise ArchiveRecallOutcomeError("archive recall metadata exceeds the bounded carrier")
    metadata[ACCEPTED_ARCHIVE_RECALL_OUTCOME_METADATA_KEY] = receipt.to_payload()
    return receipt


def load_accepted_archive_recall_outcome_receipt(
    metadata: object,
    *,
    expected_outcome: ArchiveRecallOutcome | None = None,
) -> AcceptedArchiveRecallOutcomeReceipt:
    """Load and strictly validate an archive-recall receipt from assistant metadata."""

    if isinstance(metadata, str):
        try:
            encoded = metadata.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ArchiveRecallOutcomeError("archive recall metadata must be valid UTF-8") from exc
        if len(encoded) > _MAX_METADATA_BYTES:
            raise ArchiveRecallOutcomeError("archive recall metadata exceeds the bounded carrier")
        try:
            decoded = json.loads(metadata, object_pairs_hook=_closed_object)
        except ArchiveRecallOutcomeError:
            raise
        except (ValueError, TypeError, RecursionError) as exc:
            raise ArchiveRecallOutcomeError("archive recall metadata must be one JSON object") from exc
    else:
        decoded = metadata
    if not isinstance(decoded, Mapping) or any(type(key) is not str for key in decoded):
        raise ArchiveRecallOutcomeError("archive recall metadata must be one object")
    if not isinstance(metadata, str):
        try:
            encoded = json.dumps(
                decoded,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8", errors="strict")
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise ArchiveRecallOutcomeError("archive recall metadata cannot be serialized") from exc
        if len(encoded) > _MAX_METADATA_BYTES:
            raise ArchiveRecallOutcomeError("archive recall metadata exceeds the bounded carrier")
    raw_receipt = decoded.get(ACCEPTED_ARCHIVE_RECALL_OUTCOME_METADATA_KEY)
    if not isinstance(raw_receipt, Mapping):
        raise ArchiveRecallOutcomeError("archive recall metadata has no accepted receipt")
    receipt = AcceptedArchiveRecallOutcomeReceipt.parse(raw_receipt)
    if expected_outcome is not None:
        if type(expected_outcome) is not ArchiveRecallOutcome:
            raise ArchiveRecallOutcomeError("expected archive recall outcome has the wrong contract")
        if (
            receipt.outcome != expected_outcome
            or receipt.outcome_sha256 != expected_outcome.canonical_sha256()
        ):
            raise ArchiveRecallOutcomeError("archive recall receipt does not match expected outcome")
    return receipt


__all__ = [
    "ACCEPTED_ARCHIVE_RECALL_OUTCOME_METADATA_KEY",
    "ARCHIVE_RECALL_OUTCOME_RECEIPT_SCHEMA",
    "ARCHIVE_RECALL_OUTCOME_SCHEMA",
    "AcceptedArchiveRecallOutcomeReceipt",
    "ArchiveRecallLane",
    "ArchiveRecallOutcome",
    "ArchiveRecallOutcomeError",
    "ArchiveRecallStatus",
    "ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE",
    "accept_archive_evidence_replay",
    "archive_recall_outcome_from_attestation",
    "attach_accepted_archive_recall_outcome_receipt",
    "load_accepted_archive_recall_outcome_receipt",
]
