"""Code-owned producer for body-free promoted supervisor product events.

The producer runs only after the assistant/WorkGraph publication transaction
has committed.  It opens a new ``BEGIN IMMEDIATE`` transaction, re-reads the
committed TurnTrace and exact full/terminal WorkGraph receipt, verifies their
opaque HMAC linkage, and appends one closed product event.  No controller,
model, tool, publication handle, raw identifier, message body, path, or query
enters the event.

User-visible regression evaluation is deliberately an injected typed port.
Without an exact code-owned receipt the producer records ``not_evaluated``;
it never turns a free-form label into a no-regression claim.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from friday.interaction_control_plane.compare_current_file_web_work_graph import (
    CompareCurrentFileWebGraphOutcomeStatus,
    CompareCurrentFileWebGraphState,
    CompareCurrentFileWebPublicationReceipt,
    CompareCurrentFileWebTerminalPublicationReceipt,
    CompareCurrentFileWebWorkGraph,
    load_compare_current_file_web_publication_receipt,
    load_compare_current_file_web_terminal_publication_receipt,
)
from friday.interaction_control_plane.compare_current_file_web_work_graph_store import (
    get_compare_current_file_web_work_graph_in_transaction,
)
from friday.interaction_control_plane.runtime_trace import (
    INTERACTION_TRACE_METADATA_KEY,
    load_trace_namespace_key,
)
from friday.interaction_control_plane.turn_trace import (
    CompletionDecision,
    PlaybookClass,
    PublicationStatus,
    TraceIdentifierDomain,
    TurnTrace,
    WorkRelation,
    derive_trace_identifier,
)
from friday.orchestration.supervisor_assist_promotion import (
    AssistPromotionDecision,
    AssistPromotionReadiness,
    AssistPromotionReason,
)
from friday.orchestration.supervisor_contracts import SupervisorMode, TaskClass, canonical_sha256
from friday.orchestration.supervisor_production_baseline import (
    SUPERVISOR_PROMOTED_PRODUCT_EVENT,
    PromotedObservationEligibility,
    PromotedSupervisorProductObservation,
    PromotedUserVisibleOutcome,
    SupervisorBaselineError,
)
from friday.storage._base import RUNTIME_EVENT_CAP
from friday.storage.models import new_id, utc_now

SUPERVISOR_PROMOTED_OUTCOME_INPUT_SCHEMA = "friday.semantic-supervisor-promoted-outcome-input.v1"
SUPERVISOR_PROMOTED_OUTCOME_RECEIPT_SCHEMA = "friday.semantic-supervisor-promoted-outcome-receipt.v1"
SUPERVISOR_PROMOTED_EVENT_EMISSION_RECEIPT_SCHEMA = (
    "friday.semantic-supervisor-promoted-event-emission-receipt.v1"
)
SUPERVISOR_LATENCY_BUDGET_DOCUMENT_SCHEMA = "friday.semantic-supervisor-latency-budget-document.v1"
SUPERVISOR_ACCEPTED_LATENCY_BUDGET_SCHEMA = "friday.accepted-semantic-supervisor-latency-budget.v1"
SUPERVISOR_LATENCY_BUDGET_ID = "current-file-web-user-visible-latency-v1"
SUPERVISOR_LATENCY_MEASUREMENT = "committed_turn_trace.budget.latency_ms"
SUPERVISOR_OUTCOME_EVALUATOR_ID = "code-owned-user-visible-regression-v1"

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_METADATA_BYTES = 65_536
_MAX_EVENT_BYTES = 32_768
_MAX_BUDGET_DOCUMENT_BYTES = 4_096
_MAX_TRACE_SCAN = 10_000
_MAX_LATENCY_MS = 86_400_000


class PromotedProductEventError(ValueError):
    """A promoted product event cannot be proven from committed state."""


class PromotedProductEventReplayError(PromotedProductEventError):
    """The exact committed turn already has this product event."""


class PromotedProductEventConflictError(PromotedProductEventError):
    """A conflicting event or durable identity already exists."""


class PromotedOutcomeEvaluatorAuthority(StrEnum):
    NONE = "none"
    CODE_OWNED = SUPERVISOR_OUTCOME_EVALUATOR_ID


def _digest(value: object, *, label: str, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise PromotedProductEventError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _strict_json_object(raw: str, *, maximum_bytes: int, label: str) -> dict[str, Any]:
    if type(raw) is not str:
        raise PromotedProductEventError(f"{label} must be JSON text")
    try:
        encoded = raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PromotedProductEventError(f"{label} is not valid UTF-8") from exc
    if not encoded or len(encoded) > maximum_bytes:
        raise PromotedProductEventError(f"{label} exceeds its byte budget")

    def closed_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PromotedProductEventError(f"{label} contains duplicate keys")
            result[key] = value
        return result

    def reject_constant(_value: str) -> Any:
        raise PromotedProductEventError(f"{label} contains a non-finite number")

    try:
        value = json.loads(
            raw,
            object_pairs_hook=closed_pairs,
            parse_constant=reject_constant,
        )
    except PromotedProductEventError:
        raise
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise PromotedProductEventError(f"{label} is malformed") from exc
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise PromotedProductEventError(f"{label} must be one object")
    return value


@dataclass(frozen=True, slots=True)
class PromotedProductOutcomeInput:
    """Body-free facts available to a future code-owned regression evaluator."""

    mode: SupervisorMode
    eligibility: PromotedObservationEligibility
    primary_trace_sha256: str
    execution_receipt_sha256: str | None
    graph_state: CompareCurrentFileWebGraphState | None
    graph_outcome_status: CompareCurrentFileWebGraphOutcomeStatus | None
    completion: CompletionDecision
    publication: PublicationStatus
    latency_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.mode, SupervisorMode) or self.mode not in {
            SupervisorMode.ASSIST,
            SupervisorMode.CANARY,
        }:
            raise PromotedProductEventError("outcome input mode is not promoted")
        if not isinstance(self.eligibility, PromotedObservationEligibility):
            raise PromotedProductEventError("outcome input eligibility is not typed")
        _digest(self.primary_trace_sha256, label="primary_trace_sha256")
        _digest(
            self.execution_receipt_sha256,
            label="execution_receipt_sha256",
            optional=True,
        )
        if not isinstance(self.completion, CompletionDecision) or not isinstance(
            self.publication,
            PublicationStatus,
        ):
            raise PromotedProductEventError("outcome input trace state is not typed")
        if type(self.latency_ms) is not int or not 0 <= self.latency_ms <= _MAX_LATENCY_MS:
            raise PromotedProductEventError("outcome input latency is invalid")
        if self.eligibility is PromotedObservationEligibility.PROMOTED_JOURNEY:
            if (
                self.execution_receipt_sha256 is None
                or self.graph_state
                not in {
                    CompareCurrentFileWebGraphState.TERMINAL,
                    CompareCurrentFileWebGraphState.COMPLETED,
                }
                or not isinstance(
                    self.graph_outcome_status,
                    CompareCurrentFileWebGraphOutcomeStatus,
                )
            ):
                raise PromotedProductEventError("promoted outcome input lacks a durable receipt")
        elif any(
            value is not None
            for value in (
                self.execution_receipt_sha256,
                self.graph_state,
                self.graph_outcome_status,
            )
        ):
            raise PromotedProductEventError("other-turn outcome input cannot claim a WorkGraph")

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_PROMOTED_OUTCOME_INPUT_SCHEMA,
            "mode": self.mode.value,
            "eligibility": self.eligibility.value,
            "primary_trace_sha256": self.primary_trace_sha256,
            "execution_receipt_sha256": self.execution_receipt_sha256,
            "graph_state": self.graph_state.value if self.graph_state is not None else None,
            "graph_outcome_status": (
                self.graph_outcome_status.value if self.graph_outcome_status is not None else None
            ),
            "completion": self.completion.value,
            "publication": self.publication.value,
            "latency_ms": self.latency_ms,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())


@dataclass(frozen=True, slots=True)
class PromotedProductOutcomeReceipt:
    """Exact output accepted from the optional code-owned evaluator port."""

    input_sha256: str
    outcome: PromotedUserVisibleOutcome
    evaluator_authority: PromotedOutcomeEvaluatorAuthority
    evaluator_evidence_sha256: str | None

    def __post_init__(self) -> None:
        _digest(self.input_sha256, label="outcome input digest")
        if not isinstance(self.outcome, PromotedUserVisibleOutcome) or not isinstance(
            self.evaluator_authority,
            PromotedOutcomeEvaluatorAuthority,
        ):
            raise PromotedProductEventError("outcome receipt is not typed")
        _digest(
            self.evaluator_evidence_sha256,
            label="evaluator evidence digest",
            optional=True,
        )
        if self.outcome is PromotedUserVisibleOutcome.NOT_EVALUATED:
            if (
                self.evaluator_authority is not PromotedOutcomeEvaluatorAuthority.NONE
                or self.evaluator_evidence_sha256 is not None
            ):
                raise PromotedProductEventError("not-evaluated cannot claim evaluator evidence")
        elif (
            self.evaluator_authority is not PromotedOutcomeEvaluatorAuthority.CODE_OWNED
            or self.evaluator_evidence_sha256 is None
        ):
            raise PromotedProductEventError("visible outcome requires code-owned evaluator evidence")

    @classmethod
    def not_evaluated(
        cls,
        outcome_input: PromotedProductOutcomeInput,
    ) -> PromotedProductOutcomeReceipt:
        return cls(
            input_sha256=outcome_input.canonical_sha256(),
            outcome=PromotedUserVisibleOutcome.NOT_EVALUATED,
            evaluator_authority=PromotedOutcomeEvaluatorAuthority.NONE,
            evaluator_evidence_sha256=None,
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_PROMOTED_OUTCOME_RECEIPT_SCHEMA,
            "input_sha256": self.input_sha256,
            "outcome": self.outcome.value,
            "evaluator_authority": self.evaluator_authority.value,
            "evaluator_evidence_sha256": self.evaluator_evidence_sha256,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())


class PromotedUserVisibleOutcomeEvaluator(Protocol):
    """Future pure evaluator; it receives no storage or publication authority."""

    def evaluate(
        self,
        outcome_input: PromotedProductOutcomeInput,
    ) -> PromotedProductOutcomeReceipt: ...


@dataclass(frozen=True, slots=True)
class PromotedProductEmissionRequest:
    """Only opaque committed identities and closed structural facts enter emission."""

    eligibility: PromotedObservationEligibility
    primary_trace_sha256: str
    execution_receipt_sha256: str | None
    supervisor_invoked: bool

    def __post_init__(self) -> None:
        if not isinstance(self.eligibility, PromotedObservationEligibility):
            raise PromotedProductEventError("emission eligibility is not typed")
        _digest(self.primary_trace_sha256, label="primary_trace_sha256")
        _digest(
            self.execution_receipt_sha256,
            label="execution_receipt_sha256",
            optional=True,
        )
        if type(self.supervisor_invoked) is not bool:
            raise PromotedProductEventError("supervisor_invoked must be boolean")
        if self.eligibility is PromotedObservationEligibility.PROMOTED_JOURNEY:
            if self.execution_receipt_sha256 is None or not self.supervisor_invoked:
                raise PromotedProductEventError(
                    "promoted journey requires one invoked durable execution receipt"
                )
        elif self.execution_receipt_sha256 is not None:
            raise PromotedProductEventError("other turn cannot claim an execution receipt")


@dataclass(frozen=True, slots=True)
class PromotedProductEventEmissionReceipt:
    """Body-free result; the runtime event row ID is intentionally not exposed."""

    event_sha256: str
    primary_trace_sha256: str
    promotion_evidence_sha256: str
    execution_receipt_sha256: str | None
    outcome_evaluation_sha256: str

    def __post_init__(self) -> None:
        for label, value in (
            ("event_sha256", self.event_sha256),
            ("primary_trace_sha256", self.primary_trace_sha256),
            ("promotion_evidence_sha256", self.promotion_evidence_sha256),
            ("outcome_evaluation_sha256", self.outcome_evaluation_sha256),
        ):
            _digest(value, label=label)
        _digest(
            self.execution_receipt_sha256,
            label="execution_receipt_sha256",
            optional=True,
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_PROMOTED_EVENT_EMISSION_RECEIPT_SCHEMA,
            "event_sha256": self.event_sha256,
            "primary_trace_sha256": self.primary_trace_sha256,
            "promotion_evidence_sha256": self.promotion_evidence_sha256,
            "execution_receipt_sha256": self.execution_receipt_sha256,
            "outcome_evaluation_sha256": self.outcome_evaluation_sha256,
        }


@dataclass(frozen=True, slots=True)
class SupervisorLatencyBudgetDocument:
    """Closed operator-owned product budget; no prose is retained."""

    target_mode: SupervisorMode
    source_revision_sha256: str
    maximum_user_visible_latency_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.target_mode, SupervisorMode) or self.target_mode not in {
            SupervisorMode.ASSIST,
            SupervisorMode.CANARY,
        }:
            raise PromotedProductEventError("latency budget target mode is not promoted")
        _digest(self.source_revision_sha256, label="latency budget source revision")
        if (
            type(self.maximum_user_visible_latency_ms) is not int
            or not 1 <= self.maximum_user_visible_latency_ms <= _MAX_LATENCY_MS
        ):
            raise PromotedProductEventError("latency budget maximum is invalid")

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_LATENCY_BUDGET_DOCUMENT_SCHEMA,
            "budget_id": SUPERVISOR_LATENCY_BUDGET_ID,
            "task_class": TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB.value,
            "target_mode": self.target_mode.value,
            "source_revision_sha256": self.source_revision_sha256,
            "latency_measurement": SUPERVISOR_LATENCY_MEASUREMENT,
            "maximum_user_visible_latency_ms": self.maximum_user_visible_latency_ms,
        }

    @classmethod
    def parse(cls, value: Mapping[str, object]) -> SupervisorLatencyBudgetDocument:
        expected = {
            "schema",
            "budget_id",
            "task_class",
            "target_mode",
            "source_revision_sha256",
            "latency_measurement",
            "maximum_user_visible_latency_ms",
        }
        if type(value) is not dict or set(value) != expected:
            raise PromotedProductEventError("latency budget keys do not match")
        if (
            value.get("schema") != SUPERVISOR_LATENCY_BUDGET_DOCUMENT_SCHEMA
            or value.get("budget_id") != SUPERVISOR_LATENCY_BUDGET_ID
            or value.get("task_class") != TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB.value
            or value.get("latency_measurement") != SUPERVISOR_LATENCY_MEASUREMENT
        ):
            raise PromotedProductEventError("latency budget identity is invalid")
        try:
            return cls(
                target_mode=SupervisorMode(value["target_mode"]),
                source_revision_sha256=value["source_revision_sha256"],  # type: ignore[arg-type]
                maximum_user_visible_latency_ms=value["maximum_user_visible_latency_ms"],  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            raise PromotedProductEventError("latency budget document is malformed") from exc


@dataclass(frozen=True, slots=True)
class AcceptedSupervisorLatencyBudget:
    """Exact-hash accepted budget; selection of that hash remains operator-owned."""

    document: SupervisorLatencyBudgetDocument
    document_sha256: str

    def __post_init__(self) -> None:
        if type(self.document) is not SupervisorLatencyBudgetDocument:
            raise PromotedProductEventError("accepted latency budget document is not exact")
        _digest(self.document_sha256, label="latency budget document digest")

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_ACCEPTED_LATENCY_BUDGET_SCHEMA,
            "document_sha256": self.document_sha256,
            "document": self.document.payload(),
        }


def load_accepted_supervisor_latency_budget(
    raw: bytes,
    *,
    expected_document_sha256: str,
) -> AcceptedSupervisorLatencyBudget:
    """Load one exact operator-selected document and retain no raw bytes."""

    if type(raw) is not bytes or type(expected_document_sha256) is not str:
        raise TypeError("latency budget loader requires bytes and an expected digest")
    _digest(expected_document_sha256, label="expected latency budget document digest")
    if not 0 < len(raw) <= _MAX_BUDGET_DOCUMENT_BYTES or not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(),
        expected_document_sha256,
    ):
        raise PromotedProductEventError("latency budget document digest does not match")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PromotedProductEventError("latency budget document is not UTF-8") from exc
    document = SupervisorLatencyBudgetDocument.parse(
        _strict_json_object(
            text,
            maximum_bytes=_MAX_BUDGET_DOCUMENT_BYTES,
            label="latency budget document",
        )
    )
    return AcceptedSupervisorLatencyBudget(
        document=document,
        document_sha256=expected_document_sha256,
    )


def _promotion_identity(decision: AssistPromotionDecision) -> tuple[SupervisorMode, str]:
    if type(decision) is not AssistPromotionDecision:
        raise PromotedProductEventError("emission requires the exact promotion decision")
    evidence_sha256 = decision.evidence_sha256
    if (
        not decision.promotion_admitted
        or decision.reason is not AssistPromotionReason.ADMITTED
        or decision.readiness is not AssistPromotionReadiness.LIVE_EVIDENCE_READY
        or decision.admitted_mode not in {SupervisorMode.ASSIST, SupervisorMode.CANARY}
        or decision.requested_mode is not decision.admitted_mode
        or not decision.source_ready
        or not decision.live_evidence_ready
        or not decision.operator_gate_bound
        or evidence_sha256 is None
        or decision.execution_authorized
        or decision.publication_authorized
        or decision.storage_write_authorized
    ):
        raise PromotedProductEventError("promotion decision does not prove admitted evidence")
    _digest(evidence_sha256, label="promotion evidence digest")
    return decision.admitted_mode, evidence_sha256


def _trace_from_metadata(metadata: str) -> TurnTrace:
    item = _strict_json_object(
        metadata,
        maximum_bytes=_MAX_METADATA_BYTES,
        label="assistant metadata",
    )
    raw_trace = item.get(INTERACTION_TRACE_METADATA_KEY)
    if not isinstance(raw_trace, Mapping):
        raise PromotedProductEventError("assistant metadata has no committed TurnTrace")
    try:
        return TurnTrace.parse(raw_trace)
    except Exception as exc:
        raise PromotedProductEventError("assistant TurnTrace is malformed") from exc


def _verify_trace_linkage(
    trace: TurnTrace,
    *,
    namespace_key: bytes,
    assistant_message_id: str,
    conversation_id: str,
    graph_id: str | None,
) -> None:
    expected_turn = derive_trace_identifier(
        domain=TraceIdentifierDomain.TURN,
        raw_identifier=assistant_message_id,
        namespace_key=namespace_key,
    )
    expected_conversation = derive_trace_identifier(
        domain=TraceIdentifierDomain.CONVERSATION,
        raw_identifier=conversation_id,
        namespace_key=namespace_key,
    )
    expected_work = (
        derive_trace_identifier(
            domain=TraceIdentifierDomain.WORK_ITEM,
            raw_identifier=graph_id,
            namespace_key=namespace_key,
        )
        if graph_id is not None
        else None
    )
    if (
        not hmac.compare_digest(trace.turn_digest, expected_turn)
        or not hmac.compare_digest(trace.conversation_digest, expected_conversation)
        or (
            graph_id is not None
            and (
                trace.work_item_digest is None
                or expected_work is None
                or not hmac.compare_digest(trace.work_item_digest, expected_work)
                or trace.work_relation not in {WorkRelation.NEW, WorkRelation.CONTINUED}
            )
        )
    ):
        raise PromotedProductEventError("TurnTrace durable identities do not match publication")
    if trace.publication is not PublicationStatus.ASSISTANT_COMMITTED:
        raise PromotedProductEventError("TurnTrace does not prove committed assistant publication")


def _graph_for_receipt(
    conn: sqlite3.Connection,
    receipt_sha256: str,
) -> CompareCurrentFileWebWorkGraph:
    cursor = conn.execute(
        """SELECT id,user_id,conversation_id
             FROM work_item_compare_current_file_web_graphs
            WHERE publication_receipt_sha256=?
               OR terminal_publication_receipt_sha256=?
            LIMIT 2""",
        (receipt_sha256, receipt_sha256),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise PromotedProductEventError("execution receipt is not one exact durable WorkGraph")
    row = rows[0]
    if isinstance(row, sqlite3.Row):
        graph_id, user_id, conversation_id = (
            str(row["id"]),
            str(row["user_id"]),
            str(row["conversation_id"]),
        )
    else:
        graph_id, user_id, conversation_id = map(str, row)
    graph = get_compare_current_file_web_work_graph_in_transaction(
        conn,
        graph_id=graph_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    if graph is None or graph.state not in {
        CompareCurrentFileWebGraphState.TERMINAL,
        CompareCurrentFileWebGraphState.COMPLETED,
    }:
        raise PromotedProductEventError("execution receipt WorkGraph is not closed")
    stored_digest = (
        graph.publication_receipt_sha256
        if graph.state is CompareCurrentFileWebGraphState.COMPLETED
        else graph.terminal_publication_receipt_sha256
    )
    if stored_digest is None or not hmac.compare_digest(stored_digest, receipt_sha256):
        raise PromotedProductEventError("execution receipt does not match closed WorkGraph")
    return graph


def _journey_projection(
    conn: sqlite3.Connection,
    request: PromotedProductEmissionRequest,
    *,
    mode: SupervisorMode,
    namespace_key: bytes,
) -> tuple[TurnTrace, PromotedProductOutcomeInput]:
    assert request.execution_receipt_sha256 is not None
    graph = _graph_for_receipt(conn, request.execution_receipt_sha256)
    assistant_id = graph.publication_assistant_message_id
    if assistant_id is None:
        raise PromotedProductEventError("closed WorkGraph has no publication assistant")
    row = conn.execute(
        """SELECT metadata_json FROM messages
            WHERE id=? AND user_id=? AND conversation_id=?
              AND role='assistant' AND reply_to=?
            LIMIT 1""",
        (assistant_id, graph.user_id, graph.conversation_id, graph.anchor_user_message_id),
    ).fetchone()
    if row is None:
        raise PromotedProductEventError("WorkGraph publication assistant is not committed")
    metadata = str(row["metadata_json"] if isinstance(row, sqlite3.Row) else row[0])
    trace = _trace_from_metadata(metadata)
    _verify_trace_linkage(
        trace,
        namespace_key=namespace_key,
        assistant_message_id=assistant_id,
        conversation_id=graph.conversation_id,
        graph_id=graph.id,
    )
    if trace.playbook is not PlaybookClass.COMPARE_INTERNAL_AND_EXTERNAL_SOURCES:
        raise PromotedProductEventError("TurnTrace playbook does not match promoted journey")

    if graph.state is CompareCurrentFileWebGraphState.COMPLETED:
        receipt: CompareCurrentFileWebPublicationReceipt | CompareCurrentFileWebTerminalPublicationReceipt
        receipt = load_compare_current_file_web_publication_receipt(metadata)
        if (
            receipt.canonical_sha256() != request.execution_receipt_sha256
            or trace.completion is not CompletionDecision.COMPLETE
            or not trace.authority_rechecked
        ):
            raise PromotedProductEventError("full WorkGraph and TurnTrace completion disagree")
    else:
        receipt = load_compare_current_file_web_terminal_publication_receipt(metadata)
        if (
            receipt.canonical_sha256() != request.execution_receipt_sha256
            or trace.completion is CompletionDecision.COMPLETE
            or trace.authority_rechecked is not receipt.final_authority_rechecked
            or (
                receipt.status is CompareCurrentFileWebGraphOutcomeStatus.PARTIAL
                and trace.completion is not CompletionDecision.PARTIAL
            )
            or (
                receipt.status is CompareCurrentFileWebGraphOutcomeStatus.FAILED
                and trace.completion is not CompletionDecision.FAILED
            )
        ):
            raise PromotedProductEventError("terminal WorkGraph and TurnTrace completion disagree")

    trace_sha256 = canonical_sha256(trace.to_payload())
    if not hmac.compare_digest(trace_sha256, request.primary_trace_sha256):
        raise PromotedProductEventError("committed TurnTrace digest does not match request")
    return trace, PromotedProductOutcomeInput(
        mode=mode,
        eligibility=request.eligibility,
        primary_trace_sha256=trace_sha256,
        execution_receipt_sha256=request.execution_receipt_sha256,
        graph_state=graph.state,
        graph_outcome_status=graph.outcome_status,
        completion=trace.completion,
        publication=trace.publication,
        latency_ms=trace.budget.latency_ms,
    )


def _other_turn_projection(
    conn: sqlite3.Connection,
    request: PromotedProductEmissionRequest,
    *,
    mode: SupervisorMode,
    namespace_key: bytes,
) -> tuple[TurnTrace, PromotedProductOutcomeInput]:
    candidates: list[TurnTrace] = []
    rows = conn.execute(
        """SELECT id,conversation_id,metadata_json FROM messages
            WHERE role='assistant'
            ORDER BY rowid DESC LIMIT ?""",
        (_MAX_TRACE_SCAN,),
    )
    for row in rows:
        if isinstance(row, sqlite3.Row):
            assistant_id = str(row["id"])
            conversation_id = str(row["conversation_id"])
            metadata = str(row["metadata_json"])
        else:
            assistant_id, conversation_id, metadata = map(str, row)
        try:
            trace = _trace_from_metadata(metadata)
        except PromotedProductEventError:
            continue
        if not hmac.compare_digest(
            canonical_sha256(trace.to_payload()),
            request.primary_trace_sha256,
        ):
            continue
        _verify_trace_linkage(
            trace,
            namespace_key=namespace_key,
            assistant_message_id=assistant_id,
            conversation_id=conversation_id,
            graph_id=None,
        )
        candidates.append(trace)
    if len(candidates) != 1:
        raise PromotedProductEventError("other-turn trace is not one exact committed assistant")
    trace = candidates[0]
    return trace, PromotedProductOutcomeInput(
        mode=mode,
        eligibility=request.eligibility,
        primary_trace_sha256=request.primary_trace_sha256,
        execution_receipt_sha256=None,
        graph_state=None,
        graph_outcome_status=None,
        completion=trace.completion,
        publication=trace.publication,
        latency_ms=trace.budget.latency_ms,
    )


def _outcome_receipt(
    outcome_input: PromotedProductOutcomeInput,
    evaluator: PromotedUserVisibleOutcomeEvaluator | None,
) -> PromotedProductOutcomeReceipt:
    fallback = PromotedProductOutcomeReceipt.not_evaluated(outcome_input)
    if outcome_input.eligibility is PromotedObservationEligibility.OTHER_TURN or evaluator is None:
        return fallback
    try:
        evaluated = evaluator.evaluate(outcome_input)
    except Exception:
        return fallback
    if type(evaluated) is not PromotedProductOutcomeReceipt or not hmac.compare_digest(
        evaluated.input_sha256, outcome_input.canonical_sha256()
    ):
        return fallback
    return evaluated


def _check_replay_or_conflict(
    conn: sqlite3.Connection,
    event: PromotedSupervisorProductObservation,
) -> None:
    rows = conn.execute(
        "SELECT payload FROM runtime_events WHERE event_type=?",
        (SUPERVISOR_PROMOTED_PRODUCT_EVENT,),
    )
    for row in rows:
        raw = str(row["payload"] if isinstance(row, sqlite3.Row) else row[0])
        try:
            existing = PromotedSupervisorProductObservation.parse(
                _strict_json_object(
                    raw,
                    maximum_bytes=_MAX_EVENT_BYTES,
                    label="existing promoted product event",
                )
            )
        except (PromotedProductEventError, SupervisorBaselineError) as exc:
            raise PromotedProductEventConflictError(
                "existing promoted event journal is not safely replayable"
            ) from exc
        if hmac.compare_digest(existing.primary_trace_sha256, event.primary_trace_sha256):
            if existing == event:
                raise PromotedProductEventReplayError("promoted product event already exists")
            raise PromotedProductEventConflictError(
                "committed TurnTrace already has a conflicting product event"
            )
        if (
            event.execution_receipt_sha256 is not None
            and existing.execution_receipt_sha256 is not None
            and hmac.compare_digest(
                existing.execution_receipt_sha256,
                event.execution_receipt_sha256,
            )
        ):
            raise PromotedProductEventConflictError(
                "durable execution receipt already belongs to another product event"
            )


def emit_promoted_supervisor_product_event(
    conn: sqlite3.Connection,
    *,
    promotion_decision: AssistPromotionDecision,
    request: PromotedProductEmissionRequest,
    outcome_evaluator: PromotedUserVisibleOutcomeEvaluator | None = None,
) -> PromotedProductEventEmissionReceipt:
    """Prove committed state and atomically append one promoted product event.

    ``conn`` must not already own a transaction.  This forces publication to
    have committed before observation and gives replay checking one immediate
    SQLite writer boundary.
    """

    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("promoted event producer requires a sqlite3 connection")
    if type(request) is not PromotedProductEmissionRequest:
        raise PromotedProductEventError("emission request must use the exact closed contract")
    if conn.in_transaction:
        raise PromotedProductEventError("promoted event emission requires a post-commit boundary")
    mode, promotion_evidence_sha256 = _promotion_identity(promotion_decision)

    try:
        conn.execute("BEGIN IMMEDIATE")
        namespace_key = load_trace_namespace_key(conn)
        if request.eligibility is PromotedObservationEligibility.PROMOTED_JOURNEY:
            _trace, outcome_input = _journey_projection(
                conn,
                request,
                mode=mode,
                namespace_key=namespace_key,
            )
            task_class = TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB
        else:
            _trace, outcome_input = _other_turn_projection(
                conn,
                request,
                mode=mode,
                namespace_key=namespace_key,
            )
            task_class = None
        outcome_receipt = _outcome_receipt(outcome_input, outcome_evaluator)
        event = PromotedSupervisorProductObservation(
            mode=mode,
            task_class=task_class,
            eligibility=request.eligibility,
            primary_trace_sha256=request.primary_trace_sha256,
            promotion_evidence_sha256=promotion_evidence_sha256,
            execution_receipt_sha256=request.execution_receipt_sha256,
            supervisor_invoked=request.supervisor_invoked,
            user_visible_outcome=outcome_receipt.outcome,
        )
        _check_replay_or_conflict(conn, event)
        event_payload = event.payload()
        conn.execute(
            "INSERT INTO runtime_events(id,event_type,payload,created_at) VALUES(?,?,?,?)",
            (
                new_id("evt"),
                SUPERVISOR_PROMOTED_PRODUCT_EVENT,
                json.dumps(
                    event_payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                utc_now(),
            ),
        )
        conn.execute(
            """DELETE FROM runtime_events WHERE id IN (
                   SELECT id FROM runtime_events ORDER BY created_at DESC,rowid DESC
                   LIMIT -1 OFFSET ?
               )""",
            (RUNTIME_EVENT_CAP,),
        )
        emission = PromotedProductEventEmissionReceipt(
            event_sha256=canonical_sha256(event_payload),
            primary_trace_sha256=request.primary_trace_sha256,
            promotion_evidence_sha256=promotion_evidence_sha256,
            execution_receipt_sha256=request.execution_receipt_sha256,
            outcome_evaluation_sha256=outcome_receipt.canonical_sha256(),
        )
        conn.commit()
        return emission
    except BaseException as exc:
        if conn.in_transaction:
            conn.rollback()
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, (PromotedProductEventError, TypeError)):
            raise
        raise PromotedProductEventError("promoted product event emission failed closed") from exc


__all__ = [
    "AcceptedSupervisorLatencyBudget",
    "PromotedOutcomeEvaluatorAuthority",
    "PromotedProductEmissionRequest",
    "PromotedProductEventConflictError",
    "PromotedProductEventEmissionReceipt",
    "PromotedProductEventError",
    "PromotedProductEventReplayError",
    "PromotedProductOutcomeInput",
    "PromotedProductOutcomeReceipt",
    "PromotedUserVisibleOutcomeEvaluator",
    "SUPERVISOR_ACCEPTED_LATENCY_BUDGET_SCHEMA",
    "SUPERVISOR_LATENCY_BUDGET_DOCUMENT_SCHEMA",
    "SUPERVISOR_LATENCY_BUDGET_ID",
    "SUPERVISOR_LATENCY_MEASUREMENT",
    "SUPERVISOR_OUTCOME_EVALUATOR_ID",
    "SUPERVISOR_PROMOTED_EVENT_EMISSION_RECEIPT_SCHEMA",
    "SUPERVISOR_PROMOTED_OUTCOME_INPUT_SCHEMA",
    "SUPERVISOR_PROMOTED_OUTCOME_RECEIPT_SCHEMA",
    "SupervisorLatencyBudgetDocument",
    "emit_promoted_supervisor_product_event",
    "load_accepted_supervisor_latency_budget",
]
