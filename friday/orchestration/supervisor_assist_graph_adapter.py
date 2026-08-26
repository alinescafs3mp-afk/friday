"""Typed runtime adapter for the fixed current-file/current-web WorkGraph.

The controller never receives a SQLite connection and cannot inject graph or
assistant metadata.  This adapter owns every durable boundary: admission,
structural step CAS, publication, cancellation, and restart retirement.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from friday.execution_kernel import (
    confirm_staged_request_effect,
    rollback_staged_request_effect,
    stage_request_effect_possible_in_transaction,
)
from friday.interaction_control_plane.compare_current_file_web_work_graph import (
    COMPARE_CURRENT_FILE_WEB_CANCELLED_RESPONSE,
    COMPARE_CURRENT_FILE_WEB_RESTART_UNAVAILABLE_RESPONSE,
    FILE_READ_STEP_ID,
    PRIMARY_SYNTHESIS_STEP_ID,
    WEB_READ_STEP_ID,
    CompareCurrentFileWebGraphOutcomeReason,
    CompareCurrentFileWebGraphOutcomeStatus,
    CompareCurrentFileWebGraphState,
    CompareCurrentFileWebPublicationReceipt,
    CompareCurrentFileWebStepKind,
    CompareCurrentFileWebStepState,
    CompareCurrentFileWebTerminalPublicationReceipt,
    CompareCurrentFileWebWorkGraph,
    attach_compare_current_file_web_publication_receipt,
    attach_compare_current_file_web_terminal_publication_receipt,
    bind_validated_plan_to_compare_current_file_web_graph,
)
from friday.interaction_control_plane.compare_current_file_web_work_graph_store import (
    admit_compare_current_file_web_review_recovery_in_transaction,
    claim_compare_current_file_web_step_in_transaction,
    close_compare_current_file_web_work_graph_terminal_in_transaction,
    complete_compare_current_file_web_work_graph_in_transaction,
    create_compare_current_file_web_work_graph_in_transaction,
    get_compare_current_file_web_work_graph_in_transaction,
    get_current_compare_current_file_web_work_graph_in_transaction,
    settle_compare_current_file_web_step_in_transaction,
)
from friday.interaction_control_plane.runtime_trace import (
    attach_trace_to_metadata,
    build_work_trace,
    load_trace_namespace_key,
)
from friday.interaction_control_plane.turn_trace import (
    CapabilityClass,
    CompletionDecision,
    ContinuationKind,
    CountAccounting,
    FailureReason,
    FailureStage,
    IntentClass,
    OutcomeStatus,
    PlaybookClass,
    TurnTrace,
    WorkRelation,
)
from friday.orchestration.current_file_web_comparison import (
    CurrentFileWebComparison,
    CurrentFileWebComparisonStatus,
    current_file_web_comparison_is_process_owned,
    current_file_web_request_is_admitted,
)
from friday.orchestration.execution_plan import ValidatedExecutionPlan
from friday.orchestration.supervisor_assist_surface import (
    CurrentFileWebAssistSurface,
    bind_assist_plan_to_surface,
)
from friday.orchestration.supervisor_contracts import canonical_sha256
from friday.orchestration.supervisor_review_policy import AdmittedReadRecovery
from friday.orchestration.transient_web_comparison import (
    TransientWebComparisonEvidence,
    TransientWebEvidenceStatus,
    TransientWebPublicCitation,
)
from friday.permissions import ActorContext
from friday.source_identity import (
    AuthorizedFileSnapshotToken,
    authorized_file_snapshot_token_is_process_owned,
    raw_source_identity_sha256,
)
from friday.storage._base import normalize_conversation_mode
from friday.storage._conversations import store_message_in_transaction
from friday.storage._core import read_only_storage_snapshot

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_GRAPH_ID_RE = re.compile(r"graph_[0-9a-f]{16}\Z")
_USER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}\Z")
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}\Z")
_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}\Z")
_MAX_RECONCILE = 100

_STEP_ID = {
    CompareCurrentFileWebStepKind.FILE_READ: FILE_READ_STEP_ID,
    CompareCurrentFileWebStepKind.WEB_READ: WEB_READ_STEP_ID,
    CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS: PRIMARY_SYNTHESIS_STEP_ID,
}
_CAPABILITY_TRACE = {
    CompareCurrentFileWebStepKind.FILE_READ: CapabilityClass.DOCUMENT_RETRIEVAL,
    CompareCurrentFileWebStepKind.WEB_READ: CapabilityClass.WEB_RESEARCH,
    CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS: CapabilityClass.MODEL_SYNTHESIS,
}
_OUTCOME_TRACE = {
    CompareCurrentFileWebStepState.PENDING: OutcomeStatus.NOT_STARTED,
    CompareCurrentFileWebStepState.RUNNING: OutcomeStatus.RUNNING,
    CompareCurrentFileWebStepState.COMPLETE: OutcomeStatus.SUCCEEDED,
    CompareCurrentFileWebStepState.PARTIAL: OutcomeStatus.PARTIAL,
    CompareCurrentFileWebStepState.EMPTY: OutcomeStatus.EMPTY,
    CompareCurrentFileWebStepState.UNAVAILABLE: OutcomeStatus.UNAVAILABLE,
    CompareCurrentFileWebStepState.DENIED: OutcomeStatus.DENIED,
    CompareCurrentFileWebStepState.FAILED: OutcomeStatus.FAILED,
}
_TERMINAL_CONTENT = {
    CompareCurrentFileWebGraphOutcomeReason.NO_COMPARABLE_EVIDENCE: (
        "Сравнение не выполнено: сопоставимых данных в текущем файле и веб-источниках не найдено."
    ),
    CompareCurrentFileWebGraphOutcomeReason.CAPABILITY_UNAVAILABLE: (
        "Сравнение не выполнено: один из необходимых источников сейчас недоступен."
    ),
    CompareCurrentFileWebGraphOutcomeReason.AUTHORITY_DENIED: (
        "Сравнение не выполнено: доступ к одному из необходимых источников запрещён."
    ),
    CompareCurrentFileWebGraphOutcomeReason.STEP_FAILED: (
        "Сравнение не выполнено: один из этапов завершился ошибкой."
    ),
}


class SupervisorAssistGraphAdapterError(RuntimeError):
    """The closed adapter boundary could not be proved or committed."""


class _TransactionStorage(Protocol):
    def transaction(self) -> AbstractContextManager[Any]: ...


_BoundaryT = TypeVar("_BoundaryT", contravariant=True)
_ResultT = TypeVar("_ResultT")


class AssistBoundaryCheck(Protocol[_BoundaryT]):
    """A synchronous check called inside the adapter-owned transaction."""

    def __call__(self, boundary: _BoundaryT, /) -> bool: ...


@dataclass(frozen=True, slots=True)
class AssistConversationScope:
    user_id: str
    conversation_id: str

    def __post_init__(self) -> None:
        if _USER_ID_RE.fullmatch(self.user_id) is None or _CONVERSATION_ID_RE.fullmatch(
            self.conversation_id
        ) is None:
            raise ValueError("assist conversation scope is invalid")


@dataclass(frozen=True, slots=True)
class AssistGraphCursor:
    graph_id: str
    user_id: str
    conversation_id: str
    revision: int

    def __post_init__(self) -> None:
        AssistConversationScope(self.user_id, self.conversation_id)
        if _GRAPH_ID_RE.fullmatch(self.graph_id) is None or type(self.revision) is not int or self.revision < 1:
            raise ValueError("assist graph cursor is invalid")

    @classmethod
    def from_graph(cls, graph: CompareCurrentFileWebWorkGraph) -> AssistGraphCursor:
        if type(graph) is not CompareCurrentFileWebWorkGraph:
            raise TypeError("assist cursor requires the exact WorkGraph")
        return cls(graph.id, graph.user_id, graph.conversation_id, graph.revision)


@dataclass(frozen=True, slots=True, repr=False)
class AssistGraphAdmission:
    surface: CurrentFileWebAssistSurface = field(repr=False)
    plan: ValidatedExecutionPlan = field(repr=False)
    runtime_profile_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.surface) is not CurrentFileWebAssistSurface
            or type(self.plan) is not ValidatedExecutionPlan
            or _DIGEST_RE.fullmatch(self.runtime_profile_sha256) is None
            or self.surface.actor.user_id != self.surface.actor.own_id
            or not current_file_web_request_is_admitted(self.surface.turn.message)
            or bind_assist_plan_to_surface(self.plan, self.surface) is None
        ):
            raise ValueError("assist graph admission is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class AssistAdmissionBoundary:
    actor: ActorContext = field(repr=False, compare=False)
    graph_id: str
    user_id: str
    conversation_id: str
    request_binding_sha256: str
    accepted_plan_sha256: str
    adapter_registry_sha256: str
    actor_binding_sha256: str
    conversation_binding_sha256: str
    current_file_raw_object_id: str
    current_file_source_identity_sha256: str
    current_file_content_sha256: str
    web_plan_sha256: str
    web_query_sha256: str
    runtime_profile_sha256: str


@dataclass(frozen=True, slots=True, repr=False)
class AssistCapabilityBoundary:
    actor: ActorContext = field(repr=False, compare=False)
    graph_id: str
    user_id: str
    conversation_id: str
    revision: int
    step_kind: CompareCurrentFileWebStepKind
    step_id: str
    capability_id: str
    security_id: str | None
    adapter_id: str | None
    attempt: int
    input_identity_sha256: str
    accepted_plan_sha256: str
    adapter_registry_sha256: str
    current_file_raw_object_id: str
    current_file_source_identity_sha256: str
    current_file_content_sha256: str


class AssistPublicationAction(StrEnum):
    COMPARISON = "comparison"
    TERMINAL = "terminal"
    CANCEL = "cancel"
    RESTART_RETIREMENT = "restart_retirement"


@dataclass(frozen=True, slots=True, repr=False)
class AssistPublicationBoundary:
    actor: ActorContext | None = field(repr=False, compare=False)
    action: AssistPublicationAction
    graph_id: str
    user_id: str
    conversation_id: str
    revision: int
    accepted_plan_sha256: str
    adapter_registry_sha256: str
    current_file_raw_object_id: str
    current_file_source_identity_sha256: str
    current_file_content_sha256: str
    expected_status: CompareCurrentFileWebGraphOutcomeStatus
    expected_reason: CompareCurrentFileWebGraphOutcomeReason
    comparison_binding_sha256: str | None = None
    source_evidence_sha256: str | None = None
    model_evidence_sha256: str | None = None
    citation_labels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AssistTraceInput:
    latency_ms: int
    model_calls: int
    model_call_accounting: CountAccounting
    capability_calls: int
    capability_call_accounting: CountAccounting
    state_restored: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.latency_ms) is not int
            or not 0 <= self.latency_ms <= 86_400_000
            or type(self.model_calls) is not int
            or self.model_calls < 0
            or type(self.capability_calls) is not int
            or self.capability_calls < 0
            or type(self.model_call_accounting) is not CountAccounting
            or type(self.capability_call_accounting) is not CountAccounting
            or type(self.state_restored) is not bool
        ):
            raise ValueError("assist trace input is invalid")


@dataclass(frozen=True, slots=True)
class AssistStepSettlement:
    kind: CompareCurrentFileWebStepKind
    state: CompareCurrentFileWebStepState
    outcome_sha256: str
    evidence_identity_sha256: str | None
    authority_rechecked: bool
    verified: bool

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not CompareCurrentFileWebStepKind
            or type(self.state) is not CompareCurrentFileWebStepState
            or self.state in {CompareCurrentFileWebStepState.PENDING, CompareCurrentFileWebStepState.RUNNING}
            or _DIGEST_RE.fullmatch(self.outcome_sha256) is None
            or (
                self.evidence_identity_sha256 is not None
                and _DIGEST_RE.fullmatch(self.evidence_identity_sha256) is None
            )
            or type(self.authority_rechecked) is not bool
            or type(self.verified) is not bool
        ):
            raise ValueError("assist step settlement is invalid")

    @classmethod
    def accepted(
        cls,
        kind: CompareCurrentFileWebStepKind,
        state: CompareCurrentFileWebStepState,
        *,
        outcome_sha256: str,
        evidence_identity_sha256: str,
    ) -> AssistStepSettlement:
        if state not in {
            CompareCurrentFileWebStepState.COMPLETE,
            CompareCurrentFileWebStepState.PARTIAL,
            CompareCurrentFileWebStepState.EMPTY,
        }:
            raise ValueError("accepted settlement state is invalid")
        is_read = kind is not CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS
        return cls(kind, state, outcome_sha256, evidence_identity_sha256, is_read, True)

    @classmethod
    def denied(
        cls,
        kind: CompareCurrentFileWebStepKind,
        *,
        outcome_sha256: str,
    ) -> AssistStepSettlement:
        if kind is CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS:
            raise ValueError("primary synthesis cannot settle denied")
        return cls(kind, CompareCurrentFileWebStepState.DENIED, outcome_sha256, None, True, False)

    @classmethod
    def failed(
        cls,
        kind: CompareCurrentFileWebStepKind,
        state: CompareCurrentFileWebStepState,
        *,
        outcome_sha256: str,
    ) -> AssistStepSettlement:
        if state not in {CompareCurrentFileWebStepState.UNAVAILABLE, CompareCurrentFileWebStepState.FAILED}:
            raise ValueError("failed settlement state is invalid")
        return cls(kind, state, outcome_sha256, None, False, False)


@dataclass(frozen=True, slots=True, repr=False)
class AssistComparisonPublication:
    current_file_snapshot: AuthorizedFileSnapshotToken = field(repr=False)
    comparison: CurrentFileWebComparison = field(repr=False)
    web_evidence: TransientWebComparisonEvidence = field(repr=False)
    trace: AssistTraceInput

    def __post_init__(self) -> None:
        if (
            not authorized_file_snapshot_token_is_process_owned(self.current_file_snapshot)
            or not current_file_web_comparison_is_process_owned(self.comparison)
            or type(self.web_evidence) is not TransientWebComparisonEvidence
            or type(self.trace) is not AssistTraceInput
        ):
            raise ValueError("assist comparison publication is invalid")
        try:
            self.web_evidence.__post_init__()
        except Exception as exc:
            raise ValueError("assist web evidence is invalid") from exc


@dataclass(frozen=True, slots=True)
class AssistTerminalPublication:
    expected_status: CompareCurrentFileWebGraphOutcomeStatus
    expected_reason: CompareCurrentFileWebGraphOutcomeReason
    trace: AssistTraceInput
    synthesis_settlement: AssistStepSettlement | None = None

    def __post_init__(self) -> None:
        if (
            type(self.expected_status) is not CompareCurrentFileWebGraphOutcomeStatus
            or self.expected_status is CompareCurrentFileWebGraphOutcomeStatus.COMPLETE
            or type(self.expected_reason) is not CompareCurrentFileWebGraphOutcomeReason
            or self.expected_reason is CompareCurrentFileWebGraphOutcomeReason.NONE
            or type(self.trace) is not AssistTraceInput
            or (
                self.synthesis_settlement is not None
                and (
                    type(self.synthesis_settlement) is not AssistStepSettlement
                    or self.synthesis_settlement.kind is not CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS
                    or self.synthesis_settlement.state
                    not in {CompareCurrentFileWebStepState.UNAVAILABLE, CompareCurrentFileWebStepState.FAILED}
                )
            )
        ):
            raise ValueError("assist terminal publication is invalid")


@dataclass(frozen=True, slots=True)
class AssistMixedAuthorityTerminalPublication:
    """Code-owned closure request for one usable read plus one denied read."""

    trace: AssistTraceInput

    def __post_init__(self) -> None:
        if type(self.trace) is not AssistTraceInput:
            raise ValueError("mixed-authority terminal publication is invalid")


@dataclass(frozen=True, slots=True)
class AssistCancellation:
    user_message: str
    trace: AssistTraceInput
    request_binding_sha256: str

    def __post_init__(self) -> None:
        if (
            self.user_message not in {"отмена", "cancel"}
            or type(self.trace) is not AssistTraceInput
            or _DIGEST_RE.fullmatch(self.request_binding_sha256) is None
        ):
            raise ValueError("assist cancellation is invalid")


@dataclass(frozen=True, slots=True)
class AssistClaimedStep:
    graph: CompareCurrentFileWebWorkGraph
    boundary: AssistCapabilityBoundary = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class AssistGraphPublication:
    graph: CompareCurrentFileWebWorkGraph
    assistant_message_id: str
    content: str = field(repr=False)
    public_citations: tuple[TransientWebPublicCitation, ...]
    primary_trace_sha256: str
    execution_receipt_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.graph) is not CompareCurrentFileWebWorkGraph
            or self.graph.state is CompareCurrentFileWebGraphState.ACTIVE
            or self.graph.publication_assistant_message_id != self.assistant_message_id
            or _MESSAGE_ID_RE.fullmatch(self.assistant_message_id) is None
            or type(self.content) is not str
            or not self.content.strip()
            or type(self.public_citations) is not tuple
            or any(type(item) is not TransientWebPublicCitation for item in self.public_citations)
            or _DIGEST_RE.fullmatch(self.primary_trace_sha256) is None
            or _DIGEST_RE.fullmatch(self.execution_receipt_sha256) is None
        ):
            raise ValueError("assist graph publication is invalid")


class AssistRestartDisposition(StrEnum):
    RETIRED_EVIDENCE_NOT_REPLAYABLE = "retired_evidence_not_replayable"


@dataclass(frozen=True, slots=True)
class AssistRestartResult:
    disposition: AssistRestartDisposition
    publication: AssistGraphPublication


@dataclass(frozen=True, slots=True)
class AssistRestartBatch:
    results: tuple[AssistRestartResult, ...]
    has_more: bool


def _require(check: Callable[[Any], bool], boundary: object, *, label: str) -> None:
    try:
        accepted = check(boundary)
    except Exception as exc:
        raise SupervisorAssistGraphAdapterError(f"{label} check failed") from exc
    if accepted is not True:
        raise SupervisorAssistGraphAdapterError(f"{label} check denied")


def _source_projection(row: Any) -> dict[str, Any]:
    return dict(row) if not isinstance(row, tuple) else {}


def _policy_sha256(plan: ValidatedExecutionPlan) -> str:
    return canonical_sha256(
        {"schema": "friday.semantic-supervisor-policy-pin.v1", "policy_version": plan.policy_version}
    )


def _step_inputs(
    admission: AssistGraphAdmission,
) -> tuple[dict[CompareCurrentFileWebStepKind, str], dict[CompareCurrentFileWebStepKind, str]]:
    plan_sha256 = admission.plan.canonical_sha256()
    surface = admission.surface
    bindings = bind_validated_plan_to_compare_current_file_web_graph(admission.plan)
    inputs: dict[CompareCurrentFileWebStepKind, str] = {}
    keys: dict[CompareCurrentFileWebStepKind, str] = {}
    for binding in bindings:
        kind = binding.graph_kind
        material: dict[str, object] = {
            "schema": "friday.semantic-supervisor-assist-step-input.v1",
            "accepted_plan_sha256": plan_sha256,
            "request_binding_sha256": surface.ingress_binding.canonical_sha256(),
            "kind": kind.value,
        }
        if kind is CompareCurrentFileWebStepKind.FILE_READ:
            material.update(
                raw_object_id=surface.attachment.raw_object_id,
                source_identity_sha256=surface.attachment.source_identity_sha256,
                content_sha256=surface.attachment_content_sha256,
            )
        elif kind is CompareCurrentFileWebStepKind.WEB_READ:
            material.update(
                web_plan_sha256=surface.web_plan.canonical_sha256(),
                web_query_sha256=surface.web_plan.query_sha256,
            )
        else:
            material["read_inputs_sha256"] = canonical_sha256(
                [inputs[CompareCurrentFileWebStepKind.FILE_READ], inputs[CompareCurrentFileWebStepKind.WEB_READ]]
            )
        inputs[kind] = canonical_sha256(material)
        keys[kind] = binding.plan_step.idempotency_key
    return inputs, keys


class SupervisorAssistGraphAdapter:
    """Framework-agnostic owner of one closed supervisor assist graph."""

    def __init__(self, storage: _TransactionStorage) -> None:
        if not callable(getattr(storage, "transaction", None)):
            raise TypeError("assist graph adapter requires transactional storage")
        self._storage = storage
        self._actors: dict[str, ActorContext] = {}

    def _write(self, operation: Callable[[Any], _ResultT]) -> _ResultT:
        try:
            with self._storage.transaction() as conn:
                result = operation(conn)
        except BaseException:
            rollback_staged_request_effect()
            raise
        confirm_staged_request_effect()
        return result

    @staticmethod
    def _stage(
        conn: Any,
        *,
        expected_request_binding_sha256: str | None = None,
    ) -> None:
        if not stage_request_effect_possible_in_transaction(
            conn,
            expected_request_binding_sha256=expected_request_binding_sha256,
        ):
            raise SupervisorAssistGraphAdapterError("request effect fence was not committed")

    @staticmethod
    def _live_dialogue(conn: Any, scope: AssistConversationScope) -> None:
        row = conn.execute(
            "SELECT mode,is_archived FROM conversations WHERE id=? AND user_id=?",
            (scope.conversation_id, scope.user_id),
        ).fetchone()
        if row is None or int(row["is_archived"]) != 0 or normalize_conversation_mode(row["mode"]) != "dialogue":
            raise SupervisorAssistGraphAdapterError("assist conversation is not a live dialogue")

    @staticmethod
    def _raw_pin(
        conn: Any,
        *,
        user_id: str,
        raw_id: str,
        source_sha256: str,
        content_sha256: str,
    ) -> None:
        row = conn.execute(
            """SELECT id,source,source_ref,content_type,received_at,content_hash,
                      raw_content AS _raw_content,metadata_json AS _raw_metadata
                 FROM raw_objects
                WHERE id=? AND user_id=? AND deleted_at IS NULL""",
            (raw_id, user_id),
        ).fetchone()
        if row is None:
            raise SupervisorAssistGraphAdapterError("assist current file is unavailable")
        projection = _source_projection(row)
        if (
            raw_source_identity_sha256(projection) != source_sha256
            or str(row["content_hash"] or "") != content_sha256
        ):
            raise SupervisorAssistGraphAdapterError("assist current-file pin changed")

    def admit(
        self,
        admission: AssistGraphAdmission,
        *,
        authority_check: AssistBoundaryCheck[AssistAdmissionBoundary],
        effect_check: AssistBoundaryCheck[AssistAdmissionBoundary],
    ) -> CompareCurrentFileWebWorkGraph:
        if type(admission) is not AssistGraphAdmission:
            raise TypeError("assist admission requires the exact input")
        admission.__post_init__()
        surface, plan = admission.surface, admission.plan
        graph_id = f"graph_{secrets.token_hex(8)}"
        boundary = AssistAdmissionBoundary(
            actor=surface.actor,
            graph_id=graph_id,
            user_id=surface.actor.user_id,
            conversation_id=surface.conversation_id,
            request_binding_sha256=surface.ingress_binding.canonical_sha256(),
            accepted_plan_sha256=plan.canonical_sha256(),
            adapter_registry_sha256=plan.binding_snapshot_sha256,
            actor_binding_sha256=plan.actor_binding_sha256,
            conversation_binding_sha256=plan.conversation_binding_sha256,
            current_file_raw_object_id=surface.attachment.raw_object_id,
            current_file_source_identity_sha256=surface.attachment.source_identity_sha256,
            current_file_content_sha256=surface.attachment_content_sha256,
            web_plan_sha256=surface.web_plan.canonical_sha256(),
            web_query_sha256=surface.web_plan.query_sha256,
            runtime_profile_sha256=admission.runtime_profile_sha256,
        )

        def operation(conn: Any) -> CompareCurrentFileWebWorkGraph:
            scope = AssistConversationScope(boundary.user_id, boundary.conversation_id)
            self._live_dialogue(conn, scope)
            self._raw_pin(
                conn,
                user_id=boundary.user_id,
                raw_id=boundary.current_file_raw_object_id,
                source_sha256=boundary.current_file_source_identity_sha256,
                content_sha256=boundary.current_file_content_sha256,
            )
            _require(authority_check, boundary, label="admission authority")
            _require(effect_check, boundary, label="admission effect")
            self._stage(
                conn,
                expected_request_binding_sha256=boundary.request_binding_sha256,
            )
            user = store_message_in_transaction(
                conn,
                boundary.conversation_id,
                boundary.user_id,
                "user",
                surface.turn.message,
                {
                    "answer_mode": "semantic_supervisor_assist_request",
                    "accepted_plan_sha256": boundary.accepted_plan_sha256,
                    "interaction_mode": "dialogue",
                    "private_context_lineage": True,
                },
            )
            anchor = str(user.get("id") or "")
            if _MESSAGE_ID_RE.fullmatch(anchor) is None:
                raise SupervisorAssistGraphAdapterError("assist user anchor was not stored")
            inputs, keys = _step_inputs(admission)
            graph = CompareCurrentFileWebWorkGraph.admitted(
                graph_id=boundary.graph_id,
                user_id=boundary.user_id,
                conversation_id=boundary.conversation_id,
                anchor_user_message_id=anchor,
                anchor_request_binding_sha256=boundary.request_binding_sha256,
                current_file_raw_object_id=boundary.current_file_raw_object_id,
                proposal_sha256=plan.proposal_digest,
                accepted_plan_sha256=boundary.accepted_plan_sha256,
                manifest_sha256=plan.manifest_digest,
                policy_sha256=_policy_sha256(plan),
                runtime_profile_sha256=boundary.runtime_profile_sha256,
                adapter_registry_sha256=boundary.adapter_registry_sha256,
                actor_binding_sha256=boundary.actor_binding_sha256,
                conversation_binding_sha256=boundary.conversation_binding_sha256,
                current_file_source_identity_sha256=boundary.current_file_source_identity_sha256,
                current_file_content_sha256=boundary.current_file_content_sha256,
                step_input_identities=inputs,
                step_idempotency_keys=keys,
            )
            return create_compare_current_file_web_work_graph_in_transaction(conn, graph)

        stored = self._write(operation)
        self._actors[stored.id] = surface.actor
        return stored

    def load(self, cursor: AssistGraphCursor) -> CompareCurrentFileWebWorkGraph | None:
        if type(cursor) is not AssistGraphCursor:
            raise TypeError("assist load requires a cursor")
        with read_only_storage_snapshot(self._storage) as conn:
            return get_compare_current_file_web_work_graph_in_transaction(
                conn,
                graph_id=cursor.graph_id,
                user_id=cursor.user_id,
                conversation_id=cursor.conversation_id,
            )

    def load_current(self, scope: AssistConversationScope) -> CompareCurrentFileWebWorkGraph | None:
        if type(scope) is not AssistConversationScope:
            raise TypeError("assist current lookup requires a scope")
        with read_only_storage_snapshot(self._storage) as conn:
            return get_current_compare_current_file_web_work_graph_in_transaction(
                conn, user_id=scope.user_id, conversation_id=scope.conversation_id
            )

    def claim(
        self,
        cursor: AssistGraphCursor,
        kind: CompareCurrentFileWebStepKind,
        *,
        surface: CurrentFileWebAssistSurface,
        authority_check: AssistBoundaryCheck[AssistCapabilityBoundary],
        effect_check: AssistBoundaryCheck[AssistCapabilityBoundary],
    ) -> AssistClaimedStep:
        if type(cursor) is not AssistGraphCursor or type(kind) is not CompareCurrentFileWebStepKind:
            raise TypeError("assist claim requires typed cursor and step kind")
        if type(surface) is not CurrentFileWebAssistSurface or surface.actor.user_id != cursor.user_id:
            raise SupervisorAssistGraphAdapterError("assist claim surface changed")

        claimed_boundary: AssistCapabilityBoundary | None = None

        def mutate(conn: Any) -> CompareCurrentFileWebWorkGraph:
            nonlocal claimed_boundary
            graph = self._active(conn, cursor)
            pending = graph.step(_STEP_ID[kind])
            boundary = AssistCapabilityBoundary(
                actor=surface.actor,
                graph_id=graph.id,
                user_id=graph.user_id,
                conversation_id=graph.conversation_id,
                revision=graph.revision + 1,
                step_kind=kind,
                step_id=pending.step_id,
                capability_id=pending.capability_id,
                security_id=pending.security_id,
                adapter_id=pending.adapter_id,
                attempt=pending.attempt + 1,
                input_identity_sha256=pending.input_identity_sha256,
                accepted_plan_sha256=graph.accepted_plan_sha256,
                adapter_registry_sha256=graph.adapter_registry_sha256,
                current_file_raw_object_id=graph.current_file_raw_object_id,
                current_file_source_identity_sha256=graph.current_file_source_identity_sha256,
                current_file_content_sha256=graph.current_file_content_sha256,
            )
            self._live_dialogue(conn, AssistConversationScope(graph.user_id, graph.conversation_id))
            self._raw_pin(
                conn,
                user_id=graph.user_id,
                raw_id=graph.current_file_raw_object_id,
                source_sha256=graph.current_file_source_identity_sha256,
                content_sha256=graph.current_file_content_sha256,
            )
            _require(authority_check, boundary, label="capability authority")
            _require(effect_check, boundary, label="capability effect")
            self._stage(conn)
            claimed = claim_compare_current_file_web_step_in_transaction(
                conn,
                graph_id=cursor.graph_id,
                user_id=cursor.user_id,
                conversation_id=cursor.conversation_id,
                expected_revision=cursor.revision,
                step_id=_STEP_ID[kind],
            )
            claimed_boundary = boundary
            return claimed

        graph = self._write(mutate)
        self._actors[graph.id] = surface.actor
        if claimed_boundary is None:  # pragma: no cover - operation returned a graph
            raise SupervisorAssistGraphAdapterError("claimed boundary was not retained")
        return AssistClaimedStep(graph, claimed_boundary)

    def settle(
        self, cursor: AssistGraphCursor, settlement: AssistStepSettlement
    ) -> CompareCurrentFileWebWorkGraph:
        if type(cursor) is not AssistGraphCursor or type(settlement) is not AssistStepSettlement:
            raise TypeError("assist settlement requires typed inputs")

        def operation(conn: Any) -> CompareCurrentFileWebWorkGraph:
            self._stage(conn)
            return settle_compare_current_file_web_step_in_transaction(
                conn,
                graph_id=cursor.graph_id,
                user_id=cursor.user_id,
                conversation_id=cursor.conversation_id,
                expected_revision=cursor.revision,
                step_id=_STEP_ID[settlement.kind],
                state=settlement.state,
                outcome_sha256=settlement.outcome_sha256,
                evidence_identity_sha256=settlement.evidence_identity_sha256,
                authority_rechecked=settlement.authority_rechecked,
                verified=settlement.verified,
            )

        return self._write(operation)

    def admit_review_recovery(
        self, cursor: AssistGraphCursor, recovery: AdmittedReadRecovery
    ) -> CompareCurrentFileWebWorkGraph:
        if type(cursor) is not AssistGraphCursor or type(recovery) is not AdmittedReadRecovery:
            raise TypeError("assist review recovery requires typed inputs")

        def operation(conn: Any) -> CompareCurrentFileWebWorkGraph:
            self._stage(conn)
            return admit_compare_current_file_web_review_recovery_in_transaction(
                conn,
                graph_id=cursor.graph_id,
                user_id=cursor.user_id,
                conversation_id=cursor.conversation_id,
                expected_revision=cursor.revision,
                recovery=recovery,
            )

        return self._write(operation)

    @staticmethod
    def _trace(
        conn: Any,
        graph: CompareCurrentFileWebWorkGraph,
        trace_input: AssistTraceInput,
        *,
        completion: CompletionDecision,
        failure_stage: FailureStage,
        failure_reason: FailureReason,
        authority_rechecked: bool,
    ) -> tuple[TurnTrace, str]:
        outcomes = tuple(
            (
                _CAPABILITY_TRACE[step.kind],
                (
                    OutcomeStatus.UNCERTAIN
                    if step.state is CompareCurrentFileWebStepState.PENDING and step.attempt > 0
                    else _OUTCOME_TRACE[step.state]
                ),
            )
            for step in graph.steps
        )
        trace = build_work_trace(
            namespace_key=load_trace_namespace_key(conn),
            turn_identifier=graph.anchor_user_message_id,
            conversation_identifier=graph.conversation_id,
            work_item_identifier=graph.id,
            work_relation=WorkRelation.NEW,
            intent=IntentClass.MIXED,
            playbook=PlaybookClass.COMPARE_INTERNAL_AND_EXTERNAL_SOURCES,
            capability_outcomes=outcomes,
            capability_attempts=tuple(step.attempt for step in graph.steps),
            continuation=ContinuationKind.NONE,
            completion=completion,
            failure_stage=failure_stage,
            failure_reason=failure_reason,
            ambiguity_present=False,
            partial_coverage=completion is not CompletionDecision.COMPLETE,
            state_restored=trace_input.state_restored,
            latency_ms=trace_input.latency_ms,
            model_calls=trace_input.model_calls,
            model_call_accounting=trace_input.model_call_accounting,
            capability_calls=trace_input.capability_calls,
            capability_call_accounting=trace_input.capability_call_accounting,
            authority_rechecked=authority_rechecked,
        )
        return trace, canonical_sha256(trace.to_payload())

    @staticmethod
    def _publication_boundary(
        graph: CompareCurrentFileWebWorkGraph,
        *,
        actor: ActorContext | None,
        action: AssistPublicationAction,
        status: CompareCurrentFileWebGraphOutcomeStatus,
        reason: CompareCurrentFileWebGraphOutcomeReason,
        comparison: CurrentFileWebComparison | None = None,
    ) -> AssistPublicationBoundary:
        return AssistPublicationBoundary(
            actor=actor,
            action=action,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            revision=graph.revision,
            accepted_plan_sha256=graph.accepted_plan_sha256,
            adapter_registry_sha256=graph.adapter_registry_sha256,
            current_file_raw_object_id=graph.current_file_raw_object_id,
            current_file_source_identity_sha256=graph.current_file_source_identity_sha256,
            current_file_content_sha256=graph.current_file_content_sha256,
            expected_status=status,
            expected_reason=reason,
            comparison_binding_sha256=None if comparison is None else comparison.binding_sha256,
            source_evidence_sha256=None if comparison is None else comparison.source_evidence_sha256,
            model_evidence_sha256=None if comparison is None else comparison.model_evidence_sha256,
            citation_labels=() if comparison is None else comparison.citation_labels,
        )

    @staticmethod
    def _base_metadata(graph: CompareCurrentFileWebWorkGraph, *, verified: bool) -> dict[str, Any]:
        status = "passed" if verified else "unknown"
        return {
            "answer_mode": "semantic_supervisor_assist",
            "accepted_plan_sha256": graph.accepted_plan_sha256,
            "interaction_mode": "dialogue",
            "private_context_lineage": True,
            "tools_used": [],
            "verified": verified,
            "verification_status": status,
            "verification": {"status": status, "score": 1.0 if verified else None, "issues": []},
        }

    def _active(self, conn: Any, cursor: AssistGraphCursor) -> CompareCurrentFileWebWorkGraph:
        graph = get_compare_current_file_web_work_graph_in_transaction(
            conn,
            graph_id=cursor.graph_id,
            user_id=cursor.user_id,
            conversation_id=cursor.conversation_id,
        )
        if graph is None or graph.state is not CompareCurrentFileWebGraphState.ACTIVE or graph.revision != cursor.revision:
            raise SupervisorAssistGraphAdapterError("assist graph cursor is stale")
        return graph

    def publish_comparison(
        self,
        cursor: AssistGraphCursor,
        publication: AssistComparisonPublication,
        *,
        authority_check: AssistBoundaryCheck[AssistPublicationBoundary],
        effect_check: AssistBoundaryCheck[AssistPublicationBoundary],
    ) -> AssistGraphPublication:
        if type(cursor) is not AssistGraphCursor or type(publication) is not AssistComparisonPublication:
            raise TypeError("assist comparison publication requires typed inputs")

        def operation(conn: Any) -> AssistGraphPublication:
            graph = self._active(conn, cursor)
            comparison, web = publication.comparison, publication.web_evidence
            file_step, web_step, synthesis = graph.steps
            if (
                synthesis.state is not CompareCurrentFileWebStepState.RUNNING
                or comparison.accepted_plan_sha256 != graph.accepted_plan_sha256
                or comparison.file_evidence_sha256 != file_step.evidence_identity_sha256
                or comparison.web_evidence_sha256 != web.canonical_sha256()
                or comparison.citation_labels
                != ("F1", *(citation.label for citation in web.public_citations()))
            ):
                raise SupervisorAssistGraphAdapterError("comparison does not match the active graph")
            if web.status is TransientWebEvidenceStatus.SOURCED:
                if comparison.web_evidence_sha256 != web_step.evidence_identity_sha256:
                    raise SupervisorAssistGraphAdapterError("sourced web evidence identity changed")
            elif web.status is TransientWebEvidenceStatus.EMPTY:
                if web_step.state is not CompareCurrentFileWebStepState.EMPTY or (
                    web_step.evidence_identity_sha256 != comparison.web_evidence_sha256
                ):
                    raise SupervisorAssistGraphAdapterError("empty web evidence identity changed")
            elif web_step.state is not CompareCurrentFileWebStepState.UNAVAILABLE or (
                web_step.evidence_identity_sha256 is not None
            ):
                raise SupervisorAssistGraphAdapterError("unavailable web evidence state changed")
            source_sha256 = canonical_sha256(
                {
                    "file_evidence_sha256": comparison.file_evidence_sha256,
                    "schema": "friday.current-file-web-source-evidence-identity.v1",
                    "web_evidence_sha256": comparison.web_evidence_sha256,
                }
            )
            if source_sha256 != comparison.source_evidence_sha256:
                raise SupervisorAssistGraphAdapterError("comparison source binding changed")
            token = publication.current_file_snapshot
            if (
                token.source.raw_id != graph.current_file_raw_object_id
                or token.source.identity_sha256 != graph.current_file_source_identity_sha256
                or token.content_sha256 != graph.current_file_content_sha256
            ):
                raise SupervisorAssistGraphAdapterError("final current-file snapshot changed")
            self._live_dialogue(conn, AssistConversationScope(graph.user_id, graph.conversation_id))
            self._raw_pin(
                conn,
                user_id=graph.user_id,
                raw_id=graph.current_file_raw_object_id,
                source_sha256=graph.current_file_source_identity_sha256,
                content_sha256=graph.current_file_content_sha256,
            )
            status = (
                CompareCurrentFileWebGraphOutcomeStatus.COMPLETE
                if comparison.status is CurrentFileWebComparisonStatus.COMPLETE
                else CompareCurrentFileWebGraphOutcomeStatus.PARTIAL
            )
            reason = (
                CompareCurrentFileWebGraphOutcomeReason.NONE
                if status is CompareCurrentFileWebGraphOutcomeStatus.COMPLETE
                else CompareCurrentFileWebGraphOutcomeReason.PARTIAL_EVIDENCE
            )
            boundary = self._publication_boundary(
                graph,
                actor=self._actors.get(graph.id),
                action=AssistPublicationAction.COMPARISON,
                status=status,
                reason=reason,
                comparison=comparison,
            )
            _require(authority_check, boundary, label="publication authority")
            _require(effect_check, boundary, label="publication effect")
            self._stage(conn)
            settled = settle_compare_current_file_web_step_in_transaction(
                conn,
                graph_id=graph.id,
                user_id=graph.user_id,
                conversation_id=graph.conversation_id,
                expected_revision=graph.revision,
                step_id=PRIMARY_SYNTHESIS_STEP_ID,
                state=(
                    CompareCurrentFileWebStepState.COMPLETE
                    if comparison.status is CurrentFileWebComparisonStatus.COMPLETE
                    else CompareCurrentFileWebStepState.PARTIAL
                ),
                outcome_sha256=comparison.canonical_sha256(),
                evidence_identity_sha256=comparison.binding_sha256,
                authority_rechecked=False,
                verified=True,
            )
            trace, trace_sha256 = self._trace(
                conn,
                settled,
                publication.trace,
                completion=(
                    CompletionDecision.COMPLETE
                    if comparison.status is CurrentFileWebComparisonStatus.COMPLETE
                    else CompletionDecision.PARTIAL
                ),
                failure_stage=FailureStage.NONE,
                failure_reason=FailureReason.NONE,
                authority_rechecked=True,
            )
            metadata = self._base_metadata(settled, verified=True)
            metadata["citations"] = [
                {"label": "F1", "title": "Текущий файл"},
                *(citation.payload() for citation in web.public_citations()),
            ]
            metadata["comparison"] = comparison.identity_payload()
            if not attach_trace_to_metadata(metadata, trace):
                raise SupervisorAssistGraphAdapterError("assist trace does not fit assistant metadata")
            complete_receipt: CompareCurrentFileWebPublicationReceipt | None = None
            terminal_receipt: CompareCurrentFileWebTerminalPublicationReceipt | None = None
            if comparison.status is CurrentFileWebComparisonStatus.COMPLETE:
                complete_receipt = settled.publication_receipt(final_authority_rechecked=True)
                metadata = attach_compare_current_file_web_publication_receipt(metadata, complete_receipt)
            else:
                terminal_receipt = settled.terminal_publication_receipt(final_authority_rechecked=True)
                metadata = attach_compare_current_file_web_terminal_publication_receipt(
                    metadata, terminal_receipt
                )
            assistant = store_message_in_transaction(
                conn,
                settled.conversation_id,
                settled.user_id,
                "assistant",
                comparison.answer,
                metadata,
                settled.anchor_user_message_id,
            )
            message_id = str(assistant.get("id") or "")
            if comparison.status is CurrentFileWebComparisonStatus.COMPLETE:
                assert complete_receipt is not None
                closed = complete_compare_current_file_web_work_graph_in_transaction(
                    conn,
                    graph_id=settled.id,
                    user_id=settled.user_id,
                    conversation_id=settled.conversation_id,
                    expected_revision=settled.revision,
                    publication_assistant_message_id=message_id,
                    receipt=complete_receipt,
                )
                receipt_sha256 = complete_receipt.canonical_sha256()
            else:
                assert terminal_receipt is not None
                closed = close_compare_current_file_web_work_graph_terminal_in_transaction(
                    conn,
                    graph_id=settled.id,
                    user_id=settled.user_id,
                    conversation_id=settled.conversation_id,
                    expected_revision=settled.revision,
                    publication_assistant_message_id=message_id,
                    receipt=terminal_receipt,
                )
                receipt_sha256 = terminal_receipt.canonical_sha256()
            return AssistGraphPublication(
                graph=closed,
                assistant_message_id=message_id,
                content=comparison.answer,
                public_citations=web.public_citations(),
                primary_trace_sha256=trace_sha256,
                execution_receipt_sha256=receipt_sha256,
            )

        result = self._write(operation)
        self._actors.pop(result.graph.id, None)
        return result

    def publish_terminal(
        self,
        cursor: AssistGraphCursor,
        publication: AssistTerminalPublication,
        *,
        authority_check: AssistBoundaryCheck[AssistPublicationBoundary],
        effect_check: AssistBoundaryCheck[AssistPublicationBoundary],
    ) -> AssistGraphPublication:
        if type(cursor) is not AssistGraphCursor or type(publication) is not AssistTerminalPublication:
            raise TypeError("assist terminal publication requires typed inputs")

        def operation(conn: Any) -> AssistGraphPublication:
            graph = self._active(conn, cursor)
            boundary = self._publication_boundary(
                graph,
                actor=self._actors.get(graph.id),
                action=AssistPublicationAction.TERMINAL,
                status=publication.expected_status,
                reason=publication.expected_reason,
            )
            _require(authority_check, boundary, label="terminal authority")
            _require(effect_check, boundary, label="terminal effect")
            self._stage(conn)
            settlement = publication.synthesis_settlement
            if settlement is not None:
                graph = settle_compare_current_file_web_step_in_transaction(
                    conn,
                    graph_id=graph.id,
                    user_id=graph.user_id,
                    conversation_id=graph.conversation_id,
                    expected_revision=graph.revision,
                    step_id=PRIMARY_SYNTHESIS_STEP_ID,
                    state=settlement.state,
                    outcome_sha256=settlement.outcome_sha256,
                    evidence_identity_sha256=None,
                    authority_rechecked=False,
                    verified=False,
                )
            status, reason = graph.terminal_disposition()
            if (status, reason) != (publication.expected_status, publication.expected_reason):
                raise SupervisorAssistGraphAdapterError("terminal disposition changed")
            content = _TERMINAL_CONTENT.get(reason)
            if content is None:
                raise SupervisorAssistGraphAdapterError("terminal disposition has no code-owned response")
            return self._publish_terminal_in_transaction(
                conn,
                graph,
                content=content,
                trace_input=publication.trace,
                evidence_not_replayable=False,
                cancelled=False,
            )

        result = self._write(operation)
        self._actors.pop(result.graph.id, None)
        return result

    def publish_terminal_after_mixed_authority_denial(
        self,
        cursor: AssistGraphCursor,
        publication: AssistMixedAuthorityTerminalPublication,
        *,
        authority_check: AssistBoundaryCheck[AssistPublicationBoundary],
        effect_check: AssistBoundaryCheck[AssistPublicationBoundary],
    ) -> AssistGraphPublication:
        """Atomically close mixed usable/denied reads without a model capability."""

        if (
            type(cursor) is not AssistGraphCursor
            or type(publication) is not AssistMixedAuthorityTerminalPublication
        ):
            raise TypeError("mixed-authority terminal publication requires typed inputs")
        publication.__post_init__()

        def operation(conn: Any) -> AssistGraphPublication:
            graph = self._active(conn, cursor)
            actor = self._actors.get(graph.id)
            if (
                type(actor) is not ActorContext
                or actor.user_id != graph.user_id
                or actor.own_id != graph.user_id
            ):
                raise SupervisorAssistGraphAdapterError(
                    "mixed-authority terminal publication lost its process actor"
                )
            reads = graph.steps[:2]
            synthesis = graph.steps[2]
            denied = tuple(
                step
                for step in reads
                if step.state is CompareCurrentFileWebStepState.DENIED
                and step.authority_rechecked
                and not step.verified
                and step.evidence_identity_sha256 is None
            )
            usable = tuple(
                step
                for step in reads
                if step.state
                in {
                    CompareCurrentFileWebStepState.COMPLETE,
                    CompareCurrentFileWebStepState.PARTIAL,
                }
                and step.authority_rechecked
                and step.verified
                and step.evidence_identity_sha256 is not None
            )
            if (
                len(denied) != 1
                or len(usable) != 1
                or not all(step.settled for step in reads)
                or synthesis.state is not CompareCurrentFileWebStepState.PENDING
                or synthesis.attempt != 0
            ):
                raise SupervisorAssistGraphAdapterError(
                    "mixed-authority terminal publication graph is not exact"
                )
            self._live_dialogue(conn, AssistConversationScope(graph.user_id, graph.conversation_id))
            self._raw_pin(
                conn,
                user_id=graph.user_id,
                raw_id=graph.current_file_raw_object_id,
                source_sha256=graph.current_file_source_identity_sha256,
                content_sha256=graph.current_file_content_sha256,
            )
            expected_status = CompareCurrentFileWebGraphOutcomeStatus.DENIED
            expected_reason = CompareCurrentFileWebGraphOutcomeReason.AUTHORITY_DENIED
            boundary = self._publication_boundary(
                graph,
                actor=actor,
                action=AssistPublicationAction.TERMINAL,
                status=expected_status,
                reason=expected_reason,
            )
            _require(authority_check, boundary, label="mixed-authority terminal authority")
            _require(effect_check, boundary, label="mixed-authority terminal effect")
            self._stage(conn)
            claimed = claim_compare_current_file_web_step_in_transaction(
                conn,
                graph_id=graph.id,
                user_id=graph.user_id,
                conversation_id=graph.conversation_id,
                expected_revision=graph.revision,
                step_id=PRIMARY_SYNTHESIS_STEP_ID,
            )
            settled = settle_compare_current_file_web_step_in_transaction(
                conn,
                graph_id=claimed.id,
                user_id=claimed.user_id,
                conversation_id=claimed.conversation_id,
                expected_revision=claimed.revision,
                step_id=PRIMARY_SYNTHESIS_STEP_ID,
                state=CompareCurrentFileWebStepState.UNAVAILABLE,
                outcome_sha256=canonical_sha256(
                    {
                        "schema": (
                            "friday.semantic-supervisor-assist-mixed-authority-synthesis.v1"
                        ),
                        "accepted_plan_sha256": graph.accepted_plan_sha256,
                        "denied_read": denied[0].kind.value,
                        "state": CompareCurrentFileWebStepState.UNAVAILABLE.value,
                        "usable_read_outcome_sha256": usable[0].outcome_sha256,
                    }
                ),
                evidence_identity_sha256=None,
                authority_rechecked=False,
                verified=False,
            )
            if settled.terminal_disposition() != (expected_status, expected_reason):
                raise SupervisorAssistGraphAdapterError(
                    "mixed-authority terminal disposition changed"
                )
            content = _TERMINAL_CONTENT[expected_reason]
            return self._publish_terminal_in_transaction(
                conn,
                settled,
                content=content,
                trace_input=publication.trace,
                evidence_not_replayable=False,
                cancelled=False,
            )

        result = self._write(operation)
        self._actors.pop(result.graph.id, None)
        return result

    def _publish_terminal_in_transaction(
        self,
        conn: Any,
        graph: CompareCurrentFileWebWorkGraph,
        *,
        content: str,
        trace_input: AssistTraceInput,
        evidence_not_replayable: bool,
        cancelled: bool,
    ) -> AssistGraphPublication:
        status, reason = graph.terminal_disposition(
            evidence_not_replayable=evidence_not_replayable,
            cancelled=cancelled,
        )
        completion = CompletionDecision.INCOMPLETE
        failure_reason = {
            CompareCurrentFileWebGraphOutcomeReason.AUTHORITY_DENIED: FailureReason.AUTHORITY_DENIED,
            CompareCurrentFileWebGraphOutcomeReason.NO_COMPARABLE_EVIDENCE: FailureReason.COMPLETION_UNSATISFIED,
            CompareCurrentFileWebGraphOutcomeReason.CAPABILITY_UNAVAILABLE: FailureReason.SOURCE_UNAVAILABLE,
            CompareCurrentFileWebGraphOutcomeReason.STEP_FAILED: FailureReason.INTERNAL_ERROR,
            CompareCurrentFileWebGraphOutcomeReason.EVIDENCE_NOT_REPLAYABLE: FailureReason.STALE_STATE,
            CompareCurrentFileWebGraphOutcomeReason.CANCELLED: FailureReason.NONE,
        }.get(reason, FailureReason.UNKNOWN)
        trace, trace_sha256 = self._trace(
            conn,
            graph,
            trace_input,
            completion=completion,
            failure_stage=(FailureStage.NONE if cancelled else FailureStage.CAPABILITY),
            failure_reason=failure_reason,
            authority_rechecked=False,
        )
        receipt = graph.terminal_publication_receipt(
            evidence_not_replayable=evidence_not_replayable,
            cancelled=cancelled,
            final_authority_rechecked=False,
        )
        metadata = self._base_metadata(graph, verified=False)
        metadata["terminal"] = {"status": status.value, "reason": reason.value}
        if not attach_trace_to_metadata(metadata, trace):
            raise SupervisorAssistGraphAdapterError("assist terminal trace does not fit metadata")
        metadata = attach_compare_current_file_web_terminal_publication_receipt(metadata, receipt)
        assistant = store_message_in_transaction(
            conn,
            graph.conversation_id,
            graph.user_id,
            "assistant",
            content,
            metadata,
            graph.anchor_user_message_id,
        )
        message_id = str(assistant.get("id") or "")
        closed = close_compare_current_file_web_work_graph_terminal_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            publication_assistant_message_id=message_id,
            receipt=receipt,
            evidence_not_replayable=evidence_not_replayable,
            cancelled=cancelled,
        )
        return AssistGraphPublication(
            graph=closed,
            assistant_message_id=message_id,
            content=content,
            public_citations=(),
            primary_trace_sha256=trace_sha256,
            execution_receipt_sha256=receipt.canonical_sha256(),
        )

    def cancel(
        self,
        cursor: AssistGraphCursor,
        cancellation: AssistCancellation,
        *,
        authority_check: AssistBoundaryCheck[AssistPublicationBoundary],
        effect_check: AssistBoundaryCheck[AssistPublicationBoundary],
    ) -> AssistGraphPublication:
        if type(cursor) is not AssistGraphCursor or type(cancellation) is not AssistCancellation:
            raise TypeError("assist cancellation requires typed inputs")

        def operation(conn: Any) -> AssistGraphPublication:
            graph = self._active(conn, cursor)
            boundary = self._publication_boundary(
                graph,
                actor=self._actors.get(graph.id),
                action=AssistPublicationAction.CANCEL,
                status=CompareCurrentFileWebGraphOutcomeStatus.UNAVAILABLE,
                reason=CompareCurrentFileWebGraphOutcomeReason.CANCELLED,
            )
            _require(authority_check, boundary, label="cancellation authority")
            _require(effect_check, boundary, label="cancellation effect")
            self._stage(
                conn,
                expected_request_binding_sha256=cancellation.request_binding_sha256,
            )
            cancel_user = store_message_in_transaction(
                conn,
                graph.conversation_id,
                graph.user_id,
                "user",
                cancellation.user_message,
                {"answer_mode": "semantic_supervisor_assist_cancel", "interaction_mode": "dialogue"},
            )
            if _MESSAGE_ID_RE.fullmatch(str(cancel_user.get("id") or "")) is None:
                raise SupervisorAssistGraphAdapterError("cancellation user row was not stored")
            return self._publish_terminal_in_transaction(
                conn,
                graph,
                content=COMPARE_CURRENT_FILE_WEB_CANCELLED_RESPONSE,
                trace_input=cancellation.trace,
                evidence_not_replayable=False,
                cancelled=True,
            )

        result = self._write(operation)
        self._actors.pop(result.graph.id, None)
        return result

    @staticmethod
    def _restart_trace() -> AssistTraceInput:
        return AssistTraceInput(
            latency_ms=0,
            model_calls=0,
            model_call_accounting=CountAccounting.UNAVAILABLE,
            capability_calls=0,
            capability_call_accounting=CountAccounting.UNAVAILABLE,
            state_restored=True,
        )

    def restart_or_retire(
        self,
        cursor: AssistGraphCursor,
        *,
        authority_check: AssistBoundaryCheck[AssistPublicationBoundary],
        effect_check: AssistBoundaryCheck[AssistPublicationBoundary],
    ) -> AssistRestartResult:
        if type(cursor) is not AssistGraphCursor:
            raise TypeError("assist restart retirement requires a cursor")

        def operation(conn: Any) -> AssistGraphPublication:
            graph = self._active(conn, cursor)
            boundary = self._publication_boundary(
                graph,
                actor=self._actors.get(graph.id),
                action=AssistPublicationAction.RESTART_RETIREMENT,
                status=CompareCurrentFileWebGraphOutcomeStatus.UNAVAILABLE,
                reason=CompareCurrentFileWebGraphOutcomeReason.EVIDENCE_NOT_REPLAYABLE,
            )
            _require(authority_check, boundary, label="restart authority")
            _require(effect_check, boundary, label="restart effect")
            self._stage(conn)
            return self._publish_terminal_in_transaction(
                conn,
                graph,
                content=COMPARE_CURRENT_FILE_WEB_RESTART_UNAVAILABLE_RESPONSE,
                trace_input=self._restart_trace(),
                evidence_not_replayable=True,
                cancelled=False,
            )

        publication = self._write(operation)
        self._actors.pop(publication.graph.id, None)
        return AssistRestartResult(
            AssistRestartDisposition.RETIRED_EVIDENCE_NOT_REPLAYABLE,
            publication,
        )

    def retire_active_after_restart(self, *, limit: int = _MAX_RECONCILE) -> AssistRestartBatch:
        """Deterministically retire a bounded page of process-private ACTIVE graphs."""

        if type(limit) is not int or not 1 <= limit <= _MAX_RECONCILE:
            raise ValueError("assist restart retirement limit must be between 1 and 100")

        def operation(conn: Any) -> AssistRestartBatch:
            self._stage(conn)
            rows = conn.execute(
                """SELECT id,user_id,conversation_id,revision
                     FROM work_item_compare_current_file_web_graphs
                    WHERE state='active' ORDER BY created_at,id LIMIT ?""",
                (limit + 1,),
            ).fetchall()
            results: list[AssistRestartResult] = []
            for row in rows[:limit]:
                graph = get_compare_current_file_web_work_graph_in_transaction(
                    conn,
                    graph_id=str(row["id"]),
                    user_id=str(row["user_id"]),
                    conversation_id=str(row["conversation_id"]),
                )
                if graph is None or graph.state is not CompareCurrentFileWebGraphState.ACTIVE:
                    continue
                publication = self._publish_terminal_in_transaction(
                    conn,
                    graph,
                    content=COMPARE_CURRENT_FILE_WEB_RESTART_UNAVAILABLE_RESPONSE,
                    trace_input=self._restart_trace(),
                    evidence_not_replayable=True,
                    cancelled=False,
                )
                results.append(
                    AssistRestartResult(
                        AssistRestartDisposition.RETIRED_EVIDENCE_NOT_REPLAYABLE,
                        publication,
                    )
                )
            return AssistRestartBatch(tuple(results), len(rows) > limit)

        return self._write(operation)

    def reconcile_all_active_after_restart(
        self, *, batch_limit: int = _MAX_RECONCILE
    ) -> tuple[AssistRestartBatch, ...]:
        """Drain every ACTIVE graph before startup admits new traffic."""

        if type(batch_limit) is not int or not 1 <= batch_limit <= _MAX_RECONCILE:
            raise ValueError("assist restart batch limit must be between 1 and 100")
        batches: list[AssistRestartBatch] = []
        while True:
            batch = self.retire_active_after_restart(limit=batch_limit)
            batches.append(batch)
            if not batch.has_more:
                return tuple(batches)


__all__ = [
    "AssistAdmissionBoundary",
    "AssistBoundaryCheck",
    "AssistCancellation",
    "AssistCapabilityBoundary",
    "AssistClaimedStep",
    "AssistComparisonPublication",
    "AssistConversationScope",
    "AssistGraphAdmission",
    "AssistGraphCursor",
    "AssistGraphPublication",
    "AssistMixedAuthorityTerminalPublication",
    "AssistPublicationAction",
    "AssistPublicationBoundary",
    "AssistRestartBatch",
    "AssistRestartDisposition",
    "AssistRestartResult",
    "AssistStepSettlement",
    "AssistTerminalPublication",
    "AssistTraceInput",
    "SupervisorAssistGraphAdapter",
    "SupervisorAssistGraphAdapterError",
]
