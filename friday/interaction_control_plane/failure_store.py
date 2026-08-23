"""Durable privacy-safe traces for turns that fail before assistant commit.

Committed turns keep their :class:`TurnTrace` beside the owned assistant row.
A turn which fails before that row exists has no such owner, so it is retained
here in a bounded person-scoped store.  Only closed structural fields and keyed
digests are serialized; prompts, queries, paths, exception text and provider
payloads never cross this boundary.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from friday.interaction_control_plane.failure_schema import FailureEntrypoint, FailureRoute
from friday.interaction_control_plane.runtime_trace import build_direct_trace, load_trace_namespace_key
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
    PublicationStatus,
    TurnTrace,
)


class FailureStorage(Protocol):
    """Narrow storage seam keeps failure observation outside storage imports."""

    @property
    def conn(self) -> sqlite3.Connection: ...

    def execute(self, *args: Any, **kwargs: Any) -> Any: ...

    def transaction(self) -> AbstractContextManager[sqlite3.Connection]: ...


LOGGER = logging.getLogger(__name__)

INTERACTION_FAILURE_TRACE_SCHEMA = "friday.interaction-failure-trace.v1"
INTERACTION_FAILURE_TTL_DAYS = 90
INTERACTION_FAILURE_PER_USER_CAP = 512
INTERACTION_FAILURE_GLOBAL_CAP = 8_192
INTERACTION_FAILURE_REPORT_LIMIT = 2_048

_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}")
_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}")


@dataclass(slots=True)
class FailureTraceScope:
    """Request-local structural state; bodies and exception values are excluded."""

    user_id: str
    entrypoint: FailureEntrypoint
    conversation_id: str | None = None
    route: FailureRoute = FailureRoute.ADMISSION
    stage: FailureStage = FailureStage.INTENT
    turn_identifier: str = ""
    started_monotonic: float = 0.0
    assistant_message_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, str) or not self.user_id or len(self.user_id) > 512:
            raise ValueError("failure trace requires a bounded user identity")
        if not isinstance(self.entrypoint, FailureEntrypoint):
            raise ValueError("failure trace requires a closed entrypoint")
        if self.conversation_id is not None and _CONVERSATION_ID_RE.fullmatch(self.conversation_id) is None:
            self.conversation_id = None
        if not self.turn_identifier:
            self.turn_identifier = f"failure-turn:{uuid.uuid4().hex}"
        if not self.started_monotonic:
            self.started_monotonic = time.monotonic()

    def bind_conversation(self, conversation_id: object) -> None:
        candidate = str(conversation_id or "")
        if _CONVERSATION_ID_RE.fullmatch(candidate) is not None:
            self.conversation_id = candidate


_ACTIVE_FAILURE_SCOPE: ContextVar[FailureTraceScope | None] = ContextVar(
    "friday_interaction_failure_scope",
    default=None,
)


@contextmanager
def bind_failure_trace_scope(scope: FailureTraceScope) -> Iterator[FailureTraceScope]:
    """Expose one scope to router/storage hooks for the duration of a turn."""

    if not isinstance(scope, FailureTraceScope):
        raise TypeError("scope must be a FailureTraceScope")
    token = _ACTIVE_FAILURE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _ACTIVE_FAILURE_SCOPE.reset(token)


def observe_failure_route(route: FailureRoute | str) -> None:
    """Set the selected code-owned route without accepting arbitrary labels."""

    scope = _ACTIVE_FAILURE_SCOPE.get()
    if scope is not None:
        scope.route = route if isinstance(route, FailureRoute) else FailureRoute.from_route_value(route)


def observe_failure_stage(stage: FailureStage) -> None:
    """Advance the current structural stage; unknown caller values are ignored."""

    scope = _ACTIVE_FAILURE_SCOPE.get()
    if scope is not None and isinstance(stage, FailureStage) and stage is not FailureStage.NONE:
        scope.stage = stage


def observe_owned_conversation(user_id: object, conversation_id: object) -> None:
    """Bind a newly resolved conversation only when it belongs to this person."""

    scope = _ACTIVE_FAILURE_SCOPE.get()
    if scope is None or str(user_id or "") != scope.user_id:
        return
    scope.bind_conversation(conversation_id)


def observe_owned_message_candidate(
    user_id: object,
    conversation_id: object,
    role: object,
    message_id: object,
) -> None:
    """Remember durable turn/publication candidates without retaining message bodies."""

    scope = _ACTIVE_FAILURE_SCOPE.get()
    if scope is None or str(user_id or "") != scope.user_id:
        return
    scope.bind_conversation(conversation_id)
    candidate = str(message_id or "")
    if _MESSAGE_ID_RE.fullmatch(candidate) is None:
        return
    if role == "user":
        scope.turn_identifier = candidate
    elif role == "assistant":
        scope.assistant_message_id = candidate


def _intent_for_route(route: FailureRoute) -> IntentClass:
    return {
        FailureRoute.FILE_READ: IntentClass.DOCUMENT_WORK,
        FailureRoute.ARCHIVE_READ: IntentClass.DOCUMENT_WORK,
        FailureRoute.WEB_READ: IntentClass.WEB_RESEARCH,
        FailureRoute.SMALL_TALK: IntentClass.SMALL_TALK,
        FailureRoute.ORDINARY_DIALOGUE: IntentClass.ORDINARY_DIALOGUE,
        FailureRoute.EFFECT: IntentClass.EFFECT,
    }.get(route, IntentClass.UNKNOWN)


def _capability_for_route(route: FailureRoute) -> CapabilityClass:
    return {
        FailureRoute.FILE_READ: CapabilityClass.DOCUMENT_RETRIEVAL,
        FailureRoute.ARCHIVE_READ: CapabilityClass.DOCUMENT_RETRIEVAL,
        FailureRoute.WEB_READ: CapabilityClass.WEB_RESEARCH,
        FailureRoute.SMALL_TALK: CapabilityClass.CONVERSATION,
        FailureRoute.ORDINARY_DIALOGUE: CapabilityClass.CONVERSATION,
        FailureRoute.EFFECT: CapabilityClass.OTHER_EFFECT,
    }.get(route, CapabilityClass.OTHER_READ)


def _reason_for_exception(error: BaseException) -> FailureReason | None:
    """Reduce an exception type/status to a closed code without reading its text."""

    if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        return None
    status_code = getattr(error, "status_code", None)
    if status_code in {401, 403} or isinstance(error, PermissionError):
        return FailureReason.AUTHORITY_DENIED
    if isinstance(status_code, int) and 400 <= status_code < 500:
        return FailureReason.INVALID_INPUT
    if isinstance(error, TimeoutError):
        return FailureReason.TIMEOUT
    if isinstance(error, ValueError):
        return FailureReason.INVALID_INPUT
    error_name = type(error).__name__
    if error_name in {"AuthenticationError", "AuthorizationError", "AccountGateClosed"}:
        return FailureReason.AUTHORITY_DENIED
    if "Verification" in error_name or "Verifier" in error_name:
        return FailureReason.VERIFICATION_REJECTED
    return FailureReason.INTERNAL_ERROR


def _stage_for_failure(scope: FailureTraceScope, reason: FailureReason) -> FailureStage:
    if reason is FailureReason.INVALID_INPUT and scope.stage is FailureStage.INTENT:
        return FailureStage.INTENT
    if reason is FailureReason.VERIFICATION_REJECTED:
        return FailureStage.SYNTHESIS_CONTRADICTION
    return scope.stage if scope.stage is not FailureStage.NONE else FailureStage.CAPABILITY


def _assistant_was_committed(storage: FailureStorage, scope: FailureTraceScope) -> bool:
    message_id = scope.assistant_message_id
    if message_id is None:
        return False
    row = storage.execute(
        """SELECT 1 FROM messages
             WHERE id=? AND user_id=? AND role='assistant'
               AND (? IS NULL OR conversation_id=?)
             LIMIT 1""",
        (message_id, scope.user_id, scope.conversation_id, scope.conversation_id),
    ).fetchone()
    return row is not None


def _build_failure_trace(
    storage: FailureStorage,
    scope: FailureTraceScope,
    *,
    reason: FailureReason,
) -> TurnTrace:
    stage = _stage_for_failure(scope, reason)
    capability_outcome = (
        OutcomeStatus.DENIED if reason is FailureReason.AUTHORITY_DENIED else OutcomeStatus.FAILED
    )
    conversation_identifier = scope.conversation_id or f"unbound:{scope.turn_identifier}"
    return build_direct_trace(
        namespace_key=load_trace_namespace_key(storage.conn),
        turn_identifier=scope.turn_identifier,
        conversation_identifier=conversation_identifier,
        intent=_intent_for_route(scope.route),
        playbook=(
            PlaybookClass.LOCATE_AND_EXPLAIN_DOCUMENT
            if scope.route is FailureRoute.ARCHIVE_READ
            else PlaybookClass.DIRECT
        ),
        capability_outcomes=((_capability_for_route(scope.route), capability_outcome),),
        continuation=(
            ContinuationKind.CORRECTION
            if scope.entrypoint is FailureEntrypoint.REGENERATE
            else ContinuationKind.NONE
        ),
        completion=CompletionDecision.FAILED,
        publication=PublicationStatus.NOT_ATTEMPTED,
        failure_stage=stage,
        failure_reason=reason,
        ambiguity_present=False,
        partial_coverage=False,
        state_restored=scope.entrypoint is FailureEntrypoint.REGENERATE,
        latency_ms=min(86_400_000, max(0, int((time.monotonic() - scope.started_monotonic) * 1_000))),
        model_calls=0,
        model_call_accounting=CountAccounting.UNAVAILABLE,
        capability_calls=0,
        capability_call_accounting=CountAccounting.UNAVAILABLE,
        authority_rechecked=reason is FailureReason.AUTHORITY_DENIED,
    )


def _trim_failure_store(conn: sqlite3.Connection, *, user_id: str, now: str) -> None:
    conn.execute("DELETE FROM interaction_failure_traces WHERE expires_at<=?", (now,))
    conn.execute(
        """DELETE FROM interaction_failure_traces
             WHERE id IN (
                 SELECT id FROM interaction_failure_traces
                  WHERE user_id=?
                  ORDER BY created_at DESC, id DESC
                  LIMIT -1 OFFSET ?
             )""",
        (user_id, INTERACTION_FAILURE_PER_USER_CAP),
    )
    conn.execute(
        """DELETE FROM interaction_failure_traces
             WHERE id IN (
                 SELECT id FROM interaction_failure_traces
                  ORDER BY created_at DESC, id DESC
                  LIMIT -1 OFFSET ?
             )""",
        (INTERACTION_FAILURE_GLOBAL_CAP,),
    )


def record_precommit_failure(
    storage: FailureStorage,
    scope: FailureTraceScope,
    error: BaseException,
) -> bool:
    """Best-effort retain one pre-commit failure without changing its outcome."""

    if not isinstance(scope, FailureTraceScope) or not isinstance(error, BaseException):
        return False
    try:
        reason = _reason_for_exception(error)
        if reason is None:
            return False
        if _assistant_was_committed(storage, scope):
            return False
        trace = _build_failure_trace(storage, scope, reason=reason)
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat(timespec="seconds")
        expires_at = (now_dt + timedelta(days=INTERACTION_FAILURE_TTL_DAYS)).isoformat(timespec="seconds")
        with storage.transaction() as conn:
            owner = conn.execute("SELECT 1 FROM users WHERE id=?", (scope.user_id,)).fetchone()
            if owner is None:
                return False
            conversation_id = scope.conversation_id
            if conversation_id is not None:
                conversation = conn.execute(
                    "SELECT 1 FROM conversations WHERE id=? AND user_id=?",
                    (conversation_id, scope.user_id),
                ).fetchone()
                if conversation is None:
                    conversation_id = None
            cursor = conn.execute(
                """INSERT OR IGNORE INTO interaction_failure_traces(
                       id,user_id,conversation_id,turn_digest,conversation_digest,
                       entrypoint,route,failure_stage,failure_reason,trace_json,
                       created_at,expires_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"itracef_{uuid.uuid4().hex[:16]}",
                    scope.user_id,
                    conversation_id,
                    trace.turn_digest,
                    trace.conversation_digest,
                    scope.entrypoint.value,
                    scope.route.value,
                    trace.failure_stage.value,
                    trace.failure_reason.value,
                    trace.to_json(),
                    now,
                    expires_at,
                ),
            )
            _trim_failure_store(conn, user_id=scope.user_id, now=now)
        return cursor.rowcount == 1
    except Exception as trace_error:  # noqa: BLE001 - observability cannot break the request
        LOGGER.warning("interaction failure trace omitted (%s)", type(trace_error).__name__)
        return False


