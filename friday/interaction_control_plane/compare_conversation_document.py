"""Body-free schema-42 contracts for conversation/document comparison.

This reader-first package describes every durable value the later writer may
publish, but exposes no admission or runtime route.  Content remains in the
authoritative message and document stores.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from friday.interaction_control_plane.archive_candidate_selection import ArchiveCandidateSet
from friday.interaction_control_plane.selected_archive_evidence import (
    SelectedArchiveCorpus,
    SelectedArchiveCoverageGrade,
    SelectedArchiveEvidence,
)
from friday.interaction_control_plane.work_item_contract import (
    COMPARE_CONVERSATION_DOCUMENT_ACTIVE_FRAME_JSON,
    COMPARE_CONVERSATION_DOCUMENT_ACTIVE_FRAME_SCHEMA,
    COMPARE_DOCUMENT_REFERENCE_PROMPT,
    WORK_ITEM_MAX_REVISION,
    WORK_ITEM_TTL_HOURS,
    WorkCompletionContract,
    WorkGoal,
    WorkItemContractError,
    WorkKind,
    WorkPlaybook,
    WorkState,
    WorkTransition,
    canonical_work_item_instant,
)
from friday.retrieval._contract_utils import RetrievalContractError
from friday.retrieval.identity_contract import (
    AuthorityScope,
    CanonicalObjectKind,
    RevisionKind,
    SourceKind,
    SourceRef,
)

COMPARE_CONVERSATION_DOCUMENT_WORK_ITEM_SCHEMA = "friday.compare-conversation-with-document-work-item.v1"
DOCUMENT_REFERENCE_QUESTION_SCHEMA = "friday.document-reference-question.v1"
ACCEPTED_COMPARISON_RESULT_SCHEMA = "friday.accepted-comparison-result-identity.v1"
COMPARE_CONVERSATION_DOCUMENT_OUTCOME_SCHEMA = "friday.compare-conversation-document-outcome.v1"
COMPARE_CONVERSATION_DOCUMENT_OUTCOME_RECEIPT_SCHEMA = (
    "friday.compare-conversation-document-outcome-receipt.v1"
)
COMPARE_DOCUMENT_CANDIDATE_OUTCOME_SCHEMA = "friday.compare-document-candidate-outcome.v1"
COMPARE_DOCUMENT_CANDIDATE_OUTCOME_RECEIPT_SCHEMA = "friday.compare-document-candidate-outcome-receipt.v1"
COMPARE_DOCUMENT_REFERENCE_REQUIRED_VERDICT_KIND = "compare_conversation_document_reference_required"
COMPARE_DOCUMENT_CANDIDATE_REQUIRED_VERDICT_KIND = "compare_conversation_document_candidate_required"
ACCEPTED_COMPARISON_METADATA_KEY = "accepted_compare_conversation_document_outcome"
ACCEPTED_COMPARE_DOCUMENT_CANDIDATE_METADATA_KEY = "accepted_compare_document_candidate_outcome"

_WORK_ITEM_ID_RE = re.compile(r"work_[0-9a-f]{16}\Z")
_QUESTION_ID_RE = re.compile(r"question_[0-9a-f]{16}\Z")
_CANDIDATE_SET_ID_RE = re.compile(r"cset_[0-9a-f]{16}\Z")
_RAW_OBJECT_ID_RE = re.compile(r"raw_[0-9a-f]{16}\Z")
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}\Z")
_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}\Z")
_USER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_RECEIPT_MAX_BYTES = 65_536


class DocumentReferenceAdmissionShape(StrEnum):
    DIRECT_COMPOUND = "direct_compound"
    SELECTED_EVIDENCE_FOLLOWUP = "selected_evidence_followup"


class DocumentReferenceQuestionKind(StrEnum):
    PROVIDE_DOCUMENT_REFERENCE = "provide_document_reference"
    SELECT_DOCUMENT_CANDIDATE = "select_document_candidate"


class DocumentReferenceQuestionState(StrEnum):
    WAITING = "waiting"
    ANSWERED = "answered"
    CLOSED = "closed"


class DocumentReferenceQuestionCloseReason(StrEnum):
    ANSWERED = "answered"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ResolvedDocumentProvenance(StrEnum):
    CURRENT_TURN_ATTACHMENT = "current_turn_attachment"
    HISTORICAL_EXACT_REFERENCE = "historical_exact_reference"
    HISTORICAL_CANDIDATE_ORDINAL = "historical_candidate_ordinal"


class CompareConversationDocumentStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


def _identifier(value: object, pattern: re.Pattern[str], *, label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise WorkItemContractError(f"{label} is not a valid identifier")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise WorkItemContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], *, label: str) -> None:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise WorkItemContractError(f"{label} must be an object")
    if frozenset(value) != expected:
        raise WorkItemContractError(f"{label} keys do not match")


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _closed_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkItemContractError("comparison receipt JSON contains duplicate keys")
        result[key] = value
    return result


def _receipt_payload_bytes(value: Mapping[str, object]) -> bytes:
    try:
        encoded = _canonical_json(value).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkItemContractError("comparison receipt is not serializable") from exc
    if len(encoded) > _RECEIPT_MAX_BYTES:
        raise WorkItemContractError("comparison receipt exceeds its closed byte limit")
    return encoded


def selected_evidence_sha256(value: SelectedArchiveEvidence) -> str:
    if type(value) is not SelectedArchiveEvidence:
        raise WorkItemContractError("comparison evidence must use the exact contract")
    return hashlib.sha256(_canonical_json(value.to_payload()).encode("ascii")).hexdigest()


def comparison_evidence_bundle_sha256(
    message_evidence: SelectedArchiveEvidence,
    document_evidence: ResolvedDocumentIdentity,
) -> str:
    payload = {
        "document_evidence_sha256": document_evidence.canonical_sha256,
        "message_evidence_sha256": selected_evidence_sha256(message_evidence),
        "schema": "friday.compare-conversation-document-evidence-bundle.v1",
    }
    return hashlib.sha256(_canonical_json(payload).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class CompareConversationDocumentActiveFrame:
    @classmethod
    def parse(cls, value: object) -> CompareConversationDocumentActiveFrame:
        if value != COMPARE_CONVERSATION_DOCUMENT_ACTIVE_FRAME_JSON:
            raise WorkItemContractError("comparison active frame is invalid")
        return cls()

    def to_payload(self) -> dict[str, object]:
        return {"schema": COMPARE_CONVERSATION_DOCUMENT_ACTIVE_FRAME_SCHEMA}

    def to_json(self) -> str:
        return COMPARE_CONVERSATION_DOCUMENT_ACTIVE_FRAME_JSON


@dataclass(frozen=True, slots=True)
class CompareDocumentCandidateOutcome:
    """Body-free receipt for one code-owned exact-filename ambiguity."""

    plan_sha256: str
    evidence_sha256: str
    coverage_sha256: str
    candidate_projection_sha256: str
    answer_sha256: str
    candidate_count: int
    publication_attested: bool
    authority_rechecked: bool

    def __post_init__(self) -> None:
        for label in (
            "plan_sha256",
            "evidence_sha256",
            "coverage_sha256",
            "candidate_projection_sha256",
            "answer_sha256",
        ):
            _digest(getattr(self, label), label=label)
        if (
            not isinstance(self.candidate_count, int)
            or isinstance(self.candidate_count, bool)
            or not 2 <= self.candidate_count <= 20
            or self.publication_attested is not True
            or self.authority_rechecked is not True
        ):
            raise WorkItemContractError("comparison candidate outcome is invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "answer_sha256": self.answer_sha256,
            "authority_rechecked": self.authority_rechecked,
            "candidate_count": self.candidate_count,
            "candidate_projection_sha256": self.candidate_projection_sha256,
            "coverage_sha256": self.coverage_sha256,
            "evidence_sha256": self.evidence_sha256,
            "plan_sha256": self.plan_sha256,
            "publication_attested": self.publication_attested,
            "schema": COMPARE_DOCUMENT_CANDIDATE_OUTCOME_SCHEMA,
        }

    @classmethod
    def parse(cls, value: object) -> CompareDocumentCandidateOutcome:
        if not isinstance(value, Mapping):
            raise WorkItemContractError("comparison candidate outcome must be an object")
        _exact_keys(
            value,
            frozenset(
                {
                    "answer_sha256",
                    "authority_rechecked",
                    "candidate_count",
                    "candidate_projection_sha256",
                    "coverage_sha256",
                    "evidence_sha256",
                    "plan_sha256",
                    "publication_attested",
                    "schema",
                }
            ),
            label="comparison candidate outcome",
        )
        if value["schema"] != COMPARE_DOCUMENT_CANDIDATE_OUTCOME_SCHEMA:
            raise WorkItemContractError("comparison candidate outcome schema is invalid")
        return cls(
            plan_sha256=value["plan_sha256"],  # type: ignore[arg-type]
            evidence_sha256=value["evidence_sha256"],  # type: ignore[arg-type]
            coverage_sha256=value["coverage_sha256"],  # type: ignore[arg-type]
            candidate_projection_sha256=value["candidate_projection_sha256"],  # type: ignore[arg-type]
            answer_sha256=value["answer_sha256"],  # type: ignore[arg-type]
            candidate_count=value["candidate_count"],  # type: ignore[arg-type]
            publication_attested=value["publication_attested"],  # type: ignore[arg-type]
            authority_rechecked=value["authority_rechecked"],  # type: ignore[arg-type]
        )

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_payload()).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class CompareDocumentCandidateOutcomeReceipt:
    outcome: CompareDocumentCandidateOutcome
    outcome_sha256: str

    def __post_init__(self) -> None:
        if type(self.outcome) is not CompareDocumentCandidateOutcome or not hmac.compare_digest(
            _digest(self.outcome_sha256, label="outcome_sha256"),
            self.outcome.canonical_sha256,
        ):
            raise WorkItemContractError("comparison candidate receipt is invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.to_payload(),
            "outcome_sha256": self.outcome_sha256,
            "schema": COMPARE_DOCUMENT_CANDIDATE_OUTCOME_RECEIPT_SCHEMA,
        }

    @classmethod
    def parse(cls, value: object) -> CompareDocumentCandidateOutcomeReceipt:
        if not isinstance(value, Mapping):
            raise WorkItemContractError("comparison candidate receipt must be an object")
        _exact_keys(
            value,
            frozenset({"outcome", "outcome_sha256", "schema"}),
            label="comparison candidate receipt",
        )
        if value["schema"] != COMPARE_DOCUMENT_CANDIDATE_OUTCOME_RECEIPT_SCHEMA:
            raise WorkItemContractError("comparison candidate receipt schema is invalid")
        return cls(
            outcome=CompareDocumentCandidateOutcome.parse(value["outcome"]),
            outcome_sha256=value["outcome_sha256"],  # type: ignore[arg-type]
        )


def attach_accepted_compare_document_candidate_outcome_receipt(
    metadata: dict[str, object],
    outcome: CompareDocumentCandidateOutcome,
) -> CompareDocumentCandidateOutcomeReceipt:
    if type(metadata) is not dict or ACCEPTED_COMPARE_DOCUMENT_CANDIDATE_METADATA_KEY in metadata:
        raise WorkItemContractError("comparison candidate receipt metadata slot is not pristine")
    receipt = CompareDocumentCandidateOutcomeReceipt(outcome, outcome.canonical_sha256)
    candidate = dict(metadata)
    candidate[ACCEPTED_COMPARE_DOCUMENT_CANDIDATE_METADATA_KEY] = receipt.to_payload()
    _receipt_payload_bytes(candidate)
    metadata[ACCEPTED_COMPARE_DOCUMENT_CANDIDATE_METADATA_KEY] = receipt.to_payload()
    return receipt


def load_accepted_compare_document_candidate_outcome_receipt(
    metadata: object,
) -> CompareDocumentCandidateOutcomeReceipt:
    try:
        if isinstance(metadata, str):
            encoded = metadata.encode("utf-8", errors="strict")
            if not encoded or len(encoded) > _RECEIPT_MAX_BYTES:
                raise WorkItemContractError("assistant metadata exceeds its closed byte limit")
            decoded = json.loads(
                metadata,
                object_pairs_hook=_closed_json_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    WorkItemContractError("assistant metadata contains a non-finite number")
                ),
            )
        else:
            decoded = metadata
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise WorkItemContractError("assistant metadata is invalid") from exc
    if not isinstance(decoded, Mapping) or ACCEPTED_COMPARE_DOCUMENT_CANDIDATE_METADATA_KEY not in decoded:
        raise WorkItemContractError("assistant has no accepted comparison candidate receipt")
    _receipt_payload_bytes(decoded)
    receipt = CompareDocumentCandidateOutcomeReceipt.parse(
        decoded[ACCEPTED_COMPARE_DOCUMENT_CANDIDATE_METADATA_KEY]
    )
    _receipt_payload_bytes(receipt.to_payload())
    return receipt


@dataclass(frozen=True, slots=True)
class DocumentReferenceQuestion:
    id: str
    work_item_id: str
    kind: DocumentReferenceQuestionKind
    admission_shape: DocumentReferenceAdmissionShape
    state: DocumentReferenceQuestionState
    created_at: str
    prompt_boundary_user_message_id: str
    prompt_assistant_message_id: str
    work_revision: int
    candidate_set_id: str | None = None
    answered_at: str | None = None
    answer_user_message_id: str | None = None
    selected_ordinal: int | None = None
    accepted_search_plan_sha256: str | None = None
    accepted_search_outcome_sha256: str | None = None
    closed_at: str | None = None
    close_reason: DocumentReferenceQuestionCloseReason | None = None

    def __post_init__(self) -> None:
        _identifier(self.id, _QUESTION_ID_RE, label="question_id")
        _identifier(self.work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
        _identifier(
            self.prompt_boundary_user_message_id,
            _MESSAGE_ID_RE,
            label="prompt_boundary_user_message_id",
        )
        _identifier(
            self.prompt_assistant_message_id,
            _MESSAGE_ID_RE,
            label="prompt_assistant_message_id",
        )
        if self.prompt_boundary_user_message_id == self.prompt_assistant_message_id:
            raise WorkItemContractError("question prompt anchors must differ")
        if type(self.kind) is not DocumentReferenceQuestionKind:
            raise WorkItemContractError("question kind is invalid")
        if type(self.admission_shape) is not DocumentReferenceAdmissionShape:
            raise WorkItemContractError("question admission shape is invalid")
        if type(self.state) is not DocumentReferenceQuestionState:
            raise WorkItemContractError("question state is invalid")
        if (
            not isinstance(self.work_revision, int)
            or isinstance(self.work_revision, bool)
            or not 1 <= self.work_revision <= 2
        ):
            raise WorkItemContractError("question revision is outside the closed journey")
        created = canonical_work_item_instant(self.created_at, label="question.created_at")
        if created != self.created_at:
            raise WorkItemContractError("question created_at must be canonical")
        if self.kind is DocumentReferenceQuestionKind.PROVIDE_DOCUMENT_REFERENCE:
            if (
                self.work_revision != 1
                or self.candidate_set_id is not None
                or self.accepted_search_plan_sha256 is not None
                or self.accepted_search_outcome_sha256 is not None
            ):
                raise WorkItemContractError("document-reference question identity is invalid")
        elif self.work_revision != 2 or self.candidate_set_id is None:
            raise WorkItemContractError("candidate question identity is invalid")
        else:
            _identifier(self.candidate_set_id, _CANDIDATE_SET_ID_RE, label="candidate_set_id")
            _digest(self.accepted_search_plan_sha256, label="accepted_search_plan_sha256")
            _digest(self.accepted_search_outcome_sha256, label="accepted_search_outcome_sha256")

        if self.state is DocumentReferenceQuestionState.WAITING:
            if any(
                value is not None
                for value in (
                    self.answered_at,
                    self.answer_user_message_id,
                    self.selected_ordinal,
                    self.closed_at,
                    self.close_reason,
                )
            ):
                raise WorkItemContractError("waiting question carries closure data")
            return
        if self.closed_at is None or type(self.close_reason) is not DocumentReferenceQuestionCloseReason:
            raise WorkItemContractError("closed question requires a close reason")
        closed = canonical_work_item_instant(self.closed_at, label="question.closed_at")
        if closed != self.closed_at or closed < created:
            raise WorkItemContractError("question closure time is invalid")
        if self.state is DocumentReferenceQuestionState.ANSWERED:
            if (
                self.close_reason is not DocumentReferenceQuestionCloseReason.ANSWERED
                or self.answered_at != self.closed_at
                or self.answer_user_message_id is None
            ):
                raise WorkItemContractError("answered question receipt is incomplete")
            _identifier(self.answer_user_message_id, _MESSAGE_ID_RE, label="answer_user_message_id")
            if self.kind is DocumentReferenceQuestionKind.SELECT_DOCUMENT_CANDIDATE:
                if (
                    not isinstance(self.selected_ordinal, int)
                    or isinstance(self.selected_ordinal, bool)
                    or not 1 <= self.selected_ordinal <= 20
                ):
                    raise WorkItemContractError("candidate answer ordinal is invalid")
            elif self.selected_ordinal is not None:
                raise WorkItemContractError("document-reference answer cannot carry an ordinal")
        elif (
            self.close_reason is DocumentReferenceQuestionCloseReason.ANSWERED
            or self.answered_at is not None
            or self.answer_user_message_id is not None
            or self.selected_ordinal is not None
        ):
            raise WorkItemContractError("stopped question carries answer data")

    @classmethod
    def from_storage_row(cls, value: Mapping[str, object]) -> DocumentReferenceQuestion:
        _exact_keys(
            value,
            frozenset(
                {
                    "id",
                    "work_item_id",
                    "kind",
                    "admission_shape",
                    "state",
                    "created_at",
                    "prompt_boundary_user_message_id",
                    "prompt_assistant_message_id",
                    "work_revision",
                    "candidate_set_id",
                    "answered_at",
                    "answer_user_message_id",
                    "selected_ordinal",
                    "accepted_search_plan_sha256",
                    "accepted_search_outcome_sha256",
                    "closed_at",
                    "close_reason",
                }
            ),
            label="question storage row",
        )
        try:
            return cls(
                id=value["id"],  # type: ignore[arg-type]
                work_item_id=value["work_item_id"],  # type: ignore[arg-type]
                kind=DocumentReferenceQuestionKind(value["kind"]),  # type: ignore[arg-type]
                admission_shape=DocumentReferenceAdmissionShape(value["admission_shape"]),  # type: ignore[arg-type]
                state=DocumentReferenceQuestionState(value["state"]),  # type: ignore[arg-type]
                created_at=value["created_at"],  # type: ignore[arg-type]
                prompt_boundary_user_message_id=value["prompt_boundary_user_message_id"],  # type: ignore[arg-type]
                prompt_assistant_message_id=value["prompt_assistant_message_id"],  # type: ignore[arg-type]
                work_revision=value["work_revision"],  # type: ignore[arg-type]
                candidate_set_id=value["candidate_set_id"],  # type: ignore[arg-type]
                answered_at=value["answered_at"],  # type: ignore[arg-type]
                answer_user_message_id=value["answer_user_message_id"],  # type: ignore[arg-type]
                selected_ordinal=value["selected_ordinal"],  # type: ignore[arg-type]
                accepted_search_plan_sha256=value["accepted_search_plan_sha256"],  # type: ignore[arg-type]
                accepted_search_outcome_sha256=value["accepted_search_outcome_sha256"],  # type: ignore[arg-type]
                closed_at=value["closed_at"],  # type: ignore[arg-type]
                close_reason=(
                    None
                    if value["close_reason"] is None
                    else DocumentReferenceQuestionCloseReason(value["close_reason"])  # type: ignore[arg-type]
                ),
            )
        except (TypeError, ValueError) as exc:
            raise WorkItemContractError("question storage row is invalid") from exc

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": DOCUMENT_REFERENCE_QUESTION_SCHEMA,
            "id": self.id,
            "work_item_id": self.work_item_id,
            "kind": self.kind.value,
            "admission_shape": self.admission_shape.value,
            "state": self.state.value,
            "created_at": self.created_at,
            "prompt_boundary_user_message_id": self.prompt_boundary_user_message_id,
            "prompt_assistant_message_id": self.prompt_assistant_message_id,
            "work_revision": self.work_revision,
            "candidate_set_id": self.candidate_set_id,
            "answered_at": self.answered_at,
            "answer_user_message_id": self.answer_user_message_id,
            "selected_ordinal": self.selected_ordinal,
            "accepted_search_plan_sha256": self.accepted_search_plan_sha256,
            "accepted_search_outcome_sha256": self.accepted_search_outcome_sha256,
            "closed_at": self.closed_at,
            "close_reason": self.close_reason.value if self.close_reason else None,
        }


@dataclass(frozen=True, slots=True)
class CompareConversationDocumentOutcome:
    """Accepted publication digests.

    ``model_evidence_sha256`` seals the transient body-sensitive projection
    supplied to synthesis; unlike the durable source identities it is never a
    replay authority and cannot be reconstructed without reauthorization.
    """

    plan_sha256: str
    answer_sha256: str
    status: CompareConversationDocumentStatus
    message_coverage_grade: SelectedArchiveCoverageGrade
    document_verification_complete: bool
    publication_attested: bool
    semantic_verified: bool
    message_evidence_sha256: str
    document_evidence_sha256: str
    evidence_bundle_sha256: str
    model_evidence_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.status) is not CompareConversationDocumentStatus
            or type(self.message_coverage_grade) is not SelectedArchiveCoverageGrade
            or self.status.value != self.message_coverage_grade.value
            or self.document_verification_complete is not True
            or self.publication_attested is not True
            or self.semantic_verified is not True
        ):
            raise WorkItemContractError("comparison acceptance state is invalid")
        for label in (
            "plan_sha256",
            "answer_sha256",
            "message_evidence_sha256",
            "document_evidence_sha256",
            "evidence_bundle_sha256",
            "model_evidence_sha256",
        ):
            _digest(getattr(self, label), label=label)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": COMPARE_CONVERSATION_DOCUMENT_OUTCOME_SCHEMA,
            "plan_sha256": self.plan_sha256,
            "answer_sha256": self.answer_sha256,
            "status": self.status.value,
            "message_coverage_grade": self.message_coverage_grade.value,
            "document_verification_complete": self.document_verification_complete,
            "publication_attested": self.publication_attested,
            "semantic_verified": self.semantic_verified,
            "message_evidence_sha256": self.message_evidence_sha256,
            "document_evidence_sha256": self.document_evidence_sha256,
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "model_evidence_sha256": self.model_evidence_sha256,
        }

    @classmethod
    def parse(cls, value: object) -> CompareConversationDocumentOutcome:
        if not isinstance(value, Mapping):
            raise WorkItemContractError("comparison outcome must be an object")
        _exact_keys(
            value,
            frozenset(
                {
                    "schema",
                    "plan_sha256",
                    "answer_sha256",
                    "status",
                    "message_coverage_grade",
                    "document_verification_complete",
                    "publication_attested",
                    "semantic_verified",
                    "message_evidence_sha256",
                    "document_evidence_sha256",
                    "evidence_bundle_sha256",
                    "model_evidence_sha256",
                }
            ),
            label="comparison outcome",
        )
        if value["schema"] != COMPARE_CONVERSATION_DOCUMENT_OUTCOME_SCHEMA:
            raise WorkItemContractError("comparison outcome schema is invalid")
        try:
            return cls(
                plan_sha256=value["plan_sha256"],  # type: ignore[arg-type]
                answer_sha256=value["answer_sha256"],  # type: ignore[arg-type]
                status=CompareConversationDocumentStatus(value["status"]),  # type: ignore[arg-type]
                message_coverage_grade=SelectedArchiveCoverageGrade(
                    value["message_coverage_grade"]  # type: ignore[arg-type]
                ),
                document_verification_complete=value["document_verification_complete"],  # type: ignore[arg-type]
                publication_attested=value["publication_attested"],  # type: ignore[arg-type]
                semantic_verified=value["semantic_verified"],  # type: ignore[arg-type]
                message_evidence_sha256=value["message_evidence_sha256"],  # type: ignore[arg-type]
                document_evidence_sha256=value["document_evidence_sha256"],  # type: ignore[arg-type]
                evidence_bundle_sha256=value["evidence_bundle_sha256"],  # type: ignore[arg-type]
                model_evidence_sha256=value["model_evidence_sha256"],  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            raise WorkItemContractError("comparison outcome is invalid") from exc

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_payload()).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class CompareConversationDocumentOutcomeReceipt:
    outcome: CompareConversationDocumentOutcome
    outcome_sha256: str

    def __post_init__(self) -> None:
        if type(self.outcome) is not CompareConversationDocumentOutcome:
            raise WorkItemContractError("comparison receipt outcome is invalid")
        if not hmac.compare_digest(
            _digest(self.outcome_sha256, label="outcome_sha256"),
            self.outcome.canonical_sha256,
        ):
            raise WorkItemContractError("comparison receipt digest changed")

    @classmethod
    def from_outcome(
        cls, outcome: CompareConversationDocumentOutcome
    ) -> CompareConversationDocumentOutcomeReceipt:
        return cls(outcome=outcome, outcome_sha256=outcome.canonical_sha256)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": COMPARE_CONVERSATION_DOCUMENT_OUTCOME_RECEIPT_SCHEMA,
            "outcome": self.outcome.to_payload(),
            "outcome_sha256": self.outcome_sha256,
        }

    @classmethod
    def parse(cls, value: object) -> CompareConversationDocumentOutcomeReceipt:
        if not isinstance(value, Mapping):
            raise WorkItemContractError("comparison receipt must be an object")
        _exact_keys(
            value,
            frozenset({"schema", "outcome", "outcome_sha256"}),
            label="comparison receipt",
        )
        if value["schema"] != COMPARE_CONVERSATION_DOCUMENT_OUTCOME_RECEIPT_SCHEMA:
            raise WorkItemContractError("comparison receipt schema is invalid")
        return cls(
            outcome=CompareConversationDocumentOutcome.parse(value["outcome"]),
            outcome_sha256=value["outcome_sha256"],  # type: ignore[arg-type]
        )


def attach_accepted_comparison_outcome_receipt(
    metadata: dict[str, object], outcome: CompareConversationDocumentOutcome
) -> CompareConversationDocumentOutcomeReceipt:
    if type(metadata) is not dict or ACCEPTED_COMPARISON_METADATA_KEY in metadata:
        raise WorkItemContractError("comparison receipt metadata slot is not pristine")
    receipt = CompareConversationDocumentOutcomeReceipt.from_outcome(outcome)
    payload = receipt.to_payload()
    candidate = dict(metadata)
    candidate[ACCEPTED_COMPARISON_METADATA_KEY] = payload
    _receipt_payload_bytes(candidate)
    metadata[ACCEPTED_COMPARISON_METADATA_KEY] = payload
    return receipt


def load_accepted_comparison_outcome_receipt(
    metadata: object,
) -> CompareConversationDocumentOutcomeReceipt:
    try:
        if isinstance(metadata, str):
            encoded = metadata.encode("utf-8", errors="strict")
            if not encoded or len(encoded) > _RECEIPT_MAX_BYTES:
                raise WorkItemContractError("assistant metadata exceeds its closed byte limit")
            decoded = json.loads(
                metadata,
                object_pairs_hook=_closed_json_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    WorkItemContractError("assistant metadata contains a non-finite number")
                ),
            )
        else:
            decoded = metadata
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise WorkItemContractError("assistant metadata is invalid") from exc
    if not isinstance(decoded, Mapping) or ACCEPTED_COMPARISON_METADATA_KEY not in decoded:
        raise WorkItemContractError("assistant has no accepted comparison receipt")
    _receipt_payload_bytes(decoded)
    receipt = CompareConversationDocumentOutcomeReceipt.parse(decoded[ACCEPTED_COMPARISON_METADATA_KEY])
    _receipt_payload_bytes(receipt.to_payload())
    return receipt


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedDocumentIdentity:
    """One exact Raw document pin; it contains no filename, path or body."""

    work_item_id: str
    provenance: ResolvedDocumentProvenance
    source_ref: SourceRef
    raw_object_id: str
    raw_source_identity_sha256: str
    raw_content_sha256: str
    content_sha256: str
    candidate_source_snapshot_sha256: str | None
    origin_boundary_user_message_id: str
    resolved_revision: int
    resolved_at: str
    candidate_set_id: str | None = None
    selected_ordinal: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
        _identifier(self.raw_object_id, _RAW_OBJECT_ID_RE, label="raw_object_id")
        _identifier(
            self.origin_boundary_user_message_id,
            _MESSAGE_ID_RE,
            label="origin_boundary_user_message_id",
        )
        if type(self.provenance) is not ResolvedDocumentProvenance:
            raise WorkItemContractError("document provenance is invalid")
        if (
            type(self.source_ref) is not SourceRef
            or self.source_ref.canonical_object_kind is not CanonicalObjectKind.RAW_OBJECT
            or self.source_ref.canonical_object_id != self.raw_object_id
            or self.source_ref.source_kind
            not in {SourceKind.DOCUMENT, SourceKind.WEB_CAPTURE, SourceKind.GENERATED_ARTIFACT}
            or self.source_ref.authority_scope is not AuthorityScope.TENANT_PRINCIPAL
        ):
            raise WorkItemContractError("resolved document source identity is invalid")
        for label in (
            "raw_source_identity_sha256",
            "raw_content_sha256",
            "content_sha256",
        ):
            _digest(getattr(self, label), label=label)
        if (
            not isinstance(self.resolved_revision, int)
            or isinstance(self.resolved_revision, bool)
            or not 2 <= self.resolved_revision <= 3
        ):
            raise WorkItemContractError("resolved document revision is invalid")
        resolved = canonical_work_item_instant(self.resolved_at, label="resolved_at")
        if resolved != self.resolved_at:
            raise WorkItemContractError("resolved_at must be canonical")
        if self.provenance is ResolvedDocumentProvenance.HISTORICAL_CANDIDATE_ORDINAL:
            if (
                self.candidate_set_id is None
                or self.candidate_source_snapshot_sha256 is None
                or not isinstance(self.selected_ordinal, int)
                or isinstance(self.selected_ordinal, bool)
                or not 1 <= self.selected_ordinal <= 20
            ):
                raise WorkItemContractError("candidate document provenance is incomplete")
            _identifier(self.candidate_set_id, _CANDIDATE_SET_ID_RE, label="candidate_set_id")
            _digest(
                self.candidate_source_snapshot_sha256,
                label="candidate_source_snapshot_sha256",
            )
        elif (
            self.candidate_set_id is not None
            or self.selected_ordinal is not None
            or self.candidate_source_snapshot_sha256 is not None
        ):
            raise WorkItemContractError("non-candidate document carries candidate provenance")

    def __repr__(self) -> str:
        return "ResolvedDocumentIdentity(private_source=True)"

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_payload()).encode("ascii")).hexdigest()

    @classmethod
    def from_storage_row(cls, value: Mapping[str, object]) -> ResolvedDocumentIdentity:
        _exact_keys(
            value,
            frozenset(
                {
                    "work_item_id",
                    "provenance",
                    "source_ref_json",
                    "raw_object_id",
                    "raw_source_identity_sha256",
                    "raw_content_sha256",
                    "content_sha256",
                    "origin_boundary_user_message_id",
                    "resolved_revision",
                    "resolved_at",
                    "candidate_set_id",
                    "selected_ordinal",
                    "candidate_source_snapshot_sha256",
                }
            ),
            label="resolved document storage row",
        )
        try:
            return cls(
                work_item_id=value["work_item_id"],  # type: ignore[arg-type]
                provenance=ResolvedDocumentProvenance(value["provenance"]),  # type: ignore[arg-type]
                source_ref=SourceRef.parse_private(value["source_ref_json"]),  # type: ignore[arg-type]
                raw_object_id=value["raw_object_id"],  # type: ignore[arg-type]
                raw_source_identity_sha256=value["raw_source_identity_sha256"],  # type: ignore[arg-type]
                raw_content_sha256=value["raw_content_sha256"],  # type: ignore[arg-type]
                content_sha256=value["content_sha256"],  # type: ignore[arg-type]
                candidate_source_snapshot_sha256=value["candidate_source_snapshot_sha256"],  # type: ignore[arg-type]
                origin_boundary_user_message_id=value["origin_boundary_user_message_id"],  # type: ignore[arg-type]
                resolved_revision=value["resolved_revision"],  # type: ignore[arg-type]
                resolved_at=value["resolved_at"],  # type: ignore[arg-type]
                candidate_set_id=value["candidate_set_id"],  # type: ignore[arg-type]
                selected_ordinal=value["selected_ordinal"],  # type: ignore[arg-type]
            )
        except (RetrievalContractError, TypeError, ValueError) as exc:
            raise WorkItemContractError("resolved document storage row is invalid") from exc

    def to_storage_payload(self) -> dict[str, object]:
        return {
            "work_item_id": self.work_item_id,
            "provenance": self.provenance.value,
            "source_ref_json": self.source_ref.to_private_json(),
            "raw_object_id": self.raw_object_id,
            "raw_source_identity_sha256": self.raw_source_identity_sha256,
            "raw_content_sha256": self.raw_content_sha256,
            "content_sha256": self.content_sha256,
            "candidate_source_snapshot_sha256": self.candidate_source_snapshot_sha256,
            "origin_boundary_user_message_id": self.origin_boundary_user_message_id,
            "resolved_revision": self.resolved_revision,
            "resolved_at": self.resolved_at,
            "candidate_set_id": self.candidate_set_id,
            "selected_ordinal": self.selected_ordinal,
        }

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": "friday.resolved-comparison-document-identity.v1",
            **self.to_storage_payload(),
            "source_ref": self.source_ref.to_private_payload(),
        }
        del payload["source_ref_json"]
        return payload


@dataclass(frozen=True, slots=True)
class AcceptedComparisonResultIdentity:
    work_item_id: str
    answer_boundary_user_message_id: str
    answer_assistant_message_id: str
    accepted_plan_sha256: str
    accepted_outcome_sha256: str
    comparison_status: CompareConversationDocumentStatus
    message_coverage_grade: SelectedArchiveCoverageGrade
    document_verification_complete: bool
    publication_attested: bool
    semantic_verified: bool
    message_evidence_sha256: str
    document_evidence_sha256: str
    evidence_bundle_sha256: str
    model_evidence_sha256: str
    completed_revision: int
    completed_at: str

    def __post_init__(self) -> None:
        _identifier(self.work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
        _identifier(self.answer_boundary_user_message_id, _MESSAGE_ID_RE, label="answer_boundary")
        _identifier(self.answer_assistant_message_id, _MESSAGE_ID_RE, label="answer_assistant")
        if self.answer_boundary_user_message_id == self.answer_assistant_message_id:
            raise WorkItemContractError("comparison result anchors must differ")
        if (
            type(self.comparison_status) is not CompareConversationDocumentStatus
            or type(self.message_coverage_grade) is not SelectedArchiveCoverageGrade
            or self.comparison_status.value != self.message_coverage_grade.value
            or self.document_verification_complete is not True
            or self.publication_attested is not True
            or self.semantic_verified is not True
        ):
            raise WorkItemContractError("comparison result acceptance state is invalid")
        for label in (
            "accepted_plan_sha256",
            "accepted_outcome_sha256",
            "message_evidence_sha256",
            "document_evidence_sha256",
            "evidence_bundle_sha256",
            "model_evidence_sha256",
        ):
            _digest(getattr(self, label), label=label)
        if (
            not isinstance(self.completed_revision, int)
            or isinstance(self.completed_revision, bool)
            or not 3 <= self.completed_revision <= 4
        ):
            raise WorkItemContractError("completed revision is invalid")
        completed = canonical_work_item_instant(self.completed_at, label="completed_at")
        if completed != self.completed_at:
            raise WorkItemContractError("completed_at must be canonical")

    @classmethod
    def from_storage_row(cls, value: Mapping[str, object]) -> AcceptedComparisonResultIdentity:
        _exact_keys(
            value,
            frozenset(
                {
                    "work_item_id",
                    "answer_boundary_user_message_id",
                    "answer_assistant_message_id",
                    "accepted_plan_sha256",
                    "accepted_outcome_sha256",
                    "comparison_status",
                    "message_coverage_grade",
                    "document_verification_complete",
                    "publication_attested",
                    "semantic_verified",
                    "message_evidence_sha256",
                    "document_evidence_sha256",
                    "evidence_bundle_sha256",
                    "model_evidence_sha256",
                    "completed_revision",
                    "completed_at",
                }
            ),
            label="comparison result storage row",
        )
        for key in (
            "document_verification_complete",
            "publication_attested",
            "semantic_verified",
        ):
            if type(value[key]) is not int or value[key] != 1:
                raise WorkItemContractError("comparison result acceptance flag is invalid")
        try:
            return cls(
                work_item_id=value["work_item_id"],  # type: ignore[arg-type]
                answer_boundary_user_message_id=value["answer_boundary_user_message_id"],  # type: ignore[arg-type]
                answer_assistant_message_id=value["answer_assistant_message_id"],  # type: ignore[arg-type]
                accepted_plan_sha256=value["accepted_plan_sha256"],  # type: ignore[arg-type]
                accepted_outcome_sha256=value["accepted_outcome_sha256"],  # type: ignore[arg-type]
                comparison_status=CompareConversationDocumentStatus(
                    value["comparison_status"]  # type: ignore[arg-type]
                ),
                message_coverage_grade=SelectedArchiveCoverageGrade(
                    value["message_coverage_grade"]  # type: ignore[arg-type]
                ),
                document_verification_complete=value["document_verification_complete"] == 1,
                publication_attested=value["publication_attested"] == 1,
                semantic_verified=value["semantic_verified"] == 1,
                message_evidence_sha256=value["message_evidence_sha256"],  # type: ignore[arg-type]
                document_evidence_sha256=value["document_evidence_sha256"],  # type: ignore[arg-type]
                evidence_bundle_sha256=value["evidence_bundle_sha256"],  # type: ignore[arg-type]
                model_evidence_sha256=value["model_evidence_sha256"],  # type: ignore[arg-type]
                completed_revision=value["completed_revision"],  # type: ignore[arg-type]
                completed_at=value["completed_at"],  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            raise WorkItemContractError("comparison result storage row is invalid") from exc

    def to_payload(self) -> dict[str, object]:
        payload = {"schema": ACCEPTED_COMPARISON_RESULT_SCHEMA, **self.to_storage_payload()}
        payload.update(
            {
                "document_verification_complete": self.document_verification_complete,
                "publication_attested": self.publication_attested,
                "semantic_verified": self.semantic_verified,
            }
        )
        return payload

    def to_storage_payload(self) -> dict[str, object]:
        return {
            "work_item_id": self.work_item_id,
            "answer_boundary_user_message_id": self.answer_boundary_user_message_id,
            "answer_assistant_message_id": self.answer_assistant_message_id,
            "accepted_plan_sha256": self.accepted_plan_sha256,
            "accepted_outcome_sha256": self.accepted_outcome_sha256,
            "comparison_status": self.comparison_status.value,
            "message_coverage_grade": self.message_coverage_grade.value,
            "document_verification_complete": int(self.document_verification_complete),
            "publication_attested": int(self.publication_attested),
            "semantic_verified": int(self.semantic_verified),
            "message_evidence_sha256": self.message_evidence_sha256,
            "document_evidence_sha256": self.document_evidence_sha256,
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "model_evidence_sha256": self.model_evidence_sha256,
            "completed_revision": self.completed_revision,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class CompareConversationWithDocumentWorkItem:
    id: str
    user_id: str
    conversation_id: str
    state: WorkState
    active_frame: CompareConversationDocumentActiveFrame
    anchor_user_message_id: str
    anchor_assistant_message_id: str
    accepted_plan_sha256: str
    accepted_outcome_sha256: str
    revision: int
    transition: WorkTransition
    created_at: str
    updated_at: str
    expires_at: str
    closed_at: str | None
    selected_message_evidence: SelectedArchiveEvidence
    document_questions: tuple[DocumentReferenceQuestion, ...]
    document_candidate_set: ArchiveCandidateSet | None = None
    resolved_document_evidence: ResolvedDocumentIdentity | None = None
    accepted_comparison: AcceptedComparisonResultIdentity | None = None

    def __post_init__(self) -> None:
        _identifier(self.id, _WORK_ITEM_ID_RE, label="work_item_id")
        _identifier(self.user_id, _USER_ID_RE, label="user_id")
        _identifier(self.conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
        _identifier(self.anchor_user_message_id, _MESSAGE_ID_RE, label="anchor_user_message_id")
        _identifier(self.anchor_assistant_message_id, _MESSAGE_ID_RE, label="anchor_assistant_message_id")
        if self.anchor_user_message_id == self.anchor_assistant_message_id:
            raise WorkItemContractError("comparison work anchors must differ")
        _digest(self.accepted_plan_sha256, label="accepted_plan_sha256")
        _digest(self.accepted_outcome_sha256, label="accepted_outcome_sha256")
        if type(self.state) is not WorkState or type(self.transition) is not WorkTransition:
            raise WorkItemContractError("comparison lifecycle enums are invalid")
        if type(self.active_frame) is not CompareConversationDocumentActiveFrame:
            raise WorkItemContractError("comparison active frame is invalid")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or not 1 <= self.revision <= WORK_ITEM_MAX_REVISION
        ):
            raise WorkItemContractError("comparison revision is outside the closed limit")
        created = canonical_work_item_instant(self.created_at, label="created_at")
        updated = canonical_work_item_instant(self.updated_at, label="updated_at")
        expires = canonical_work_item_instant(self.expires_at, label="expires_at")
        if (created, updated, expires) != (self.created_at, self.updated_at, self.expires_at):
            raise WorkItemContractError("comparison timestamps must be canonical")
        if updated < created or datetime.fromisoformat(expires) > datetime.fromisoformat(updated) + timedelta(
            hours=WORK_ITEM_TTL_HOURS
        ):
            raise WorkItemContractError("comparison timestamps are invalid")
        self._validate_evidence()
        self._validate_questions()
        self._validate_lifecycle()

    def _validate_evidence(self) -> None:
        evidence = self.selected_message_evidence
        if (
            type(evidence) is not SelectedArchiveEvidence
            or evidence.work_item_id != self.id
            or evidence.corpus is not SelectedArchiveCorpus.MESSAGES
            or evidence.origin_boundary_user_message_id != self.anchor_user_message_id
            or evidence.source_ref.source_kind is not SourceKind.CONVERSATION
            or evidence.source_ref.authority_scope is not AuthorityScope.PRINCIPAL
            or evidence.source_ref.tenant_id is not None
            or evidence.source_ref.principal_id != self.user_id
            or evidence.source_ref.canonical_object_kind is not CanonicalObjectKind.CONVERSATION
        ):
            raise WorkItemContractError("selected message evidence is not exact and owned")
        document = self.resolved_document_evidence
        if document is not None and (
            type(document) is not ResolvedDocumentIdentity
            or document.work_item_id != self.id
            or document.source_ref.principal_id != self.user_id
        ):
            raise WorkItemContractError("resolved document evidence is not exact and owned")

    def _validate_questions(self) -> None:
        if (
            type(self.document_questions) is not tuple
            or not 1 <= len(self.document_questions) <= 2
            or any(type(item) is not DocumentReferenceQuestion for item in self.document_questions)
            or any(item.work_item_id != self.id for item in self.document_questions)
            or any(
                item.admission_shape is not self.document_questions[0].admission_shape
                for item in self.document_questions
            )
            or tuple(item.work_revision for item in self.document_questions)
            != tuple(range(1, len(self.document_questions) + 1))
            or self.document_questions[0].kind is not DocumentReferenceQuestionKind.PROVIDE_DOCUMENT_REFERENCE
        ):
            raise WorkItemContractError("comparison question history is invalid")
        if (
            self.document_questions[0].created_at != self.created_at
            or any(question.created_at > self.updated_at for question in self.document_questions)
            or any(
                question.closed_at is not None and question.closed_at > self.updated_at
                for question in self.document_questions
            )
            or any(
                earlier.closed_at is None or later.created_at < earlier.closed_at
                for earlier, later in zip(
                    self.document_questions,
                    self.document_questions[1:],
                    strict=False,
                )
            )
        ):
            raise WorkItemContractError("comparison question timeline is invalid")
        candidate_set = self.document_candidate_set
        if len(self.document_questions) == 2:
            question = self.document_questions[1]
            if (
                question.kind is not DocumentReferenceQuestionKind.SELECT_DOCUMENT_CANDIDATE
                or candidate_set is None
                or candidate_set.work_item_id != self.id
                or question.candidate_set_id != candidate_set.id
                or candidate_set.origin_boundary_user_message_id
                != self.document_questions[0].answer_user_message_id
                or any(
                    candidate.corpus is not SelectedArchiveCorpus.DOCUMENTS
                    or candidate.source_ref.principal_id != self.user_id
                    for candidate in candidate_set.candidates
                )
                or len({candidate.source_ref.tenant_id for candidate in candidate_set.candidates}) != 1
            ):
                raise WorkItemContractError("document candidate ambiguity is invalid")
            if question.state is DocumentReferenceQuestionState.ANSWERED:
                selected = candidate_set.selected_evidence(question.selected_ordinal or 0)
                resolved = self.resolved_document_evidence
                if resolved is None or (
                    resolved.source_ref,
                    resolved.candidate_source_snapshot_sha256,
                    resolved.candidate_set_id,
                    resolved.selected_ordinal,
                ) != (
                    selected.source_ref,
                    selected.source_snapshot_sha256,
                    candidate_set.id,
                    question.selected_ordinal,
                ):
                    raise WorkItemContractError("resolved document changed after ordinal selection")
                if any(
                    passage.source_revision.kind is not RevisionKind.RAW_CONTENT_SHA256
                    or passage.source_revision.value != resolved.raw_content_sha256
                    for passage in selected.passage_refs
                ):
                    raise WorkItemContractError("resolved document Raw revision changed after selection")
        elif candidate_set is not None:
            raise WorkItemContractError("candidate set requires an ordinal question")

    def _validate_lifecycle(self) -> None:
        questions = self.document_questions
        waiting = tuple(item for item in questions if item.state is DocumentReferenceQuestionState.WAITING)
        resolved_revision = len(questions) + 1
        document = self.resolved_document_evidence
        if document is not None:
            last_closed_at = questions[-1].closed_at
            if (
                last_closed_at is None
                or document.resolved_revision != resolved_revision
                or document.origin_boundary_user_message_id != questions[-1].answer_user_message_id
                or document.resolved_at < last_closed_at
                or document.resolved_at > self.updated_at
            ):
                raise WorkItemContractError("resolved document revision boundary is invalid")
        if self.state is WorkState.WAITING_FOR_INPUT:
            expected_transition = (
                WorkTransition.QUESTION_ASKED if len(questions) == 1 else WorkTransition.QUESTION_REASKED
            )
            if (
                self.transition is not expected_transition
                or self.revision != len(questions)
                or len(waiting) != 1
                or waiting[0] is not questions[-1]
                or self.resolved_document_evidence is not None
                or self.accepted_comparison is not None
                or self.closed_at is not None
                or self.expires_at <= self.updated_at
            ):
                raise WorkItemContractError("waiting comparison lifecycle is invalid")
            return
        if self.state is WorkState.ACTIVE:
            if (
                self.transition is not WorkTransition.DOCUMENT_RESOLVED
                or self.revision != resolved_revision
                or waiting
                or any(item.state is not DocumentReferenceQuestionState.ANSWERED for item in questions)
                or self.resolved_document_evidence is None
                or self.accepted_comparison is not None
                or self.closed_at is not None
                or self.expires_at <= self.updated_at
            ):
                raise WorkItemContractError("active comparison lifecycle is invalid")
            return
        if self.state is WorkState.COMPLETED:
            result = self.accepted_comparison
            document = self.resolved_document_evidence
            if (
                self.transition is not WorkTransition.COMPARISON_PUBLISHED
                or self.revision != resolved_revision + 1
                or waiting
                or document is None
                or result is None
                or self.closed_at != self.updated_at
                or result.completed_at != self.updated_at
                or result.completed_revision != self.revision
                or result.work_item_id != self.id
                or result.message_coverage_grade is not self.selected_message_evidence.coverage_grade
                or result.comparison_status.value != self.selected_message_evidence.coverage_grade.value
                or result.message_evidence_sha256 != selected_evidence_sha256(self.selected_message_evidence)
                or result.document_evidence_sha256 != document.canonical_sha256
                or result.evidence_bundle_sha256
                != comparison_evidence_bundle_sha256(self.selected_message_evidence, document)
            ):
                raise WorkItemContractError("completed comparison lifecycle is invalid")
            return
        if self.state not in {WorkState.SUSPENDED, WorkState.CANCELLED, WorkState.EXPIRED}:
            raise WorkItemContractError("comparison state is unsupported")
        if self.transition.value != self.state.value or waiting:
            raise WorkItemContractError("stopped comparison lifecycle is invalid")
        if self.accepted_comparison is not None:
            raise WorkItemContractError("stopped comparison cannot carry an accepted result")
        base_revision = resolved_revision if document is not None else len(questions)
        if self.state is WorkState.SUSPENDED and self.revision != base_revision + 1:
            raise WorkItemContractError("suspended comparison revision is invalid")
        if self.state in {WorkState.CANCELLED, WorkState.EXPIRED} and self.revision not in {
            base_revision + 1,
            base_revision + 2,
        }:
            raise WorkItemContractError("stopped comparison revision is invalid")
        if document is None:
            if questions[-1].state is not DocumentReferenceQuestionState.CLOSED:
                raise WorkItemContractError("stopped unresolved question is not closed")
            expected_reason = (
                DocumentReferenceQuestionCloseReason.SUSPENDED
                if self.state is WorkState.SUSPENDED or self.revision == base_revision + 2
                else DocumentReferenceQuestionCloseReason(self.state.value)
            )
            if questions[-1].close_reason is not expected_reason:
                raise WorkItemContractError("stopped question reason is invalid")
        elif any(question.state is not DocumentReferenceQuestionState.ANSWERED for question in questions):
            raise WorkItemContractError("stopped resolved comparison has open questions")
        if self.state is WorkState.SUSPENDED:
            if self.closed_at is not None or self.expires_at <= self.updated_at:
                raise WorkItemContractError("suspended comparison lifecycle is invalid")
        elif self.closed_at != self.updated_at:
            raise WorkItemContractError("terminal comparison closure is invalid")
        if self.state is WorkState.EXPIRED and self.expires_at > self.updated_at:
            raise WorkItemContractError("expired comparison is not due")

    @classmethod
    def from_storage_rows(
        cls,
        work: Mapping[str, object],
        selected_message_evidence: SelectedArchiveEvidence,
        document_questions: tuple[DocumentReferenceQuestion, ...],
        document_candidate_set: ArchiveCandidateSet | None,
        resolved_document_evidence: ResolvedDocumentIdentity | None,
        accepted_comparison: AcceptedComparisonResultIdentity | None,
    ) -> CompareConversationWithDocumentWorkItem:
        _exact_keys(
            work,
            frozenset(
                {
                    "id",
                    "user_id",
                    "conversation_id",
                    "kind",
                    "goal",
                    "state",
                    "playbook",
                    "completion_contract",
                    "active_frame_json",
                    "anchor_user_message_id",
                    "anchor_assistant_message_id",
                    "accepted_plan_sha256",
                    "accepted_outcome_sha256",
                    "revision",
                    "transition",
                    "created_at",
                    "updated_at",
                    "expires_at",
                    "closed_at",
                }
            ),
            label="comparison work storage row",
        )
        if (
            work["kind"] != WorkKind.COMPARE_CONVERSATION_WITH_DOCUMENT.value
            or work["goal"] != WorkGoal.COMPARE_EXACT_MESSAGE_EVIDENCE_WITH_DOCUMENT.value
            or work["playbook"] != WorkPlaybook.COMPARE_CONVERSATION_WITH_DOCUMENT.value
            or work["completion_contract"]
            != WorkCompletionContract.ACCEPTED_EXACT_MESSAGE_AND_DOCUMENT_COMPARISON.value
        ):
            raise WorkItemContractError("comparison workflow identity is invalid")
        try:
            return cls(
                id=work["id"],  # type: ignore[arg-type]
                user_id=work["user_id"],  # type: ignore[arg-type]
                conversation_id=work["conversation_id"],  # type: ignore[arg-type]
                state=WorkState(work["state"]),  # type: ignore[arg-type]
                active_frame=CompareConversationDocumentActiveFrame.parse(work["active_frame_json"]),
                anchor_user_message_id=work["anchor_user_message_id"],  # type: ignore[arg-type]
                anchor_assistant_message_id=work["anchor_assistant_message_id"],  # type: ignore[arg-type]
                accepted_plan_sha256=work["accepted_plan_sha256"],  # type: ignore[arg-type]
                accepted_outcome_sha256=work["accepted_outcome_sha256"],  # type: ignore[arg-type]
                revision=work["revision"],  # type: ignore[arg-type]
                transition=WorkTransition(work["transition"]),  # type: ignore[arg-type]
                created_at=work["created_at"],  # type: ignore[arg-type]
                updated_at=work["updated_at"],  # type: ignore[arg-type]
                expires_at=work["expires_at"],  # type: ignore[arg-type]
                closed_at=work["closed_at"],  # type: ignore[arg-type]
                selected_message_evidence=selected_message_evidence,
                document_questions=document_questions,
                document_candidate_set=document_candidate_set,
                resolved_document_evidence=resolved_document_evidence,
                accepted_comparison=accepted_comparison,
            )
        except (TypeError, ValueError) as exc:
            raise WorkItemContractError("comparison work storage row is invalid") from exc

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": COMPARE_CONVERSATION_DOCUMENT_WORK_ITEM_SCHEMA,
            "id": self.id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "kind": WorkKind.COMPARE_CONVERSATION_WITH_DOCUMENT.value,
            "goal": WorkGoal.COMPARE_EXACT_MESSAGE_EVIDENCE_WITH_DOCUMENT.value,
            "state": self.state.value,
            "playbook": WorkPlaybook.COMPARE_CONVERSATION_WITH_DOCUMENT.value,
            "completion_contract": (
                WorkCompletionContract.ACCEPTED_EXACT_MESSAGE_AND_DOCUMENT_COMPARISON.value
            ),
            "active_frame": self.active_frame.to_payload(),
            "anchor_user_message_id": self.anchor_user_message_id,
            "anchor_assistant_message_id": self.anchor_assistant_message_id,
            "accepted_plan_sha256": self.accepted_plan_sha256,
            "accepted_outcome_sha256": self.accepted_outcome_sha256,
            "revision": self.revision,
            "transition": self.transition.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "closed_at": self.closed_at,
            "selected_message_evidence": self.selected_message_evidence.to_payload(),
            "document_questions": [item.to_payload() for item in self.document_questions],
            "document_candidate_set": (
                None if self.document_candidate_set is None else self.document_candidate_set.to_payload()
            ),
            "resolved_document_evidence": (
                None
                if self.resolved_document_evidence is None
                else self.resolved_document_evidence.to_payload()
            ),
            "accepted_comparison": (
                None if self.accepted_comparison is None else self.accepted_comparison.to_payload()
            ),
        }


__all__ = [
    "ACCEPTED_COMPARE_DOCUMENT_CANDIDATE_METADATA_KEY",
    "ACCEPTED_COMPARISON_METADATA_KEY",
    "ACCEPTED_COMPARISON_RESULT_SCHEMA",
    "COMPARE_CONVERSATION_DOCUMENT_ACTIVE_FRAME_SCHEMA",
    "COMPARE_CONVERSATION_DOCUMENT_OUTCOME_RECEIPT_SCHEMA",
    "COMPARE_CONVERSATION_DOCUMENT_OUTCOME_SCHEMA",
    "COMPARE_CONVERSATION_DOCUMENT_WORK_ITEM_SCHEMA",
    "COMPARE_DOCUMENT_CANDIDATE_OUTCOME_RECEIPT_SCHEMA",
    "COMPARE_DOCUMENT_CANDIDATE_OUTCOME_SCHEMA",
    "COMPARE_DOCUMENT_CANDIDATE_REQUIRED_VERDICT_KIND",
    "COMPARE_DOCUMENT_REFERENCE_REQUIRED_VERDICT_KIND",
    "COMPARE_DOCUMENT_REFERENCE_PROMPT",
    "DOCUMENT_REFERENCE_QUESTION_SCHEMA",
    "AcceptedComparisonResultIdentity",
    "CompareConversationDocumentActiveFrame",
    "CompareConversationDocumentOutcome",
    "CompareConversationDocumentOutcomeReceipt",
    "CompareConversationDocumentStatus",
    "CompareConversationWithDocumentWorkItem",
    "CompareDocumentCandidateOutcome",
    "CompareDocumentCandidateOutcomeReceipt",
    "DocumentReferenceAdmissionShape",
    "DocumentReferenceQuestion",
    "DocumentReferenceQuestionCloseReason",
    "DocumentReferenceQuestionKind",
    "DocumentReferenceQuestionState",
    "ResolvedDocumentIdentity",
    "ResolvedDocumentProvenance",
    "attach_accepted_compare_document_candidate_outcome_receipt",
    "attach_accepted_comparison_outcome_receipt",
    "comparison_evidence_bundle_sha256",
    "load_accepted_comparison_outcome_receipt",
    "load_accepted_compare_document_candidate_outcome_receipt",
    "selected_evidence_sha256",
]
