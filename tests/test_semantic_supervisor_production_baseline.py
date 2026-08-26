from __future__ import annotations

import json
import sqlite3

from friday.interaction_control_plane.runtime_trace import build_committed_direct_trace
from friday.interaction_control_plane.turn_trace import (
    CapabilityClass,
    CountAccounting,
    IntentClass,
    PlaybookClass,
)
from friday.orchestration.supervisor_contracts import canonical_sha256
from friday.orchestration.supervisor_observation import parsed_observation
from friday.orchestration.supervisor_production_baseline import (
    SUPERVISOR_PRODUCTION_BASELINE_KIND,
    SUPERVISOR_PRODUCTION_BASELINE_SCHEMA,
    build_production_baseline,
)
from friday.orchestration.supervisor_trace_join import (
    SUPERVISOR_TRACE_EVENT,
    SUPERVISOR_TRACE_JOIN_SCHEMA,
    PrimaryTraceProjection,
)


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        );
        CREATE TABLE runtime_events (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        """
    )
    return conn


def _trace() -> object:
    return build_committed_direct_trace(
        namespace_key=b"p" * 32,
        turn_identifier="msg_aaaaaaaaaaaaaaaa",
        conversation_identifier="conv_bbbbbbbbbbbbbbbb",
        intent=IntentClass.MIXED,
        playbook=PlaybookClass.COMPARE_INTERNAL_AND_EXTERNAL_SOURCES,
        capabilities=(CapabilityClass.DOCUMENT_RETRIEVAL, CapabilityClass.MODEL_SYNTHESIS),
        latency_ms=432,
        model_calls=1,
        model_call_accounting=CountAccounting.LOWER_BOUND,
        capability_calls=1,
        capability_call_accounting=CountAccounting.COMPLETE,
        authority_rechecked=True,
    )


def _insert_trace_and_join(conn: sqlite3.Connection) -> None:
    trace = _trace()
    conn.execute(
        "INSERT INTO messages(id,role,content,metadata_json) VALUES(?,?,?,?)",
        (
            "msg_cccccccccccccccc",
            "assistant",
            "PRIVATE ANSWER BODY",
            json.dumps({"interaction_trace": trace.to_payload()}),  # type: ignore[attr-defined]
        ),
    )
    projection = PrimaryTraceProjection.from_trace(trace)  # type: ignore[arg-type]
    observation = parsed_observation(
        requested_mode="shadow",
        manifest_digest="1" * 64,
        supervisor_input_digest="2" * 64,
        proposal_digest="3" * 64,
        proposal_parse_status="parsed",
        policy_verdict="valid",
        policy_reason="admitted",
        task_class="compare_current_file_with_current_web",
        step_count=3,
        effect_classes=("read", "read", "read"),
        current_route="legacy",
        endpoint_health_class="accepted",
        accepted_profile_id="accepted-profile",
        planner_latency_bucket="250_999ms",
    ).with_primary_trace(
        trace_digest=projection.trace_digest,
        capability_outcomes=projection.capability_outcomes,
        completion=projection.completion,
        publication=projection.publication,
        authority_rechecked=projection.authority_rechecked,
        state_restored=projection.state_restored,
        retry_occurred=projection.retry_occurred,
    )
    conn.execute(
        "INSERT INTO runtime_events(id,event_type,payload) VALUES(?,?,?)",
        (
            "evt_dddddddddddddddd",
            SUPERVISOR_TRACE_EVENT,
            json.dumps(
                {
                    "schema": SUPERVISOR_TRACE_JOIN_SCHEMA,
                    "supervisor": observation.payload(),
                    "primary_trace": projection.payload(),
                }
            ),
        ),
    )
    conn.commit()


def test_baseline_aggregates_only_typed_body_free_rows() -> None:
    conn = _connection()
    _insert_trace_and_join(conn)
    sql: list[str] = []
    conn.set_trace_callback(sql.append)

    report = build_production_baseline(conn, limit=100)

    assert report["schema"] == SUPERVISOR_PRODUCTION_BASELINE_SCHEMA
    assert report["evidence"] == {
        "kind": SUPERVISOR_PRODUCTION_BASELINE_KIND,
        "body_free": True,
        "production_acceptance": False,
        "acceptance_authority": "operator_review_required",
        "representative_window_attested": False,
        "promotion_authority": False,
    }
    assert report["sample"] == {
        "limit": 100,
        "turn_traces": 1,
        "joined_supervisor_events": 1,
        "malformed_turn_traces": 0,
        "malformed_joined_events": 0,
    }
    assert report["primary_baseline"]["completion_counts"] == {"complete": 1}
    joined = report["supervisor_join"]
    assert joined["task_counts"] == {"compare_current_file_with_current_web": 1}
    assert joined["policy_reason_counts"] == {"admitted": 1}
    assert joined["planner_latency_bucket_counts"] == {"250_999ms": 1}
    assert joined["actual_completion_counts"] == {"complete": 1}
    assert joined["actual_publication_counts"] == {"assistant_committed": 1}
    assert joined["actual_capability_outcome_counts"] == {
        "document_retrieval:succeeded": 1,
        "model_synthesis:succeeded": 1,
    }
    assert joined["invoked_count"] == 1
    assert joined["admitted_count"] == 1
    unsigned = dict(report)
    digest = unsigned.pop("report_sha256")
    assert digest == canonical_sha256(unsigned)

    observed_sql = " ".join(sql).casefold()
    assert "select content" not in observed_sql
    assert "conversation_id" not in observed_sql
    assert "private answer body" not in json.dumps(report).casefold()


def test_malformed_trace_and_join_are_counted_but_never_reflected() -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO messages(id,role,content,metadata_json) VALUES(?,?,?,?)",
        ("msg_eeeeeeeeeeeeeeee", "assistant", "SECRET BODY", '{"interaction_trace":{"schema":"bad"}}'),
    )
    conn.execute(
        "INSERT INTO runtime_events(id,event_type,payload) VALUES(?,?,?)",
        (
            "evt_ffffffffffffffff",
            SUPERVISOR_TRACE_EVENT,
            json.dumps(
                {
                    "schema": SUPERVISOR_TRACE_JOIN_SCHEMA,
                    "supervisor": {},
                    "primary_trace": {},
                    "private_extension": "SECRET BODY",
                }
            ),
        ),
    )
    conn.commit()

    report = build_production_baseline(conn, limit=10)

    assert report["sample"]["turn_traces"] == 0
    assert report["sample"]["malformed_turn_traces"] == 1
    assert report["sample"]["joined_supervisor_events"] == 0
    assert report["sample"]["malformed_joined_events"] == 1
    assert "SECRET BODY" not in json.dumps(report)