def interaction_episode_baseline(
    storage: FailureStorage,
    user_id: str,
    *,
    since: str | None = None,
    limit: int = INTERACTION_FAILURE_REPORT_LIMIT,
) -> dict[str, Any]:
    """Return one bounded, body-free aggregate over committed and failed turns."""

    bounded_limit = max(1, min(int(limit), INTERACTION_FAILURE_REPORT_LIMIT))
    since_value = str(since or "")
    failure_rows = storage.execute(
        """SELECT route,failure_stage,failure_reason,trace_json
             FROM interaction_failure_traces
            WHERE user_id=? AND (?='' OR created_at>=?)
            ORDER BY created_at DESC,id DESC LIMIT ?""",
        (user_id, since_value, since_value, bounded_limit),
    ).fetchall()
    committed_rows = storage.execute(
        """SELECT json_extract(metadata_json,'$.interaction_trace') AS trace_json
             FROM messages
            WHERE user_id=? AND role='assistant'
              AND json_valid(metadata_json)
              AND json_type(metadata_json,'$.interaction_trace')='object'
              AND (?='' OR created_at>=?)
            ORDER BY created_at DESC,id DESC LIMIT ?""",
        (user_id, since_value, since_value, bounded_limit),
    ).fetchall()

    stage_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    completion_counts: Counter[str] = Counter()
    publication_counts: Counter[str] = Counter()
    intent_counts: Counter[str] = Counter()
    signal_counts: Counter[str] = Counter()
    episode_digests: set[str] = set()
    valid_committed = 0
    for row in committed_rows:
        try:
            trace = TurnTrace.parse(str(row["trace_json"] or ""))
        except (TypeError, ValueError):
            continue
        valid_committed += 1
        episode_digests.add(trace.conversation_digest)
        completion_counts[trace.completion.value] += 1
        publication_counts[trace.publication.value] += 1
        intent_counts[trace.intent.value] += 1
        signal_counts.update(
            name
            for name, present in (
                ("ambiguity_present", trace.ambiguity_present),
                ("partial_coverage", trace.partial_coverage),
                ("state_restored", trace.state_restored),
                ("authority_rechecked", trace.authority_rechecked),
            )
            if present
        )
        if trace.failure_stage is not FailureStage.NONE:
            stage_counts[trace.failure_stage.value] += 1
            reason_counts[trace.failure_reason.value] += 1
    valid_failures = 0
    for row in failure_rows:
        try:
            trace = TurnTrace.parse(str(row["trace_json"] or ""))
        except (TypeError, ValueError):
            continue
        valid_failures += 1
        episode_digests.add(trace.conversation_digest)
        route_counts[str(row["route"])] += 1
        stage_counts[trace.failure_stage.value] += 1
        reason_counts[trace.failure_reason.value] += 1
        completion_counts[trace.completion.value] += 1
        publication_counts[trace.publication.value] += 1
        intent_counts[trace.intent.value] += 1
        signal_counts.update(
            name
            for name, present in (
                ("ambiguity_present", trace.ambiguity_present),
                ("partial_coverage", trace.partial_coverage),
                ("state_restored", trace.state_restored),
                ("authority_rechecked", trace.authority_rechecked),
            )
            if present
        )

    observed = valid_committed + valid_failures
    return {
        "schema": "friday.interaction-episode-baseline.v1",
        "observed_turns": observed,
        "observed_episodes": len(episode_digests),
        "assistant_committed": valid_committed,
        "precommit_failures": valid_failures,
        "assistant_commit_rate_milli": round(valid_committed * 1000 / observed) if observed else None,
        "intent": dict(sorted(intent_counts.items())),
        "completion": dict(sorted(completion_counts.items())),
        "publication": dict(sorted(publication_counts.items())),
        "signals": {
            name: signal_counts[name]
            for name in (
                "ambiguity_present",
                "partial_coverage",
                "state_restored",
                "authority_rechecked",
            )
        },
        "failure_stages": dict(sorted(stage_counts.items())),
        "failure_reasons": dict(sorted(reason_counts.items())),
        "precommit_routes": dict(sorted(route_counts.items())),
        "bounded": bool(len(failure_rows) == bounded_limit or len(committed_rows) == bounded_limit),
    }


__all__ = [
    "FailureEntrypoint",
    "FailureRoute",
    "FailureTraceScope",
    "INTERACTION_FAILURE_GLOBAL_CAP",
    "INTERACTION_FAILURE_PER_USER_CAP",
    "INTERACTION_FAILURE_TRACE_SCHEMA",
    "bind_failure_trace_scope",
    "interaction_episode_baseline",
    "observe_failure_route",
    "observe_failure_stage",
    "observe_owned_conversation",
    "observe_owned_message_candidate",
    "record_precommit_failure",
]
