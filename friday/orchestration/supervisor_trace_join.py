"""Join body-free supervisor observations to Friday's committed ICP trace.

The optional supervisor finishes after the primary answer has already been
published.  It therefore cannot amend assistant metadata without weakening the
atomic publication boundary.  This module reads the immutable trace already
stored with that assistant row and emits one bounded operational sidecar event.
No message, query, model response, source body, raw identifier, or path enters
the event.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from friday.interaction_control_plane.runtime_trace import INTERACTION_TRACE_METADATA_KEY
from friday.interaction_control_plane.turn_trace import TurnTrace
from friday.orchestration.supervisor_contracts import canonical_sha256

SUPERVISOR_TRACE_EVENT = "semantic_supervisor.shadow"
SUPERVISOR_TRACE_JOIN_SCHEMA = "friday.supervisor-trace-join.v1"

_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}")
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}")
_MAX_METADATA_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class PrimaryTraceProjection:
    """Closed facts copied from one already-committed :class:`TurnTrace`."""

    trace_digest: str
    turn_digest: str
    conversation_digest: str
    capability_outcomes: tuple[str, ...]
    completion: str
    publication: str
    authority_rechecked: bool
    state_restored: bool
    retry_occurred: bool

    @classmethod
    def from_trace(cls, trace: TurnTrace) -> PrimaryTraceProjection:
        return cls(
            trace_digest=canonical_sha256(trace.to_payload()),
            turn_digest=trace.turn_digest,
            conversation_digest=trace.conversation_digest,
            capability_outcomes=tuple(
                f"{step.capability.value}:{step.outcome.value}" for step in trace.steps
            ),
            completion=trace.completion.value,
            publication=trace.publication.value,
            authority_rechecked=trace.authority_rechecked,
            state_restored=trace.state_restored,
            retry_occurred=any(step.attempts > 1 for step in trace.steps),
        )

    def payload(self) -> dict[str, object]:
        return {
            "trace_digest": self.trace_digest,
            "turn_digest": self.turn_digest,
            "conversation_digest": self.conversation_digest,
            "capability_outcomes": list(self.capability_outcomes),
            "completion": self.completion,
            "publication": self.publication,
            "authority_rechecked": self.authority_rechecked,
            "state_restored": self.state_restored,
            "retry_occurred": self.retry_occurred,
        }


def _bounded_metadata(value: object) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return None
        if len(encoded) > _MAX_METADATA_BYTES:
            return None
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, UnicodeError, RecursionError):
            return None
    elif isinstance(value, dict):
        decoded = value
    else:
        return None
    return decoded if isinstance(decoded, dict) else None


def load_primary_trace_projection(
    runtime: object,
    primary_result: object,
) -> PrimaryTraceProjection | None:
    """Read the exact assistant trace selected by the primary response.

    Missing storage, narrow test doubles, malformed responses and old rows are
    ordinary observability misses.  They never affect the primary result.
    """

    if type(primary_result) is not dict:
        return None
    message_id = primary_result.get("message_id")
    conversation_id = primary_result.get("conversation_id")
    if (
        not isinstance(message_id, str)
        or _MESSAGE_ID_RE.fullmatch(message_id) is None
        or not isinstance(conversation_id, str)
        or _CONVERSATION_ID_RE.fullmatch(conversation_id) is None
    ):
        return None
    storage = getattr(runtime, "storage", None)
    execute = getattr(storage, "execute", None)
    if not callable(execute):
        return None
    try:
        row = execute(
            """SELECT metadata_json FROM messages
                 WHERE id=? AND conversation_id=? AND role='assistant'
                 LIMIT 1""",
            (message_id, conversation_id),
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    try:
        raw_metadata = row["metadata_json"]
    except (KeyError, TypeError, IndexError):
        try:
            raw_metadata = row[0]
        except (TypeError, IndexError):
            return None
    metadata = _bounded_metadata(raw_metadata)
    if metadata is None:
        return None
    try:
        trace = TurnTrace.parse(metadata.get(INTERACTION_TRACE_METADATA_KEY))
    except Exception:
        return None
    return PrimaryTraceProjection.from_trace(trace)


def persist_joined_supervisor_observation(
    runtime: object,
    *,
    observation_payload: dict[str, object],
    primary_trace: PrimaryTraceProjection | None,
) -> bool:
    """Append one joined body-free event through the existing bounded journal."""

    if primary_trace is None:
        return False
    storage = getattr(runtime, "storage", None)
    record_event = getattr(storage, "record_event", None)
    if not callable(record_event):
        return False
    payload: dict[str, object] = {
        "schema": SUPERVISOR_TRACE_JOIN_SCHEMA,
        "supervisor": observation_payload,
        "primary_trace": primary_trace.payload(),
    }
    try:
        # Canonical serialization is also a final finite/JSON-shape preflight;
        # only the digest is discarded.  The bounded runtime journal owns
        # retention and atomic insertion.
        canonical_sha256(payload)
        record_event(SUPERVISOR_TRACE_EVENT, payload)
    except Exception:
        return False
    return True


__all__ = [
    "PrimaryTraceProjection",
    "SUPERVISOR_TRACE_EVENT",
    "SUPERVISOR_TRACE_JOIN_SCHEMA",
    "load_primary_trace_projection",
    "persist_joined_supervisor_observation",
]
