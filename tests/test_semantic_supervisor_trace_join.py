from __future__ import annotations

import json
from typing import Any

from friday.interaction_control_plane.runtime_trace import (
    INTERACTION_TRACE_METADATA_KEY,
    build_committed_direct_trace,
)
from friday.interaction_control_plane.turn_trace import (
    CapabilityClass,
    CountAccounting,
    IntentClass,
    PlaybookClass,
)
from friday.orchestration.supervisor_observation import (
    SupervisorSkipReason,
    skipped_observation,
)
from friday.orchestration.supervisor_trace_join import (
    SUPERVISOR_TRACE_EVENT,
    SUPERVISOR_TRACE_JOIN_SCHEMA,
    load_primary_trace_projection,
    persist_joined_supervisor_observation,
)


def _trace() -> object:
    return build_committed_direct_trace(
        namespace_key=b"k" * 32,
        turn_identifier="msg_1111111111111111",
        conversation_identifier="conv_2222222222222222",
        intent=IntentClass.MIXED,
        playbook=PlaybookClass.COMPARE_INTERNAL_AND_EXTERNAL_SOURCES,
        capabilities=(
            CapabilityClass.DOCUMENT_RETRIEVAL,
            CapabilityClass.WEB_RESEARCH,
            CapabilityClass.MODEL_SYNTHESIS,
        ),
        latency_ms=321,
        model_calls=1,
        model_call_accounting=CountAccounting.LOWER_BOUND,
        capability_calls=2,
        capability_call_accounting=CountAccounting.COMPLETE,
        authority_rechecked=True,
    )


class _Cursor:
    def __init__(self, row: object) -> None:
        self._row = row

    def fetchone(self) -> object:
        return self._row


class _Storage:
    def __init__(self, metadata: object) -> None:
        self.metadata = metadata
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: tuple[object, ...]) -> _Cursor:
        self.execute_calls.append((sql, params))
        return _Cursor({"metadata_json": self.metadata})

    def record_event(self, event_type: str, payload: dict[str, Any]) -> str:
        self.events.append((event_type, payload))
        return "evt_1111111111111111"


class _Runtime:
    def __init__(self, storage: _Storage) -> None:
        self.storage = storage


def test_committed_primary_trace_is_joined_without_reading_a_body() -> None:
    trace = _trace()
    storage = _Storage(
        json.dumps(
            {INTERACTION_TRACE_METADATA_KEY: trace.to_payload()},  # type: ignore[attr-defined]
            ensure_ascii=False,
        )
    )
    runtime = _Runtime(storage)

    projection = load_primary_trace_projection(
        runtime,
        {
            "conversation_id": "conv_2222222222222222",
            "message_id": "msg_3333333333333333",
            "message": "private answer body must stay out",
        },
    )

    assert projection is not None
    assert projection.completion == "complete"
    assert projection.publication == "assistant_committed"
    assert projection.authority_rechecked is True
    assert projection.capability_outcomes == (
        "document_retrieval:succeeded",
        "web_research:succeeded",
        "model_synthesis:succeeded",
    )
    assert storage.execute_calls[0][1] == (
        "msg_3333333333333333",
        "conv_2222222222222222",
    )


def test_joined_event_contains_only_closed_observation_and_trace_facts() -> None:
    trace = _trace()
    storage = _Storage({INTERACTION_TRACE_METADATA_KEY: trace.to_payload()})  # type: ignore[attr-defined]
    runtime = _Runtime(storage)
    projection = load_primary_trace_projection(
        runtime,
        {
            "conversation_id": "conv_2222222222222222",
            "message_id": "msg_3333333333333333",
        },
    )
    assert projection is not None
    observation = skipped_observation(
        requested_mode="shadow",
        skip_reason=SupervisorSkipReason.ORDINARY_DIALOGUE,
    ).with_primary_trace(
        trace_digest=projection.trace_digest,
        capability_outcomes=projection.capability_outcomes,
        completion=projection.completion,
        publication=projection.publication,
        authority_rechecked=projection.authority_rechecked,
        state_restored=projection.state_restored,
        retry_occurred=projection.retry_occurred,
    )

    assert persist_joined_supervisor_observation(
        runtime,
        observation_payload=observation.payload(),
        primary_trace=projection,
    )
    assert storage.events[0][0] == SUPERVISOR_TRACE_EVENT
    payload = storage.events[0][1]
    assert payload["schema"] == SUPERVISOR_TRACE_JOIN_SCHEMA
    serialized = json.dumps(payload, sort_keys=True)
    for forbidden in (
        "msg_1111111111111111",
        "msg_3333333333333333",
        "conv_2222222222222222",
        "private answer body must stay out",
    ):
        assert forbidden not in serialized


def test_missing_or_malformed_committed_trace_is_an_observability_miss() -> None:
    for metadata in ("not-json", {}, {INTERACTION_TRACE_METADATA_KEY: {"schema": "wrong"}}):
        projection = load_primary_trace_projection(
            _Runtime(_Storage(metadata)),
            {
                "conversation_id": "conv_2222222222222222",
                "message_id": "msg_3333333333333333",
            },
        )
        assert projection is None

    assert (
        load_primary_trace_projection(
            _Runtime(_Storage({})),
            {"conversation_id": "raw", "message_id": "../../../etc/passwd"},
        )
        is None
    )
