"""Closed durable graph for one current-file/current-web comparison journey.

This is deliberately not a generic graph vocabulary.  It admits exactly two
independent read steps followed by one primary-synthesis step.  The contract is
body-free: durable inputs, evidence, actors and conversations are represented by
already code-owned identifiers or SHA-256 identities, never by prompts, queries,
paths or evidence text.

The module owns no adapter, executor, model client or publication handle.  It is
the dormant schema-44 state contract on which a later separately admitted P3
runtime may depend.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from friday.interaction_control_plane.work_item_contract import (
    WORK_ITEM_MAX_REVISION,
    WORK_ITEM_TTL_HOURS,
    canonical_work_item_instant,
)
from friday.orchestration.execution_plan import ValidatedExecutionPlan, ValidatedStep
from friday.orchestration.supervisor_contracts import (
    FILE_CURRENT_READ_ID,
    PRIMARY_SYNTHESIS_ID,
    WEB_SEARCH_CURRENT_ID,
    CapabilityEffectClass,
)

COMPARE_CURRENT_FILE_WEB_WORK_GRAPH_SCHEMA = "friday.compare-current-file-with-current-web-work-graph.v1"
COMPARE_CURRENT_FILE_WEB_STEP_OUTCOME_SCHEMA = "friday.compare-current-file-with-current-web-step-outcome.v1"
COMPARE_CURRENT_FILE_WEB_PUBLICATION_RECEIPT_SCHEMA = (
    "friday.accepted-compare-current-file-with-current-web-work-graph-receipt.v1"
)
COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_RECEIPT_SCHEMA = (
    "friday.accepted-compare-current-file-with-current-web-terminal-receipt.v1"
)
COMPARE_CURRENT_FILE_WEB_PUBLICATION_METADATA_KEY = (
    "accepted_compare_current_file_with_current_web_work_graph"
)
COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY = (
    "accepted_compare_current_file_with_current_web_terminal"
)
COMPARE_CURRENT_FILE_WEB_COMPLETION_CONTRACT = "accepted_complete_current_file_current_web_comparison"
COMPARE_CURRENT_FILE_WEB_FALLBACK_OWNER = "primary_only"
COMPARE_CURRENT_FILE_WEB_PUBLICATION_OWNER = "primary"
COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS = 2
COMPARE_CURRENT_FILE_WEB_MAX_ACTIVE_REVISION = WORK_ITEM_MAX_REVISION - 1
COMPARE_CURRENT_FILE_WEB_ARCHIVED_RESPONSE = (
    "Сравнение текущего файла с вебом остановлено: разговор архивирован."
)
COMPARE_CURRENT_FILE_WEB_CANCELLED_RESPONSE = "Сравнение текущего файла с вебом отменено по вашему запросу."
COMPARE_CURRENT_FILE_WEB_EXPIRED_RESPONSE = (
    "Сравнение текущего файла с вебом остановлено: срок выполнения истёк."
)
COMPARE_CURRENT_FILE_WEB_RESTART_UNAVAILABLE_RESPONSE = (
    "Сравнение нельзя безопасно продолжить после перезапуска: веб-источники не повторяются."
)

FILE_READ_STEP_ID = "read_current_file"
WEB_READ_STEP_ID = "read_current_web"
PRIMARY_SYNTHESIS_STEP_ID = "primary_synthesis"
EVIDENCE_PARALLEL_GROUP = "current_evidence"

FILE_CURRENT_READ_CAPABILITY_ID = "file.current.read"
WEB_SEARCH_CURRENT_CAPABILITY_ID = "web.search.current"
PRIMARY_SYNTHESIS_CAPABILITY_ID = "model.primary.synthesis"
WEB_SEARCH_CURRENT_SECURITY_ID = "web.compare.transient"
WEB_SEARCH_CURRENT_ADAPTER_ID = "transient_web_comparison"

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_GRAPH_ID_RE = re.compile(r"graph_[0-9a-f]{16}")
_USER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}")
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}")
_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}")
_RAW_OBJECT_ID_RE = re.compile(r"raw_[0-9a-f]{16}")
_MAX_SERIALIZED_BYTES = 32_768
_MAX_RECEIPT_SERIALIZED_BYTES = 8_192
_MAX_ASSISTANT_METADATA_BYTES = 65_536


class CompareCurrentFileWebGraphError(ValueError):
    """A value is outside the closed journey-specific WorkGraph contract."""


class CompareCurrentFileWebGraphState(StrEnum):
    ACTIVE = "active"
    TERMINAL = "terminal"
    COMPLETED = "completed"


class CompareCurrentFileWebGraphTransition(StrEnum):
    ADMITTED = "admitted"
    STEP_CLAIMED = "step_claimed"
    STEP_SETTLED = "step_settled"
    STEP_REQUEUED = "step_requeued"
    RESTART_REBIND = "restart_rebind"
    REVIEW_RECOVERY_ADMITTED = "review_recovery_admitted"
    TERMINAL_SETTLED = "terminal_settled"
    PUBLICATION_COMMITTED = "publication_committed"


class CompareCurrentFileWebGraphOutcomeStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"
    FAILED = "failed"


class CompareCurrentFileWebGraphOutcomeReason(StrEnum):
    NONE = "none"
    PARTIAL_EVIDENCE = "partial_evidence"
    NO_COMPARABLE_EVIDENCE = "no_comparable_evidence"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    AUTHORITY_DENIED = "authority_denied"
    STEP_FAILED = "step_failed"
    EVIDENCE_NOT_REPLAYABLE = "evidence_not_replayable"
    CONVERSATION_ARCHIVED = "conversation_archived"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class CompareCurrentFileWebStepKind(StrEnum):
    FILE_READ = "file_current_read"
    WEB_READ = "web_current_read"
    PRIMARY_SYNTHESIS = "primary_synthesis"


class CompareCurrentFileWebStepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"
    FAILED = "failed"


_STEP_ID_BY_KIND = {
    CompareCurrentFileWebStepKind.FILE_READ: FILE_READ_STEP_ID,
    CompareCurrentFileWebStepKind.WEB_READ: WEB_READ_STEP_ID,
    CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS: PRIMARY_SYNTHESIS_STEP_ID,
}
_CAPABILITY_BY_KIND = {
    CompareCurrentFileWebStepKind.FILE_READ: FILE_CURRENT_READ_CAPABILITY_ID,
    CompareCurrentFileWebStepKind.WEB_READ: WEB_SEARCH_CURRENT_CAPABILITY_ID,
    CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS: PRIMARY_SYNTHESIS_CAPABILITY_ID,
}
_DEPENDENCIES_BY_KIND = {
    CompareCurrentFileWebStepKind.FILE_READ: (),
    CompareCurrentFileWebStepKind.WEB_READ: (),
    CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS: (
        FILE_READ_STEP_ID,
        WEB_READ_STEP_ID,
    ),
}
_PARALLEL_GROUP_BY_KIND = {
    CompareCurrentFileWebStepKind.FILE_READ: EVIDENCE_PARALLEL_GROUP,
    CompareCurrentFileWebStepKind.WEB_READ: EVIDENCE_PARALLEL_GROUP,
    CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS: None,
}
_SECURITY_ID_BY_KIND = {
    CompareCurrentFileWebStepKind.FILE_READ: None,
    CompareCurrentFileWebStepKind.WEB_READ: WEB_SEARCH_CURRENT_SECURITY_ID,
    CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS: None,
}
_ADAPTER_ID_BY_KIND = {
    CompareCurrentFileWebStepKind.FILE_READ: None,
    CompareCurrentFileWebStepKind.WEB_READ: WEB_SEARCH_CURRENT_ADAPTER_ID,
    CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS: None,
}
_EVIDENCE_REPLAYABILITY_BY_KIND = {kind: "process_private" for kind in CompareCurrentFileWebStepKind}
_FIXED_STEP_ORDER = (
    CompareCurrentFileWebStepKind.FILE_READ,
    CompareCurrentFileWebStepKind.WEB_READ,
    CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS,
)
_PLAN_CAPABILITY_ID_BY_KIND = {
    CompareCurrentFileWebStepKind.FILE_READ: FILE_CURRENT_READ_ID,
    CompareCurrentFileWebStepKind.WEB_READ: WEB_SEARCH_CURRENT_ID,
    # P2 intentionally calls the model role ``primary.synthesis`` while the
    # durable P3 graph calls its structural node ``model.primary.synthesis``.
    # Keep that translation explicit instead of accepting either spelling in
    # either contract.
    CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS: PRIMARY_SYNTHESIS_ID,
}
_SETTLED_STATES = frozenset(
    {
        CompareCurrentFileWebStepState.COMPLETE,
        CompareCurrentFileWebStepState.PARTIAL,
        CompareCurrentFileWebStepState.EMPTY,
        CompareCurrentFileWebStepState.UNAVAILABLE,
        CompareCurrentFileWebStepState.DENIED,
        CompareCurrentFileWebStepState.FAILED,
    }
)


def _digest(value: object, *, label: str, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise CompareCurrentFileWebGraphError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _identifier(value: object, pattern: re.Pattern[str], *, label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise CompareCurrentFileWebGraphError(f"{label} is not a canonical identifier")
    return value


def _instant(value: object, *, label: str, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    try:
        return canonical_work_item_instant(value, label=label)
    except ValueError as exc:
        raise CompareCurrentFileWebGraphError(str(exc)) from exc


def _canonical_json(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CompareCurrentFileWebGraphError("WorkGraph value is not canonical JSON") from exc
    try:
        raw = encoded.encode("ascii", errors="strict")
    except UnicodeEncodeError as exc:  # pragma: no cover - ensure_ascii is fixed above
        raise CompareCurrentFileWebGraphError("WorkGraph JSON is not ASCII canonical") from exc
    if len(raw) > _MAX_SERIALIZED_BYTES:
        raise CompareCurrentFileWebGraphError("WorkGraph JSON exceeds the closed byte budget")
    return encoded


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("ascii")).hexdigest()


def _closed_json_object(value: str | Mapping[str, Any], *, maximum: int, label: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            raw = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise CompareCurrentFileWebGraphError(f"{label} must be valid UTF-8") from exc
        if len(raw) > maximum:
            raise CompareCurrentFileWebGraphError(f"{label} exceeds its byte budget")

        def closed_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise CompareCurrentFileWebGraphError(f"{label} contains a duplicate key")
                result[key] = item
            return result

        try:
            decoded = json.loads(value, object_pairs_hook=closed_pairs)
        except json.JSONDecodeError as exc:
            raise CompareCurrentFileWebGraphError(f"{label} is not valid JSON") from exc
    elif isinstance(value, Mapping):
        decoded = dict(value)
        if len(_canonical_json(decoded).encode("ascii")) > maximum:
            raise CompareCurrentFileWebGraphError(f"{label} exceeds its byte budget")
    else:
        raise CompareCurrentFileWebGraphError(f"{label} must be an object or JSON object")
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise CompareCurrentFileWebGraphError(f"{label} must be a JSON object")
    return decoded


@dataclass(frozen=True, slots=True)
class CompareCurrentFileWebGraphStep:
    graph_id: str
    step_id: str
    kind: CompareCurrentFileWebStepKind
    capability_id: str
    security_id: str | None
    adapter_id: str | None
    evidence_replayability: str
    depends_on: tuple[str, ...]
    parallel_group: str | None
    input_identity_sha256: str
    idempotency_key_sha256: str
    state: CompareCurrentFileWebStepState
    attempt: int
    outcome_sha256: str | None
    prior_outcome_sha256: str | None
    recovery_review_sha256: str | None
    recovery_context_sha256: str | None
    evidence_identity_sha256: str | None
    authority_rechecked: bool
    verified: bool
    started_at: str | None
    settled_at: str | None

    def __post_init__(self) -> None:
        _identifier(self.graph_id, _GRAPH_ID_RE, label="graph_id")
        if not isinstance(self.kind, CompareCurrentFileWebStepKind):
            raise CompareCurrentFileWebGraphError("step kind must be closed")
        if self.step_id != _STEP_ID_BY_KIND[self.kind]:
            raise CompareCurrentFileWebGraphError("step identity does not match the fixed graph")
        if self.capability_id != _CAPABILITY_BY_KIND[self.kind]:
            raise CompareCurrentFileWebGraphError("capability identity does not match the fixed graph")
        if self.security_id != _SECURITY_ID_BY_KIND[self.kind]:
            raise CompareCurrentFileWebGraphError("security binding does not match the fixed graph")
        if self.adapter_id != _ADAPTER_ID_BY_KIND[self.kind]:
            raise CompareCurrentFileWebGraphError("adapter binding does not match the fixed graph")
        if self.evidence_replayability != _EVIDENCE_REPLAYABILITY_BY_KIND[self.kind]:
            raise CompareCurrentFileWebGraphError("step evidence replayability is not process-private")
        if self.depends_on != _DEPENDENCIES_BY_KIND[self.kind]:
            raise CompareCurrentFileWebGraphError("step dependencies do not match the fixed graph")
        if self.parallel_group != _PARALLEL_GROUP_BY_KIND[self.kind]:
            raise CompareCurrentFileWebGraphError("parallel group does not match the fixed graph")
        _digest(self.input_identity_sha256, label="input_identity_sha256")
        _digest(self.idempotency_key_sha256, label="idempotency_key_sha256")
        if not isinstance(self.state, CompareCurrentFileWebStepState):
            raise CompareCurrentFileWebGraphError("step state must be closed")
        if type(self.attempt) is not int or not 0 <= self.attempt <= COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS:
            raise CompareCurrentFileWebGraphError("step attempt exceeds the fixed retry budget")
        _digest(self.outcome_sha256, label="outcome_sha256", optional=True)
        _digest(self.prior_outcome_sha256, label="prior_outcome_sha256", optional=True)
        _digest(self.recovery_review_sha256, label="recovery_review_sha256", optional=True)
        _digest(self.recovery_context_sha256, label="recovery_context_sha256", optional=True)
        if (self.recovery_review_sha256 is None) != (self.recovery_context_sha256 is None):
            raise CompareCurrentFileWebGraphError("review recovery witness must be an exact digest pair")
        if self.recovery_review_sha256 is not None and (
            self.kind is not CompareCurrentFileWebStepKind.WEB_READ
            or self.attempt < 1
            or self.prior_outcome_sha256 is None
        ):
            raise CompareCurrentFileWebGraphError(
                "review recovery witness belongs only to a retried current-web read"
            )
        _digest(
            self.evidence_identity_sha256,
            label="evidence_identity_sha256",
            optional=True,
        )
        if type(self.authority_rechecked) is not bool or type(self.verified) is not bool:
            raise CompareCurrentFileWebGraphError("step authority and verification flags must be booleans")
        started = _instant(self.started_at, label="started_at", optional=True)
        settled = _instant(self.settled_at, label="settled_at", optional=True)
        if started != self.started_at or settled != self.settled_at:
            raise CompareCurrentFileWebGraphError("step timestamps must already be canonical")
        self._validate_lifecycle()

    def _validate_lifecycle(self) -> None:
        if self.state is CompareCurrentFileWebStepState.PENDING:
            if (
                self.outcome_sha256 is not None
                or self.evidence_identity_sha256 is not None
                or self.authority_rechecked
                or self.verified
                or self.started_at is not None
                or self.settled_at is not None
            ):
                raise CompareCurrentFileWebGraphError("pending step cannot claim execution or outcome state")
            if self.prior_outcome_sha256 is not None and self.attempt < 1:
                raise CompareCurrentFileWebGraphError("prior outcome requires one consumed attempt")
            return
        if self.state is CompareCurrentFileWebStepState.RUNNING:
            if (
                self.attempt < 1
                or self.started_at is None
                or self.settled_at is not None
                or self.outcome_sha256 is not None
                or self.evidence_identity_sha256 is not None
                or self.authority_rechecked
                or self.verified
            ):
                raise CompareCurrentFileWebGraphError("running step lifecycle is inconsistent")
            if self.attempt == 1 and self.prior_outcome_sha256 is not None:
                raise CompareCurrentFileWebGraphError("first attempt cannot carry a prior outcome")
            return
        if self.state not in _SETTLED_STATES or self.attempt < 1:
            raise CompareCurrentFileWebGraphError("step lifecycle state is not admitted")
        if self.started_at is None or self.settled_at is None or self.outcome_sha256 is None:
            raise CompareCurrentFileWebGraphError(
                "settled step requires start, settlement and outcome identity"
            )
        if self.started_at > self.settled_at:
            raise CompareCurrentFileWebGraphError("step cannot settle before it starts")

        is_read = self.kind in {
            CompareCurrentFileWebStepKind.FILE_READ,
            CompareCurrentFileWebStepKind.WEB_READ,
        }
        if not is_read:
            if self.state not in {
                CompareCurrentFileWebStepState.COMPLETE,
                CompareCurrentFileWebStepState.PARTIAL,
                CompareCurrentFileWebStepState.UNAVAILABLE,
                CompareCurrentFileWebStepState.FAILED,
            }:
                raise CompareCurrentFileWebGraphError("primary synthesis has an invalid terminal state")
            if self.authority_rechecked:
                raise CompareCurrentFileWebGraphError("primary synthesis cannot mint an authority check")
            if self.state in {
                CompareCurrentFileWebStepState.COMPLETE,
                CompareCurrentFileWebStepState.PARTIAL,
            }:
                if self.evidence_identity_sha256 is None or not self.verified:
                    raise CompareCurrentFileWebGraphError(
                        "accepted synthesis requires a verified admitted-outcome identity"
                    )
            elif self.evidence_identity_sha256 is not None or self.verified:
                raise CompareCurrentFileWebGraphError("failed synthesis cannot claim accepted evidence")
            return

        if self.state in {
            CompareCurrentFileWebStepState.COMPLETE,
            CompareCurrentFileWebStepState.PARTIAL,
            CompareCurrentFileWebStepState.EMPTY,
        }:
            if self.evidence_identity_sha256 is None or not self.authority_rechecked or not self.verified:
                raise CompareCurrentFileWebGraphError(
                    "accepted read outcome requires authorized verified evidence identity"
                )
        elif self.state is CompareCurrentFileWebStepState.DENIED:
            if self.evidence_identity_sha256 is not None or not self.authority_rechecked or self.verified:
                raise CompareCurrentFileWebGraphError("denied read outcome has inconsistent authority state")
        elif self.evidence_identity_sha256 is not None or self.authority_rechecked or self.verified:
            raise CompareCurrentFileWebGraphError("failed read outcome cannot claim accepted evidence")

    @classmethod
    def pending(
        cls,
        *,
        graph_id: str,
        kind: CompareCurrentFileWebStepKind,
        input_identity_sha256: str,
        idempotency_key_sha256: str,
    ) -> CompareCurrentFileWebGraphStep:
        return cls(
            graph_id=graph_id,
            step_id=_STEP_ID_BY_KIND[kind],
            kind=kind,
            capability_id=_CAPABILITY_BY_KIND[kind],
            security_id=_SECURITY_ID_BY_KIND[kind],
            adapter_id=_ADAPTER_ID_BY_KIND[kind],
            evidence_replayability=_EVIDENCE_REPLAYABILITY_BY_KIND[kind],
            depends_on=_DEPENDENCIES_BY_KIND[kind],
            parallel_group=_PARALLEL_GROUP_BY_KIND[kind],
            input_identity_sha256=input_identity_sha256,
            idempotency_key_sha256=idempotency_key_sha256,
            state=CompareCurrentFileWebStepState.PENDING,
            attempt=0,
            outcome_sha256=None,
            prior_outcome_sha256=None,
            recovery_review_sha256=None,
            recovery_context_sha256=None,
            evidence_identity_sha256=None,
            authority_rechecked=False,
            verified=False,
            started_at=None,
            settled_at=None,
        )

    @property
    def settled(self) -> bool:
        return self.state in _SETTLED_STATES

    def payload(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "kind": self.kind.value,
            "capability_id": self.capability_id,
            "security_id": self.security_id,
            "adapter_id": self.adapter_id,
            "effect_class": "read",
            "evidence_replayability": self.evidence_replayability,
            "depends_on": list(self.depends_on),
            "parallel_group": self.parallel_group,
            "input_identity_sha256": self.input_identity_sha256,
            "idempotency_key_sha256": self.idempotency_key_sha256,
            "state": self.state.value,
            "attempt": self.attempt,
            "outcome_schema": COMPARE_CURRENT_FILE_WEB_STEP_OUTCOME_SCHEMA,
            "outcome_sha256": self.outcome_sha256,
            "prior_outcome_sha256": self.prior_outcome_sha256,
            "recovery_review_sha256": self.recovery_review_sha256,
            "recovery_context_sha256": self.recovery_context_sha256,
            "evidence_identity_sha256": self.evidence_identity_sha256,
            "authority_rechecked": self.authority_rechecked,
            "verified": self.verified,
            "started_at": self.started_at,
            "settled_at": self.settled_at,
        }


@dataclass(frozen=True, slots=True)
class CompareCurrentFileWebPlanStepBinding:
    """Exact P2 step bound to its distinct fixed P3 graph node."""

    graph_kind: CompareCurrentFileWebStepKind
    graph_step_id: str
    graph_capability_id: str
    plan_step: ValidatedStep

    def __post_init__(self) -> None:
        if (
            not isinstance(self.graph_kind, CompareCurrentFileWebStepKind)
            or self.graph_step_id != _STEP_ID_BY_KIND[self.graph_kind]
            or self.graph_capability_id != _CAPABILITY_BY_KIND[self.graph_kind]
            or type(self.plan_step) is not ValidatedStep
            or self.plan_step.capability_id != _PLAN_CAPABILITY_ID_BY_KIND[self.graph_kind]
            or self.plan_step.effect_class is not CapabilityEffectClass.READ
        ):
            raise CompareCurrentFileWebGraphError("P2/P3 step binding is invalid")


def bind_validated_plan_to_compare_current_file_web_graph(
    plan: ValidatedExecutionPlan,
) -> tuple[CompareCurrentFileWebPlanStepBinding, ...]:
    """Require the exact admitted P2 journey and map it to fixed P3 nodes.

    The mapping is deliberately capability-based because P2 step identifiers
    are proposal-local (``s1``/``s2``/``s3``), while P3 identifiers are durable
    controller identifiers.  No alias is accepted in the opposite namespace.
    """

    if type(plan) is not ValidatedExecutionPlan or len(plan.steps) != len(_FIXED_STEP_ORDER):
        raise CompareCurrentFileWebGraphError("P3 graph requires one exact admitted P2 plan")
    by_capability: dict[str, ValidatedStep] = {}
    for step in plan.steps:
        if step.capability_id in by_capability:
            raise CompareCurrentFileWebGraphError("P2 plan capability mapping is not unique")
        by_capability[step.capability_id] = step
    if set(by_capability) != set(_PLAN_CAPABILITY_ID_BY_KIND.values()):
        raise CompareCurrentFileWebGraphError("P2 plan does not match the fixed P3 journey")

    file_step = by_capability[FILE_CURRENT_READ_ID]
    web_step = by_capability[WEB_SEARCH_CURRENT_ID]
    synthesis = by_capability[PRIMARY_SYNTHESIS_ID]
    if (
        file_step.depends_on
        or web_step.depends_on
        or set(synthesis.depends_on) != {file_step.step_id, web_step.step_id}
        or len(synthesis.depends_on) != 2
        or file_step.parallel_group is None
        or file_step.parallel_group != web_step.parallel_group
        or synthesis.parallel_group is not None
    ):
        raise CompareCurrentFileWebGraphError("P2 plan dependency shape does not match P3")
    return tuple(
        CompareCurrentFileWebPlanStepBinding(
            graph_kind=kind,
            graph_step_id=_STEP_ID_BY_KIND[kind],
            graph_capability_id=_CAPABILITY_BY_KIND[kind],
            plan_step=by_capability[_PLAN_CAPABILITY_ID_BY_KIND[kind]],
        )
        for kind in _FIXED_STEP_ORDER
    )


@dataclass(frozen=True, slots=True)
class CompareCurrentFileWebPublicationReceipt:
    graph_id: str
    completed_revision: int
    accepted_plan_sha256: str
    graph_outcome_sha256: str
    steps_sha256: str
    final_authority_rechecked: bool

    def __post_init__(self) -> None:
        _identifier(self.graph_id, _GRAPH_ID_RE, label="graph_id")
        if (
            type(self.completed_revision) is not int
            or not 2 <= self.completed_revision <= WORK_ITEM_MAX_REVISION
        ):
            raise CompareCurrentFileWebGraphError("completed_revision is outside the WorkGraph bound")
        _digest(self.accepted_plan_sha256, label="accepted_plan_sha256")
        _digest(self.graph_outcome_sha256, label="graph_outcome_sha256")
        _digest(self.steps_sha256, label="steps_sha256")
        if self.final_authority_rechecked is not True:
            raise CompareCurrentFileWebGraphError(
                "full publication requires a final source/permission recheck"
            )

    def payload(self) -> dict[str, Any]:
        return {
            "schema": COMPARE_CURRENT_FILE_WEB_PUBLICATION_RECEIPT_SCHEMA,
            "graph_id": self.graph_id,
            "completed_revision": self.completed_revision,
            "accepted_plan_sha256": self.accepted_plan_sha256,
            "graph_outcome_sha256": self.graph_outcome_sha256,
            "steps_sha256": self.steps_sha256,
            "final_authority_rechecked": self.final_authority_rechecked,
        }

    def canonical_sha256(self) -> str:
        return _canonical_sha256(self.payload())

    @classmethod
    def parse(
        cls,
        value: str | Mapping[str, Any],
    ) -> CompareCurrentFileWebPublicationReceipt:
        item = _closed_json_object(
            value,
            maximum=_MAX_RECEIPT_SERIALIZED_BYTES,
            label="WorkGraph publication receipt",
        )
        expected = {
            "schema",
            "graph_id",
            "completed_revision",
            "accepted_plan_sha256",
            "graph_outcome_sha256",
            "steps_sha256",
            "final_authority_rechecked",
        }
        if set(item) != expected or item["schema"] != COMPARE_CURRENT_FILE_WEB_PUBLICATION_RECEIPT_SCHEMA:
            raise CompareCurrentFileWebGraphError("WorkGraph publication receipt keys/schema are invalid")
        return cls(
            graph_id=item["graph_id"],
            completed_revision=item["completed_revision"],
            accepted_plan_sha256=item["accepted_plan_sha256"],
            graph_outcome_sha256=item["graph_outcome_sha256"],
            steps_sha256=item["steps_sha256"],
            final_authority_rechecked=item["final_authority_rechecked"],
        )


@dataclass(frozen=True, slots=True)
class CompareCurrentFileWebTerminalPublicationReceipt:
    graph_id: str
    terminal_revision: int
    accepted_plan_sha256: str
    status: CompareCurrentFileWebGraphOutcomeStatus
    reason: CompareCurrentFileWebGraphOutcomeReason
    graph_outcome_sha256: str
    steps_sha256: str
    model_spoke: bool
    evidence_cited: bool
    final_authority_rechecked: bool
    completion_claimed: bool = False

    def __post_init__(self) -> None:
        _identifier(self.graph_id, _GRAPH_ID_RE, label="graph_id")
        if (
            type(self.terminal_revision) is not int
            or not 2 <= self.terminal_revision <= WORK_ITEM_MAX_REVISION
        ):
            raise CompareCurrentFileWebGraphError("terminal_revision is outside the WorkGraph bound")
        _digest(self.accepted_plan_sha256, label="accepted_plan_sha256")
        _digest(self.graph_outcome_sha256, label="graph_outcome_sha256")
        _digest(self.steps_sha256, label="steps_sha256")
        if (
            not isinstance(self.status, CompareCurrentFileWebGraphOutcomeStatus)
            or self.status is CompareCurrentFileWebGraphOutcomeStatus.COMPLETE
            or not isinstance(self.reason, CompareCurrentFileWebGraphOutcomeReason)
            or self.reason is CompareCurrentFileWebGraphOutcomeReason.NONE
        ):
            raise CompareCurrentFileWebGraphError("terminal receipt status/reason is not closed")
        if any(
            type(value) is not bool
            for value in (
                self.model_spoke,
                self.evidence_cited,
                self.final_authority_rechecked,
                self.completion_claimed,
            )
        ):
            raise CompareCurrentFileWebGraphError("terminal receipt claims must be booleans")
        is_partial = self.status is CompareCurrentFileWebGraphOutcomeStatus.PARTIAL
        if self.completion_claimed or (
            is_partial != (self.model_spoke and self.evidence_cited and self.final_authority_rechecked)
        ):
            raise CompareCurrentFileWebGraphError(
                "terminal receipt cannot claim completion or unverified evidence publication"
            )
        if not is_partial and (self.model_spoke or self.evidence_cited or self.final_authority_rechecked):
            raise CompareCurrentFileWebGraphError(
                "deterministic terminal fallback cannot claim model/evidence authority"
            )

    def payload(self) -> dict[str, Any]:
        return {
            "schema": COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_RECEIPT_SCHEMA,
            "graph_id": self.graph_id,
            "terminal_revision": self.terminal_revision,
            "accepted_plan_sha256": self.accepted_plan_sha256,
            "status": self.status.value,
            "reason": self.reason.value,
            "graph_outcome_sha256": self.graph_outcome_sha256,
            "steps_sha256": self.steps_sha256,
            "model_spoke": self.model_spoke,
            "evidence_cited": self.evidence_cited,
            "final_authority_rechecked": self.final_authority_rechecked,
            "completion_claimed": self.completion_claimed,
        }

    def canonical_sha256(self) -> str:
        return _canonical_sha256(self.payload())

    @classmethod
    def parse(
        cls,
        value: str | Mapping[str, Any],
    ) -> CompareCurrentFileWebTerminalPublicationReceipt:
        item = _closed_json_object(
            value,
            maximum=_MAX_RECEIPT_SERIALIZED_BYTES,
            label="WorkGraph terminal publication receipt",
        )
        expected = {
            "schema",
            "graph_id",
            "terminal_revision",
            "accepted_plan_sha256",
            "status",
            "reason",
            "graph_outcome_sha256",
            "steps_sha256",
            "model_spoke",
            "evidence_cited",
            "final_authority_rechecked",
            "completion_claimed",
        }
        if (
            set(item) != expected
            or item["schema"] != COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_RECEIPT_SCHEMA
        ):
            raise CompareCurrentFileWebGraphError(
                "WorkGraph terminal publication receipt keys/schema are invalid"
            )
        try:
            return cls(
                graph_id=item["graph_id"],
                terminal_revision=item["terminal_revision"],
                accepted_plan_sha256=item["accepted_plan_sha256"],
                status=CompareCurrentFileWebGraphOutcomeStatus(item["status"]),
                reason=CompareCurrentFileWebGraphOutcomeReason(item["reason"]),
                graph_outcome_sha256=item["graph_outcome_sha256"],
                steps_sha256=item["steps_sha256"],
                model_spoke=item["model_spoke"],
                evidence_cited=item["evidence_cited"],
                final_authority_rechecked=item["final_authority_rechecked"],
                completion_claimed=item["completion_claimed"],
            )
        except ValueError as exc:
            raise CompareCurrentFileWebGraphError(
                "WorkGraph terminal publication receipt enum is invalid"
            ) from exc


@dataclass(frozen=True, slots=True)
class CompareCurrentFileWebWorkGraph:
    id: str
    user_id: str
    conversation_id: str
    anchor_user_message_id: str
    current_file_raw_object_id: str
    state: CompareCurrentFileWebGraphState
    revision: int
    transition: CompareCurrentFileWebGraphTransition
    proposal_sha256: str
    accepted_plan_sha256: str
    manifest_sha256: str
    policy_sha256: str
    runtime_profile_sha256: str
    adapter_registry_sha256: str
    actor_binding_sha256: str
    conversation_binding_sha256: str
    current_file_source_identity_sha256: str
    current_file_content_sha256: str
    created_at: str
    updated_at: str
    expires_at: str
    closed_at: str | None
    outcome_status: CompareCurrentFileWebGraphOutcomeStatus | None
    outcome_reason: CompareCurrentFileWebGraphOutcomeReason | None
    publication_assistant_message_id: str | None
    accepted_graph_outcome_sha256: str | None
    accepted_steps_sha256: str | None
    terminal_publication_receipt_sha256: str | None
    publication_receipt_sha256: str | None
    steps: tuple[CompareCurrentFileWebGraphStep, ...]

    def __post_init__(self) -> None:
        _identifier(self.id, _GRAPH_ID_RE, label="graph id")
        _identifier(self.user_id, _USER_ID_RE, label="user_id")
        _identifier(self.conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
        _identifier(self.anchor_user_message_id, _MESSAGE_ID_RE, label="anchor_user_message_id")
        _identifier(self.current_file_raw_object_id, _RAW_OBJECT_ID_RE, label="current_file_raw_object_id")
        if not isinstance(self.state, CompareCurrentFileWebGraphState):
            raise CompareCurrentFileWebGraphError("graph state must be closed")
        if type(self.revision) is not int or not 1 <= self.revision <= WORK_ITEM_MAX_REVISION:
            raise CompareCurrentFileWebGraphError("graph revision is outside the Work Item bound")
        if not isinstance(self.transition, CompareCurrentFileWebGraphTransition):
            raise CompareCurrentFileWebGraphError("graph transition must be closed")
        if self.outcome_status is not None and not isinstance(
            self.outcome_status, CompareCurrentFileWebGraphOutcomeStatus
        ):
            raise CompareCurrentFileWebGraphError("graph outcome status must be closed")
        if self.outcome_reason is not None and not isinstance(
            self.outcome_reason, CompareCurrentFileWebGraphOutcomeReason
        ):
            raise CompareCurrentFileWebGraphError("graph outcome reason must be closed")
        for label, value in (
            ("proposal_sha256", self.proposal_sha256),
            ("accepted_plan_sha256", self.accepted_plan_sha256),
            ("manifest_sha256", self.manifest_sha256),
            ("policy_sha256", self.policy_sha256),
            ("runtime_profile_sha256", self.runtime_profile_sha256),
            ("adapter_registry_sha256", self.adapter_registry_sha256),
            ("actor_binding_sha256", self.actor_binding_sha256),
            ("conversation_binding_sha256", self.conversation_binding_sha256),
            ("current_file_source_identity_sha256", self.current_file_source_identity_sha256),
            ("current_file_content_sha256", self.current_file_content_sha256),
        ):
            _digest(value, label=label)
        _digest(
            self.accepted_graph_outcome_sha256,
            label="accepted_graph_outcome_sha256",
            optional=True,
        )
        _digest(self.accepted_steps_sha256, label="accepted_steps_sha256", optional=True)
        _digest(
            self.terminal_publication_receipt_sha256,
            label="terminal_publication_receipt_sha256",
            optional=True,
        )
        _digest(
            self.publication_receipt_sha256,
            label="publication_receipt_sha256",
            optional=True,
        )
        created = _instant(self.created_at, label="created_at")
        updated = _instant(self.updated_at, label="updated_at")
        expires = _instant(self.expires_at, label="expires_at")
        closed = _instant(self.closed_at, label="closed_at", optional=True)
        if (created, updated, expires, closed) != (
            self.created_at,
            self.updated_at,
            self.expires_at,
            self.closed_at,
        ):
            raise CompareCurrentFileWebGraphError("graph timestamps must already be canonical")
        created_dt = datetime.fromisoformat(self.created_at)
        updated_dt = datetime.fromisoformat(self.updated_at)
        expires_dt = datetime.fromisoformat(self.expires_at)
        if updated_dt < created_dt or expires_dt <= created_dt:
            raise CompareCurrentFileWebGraphError("graph temporal frame is invalid")
        if expires_dt - created_dt > timedelta(hours=WORK_ITEM_TTL_HOURS):
            raise CompareCurrentFileWebGraphError("graph exceeds the bounded Work Item TTL")
        if tuple(step.kind for step in self.steps) != _FIXED_STEP_ORDER:
            raise CompareCurrentFileWebGraphError("WorkGraph must contain the exact three-step journey")
        if any(step.graph_id != self.id for step in self.steps):
            raise CompareCurrentFileWebGraphError("step belongs to a different WorkGraph")
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise CompareCurrentFileWebGraphError("WorkGraph step IDs must be unique")
        self._validate_lifecycle()
        if len(_canonical_json(self.payload()).encode("ascii")) > _MAX_SERIALIZED_BYTES:
            raise CompareCurrentFileWebGraphError("WorkGraph exceeds the serialized byte budget")

    def _validate_lifecycle(self) -> None:
        completion_values = (
            self.outcome_status,
            self.outcome_reason,
            self.publication_assistant_message_id,
            self.accepted_graph_outcome_sha256,
            self.accepted_steps_sha256,
            self.terminal_publication_receipt_sha256,
            self.publication_receipt_sha256,
        )
        file_step, web_step, synthesis = self.steps
        reads = (file_step, web_step)
        if synthesis.state is not CompareCurrentFileWebStepState.PENDING:
            if not all(step.settled for step in reads):
                raise CompareCurrentFileWebGraphError("primary synthesis cannot precede settled reads")
            usable_reads = tuple(
                step
                for step in reads
                if step.state
                in {
                    CompareCurrentFileWebStepState.COMPLETE,
                    CompareCurrentFileWebStepState.PARTIAL,
                }
            )
            if synthesis.state is CompareCurrentFileWebStepState.COMPLETE and not all(
                step.state is CompareCurrentFileWebStepState.COMPLETE for step in reads
            ):
                raise CompareCurrentFileWebGraphError(
                    "complete synthesis requires two complete read outcomes"
                )
            if synthesis.state is CompareCurrentFileWebStepState.PARTIAL and not usable_reads:
                raise CompareCurrentFileWebGraphError(
                    "partial synthesis requires usable process-owned read evidence"
                )
        if self.state is CompareCurrentFileWebGraphState.ACTIVE:
            if self.revision > COMPARE_CURRENT_FILE_WEB_MAX_ACTIVE_REVISION:
                raise CompareCurrentFileWebGraphError(
                    "active WorkGraph must reserve one revision for deterministic retirement"
                )
            if self.transition in {
                CompareCurrentFileWebGraphTransition.TERMINAL_SETTLED,
                CompareCurrentFileWebGraphTransition.PUBLICATION_COMMITTED,
            }:
                raise CompareCurrentFileWebGraphError("active WorkGraph cannot claim closure")
            if self.closed_at is not None or any(value is not None for value in completion_values):
                raise CompareCurrentFileWebGraphError("active WorkGraph cannot carry completion fields")
            if self.expires_at <= self.updated_at:
                raise CompareCurrentFileWebGraphError("active WorkGraph must remain inside its deadline")
            if self.transition is CompareCurrentFileWebGraphTransition.ADMITTED and (
                self.revision != 1
                or any(step.state is not CompareCurrentFileWebStepState.PENDING for step in self.steps)
            ):
                raise CompareCurrentFileWebGraphError(
                    "admitted WorkGraph must be the pristine first revision"
                )
            return
        if self.state is CompareCurrentFileWebGraphState.TERMINAL:
            if (
                self.transition is not CompareCurrentFileWebGraphTransition.TERMINAL_SETTLED
                or self.closed_at != self.updated_at
                or self.outcome_status is None
                or self.outcome_status is CompareCurrentFileWebGraphOutcomeStatus.COMPLETE
                or self.outcome_reason is None
                or self.publication_assistant_message_id is None
                or self.terminal_publication_receipt_sha256 is None
                or self.publication_receipt_sha256 is not None
                or self.accepted_graph_outcome_sha256 is None
                or self.accepted_steps_sha256 is None
            ):
                raise CompareCurrentFileWebGraphError(
                    "terminal WorkGraph lacks exact non-completion publication proof"
                )
            expected = self.terminal_disposition(
                evidence_not_replayable=(
                    self.outcome_reason is CompareCurrentFileWebGraphOutcomeReason.EVIDENCE_NOT_REPLAYABLE
                ),
                conversation_archived=(
                    self.outcome_reason is CompareCurrentFileWebGraphOutcomeReason.CONVERSATION_ARCHIVED
                ),
                cancelled=(self.outcome_reason is CompareCurrentFileWebGraphOutcomeReason.CANCELLED),
                expired=(self.outcome_reason is CompareCurrentFileWebGraphOutcomeReason.EXPIRED),
            )
            if expected != (self.outcome_status, self.outcome_reason):
                raise CompareCurrentFileWebGraphError("terminal WorkGraph disposition is stale")
            if self.accepted_steps_sha256 != self.steps_sha256():
                raise CompareCurrentFileWebGraphError("terminal WorkGraph step digest is stale")
            if self.accepted_graph_outcome_sha256 != self.terminal_outcome_sha256(
                status=self.outcome_status,
                reason=self.outcome_reason,
            ):
                raise CompareCurrentFileWebGraphError("terminal WorkGraph outcome digest is stale")
            return
        if (
            self.transition is not CompareCurrentFileWebGraphTransition.PUBLICATION_COMMITTED
            or self.closed_at != self.updated_at
            or self.outcome_status is not CompareCurrentFileWebGraphOutcomeStatus.COMPLETE
            or self.outcome_reason is not CompareCurrentFileWebGraphOutcomeReason.NONE
            or self.publication_assistant_message_id is None
            or self.accepted_graph_outcome_sha256 is None
            or self.accepted_steps_sha256 is None
            or self.terminal_publication_receipt_sha256 is not None
            or self.publication_receipt_sha256 is None
            or any(step.state is not CompareCurrentFileWebStepState.COMPLETE for step in self.steps)
        ):
            raise CompareCurrentFileWebGraphError("completed WorkGraph lacks exact publication proof")
        assert self.accepted_steps_sha256 is not None
        assert self.accepted_graph_outcome_sha256 is not None
        if self.accepted_steps_sha256 != self.steps_sha256():
            raise CompareCurrentFileWebGraphError("completed WorkGraph step digest is stale")
        if self.accepted_graph_outcome_sha256 != self.completion_outcome_sha256():
            raise CompareCurrentFileWebGraphError("completed WorkGraph outcome digest is stale")

    @classmethod
    def admitted(
        cls,
        *,
        user_id: str,
        conversation_id: str,
        anchor_user_message_id: str,
        current_file_raw_object_id: str,
        proposal_sha256: str,
        accepted_plan_sha256: str,
        manifest_sha256: str,
        policy_sha256: str,
        runtime_profile_sha256: str,
        adapter_registry_sha256: str,
        actor_binding_sha256: str,
        conversation_binding_sha256: str,
        current_file_source_identity_sha256: str,
        current_file_content_sha256: str,
        step_input_identities: Mapping[CompareCurrentFileWebStepKind, str],
        step_idempotency_keys: Mapping[CompareCurrentFileWebStepKind, str],
        graph_id: str | None = None,
        now: str | None = None,
        expires_at: str | None = None,
    ) -> CompareCurrentFileWebWorkGraph:
        identifier = graph_id or f"graph_{secrets.token_hex(8)}"
        current = canonical_work_item_instant(
            now or datetime.now(UTC).isoformat(),
            label="now",
        )
        expiry = canonical_work_item_instant(
            expires_at
            or (datetime.fromisoformat(current) + timedelta(hours=WORK_ITEM_TTL_HOURS)).isoformat(),
            label="expires_at",
        )
        if set(step_input_identities) != set(_FIXED_STEP_ORDER):
            raise CompareCurrentFileWebGraphError("every fixed step needs one input identity")
        if set(step_idempotency_keys) != set(_FIXED_STEP_ORDER):
            raise CompareCurrentFileWebGraphError("every fixed step needs one idempotency identity")
        steps = tuple(
            CompareCurrentFileWebGraphStep.pending(
                graph_id=identifier,
                kind=kind,
                input_identity_sha256=step_input_identities[kind],
                idempotency_key_sha256=step_idempotency_keys[kind],
            )
            for kind in _FIXED_STEP_ORDER
        )
        return cls(
            id=identifier,
            user_id=user_id,
            conversation_id=conversation_id,
            anchor_user_message_id=anchor_user_message_id,
            current_file_raw_object_id=current_file_raw_object_id,
            state=CompareCurrentFileWebGraphState.ACTIVE,
            revision=1,
            transition=CompareCurrentFileWebGraphTransition.ADMITTED,
            proposal_sha256=proposal_sha256,
            accepted_plan_sha256=accepted_plan_sha256,
            manifest_sha256=manifest_sha256,
            policy_sha256=policy_sha256,
            runtime_profile_sha256=runtime_profile_sha256,
            adapter_registry_sha256=adapter_registry_sha256,
            actor_binding_sha256=actor_binding_sha256,
            conversation_binding_sha256=conversation_binding_sha256,
            current_file_source_identity_sha256=current_file_source_identity_sha256,
            current_file_content_sha256=current_file_content_sha256,
            created_at=current,
            updated_at=current,
            expires_at=expiry,
            closed_at=None,
            outcome_status=None,
            outcome_reason=None,
            publication_assistant_message_id=None,
            accepted_graph_outcome_sha256=None,
            accepted_steps_sha256=None,
            terminal_publication_receipt_sha256=None,
            publication_receipt_sha256=None,
            steps=steps,
        )

    def step(self, step_id: str) -> CompareCurrentFileWebGraphStep:
        for item in self.steps:
            if item.step_id == step_id:
                return item
        raise CompareCurrentFileWebGraphError("step is not part of the fixed WorkGraph")

    def steps_sha256(self) -> str:
        return _canonical_sha256([step.payload() for step in self.steps])

    def completion_outcome_sha256(self) -> str:
        return _canonical_sha256(
            {
                "schema": "friday.compare-current-file-with-current-web-completion.v1",
                "graph_id": self.id,
                "accepted_plan_sha256": self.accepted_plan_sha256,
                "steps_sha256": self.steps_sha256(),
                "completion_contract": COMPARE_CURRENT_FILE_WEB_COMPLETION_CONTRACT,
                "status": "complete",
            }
        )

    def terminal_disposition(
        self,
        *,
        evidence_not_replayable: bool = False,
        conversation_archived: bool = False,
        cancelled: bool = False,
        expired: bool = False,
    ) -> tuple[CompareCurrentFileWebGraphOutcomeStatus, CompareCurrentFileWebGraphOutcomeReason]:
        """Return the only honest non-completion publication disposition."""

        if self.state is CompareCurrentFileWebGraphState.COMPLETED:
            raise CompareCurrentFileWebGraphError("completed WorkGraph has no terminal fallback")
        if sum((evidence_not_replayable, conversation_archived, cancelled, expired)) > 1:
            raise CompareCurrentFileWebGraphError("terminal retirement reason must be singular")
        if conversation_archived:
            return (
                CompareCurrentFileWebGraphOutcomeStatus.UNAVAILABLE,
                CompareCurrentFileWebGraphOutcomeReason.CONVERSATION_ARCHIVED,
            )
        if cancelled:
            return (
                CompareCurrentFileWebGraphOutcomeStatus.UNAVAILABLE,
                CompareCurrentFileWebGraphOutcomeReason.CANCELLED,
            )
        if expired:
            return (
                CompareCurrentFileWebGraphOutcomeStatus.UNAVAILABLE,
                CompareCurrentFileWebGraphOutcomeReason.EXPIRED,
            )
        reads = self.steps[:2]
        synthesis = self.steps[2]
        if evidence_not_replayable:
            return (
                CompareCurrentFileWebGraphOutcomeStatus.UNAVAILABLE,
                CompareCurrentFileWebGraphOutcomeReason.EVIDENCE_NOT_REPLAYABLE,
            )
        if synthesis.state is CompareCurrentFileWebStepState.PARTIAL:
            return (
                CompareCurrentFileWebGraphOutcomeStatus.PARTIAL,
                CompareCurrentFileWebGraphOutcomeReason.PARTIAL_EVIDENCE,
            )
        if synthesis.state is CompareCurrentFileWebStepState.UNAVAILABLE:
            return (
                CompareCurrentFileWebGraphOutcomeStatus.UNAVAILABLE,
                CompareCurrentFileWebGraphOutcomeReason.CAPABILITY_UNAVAILABLE,
            )
        if synthesis.state is CompareCurrentFileWebStepState.FAILED:
            return (
                CompareCurrentFileWebGraphOutcomeStatus.FAILED,
                CompareCurrentFileWebGraphOutcomeReason.STEP_FAILED,
            )
        if synthesis.state is not CompareCurrentFileWebStepState.PENDING or not all(
            step.settled for step in reads
        ):
            raise CompareCurrentFileWebGraphError("WorkGraph has no honest terminal disposition yet")
        if any(
            step.state
            in {
                CompareCurrentFileWebStepState.COMPLETE,
                CompareCurrentFileWebStepState.PARTIAL,
            }
            for step in reads
        ):
            raise CompareCurrentFileWebGraphError(
                "usable process-private evidence requires primary synthesis"
            )
        if any(step.state is CompareCurrentFileWebStepState.DENIED for step in reads):
            return (
                CompareCurrentFileWebGraphOutcomeStatus.DENIED,
                CompareCurrentFileWebGraphOutcomeReason.AUTHORITY_DENIED,
            )
        if any(step.state is CompareCurrentFileWebStepState.FAILED for step in reads):
            return (
                CompareCurrentFileWebGraphOutcomeStatus.FAILED,
                CompareCurrentFileWebGraphOutcomeReason.STEP_FAILED,
            )
        if any(step.state is CompareCurrentFileWebStepState.UNAVAILABLE for step in reads):
            return (
                CompareCurrentFileWebGraphOutcomeStatus.UNAVAILABLE,
                CompareCurrentFileWebGraphOutcomeReason.CAPABILITY_UNAVAILABLE,
            )
        return (
            CompareCurrentFileWebGraphOutcomeStatus.EMPTY,
            CompareCurrentFileWebGraphOutcomeReason.NO_COMPARABLE_EVIDENCE,
        )

    def terminal_outcome_sha256(
        self,
        *,
        status: CompareCurrentFileWebGraphOutcomeStatus,
        reason: CompareCurrentFileWebGraphOutcomeReason,
    ) -> str:
        if status is CompareCurrentFileWebGraphOutcomeStatus.COMPLETE:
            raise CompareCurrentFileWebGraphError("full completion uses the publication outcome")
        return _canonical_sha256(
            {
                "schema": "friday.compare-current-file-with-current-web-terminal.v1",
                "graph_id": self.id,
                "accepted_plan_sha256": self.accepted_plan_sha256,
                "steps_sha256": self.steps_sha256(),
                "completion_contract": COMPARE_CURRENT_FILE_WEB_COMPLETION_CONTRACT,
                "status": status.value,
                "reason": reason.value,
                "publication_attested": False,
            }
        )

    def terminal_publication_receipt(
        self,
        *,
        evidence_not_replayable: bool = False,
        conversation_archived: bool = False,
        cancelled: bool = False,
        expired: bool = False,
        final_authority_rechecked: bool,
    ) -> CompareCurrentFileWebTerminalPublicationReceipt:
        if self.state is not CompareCurrentFileWebGraphState.ACTIVE:
            raise CompareCurrentFileWebGraphError("terminal receipt must be prepared from active state")
        status, reason = self.terminal_disposition(
            evidence_not_replayable=evidence_not_replayable,
            conversation_archived=conversation_archived,
            cancelled=cancelled,
            expired=expired,
        )
        is_partial = status is CompareCurrentFileWebGraphOutcomeStatus.PARTIAL
        if is_partial and final_authority_rechecked is not True:
            raise CompareCurrentFileWebGraphError(
                "partial publication requires a final source/permission recheck"
            )
        if not is_partial and final_authority_rechecked is not False:
            raise CompareCurrentFileWebGraphError(
                "source-free terminal fallback cannot claim a final evidence recheck"
            )
        return CompareCurrentFileWebTerminalPublicationReceipt(
            graph_id=self.id,
            terminal_revision=self.revision + 1,
            accepted_plan_sha256=self.accepted_plan_sha256,
            status=status,
            reason=reason,
            graph_outcome_sha256=self.terminal_outcome_sha256(
                status=status,
                reason=reason,
            ),
            steps_sha256=self.steps_sha256(),
            model_spoke=is_partial,
            evidence_cited=is_partial,
            final_authority_rechecked=final_authority_rechecked,
            completion_claimed=False,
        )

    def publication_receipt(
        self,
        *,
        final_authority_rechecked: bool,
    ) -> CompareCurrentFileWebPublicationReceipt:
        if self.state is not CompareCurrentFileWebGraphState.ACTIVE:
            raise CompareCurrentFileWebGraphError("publication receipt must be prepared from active state")
        if any(step.state is not CompareCurrentFileWebStepState.COMPLETE for step in self.steps):
            raise CompareCurrentFileWebGraphError("publication receipt requires all fixed steps complete")
        return CompareCurrentFileWebPublicationReceipt(
            graph_id=self.id,
            completed_revision=self.revision + 1,
            accepted_plan_sha256=self.accepted_plan_sha256,
            graph_outcome_sha256=self.completion_outcome_sha256(),
            steps_sha256=self.steps_sha256(),
            final_authority_rechecked=final_authority_rechecked,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "schema": COMPARE_CURRENT_FILE_WEB_WORK_GRAPH_SCHEMA,
            "id": self.id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "anchor_user_message_id": self.anchor_user_message_id,
            "current_file_raw_object_id": self.current_file_raw_object_id,
            "state": self.state.value,
            "revision": self.revision,
            "transition": self.transition.value,
            "proposal_sha256": self.proposal_sha256,
            "accepted_plan_sha256": self.accepted_plan_sha256,
            "manifest_sha256": self.manifest_sha256,
            "policy_sha256": self.policy_sha256,
            "runtime_profile_sha256": self.runtime_profile_sha256,
            "adapter_registry_sha256": self.adapter_registry_sha256,
            "actor_binding_sha256": self.actor_binding_sha256,
            "conversation_binding_sha256": self.conversation_binding_sha256,
            "current_file_source_identity_sha256": self.current_file_source_identity_sha256,
            "current_file_content_sha256": self.current_file_content_sha256,
            "completion_contract": COMPARE_CURRENT_FILE_WEB_COMPLETION_CONTRACT,
            "fallback_owner": COMPARE_CURRENT_FILE_WEB_FALLBACK_OWNER,
            "publication_owner": COMPARE_CURRENT_FILE_WEB_PUBLICATION_OWNER,
            "max_attempts": COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "closed_at": self.closed_at,
            "outcome_status": None if self.outcome_status is None else self.outcome_status.value,
            "outcome_reason": None if self.outcome_reason is None else self.outcome_reason.value,
            "publication_assistant_message_id": self.publication_assistant_message_id,
            "accepted_graph_outcome_sha256": self.accepted_graph_outcome_sha256,
            "accepted_steps_sha256": self.accepted_steps_sha256,
            "terminal_publication_receipt_sha256": self.terminal_publication_receipt_sha256,
            "publication_receipt_sha256": self.publication_receipt_sha256,
            "steps": [step.payload() for step in self.steps],
        }

    def canonical_sha256(self) -> str:
        return _canonical_sha256(self.payload())

    @classmethod
    def from_storage_rows(
        cls,
        graph: Mapping[str, Any],
        steps: Sequence[Mapping[str, Any]],
    ) -> CompareCurrentFileWebWorkGraph:
        try:
            parsed_steps = tuple(
                CompareCurrentFileWebGraphStep(
                    graph_id=str(row["graph_id"]),
                    step_id=str(row["step_id"]),
                    kind=CompareCurrentFileWebStepKind(str(row["kind"])),
                    capability_id=str(row["capability_id"]),
                    security_id=(None if row["security_id"] is None else str(row["security_id"])),
                    adapter_id=(None if row["adapter_id"] is None else str(row["adapter_id"])),
                    evidence_replayability=str(row["evidence_replayability"]),
                    depends_on=tuple(json.loads(str(row["depends_on_json"]))),
                    parallel_group=(None if row["parallel_group"] is None else str(row["parallel_group"])),
                    input_identity_sha256=str(row["input_identity_sha256"]),
                    idempotency_key_sha256=str(row["idempotency_key_sha256"]),
                    state=CompareCurrentFileWebStepState(str(row["state"])),
                    attempt=int(row["attempt"]),
                    outcome_sha256=(None if row["outcome_sha256"] is None else str(row["outcome_sha256"])),
                    prior_outcome_sha256=(
                        None if row["prior_outcome_sha256"] is None else str(row["prior_outcome_sha256"])
                    ),
                    recovery_review_sha256=(
                        None if row["recovery_review_sha256"] is None else str(row["recovery_review_sha256"])
                    ),
                    recovery_context_sha256=(
                        None
                        if row["recovery_context_sha256"] is None
                        else str(row["recovery_context_sha256"])
                    ),
                    evidence_identity_sha256=(
                        None
                        if row["evidence_identity_sha256"] is None
                        else str(row["evidence_identity_sha256"])
                    ),
                    authority_rechecked=bool(row["authority_rechecked"]),
                    verified=bool(row["verified"]),
                    started_at=None if row["started_at"] is None else str(row["started_at"]),
                    settled_at=None if row["settled_at"] is None else str(row["settled_at"]),
                )
                for row in steps
            )
            return cls(
                id=str(graph["id"]),
                user_id=str(graph["user_id"]),
                conversation_id=str(graph["conversation_id"]),
                anchor_user_message_id=str(graph["anchor_user_message_id"]),
                current_file_raw_object_id=str(graph["current_file_raw_object_id"]),
                state=CompareCurrentFileWebGraphState(str(graph["state"])),
                revision=int(graph["revision"]),
                transition=CompareCurrentFileWebGraphTransition(str(graph["transition"])),
                proposal_sha256=str(graph["proposal_sha256"]),
                accepted_plan_sha256=str(graph["accepted_plan_sha256"]),
                manifest_sha256=str(graph["manifest_sha256"]),
                policy_sha256=str(graph["policy_sha256"]),
                runtime_profile_sha256=str(graph["runtime_profile_sha256"]),
                adapter_registry_sha256=str(graph["adapter_registry_sha256"]),
                actor_binding_sha256=str(graph["actor_binding_sha256"]),
                conversation_binding_sha256=str(graph["conversation_binding_sha256"]),
                current_file_source_identity_sha256=str(graph["current_file_source_identity_sha256"]),
                current_file_content_sha256=str(graph["current_file_content_sha256"]),
                created_at=str(graph["created_at"]),
                updated_at=str(graph["updated_at"]),
                expires_at=str(graph["expires_at"]),
                closed_at=None if graph["closed_at"] is None else str(graph["closed_at"]),
                outcome_status=(
                    None
                    if graph["outcome_status"] is None
                    else CompareCurrentFileWebGraphOutcomeStatus(str(graph["outcome_status"]))
                ),
                outcome_reason=(
                    None
                    if graph["outcome_reason"] is None
                    else CompareCurrentFileWebGraphOutcomeReason(str(graph["outcome_reason"]))
                ),
                publication_assistant_message_id=(
                    None
                    if graph["publication_assistant_message_id"] is None
                    else str(graph["publication_assistant_message_id"])
                ),
                accepted_graph_outcome_sha256=(
                    None
                    if graph["accepted_graph_outcome_sha256"] is None
                    else str(graph["accepted_graph_outcome_sha256"])
                ),
                accepted_steps_sha256=(
                    None if graph["accepted_steps_sha256"] is None else str(graph["accepted_steps_sha256"])
                ),
                terminal_publication_receipt_sha256=(
                    None
                    if graph["terminal_publication_receipt_sha256"] is None
                    else str(graph["terminal_publication_receipt_sha256"])
                ),
                publication_receipt_sha256=(
                    None
                    if graph["publication_receipt_sha256"] is None
                    else str(graph["publication_receipt_sha256"])
                ),
                steps=parsed_steps,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CompareCurrentFileWebGraphError("stored WorkGraph rows are invalid") from exc


def attach_compare_current_file_web_publication_receipt(
    metadata: Mapping[str, Any] | None,
    receipt: CompareCurrentFileWebPublicationReceipt,
) -> dict[str, Any]:
    if type(receipt) is not CompareCurrentFileWebPublicationReceipt:
        raise CompareCurrentFileWebGraphError("publication receipt must use the exact contract")
    result = dict(metadata or {})
    if COMPARE_CURRENT_FILE_WEB_PUBLICATION_METADATA_KEY in result:
        raise CompareCurrentFileWebGraphError("assistant metadata already contains a WorkGraph receipt")
    if COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY in result:
        raise CompareCurrentFileWebGraphError(
            "one assistant cannot claim complete and terminal WorkGraph publication"
        )
    result[COMPARE_CURRENT_FILE_WEB_PUBLICATION_METADATA_KEY] = receipt.payload()
    try:
        encoded = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CompareCurrentFileWebGraphError(
            "assistant metadata cannot carry the WorkGraph receipt"
        ) from exc
    if len(encoded) > _MAX_ASSISTANT_METADATA_BYTES:
        raise CompareCurrentFileWebGraphError("assistant metadata exceeds the receipt budget")
    return result


def load_compare_current_file_web_publication_receipt(
    metadata: str | Mapping[str, Any],
    *,
    expected: CompareCurrentFileWebPublicationReceipt | None = None,
) -> CompareCurrentFileWebPublicationReceipt:
    item = _closed_json_object(
        metadata,
        maximum=_MAX_ASSISTANT_METADATA_BYTES,
        label="assistant metadata",
    )
    raw = item.get(COMPARE_CURRENT_FILE_WEB_PUBLICATION_METADATA_KEY)
    if not isinstance(raw, Mapping):
        raise CompareCurrentFileWebGraphError("assistant metadata has no WorkGraph receipt")
    receipt = CompareCurrentFileWebPublicationReceipt.parse(raw)
    if expected is not None and receipt != expected:
        raise CompareCurrentFileWebGraphError(
            "assistant WorkGraph receipt does not match expected publication"
        )
    return receipt


def attach_compare_current_file_web_terminal_publication_receipt(
    metadata: Mapping[str, Any] | None,
    receipt: CompareCurrentFileWebTerminalPublicationReceipt,
) -> dict[str, Any]:
    if type(receipt) is not CompareCurrentFileWebTerminalPublicationReceipt:
        raise CompareCurrentFileWebGraphError("terminal publication receipt must use the exact contract")
    result = dict(metadata or {})
    if COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY in result:
        raise CompareCurrentFileWebGraphError(
            "assistant metadata already contains a terminal WorkGraph receipt"
        )
    if COMPARE_CURRENT_FILE_WEB_PUBLICATION_METADATA_KEY in result:
        raise CompareCurrentFileWebGraphError(
            "one assistant cannot claim terminal and complete WorkGraph publication"
        )
    result[COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY] = receipt.payload()
    try:
        encoded = json.dumps(
            result,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CompareCurrentFileWebGraphError(
            "assistant metadata cannot carry the terminal WorkGraph receipt"
        ) from exc
    if len(encoded) > _MAX_ASSISTANT_METADATA_BYTES:
        raise CompareCurrentFileWebGraphError("assistant metadata exceeds the terminal receipt budget")
    return result


def load_compare_current_file_web_terminal_publication_receipt(
    metadata: str | Mapping[str, Any],
    *,
    expected: CompareCurrentFileWebTerminalPublicationReceipt | None = None,
) -> CompareCurrentFileWebTerminalPublicationReceipt:
    item = _closed_json_object(
        metadata,
        maximum=_MAX_ASSISTANT_METADATA_BYTES,
        label="assistant metadata",
    )
    raw = item.get(COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY)
    if not isinstance(raw, Mapping):
        raise CompareCurrentFileWebGraphError("assistant metadata has no terminal WorkGraph receipt")
    receipt = CompareCurrentFileWebTerminalPublicationReceipt.parse(raw)
    if expected is not None and receipt != expected:
        raise CompareCurrentFileWebGraphError(
            "assistant terminal WorkGraph receipt does not match expected publication"
        )
    return receipt


__all__ = [
    "COMPARE_CURRENT_FILE_WEB_COMPLETION_CONTRACT",
    "COMPARE_CURRENT_FILE_WEB_ARCHIVED_RESPONSE",
    "COMPARE_CURRENT_FILE_WEB_CANCELLED_RESPONSE",
    "COMPARE_CURRENT_FILE_WEB_EXPIRED_RESPONSE",
    "COMPARE_CURRENT_FILE_WEB_RESTART_UNAVAILABLE_RESPONSE",
    "COMPARE_CURRENT_FILE_WEB_FALLBACK_OWNER",
    "COMPARE_CURRENT_FILE_WEB_MAX_ACTIVE_REVISION",
    "COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS",
    "COMPARE_CURRENT_FILE_WEB_PUBLICATION_METADATA_KEY",
    "COMPARE_CURRENT_FILE_WEB_PUBLICATION_OWNER",
    "COMPARE_CURRENT_FILE_WEB_PUBLICATION_RECEIPT_SCHEMA",
    "COMPARE_CURRENT_FILE_WEB_STEP_OUTCOME_SCHEMA",
    "COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY",
    "COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_RECEIPT_SCHEMA",
    "COMPARE_CURRENT_FILE_WEB_WORK_GRAPH_SCHEMA",
    "EVIDENCE_PARALLEL_GROUP",
    "FILE_CURRENT_READ_CAPABILITY_ID",
    "FILE_READ_STEP_ID",
    "PRIMARY_SYNTHESIS_CAPABILITY_ID",
    "PRIMARY_SYNTHESIS_STEP_ID",
    "WEB_READ_STEP_ID",
    "WEB_SEARCH_CURRENT_CAPABILITY_ID",
    "WEB_SEARCH_CURRENT_ADAPTER_ID",
    "WEB_SEARCH_CURRENT_SECURITY_ID",
    "CompareCurrentFileWebGraphError",
    "CompareCurrentFileWebGraphOutcomeReason",
    "CompareCurrentFileWebGraphOutcomeStatus",
    "CompareCurrentFileWebGraphState",
    "CompareCurrentFileWebGraphStep",
    "CompareCurrentFileWebGraphTransition",
    "CompareCurrentFileWebPublicationReceipt",
    "CompareCurrentFileWebPlanStepBinding",
    "CompareCurrentFileWebTerminalPublicationReceipt",
    "CompareCurrentFileWebStepKind",
    "CompareCurrentFileWebStepState",
    "CompareCurrentFileWebWorkGraph",
    "attach_compare_current_file_web_publication_receipt",
    "attach_compare_current_file_web_terminal_publication_receipt",
    "bind_validated_plan_to_compare_current_file_web_graph",
    "load_compare_current_file_web_publication_receipt",
    "load_compare_current_file_web_terminal_publication_receipt",
]
