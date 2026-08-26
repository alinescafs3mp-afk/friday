"""Body-free production baseline for semantic-supervisor rollout decisions."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Mapping
from typing import Any

from friday.interaction_control_plane.turn_trace import TurnTrace
from friday.orchestration.supervisor_contracts import canonical_sha256
from friday.orchestration.supervisor_trace_join import (
    SUPERVISOR_TRACE_EVENT,
    SUPERVISOR_TRACE_JOIN_SCHEMA,
)

SUPERVISOR_PRODUCTION_BASELINE_SCHEMA = "friday.semantic-supervisor-production-baseline.v1"
SUPERVISOR_PRODUCTION_BASELINE_KIND = "joined_body_free_production_candidate"

_MAX_ROWS = 100_000
_MAX_JSON_BYTES = 32_768
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


class SupervisorBaselineError(ValueError):
    """A baseline input is malformed or outside the body-free contract."""


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
            "malformed_turn_traces": malformed_traces,
            "malformed_joined_events": malformed_events,
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
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


__all__ = [
    "SUPERVISOR_PRODUCTION_BASELINE_KIND",
    "SUPERVISOR_PRODUCTION_BASELINE_SCHEMA",
    "SupervisorBaselineError",
    "build_production_baseline",
]
