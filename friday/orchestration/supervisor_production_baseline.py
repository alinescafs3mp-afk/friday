"""Body-free production baseline for semantic-supervisor rollout decisions."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from friday.interaction_control_plane.turn_trace import TurnTrace
from friday.orchestration.supervisor_contracts import SupervisorMode, TaskClass, canonical_sha256
from friday.orchestration.supervisor_observation import SUPERVISOR_OBSERVATION_SCHEMA
from friday.orchestration.supervisor_trace_join import (
    SUPERVISOR_TRACE_EVENT,
    SUPERVISOR_TRACE_JOIN_SCHEMA,
    PrimaryTraceProjection,
)

SUPERVISOR_PRODUCTION_BASELINE_SCHEMA = "friday.semantic-supervisor-production-baseline.v2"
SUPERVISOR_PRODUCTION_BASELINE_KIND = "joined_body_free_production_candidate"
SUPERVISOR_PROMOTED_PRODUCT_EVENT = "semantic_supervisor.promoted_product"
SUPERVISOR_PROMOTED_PRODUCT_EVENT_SCHEMA = "friday.semantic-supervisor-promoted-product-observation.v1"
SUPERVISOR_PRODUCT_WINDOW_SCHEMA = "friday.semantic-supervisor-product-window.v1"

_MAX_ROWS = 100_000
_MAX_JSON_BYTES = 32_768
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_SKIP_REASONS = frozenset(
    {
        "mode_off",
        "exact_lane",
        "small_talk",
        "ordinary_dialogue",
        "established_file_read",
        "task_not_allowlisted",
        "secondary_unavailable",
        "special_surface",
        "evidence_unavailable",
        "secret_material",
        "binding_unavailable",
        "malformed_proposal",
        "policy_rejected",
        "workload_disallowed",
        "saturated",
        "timeout",
        "none",
    }
)
_SAFE_PARSE_STATUSES = frozenset({"skipped", "not_received", "parsed", "malformed"})
_SAFE_PRIMARY_COMPLETION = frozenset(
    {"not_evaluated", "incomplete", "waiting_for_input", "partial", "complete", "failed", "uncertain"}
)
_SAFE_PRIMARY_PUBLICATION = frozenset(
    {"not_attempted", "suppressed", "assistant_committed", "failed", "denied"}
)
_SHADOW_OBSERVATION_KEYS = frozenset(
    {
        "schema",
        "supervisor_mode",
        "requested_mode",
        "effective_mode",
        "promotion_admitted",
        "invoked",
        "skip_reason",
        "policy_id",
        "policy_sha256",
        "accepted_profile_id",
        "manifest_digest",
        "supervisor_input_digest",
        "proposal_digest",
        "proposal_parse_status",
        "policy_verdict",
        "policy_reason",
        "task_class",
        "step_count",
        "effect_classes",
        "fallback_owner",
        "publication_owner",
        "endpoint_health_class",
        "current_route",
        "runtime_owner",
        "planner_latency_bucket",
        "review_latency_bucket",
        "primary_trace_digest",
        "capability_outcome_classes",
        "completion_verdict",
        "publication_result",
        "authority_rechecked",
        "state_restored",
        "retry_occurred",
    }
)


class SupervisorBaselineError(ValueError):
    """A baseline input is malformed or outside the body-free contract."""


class PromotedObservationEligibility(StrEnum):
    PROMOTED_JOURNEY = "promoted_journey"
    OTHER_TURN = "other_turn"


class PromotedUserVisibleOutcome(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    NO_REGRESSION = "no_regression"
    REGRESSION = "regression"


@dataclass(frozen=True, slots=True)
class PromotedSupervisorProductObservation:
    """Strict body-free join seam for a future assist/canary controller."""

    mode: SupervisorMode
    task_class: TaskClass | None
    eligibility: PromotedObservationEligibility
    primary_trace_sha256: str
    promotion_evidence_sha256: str
    execution_receipt_sha256: str | None
    supervisor_invoked: bool
    user_visible_outcome: PromotedUserVisibleOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.mode, SupervisorMode) or self.mode not in {
            SupervisorMode.ASSIST,
            SupervisorMode.CANARY,
        }:
            raise SupervisorBaselineError("promoted observation mode is not admitted")
        if not isinstance(self.eligibility, PromotedObservationEligibility):
            raise SupervisorBaselineError("promoted observation eligibility is invalid")
        for label, value in (
            ("trace", self.primary_trace_sha256),
            ("promotion evidence", self.promotion_evidence_sha256),
        ):
            if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
                raise SupervisorBaselineError(f"promoted observation {label} digest is invalid")
        if self.execution_receipt_sha256 is not None and (
            not isinstance(self.execution_receipt_sha256, str)
            or _DIGEST_RE.fullmatch(self.execution_receipt_sha256) is None
        ):
            raise SupervisorBaselineError("promoted observation execution receipt is invalid")
        if type(self.supervisor_invoked) is not bool:
            raise SupervisorBaselineError("promoted observation invocation must be boolean")
        if not isinstance(self.user_visible_outcome, PromotedUserVisibleOutcome):
            raise SupervisorBaselineError("promoted observation user outcome is invalid")
        if self.eligibility is PromotedObservationEligibility.PROMOTED_JOURNEY:
            if self.task_class is not TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB:
                raise SupervisorBaselineError("promoted journey task class is invalid")
            if self.execution_receipt_sha256 is None:
                raise SupervisorBaselineError("promoted journey requires an execution receipt")
        elif self.task_class is not None or self.execution_receipt_sha256 is not None:
            raise SupervisorBaselineError(
                "an ineligible turn cannot claim a promoted task or execution receipt"
            )
        if (
            self.eligibility is PromotedObservationEligibility.OTHER_TURN
            and self.user_visible_outcome is not PromotedUserVisibleOutcome.NOT_EVALUATED
        ):
            raise SupervisorBaselineError("an ineligible turn cannot claim a user-visible outcome")

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_PROMOTED_PRODUCT_EVENT_SCHEMA,
            "mode": self.mode.value,
            "task_class": self.task_class.value if self.task_class is not None else None,
            "eligibility": self.eligibility.value,
            "primary_trace_sha256": self.primary_trace_sha256,
            "promotion_evidence_sha256": self.promotion_evidence_sha256,
            "execution_receipt_sha256": self.execution_receipt_sha256,
            "supervisor_invoked": self.supervisor_invoked,
            "user_visible_outcome": self.user_visible_outcome.value,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())

    @classmethod
    def parse(cls, value: Mapping[str, object]) -> PromotedSupervisorProductObservation:
        expected = {
            "schema",
            "mode",
            "task_class",
            "eligibility",
            "primary_trace_sha256",
            "promotion_evidence_sha256",
            "execution_receipt_sha256",
            "supervisor_invoked",
            "user_visible_outcome",
        }
        if type(value) is not dict or set(value) != expected:
            raise SupervisorBaselineError("promoted observation keys do not match")
        if value.get("schema") != SUPERVISOR_PROMOTED_PRODUCT_EVENT_SCHEMA:
            raise SupervisorBaselineError("promoted observation schema is invalid")
        raw_task = value.get("task_class")
        try:
            task_class = None if raw_task is None else TaskClass(raw_task)
            return cls(
                mode=SupervisorMode(value["mode"]),
                task_class=task_class,
                eligibility=PromotedObservationEligibility(value["eligibility"]),
                primary_trace_sha256=value["primary_trace_sha256"],  # type: ignore[arg-type]
                promotion_evidence_sha256=value["promotion_evidence_sha256"],  # type: ignore[arg-type]
                execution_receipt_sha256=value["execution_receipt_sha256"],  # type: ignore[arg-type]
                supervisor_invoked=value["supervisor_invoked"],  # type: ignore[arg-type]
                user_visible_outcome=PromotedUserVisibleOutcome(value["user_visible_outcome"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SupervisorBaselineError("promoted observation is malformed") from exc


def _bounded_json(value: object) -> Mapping[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
    if not encoded or len(encoded) > _MAX_JSON_BYTES:
        return None
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SupervisorBaselineError("promoted observation contains duplicate keys")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> Any:
    raise SupervisorBaselineError("promoted observation contains a non-finite number")


def _bounded_promoted_json(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
    if not encoded or len(encoded) > _MAX_JSON_BYTES:
        return None
    try:
        decoded = json.loads(
            value,
            object_pairs_hook=_closed_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeError, RecursionError, SupervisorBaselineError):
        return None
    return decoded if type(decoded) is dict else None


def _bounded_limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= _MAX_ROWS:
        raise SupervisorBaselineError(f"limit must be between 1 and {_MAX_ROWS}")
    return value


def _closed_string(value: object, allowed: frozenset[str], *, fallback: str) -> str:
    return value if isinstance(value, str) and value in allowed else fallback


def _safe_task_class(value: object) -> str:
    if value in {
        "compare_current_file_with_current_web",
        "compare_archive_with_current_web",
    }:
        return str(value)
    return "unknown"


def _safe_policy_reason(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        return "unknown"
    if any(not (character.isascii() and (character.isalnum() or character == "_")) for character in value):
        return "unknown"
    return value


def _safe_capability_outcome(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or value.count(":") != 1:
        return "unknown"
    if any(
        not (character.isascii() and (character.isalnum() or character in {"_", ":"})) for character in value
    ):
        return "unknown"
    return value


def _load_turn_traces(conn: sqlite3.Connection, *, limit: int) -> tuple[list[TurnTrace], int]:
    malformed = 0
    traces: list[TurnTrace] = []
    rows = conn.execute(
        """SELECT json_extract(metadata_json, '$.interaction_trace') AS trace_json
             FROM messages
            WHERE role='assistant'
              AND json_valid(metadata_json)
              AND json_type(metadata_json, '$.interaction_trace')='object'
            ORDER BY rowid DESC
            LIMIT ?""",
        (limit,),
    )
    for row in rows:
        raw = row[0]
        payload = _bounded_json(raw)
        if payload is None:
            malformed += 1
            continue
        try:
            traces.append(TurnTrace.parse(payload))
        except Exception:
            malformed += 1
    return traces, malformed


def _load_joined_events(
    conn: sqlite3.Connection,
    *,
    limit: int,
) -> tuple[list[Mapping[str, Any]], int]:
    malformed = 0
    events: list[Mapping[str, Any]] = []
    rows = conn.execute(
        """SELECT payload FROM runtime_events
            WHERE event_type=? AND json_valid(payload)
            ORDER BY rowid DESC
            LIMIT ?""",
        (SUPERVISOR_TRACE_EVENT, limit),
    )
    for row in rows:
        payload = _bounded_json(row[0])
        if payload is None or payload.get("schema") != SUPERVISOR_TRACE_JOIN_SCHEMA:
            malformed += 1
            continue
        supervisor = payload.get("supervisor")
        primary = payload.get("primary_trace")
        if not isinstance(supervisor, Mapping) or not isinstance(primary, Mapping):
            malformed += 1
            continue
        # A raw/private extension must not be silently ignored and blessed by
        # the baseline.  The event writer owns these exact outer keys.
        if frozenset(payload) != frozenset({"schema", "supervisor", "primary_trace"}):
            malformed += 1
            continue
        events.append(payload)
    return events, malformed


def _load_promoted_events(
    conn: sqlite3.Connection,
    *,
    limit: int,
) -> tuple[list[PromotedSupervisorProductObservation], int, int]:
    malformed = 0
    rows_by_digest: dict[str, PromotedSupervisorProductObservation] = {}
    duplicate_digests: set[str] = set()
    rows = conn.execute(
        """SELECT payload FROM runtime_events
            WHERE event_type=?
            ORDER BY rowid DESC
            LIMIT ?""",
        (SUPERVISOR_PROMOTED_PRODUCT_EVENT, limit),
    )
    for row in rows:
        payload = _bounded_promoted_json(row[0])
        if payload is None:
            malformed += 1
            continue
        try:
            event = PromotedSupervisorProductObservation.parse(payload)
        except SupervisorBaselineError:
            malformed += 1
            continue
        digest = event.canonical_sha256()
        if digest in rows_by_digest or digest in duplicate_digests:
            duplicate_digests.add(digest)
            rows_by_digest.pop(digest, None)
            continue
        rows_by_digest[digest] = event
    return list(rows_by_digest.values()), malformed, len(duplicate_digests)


def _trace_index(
    traces: list[TurnTrace],
) -> tuple[dict[str, TurnTrace], int]:
    by_digest: dict[str, TurnTrace] = {}
    duplicate_digests: set[str] = set()
    for trace in traces:
        digest = canonical_sha256(trace.to_payload())
        if digest in by_digest or digest in duplicate_digests:
            duplicate_digests.add(digest)
            by_digest.pop(digest, None)
            continue
        by_digest[digest] = trace
    return by_digest, len(duplicate_digests)


def _failure_class(trace: TurnTrace) -> str:
    return f"{trace.failure_stage.value}:{trace.failure_reason.value}"


def _product_window_sha256(*, stage: str, identities: list[str]) -> str:
    return canonical_sha256(
        {
            "schema": SUPERVISOR_PRODUCT_WINDOW_SCHEMA,
            "stage": stage,
            "joined_observation_sha256s": sorted(identities),
        }
    )


def _trace_product_metrics(traces: list[TurnTrace], *, stage: str) -> dict[str, Any]:
    completion_counts = Counter(trace.completion.value for trace in traces)
    failure_class_counts = Counter(_failure_class(trace) for trace in traces)
    latency_total_ms = sum(trace.budget.latency_ms for trace in traces)
    latency_max_ms = max((trace.budget.latency_ms for trace in traces), default=0)
    return {
        "schema": SUPERVISOR_PRODUCT_WINDOW_SCHEMA,
        "stage": stage,
        "observation_count": len(traces),
        "completion_counts": _counter_payload(completion_counts),
        "complete_count": completion_counts["complete"],
        "failure_class_counts": _counter_payload(failure_class_counts),
        "latency_observation_count": len(traces),
        "latency_total_ms": latency_total_ms,
        "latency_max_ms": latency_max_ms,
        "window_sha256": _product_window_sha256(
            stage=stage,
            identities=[canonical_sha256(trace.to_payload()) for trace in traces],
        ),
    }


def _shadow_product_window(
    events: list[Mapping[str, Any]],
    traces_by_digest: Mapping[str, TurnTrace],
) -> tuple[dict[str, Any], int, int]:
    candidates: dict[str, list[tuple[Mapping[str, Any], TurnTrace]]] = {}
    unmatched = 0
    for event in events:
        supervisor = event.get("supervisor")
        primary = event.get("primary_trace")
        if not isinstance(supervisor, Mapping) or not isinstance(primary, Mapping):
            unmatched += 1
            continue
        trace_digest = primary.get("trace_digest")
        if not isinstance(trace_digest, str):
            unmatched += 1
            continue
        trace = traces_by_digest.get(trace_digest)
        expected_primary = PrimaryTraceProjection.from_trace(trace).payload() if trace else None
        if (
            trace is None
            or set(supervisor) != _SHADOW_OBSERVATION_KEYS
            or supervisor.get("schema") != SUPERVISOR_OBSERVATION_SCHEMA
            or dict(primary) != expected_primary
            or supervisor.get("primary_trace_digest") != trace_digest
            or supervisor.get("effective_mode") != SupervisorMode.SHADOW.value
            or supervisor.get("promotion_admitted") is not False
            or supervisor.get("fallback_owner") != "primary_only"
            or supervisor.get("publication_owner") != "primary"
            or supervisor.get("runtime_owner") != "unchanged"
        ):
            unmatched += 1
            continue
        candidates.setdefault(trace_digest, []).append((event, trace))

    matched: list[tuple[Mapping[str, Any], TurnTrace]] = []
    duplicate = 0
    for rows in candidates.values():
        if len(rows) == 1:
            matched.extend(rows)
        else:
            duplicate += len(rows) - 1

    eligible = [
        trace
        for event, trace in matched
        if event["supervisor"].get("task_class") == TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB.value
    ]
    invoked = sum(event["supervisor"].get("invoked") is True for event, _trace in matched)
    unnecessary = sum(
        event["supervisor"].get("invoked") is True
        and event["supervisor"].get("task_class") != TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB.value
        for event, _trace in matched
    )
    baseline = _trace_product_metrics(eligible, stage=SupervisorMode.SHADOW.value)
    witness_identities = [
        canonical_sha256(
            {
                "event_sha256": canonical_sha256(dict(event)),
                "trace_sha256": canonical_sha256(trace.to_payload()),
            }
        )
        for event, trace in matched
    ]
    return (
        {
            "schema": SUPERVISOR_PRODUCT_WINDOW_SCHEMA,
            "mode": SupervisorMode.SHADOW.value,
            "production_joined": True,
            "actual_promoted_execution": False,
            "quality_claim": "documented_baseline_failure_only",
            "observation_count": len(matched),
            "joined_trace_count": len(matched),
            "baseline": baseline,
            "readiness_observation_count": len(eligible),
            "call_rate_observation_count": len(matched),
            "supervisor_invocation_count": invoked,
            "unnecessary_supervisor_invocation_count": unnecessary,
            "user_visible_observation_count": len(eligible),
            "user_visible_regression_count": 0,
            "readiness_witness_sha256": _product_window_sha256(
                stage="shadow_readiness",
                identities=witness_identities,
            ),
        },
        unmatched,
        duplicate,
    )


def _promoted_product_windows(
    events: list[PromotedSupervisorProductObservation],
    traces_by_digest: Mapping[str, TurnTrace],
) -> tuple[dict[str, Any], int, int]:
    unmatched = 0
    duplicate = 0
    matched: dict[
        SupervisorMode,
        list[tuple[PromotedSupervisorProductObservation, TurnTrace]],
    ] = {SupervisorMode.ASSIST: [], SupervisorMode.CANARY: []}
    references = Counter(event.primary_trace_sha256 for event in events)
    for event in events:
        if references[event.primary_trace_sha256] > 1:
            continue
        trace = traces_by_digest.get(event.primary_trace_sha256)
        if trace is None:
            unmatched += 1
            continue
        matched[event.mode].append((event, trace))
    duplicate = sum(count - 1 for count in references.values() if count > 1)

    result: dict[str, Any] = {}
    for mode in (SupervisorMode.ASSIST, SupervisorMode.CANARY):
        mode_rows = matched[mode]
        eligible_rows = [
            (event, trace)
            for event, trace in mode_rows
            if event.eligibility is PromotedObservationEligibility.PROMOTED_JOURNEY
        ]
        metrics = _trace_product_metrics(
            [trace for _event, trace in eligible_rows],
            stage=mode.value,
        )
        evaluated = [
            event
            for event, _trace in eligible_rows
            if event.user_visible_outcome is not PromotedUserVisibleOutcome.NOT_EVALUATED
        ]
        identities = [
            canonical_sha256(
                {
                    "event_sha256": event.canonical_sha256(),
                    "trace_sha256": canonical_sha256(trace.to_payload()),
                }
            )
            for event, trace in mode_rows
        ]
        promotion_evidence = sorted({event.promotion_evidence_sha256 for event, _ in mode_rows})
        result[mode.value] = {
            "schema": SUPERVISOR_PRODUCT_WINDOW_SCHEMA,
            "mode": mode.value,
            "production_joined": True,
            "actual_promoted_execution": True,
            "observation_count": len(mode_rows),
            "joined_trace_count": len(mode_rows),
            "promotion_evidence_count": len(promotion_evidence),
            "promotion_evidence_sha256": (promotion_evidence[0] if len(promotion_evidence) == 1 else None),
            "promoted": metrics,
            "call_rate_observation_count": len(mode_rows),
            "supervisor_invocation_count": sum(event.supervisor_invoked for event, _trace in mode_rows),
            "unnecessary_supervisor_invocation_count": sum(
                event.supervisor_invoked and event.eligibility is PromotedObservationEligibility.OTHER_TURN
                for event, _trace in mode_rows
            ),
            "user_visible_observation_count": len(evaluated),
            "user_visible_regression_count": sum(
                event.user_visible_outcome is PromotedUserVisibleOutcome.REGRESSION for event in evaluated
            ),
            "product_window_sha256": _product_window_sha256(
                stage=mode.value,
                identities=identities,
            ),
        }
    return result, unmatched, duplicate


def _counter_payload(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items()))


def build_production_baseline(
    conn: sqlite3.Connection,
    *,
    limit: int = 10_000,
) -> dict[str, Any]:
    """Aggregate only typed traces and joined body-free shadow events.

    The report is a candidate for operator review.  It deliberately cannot
    grant promotion or claim that a sampling window is representative.
    """

    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("baseline requires a sqlite3 connection")
    bounded = _bounded_limit(limit)
    traces, malformed_traces = _load_turn_traces(conn, limit=bounded)
    events, malformed_events = _load_joined_events(conn, limit=bounded)
    promoted_events, malformed_promoted_events, duplicate_promoted_events = _load_promoted_events(
        conn, limit=bounded
    )
    traces_by_digest, duplicate_trace_digests = _trace_index(traces)
    shadow_product, unmatched_shadow_product, duplicate_shadow_product = _shadow_product_window(
        events, traces_by_digest
    )
    promoted_product, unmatched_promoted_product, duplicate_promoted_joins = _promoted_product_windows(
        promoted_events,
        traces_by_digest,
    )
    duplicate_promoted_events += duplicate_promoted_joins

    intents: Counter[str] = Counter()
    playbooks: Counter[str] = Counter()
    completions: Counter[str] = Counter()
    publications: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    trace_authority = 0
    trace_partial = 0
    trace_restored = 0
    for trace in traces:
        intents[trace.intent.value] += 1
        playbooks[trace.playbook.value] += 1
        completions[trace.completion.value] += 1
        publications[trace.publication.value] += 1
        failures[f"{trace.failure_stage.value}:{trace.failure_reason.value}"] += 1
        trace_authority += int(trace.authority_rechecked)
        trace_partial += int(trace.partial_coverage)
        trace_restored += int(trace.state_restored)

    tasks: Counter[str] = Counter()
    skips: Counter[str] = Counter()
    parses: Counter[str] = Counter()
    policies: Counter[str] = Counter()
    latencies: Counter[str] = Counter()
    actual_completion: Counter[str] = Counter()
    actual_publication: Counter[str] = Counter()
    actual_capabilities: Counter[str] = Counter()
    invoked = 0
    admitted = 0
    final_rechecks = 0
    restored = 0
    retried = 0
    for event in events:
        supervisor = event["supervisor"]
        primary = event["primary_trace"]
        assert isinstance(supervisor, Mapping)
        assert isinstance(primary, Mapping)
        task = _safe_task_class(supervisor.get("task_class"))
        skip = _closed_string(supervisor.get("skip_reason"), _SAFE_SKIP_REASONS, fallback="unknown")
        parse = _closed_string(
            supervisor.get("proposal_parse_status"),
            _SAFE_PARSE_STATUSES,
            fallback="unknown",
        )
        policy = _safe_policy_reason(supervisor.get("policy_reason"))
        latency = _safe_policy_reason(supervisor.get("planner_latency_bucket"))
        completion = _closed_string(
            primary.get("completion"),
            _SAFE_PRIMARY_COMPLETION,
            fallback="unknown",
        )
        publication = _closed_string(
            primary.get("publication"),
            _SAFE_PRIMARY_PUBLICATION,
            fallback="unknown",
        )
        tasks[task] += 1
        skips[skip] += 1
        parses[parse] += 1
        policies[policy] += 1
        latencies[latency] += 1
        actual_completion[completion] += 1
        actual_publication[publication] += 1
        invoked += int(supervisor.get("invoked") is True)
        admitted += int(policy == "admitted")
        final_rechecks += int(primary.get("authority_rechecked") is True)
        restored += int(primary.get("state_restored") is True)
        retried += int(primary.get("retry_occurred") is True)
        capabilities = primary.get("capability_outcomes")
        if isinstance(capabilities, list):
            for value in capabilities[:32]:
                safe_value = _safe_capability_outcome(value)
                actual_capabilities[safe_value] += 1

    report: dict[str, Any] = {
        "schema": SUPERVISOR_PRODUCTION_BASELINE_SCHEMA,
        "evidence": {
            "kind": SUPERVISOR_PRODUCTION_BASELINE_KIND,
            "body_free": True,
            "production_acceptance": False,
            "acceptance_authority": "operator_review_required",
            "representative_window_attested": False,
            "promotion_authority": False,
        },
        "sample": {
            "limit": bounded,
            "turn_traces": len(traces),
            "joined_supervisor_events": len(events),
            "promoted_product_events": len(promoted_events),
            "malformed_turn_traces": malformed_traces,
            "malformed_joined_events": malformed_events,
            "malformed_promoted_product_events": malformed_promoted_events,
            "duplicate_turn_trace_digests": duplicate_trace_digests,
            "duplicate_shadow_product_events": duplicate_shadow_product,
            "duplicate_promoted_product_events": duplicate_promoted_events,
            "unmatched_shadow_product_events": unmatched_shadow_product,
            "unmatched_promoted_product_events": unmatched_promoted_product,
        },
        "primary_baseline": {
            "intent_counts": _counter_payload(intents),
            "playbook_counts": _counter_payload(playbooks),
            "completion_counts": _counter_payload(completions),
            "publication_counts": _counter_payload(publications),
            "failure_counts": _counter_payload(failures),
            "authority_rechecked_count": trace_authority,
            "partial_coverage_count": trace_partial,
            "state_restored_count": trace_restored,
        },
        "supervisor_join": {
            "task_counts": _counter_payload(tasks),
            "skip_counts": _counter_payload(skips),
            "parse_counts": _counter_payload(parses),
            "policy_reason_counts": _counter_payload(policies),
            "planner_latency_bucket_counts": _counter_payload(latencies),
            "actual_completion_counts": _counter_payload(actual_completion),
            "actual_publication_counts": _counter_payload(actual_publication),
            "actual_capability_outcome_counts": _counter_payload(actual_capabilities),
            "invoked_count": invoked,
            "admitted_count": admitted,
            "final_authority_rechecked_count": final_rechecks,
            "state_restored_count": restored,
            "retry_occurred_count": retried,
        },
        "product_windows": {
            "shadow_readiness": shadow_product,
            "promoted_execution": promoted_product,
        },
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


__all__ = [
    "PromotedObservationEligibility",
    "PromotedSupervisorProductObservation",
    "PromotedUserVisibleOutcome",
    "SUPERVISOR_PRODUCTION_BASELINE_KIND",
    "SUPERVISOR_PRODUCTION_BASELINE_SCHEMA",
    "SUPERVISOR_PRODUCT_WINDOW_SCHEMA",
    "SUPERVISOR_PROMOTED_PRODUCT_EVENT",
    "SUPERVISOR_PROMOTED_PRODUCT_EVENT_SCHEMA",
    "SupervisorBaselineError",
    "build_production_baseline",
]
