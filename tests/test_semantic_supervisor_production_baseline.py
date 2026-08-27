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
from friday.orchestration.supervisor_contracts import (
    SupervisorMode,
    TaskClass,
    canonical_sha256,
)
from friday.orchestration.supervisor_observation import parsed_observation
from friday.orchestration.supervisor_production_baseline import (
    SUPERVISOR_PRODUCTION_BASELINE_KIND,
    SUPERVISOR_PRODUCTION_BASELINE_SCHEMA,
    SUPERVISOR_PROMOTED_PRODUCT_EVENT,
    PromotedObservationEligibility,
    PromotedSupervisorProductObservation,
    PromotedUserVisibleOutcome,
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


def _trace(*, turn_identifier: str = "msg_aaaaaaaaaaaaaaaa") -> object:
    return build_committed_direct_trace(
        namespace_key=b"p" * 32,
        turn_identifier=turn_identifier,
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


def _insert_trace_and_join(conn: sqlite3.Connection) -> object:
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
    return trace


def _insert_trace(
    conn: sqlite3.Connection,
    *,
    trace: object,
    message_id: str,
) -> None:
    conn.execute(
        "INSERT INTO messages(id,role,content,metadata_json) VALUES(?,?,?,?)",
        (
            message_id,
            "assistant",
            "ANOTHER PRIVATE ANSWER BODY",
            json.dumps({"interaction_trace": trace.to_payload()}),  # type: ignore[attr-defined]
        ),
    )


def _insert_promoted(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    event: PromotedSupervisorProductObservation,
) -> None:
    conn.execute(
        "INSERT INTO runtime_events(id,event_type,payload) VALUES(?,?,?)",
        (event_id, SUPERVISOR_PROMOTED_PRODUCT_EVENT, json.dumps(event.payload())),
    )


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
        "promoted_product_events": 0,
        "malformed_turn_traces": 0,
        "malformed_joined_events": 0,
        "malformed_promoted_product_events": 0,
        "duplicate_turn_trace_digests": 0,
        "duplicate_shadow_product_events": 0,
        "duplicate_promoted_product_events": 0,
        "unmatched_shadow_product_events": 0,
        "unmatched_promoted_product_events": 0,
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
    readiness = report["product_windows"]["shadow_readiness"]
    assert readiness["actual_promoted_execution"] is False
    assert readiness["quality_claim"] == "documented_baseline_failure_only"
    assert readiness["baseline"]["observation_count"] == 1
    assert readiness["baseline"]["complete_count"] == 1
    assert readiness["call_rate_observation_count"] == 1
    assert readiness["unnecessary_supervisor_invocation_count"] == 0
    assert report["product_windows"]["promoted_execution"]["assist"]["observation_count"] == 0
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


def test_promoted_assist_and_canary_windows_never_count_as_shadow() -> None:
    conn = _connection()
    _insert_trace_and_join(conn)
    assist_trace = _trace(turn_identifier="msg_1111111111111111")
    ordinary_trace = _trace(turn_identifier="msg_2222222222222222")
    canary_trace = _trace(turn_identifier="msg_3333333333333333")
    _insert_trace(conn, trace=assist_trace, message_id="msg_1111111111111111")
    _insert_trace(conn, trace=ordinary_trace, message_id="msg_2222222222222222")
    _insert_trace(conn, trace=canary_trace, message_id="msg_3333333333333333")
    _insert_promoted(
        conn,
        event_id="evt_1111111111111111",
        event=PromotedSupervisorProductObservation(
            mode=SupervisorMode.ASSIST,
            task_class=TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
            eligibility=PromotedObservationEligibility.PROMOTED_JOURNEY,
            primary_trace_sha256=canonical_sha256(assist_trace.to_payload()),  # type: ignore[attr-defined]
            promotion_evidence_sha256="8" * 64,
            execution_receipt_sha256="9" * 64,
            supervisor_invoked=True,
            user_visible_outcome=PromotedUserVisibleOutcome.NO_REGRESSION,
        ),
    )
    _insert_promoted(
        conn,
        event_id="evt_2222222222222222",
        event=PromotedSupervisorProductObservation(
            mode=SupervisorMode.ASSIST,
            task_class=None,
            eligibility=PromotedObservationEligibility.OTHER_TURN,
            primary_trace_sha256=canonical_sha256(ordinary_trace.to_payload()),  # type: ignore[attr-defined]
            promotion_evidence_sha256="8" * 64,
            execution_receipt_sha256=None,
            supervisor_invoked=True,
            user_visible_outcome=PromotedUserVisibleOutcome.NOT_EVALUATED,
        ),
    )
    _insert_promoted(
        conn,
        event_id="evt_3333333333333333",
        event=PromotedSupervisorProductObservation(
            mode=SupervisorMode.CANARY,
            task_class=TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
            eligibility=PromotedObservationEligibility.PROMOTED_JOURNEY,
            primary_trace_sha256=canonical_sha256(canary_trace.to_payload()),  # type: ignore[attr-defined]
            promotion_evidence_sha256="a" * 64,
            execution_receipt_sha256="b" * 64,
            supervisor_invoked=True,
            user_visible_outcome=PromotedUserVisibleOutcome.REGRESSION,
        ),
    )
    conn.commit()

    report = build_production_baseline(conn, limit=100)

    shadow = report["product_windows"]["shadow_readiness"]
    assist = report["product_windows"]["promoted_execution"]["assist"]
    canary = report["product_windows"]["promoted_execution"]["canary"]
    assert shadow["observation_count"] == 1
    assert shadow["actual_promoted_execution"] is False
    assert assist["actual_promoted_execution"] is True
    assert assist["observation_count"] == 2
    assert assist["promoted"]["observation_count"] == 1
    assert assist["promoted"]["complete_count"] == 1
    assert assist["call_rate_observation_count"] == 2
    assert assist["supervisor_invocation_count"] == 2
    assert assist["unnecessary_supervisor_invocation_count"] == 1
    assert assist["user_visible_observation_count"] == 1
    assert assist["user_visible_regression_count"] == 0
    assert canary["observation_count"] == 1
    assert canary["user_visible_regression_count"] == 1
    assert report["sample"]["promoted_product_events"] == 3


def test_promoted_product_seam_rejects_extensions_and_unmatched_traces() -> None:
    conn = _connection()
    valid_shape = PromotedSupervisorProductObservation(
        mode=SupervisorMode.ASSIST,
        task_class=TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
        eligibility=PromotedObservationEligibility.PROMOTED_JOURNEY,
        primary_trace_sha256="4" * 64,
        promotion_evidence_sha256="8" * 64,
        execution_receipt_sha256="9" * 64,
        supervisor_invoked=True,
        user_visible_outcome=PromotedUserVisibleOutcome.NO_REGRESSION,
    ).payload()
    extended = dict(valid_shape)
    extended["body"] = "SECRET PRODUCT BODY"
    conn.execute(
        "INSERT INTO runtime_events(id,event_type,payload) VALUES(?,?,?)",
        (
            "evt_4444444444444444",
            SUPERVISOR_PROMOTED_PRODUCT_EVENT,
            json.dumps(extended),
        ),
    )
    conn.execute(
        "INSERT INTO runtime_events(id,event_type,payload) VALUES(?,?,?)",
        (
            "evt_5555555555555555",
            SUPERVISOR_PROMOTED_PRODUCT_EVENT,
            json.dumps(valid_shape),
        ),
    )
    conn.commit()

    report = build_production_baseline(conn, limit=100)

    assert report["sample"]["malformed_promoted_product_events"] == 1
    assert report["sample"]["unmatched_promoted_product_events"] == 1
    assert report["product_windows"]["promoted_execution"]["assist"]["observation_count"] == 0
    assert "SECRET PRODUCT BODY" not in json.dumps(report)


def test_promoted_product_seam_excludes_conflicting_replays_of_one_trace() -> None:
    conn = _connection()
    trace = _trace(turn_identifier="msg_6666666666666666")
    _insert_trace(conn, trace=trace, message_id="msg_6666666666666666")
    base = {
        "mode": SupervisorMode.ASSIST,
        "task_class": TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
        "eligibility": PromotedObservationEligibility.PROMOTED_JOURNEY,
        "primary_trace_sha256": canonical_sha256(trace.to_payload()),  # type: ignore[attr-defined]
        "promotion_evidence_sha256": "8" * 64,
        "execution_receipt_sha256": "9" * 64,
        "supervisor_invoked": True,
    }
    _insert_promoted(
        conn,
        event_id="evt_6666666666666666",
        event=PromotedSupervisorProductObservation(
            **base,
            user_visible_outcome=PromotedUserVisibleOutcome.NO_REGRESSION,
        ),  # type: ignore[arg-type]
    )
    _insert_promoted(
        conn,
        event_id="evt_7777777777777777",
        event=PromotedSupervisorProductObservation(
            **base,
            user_visible_outcome=PromotedUserVisibleOutcome.REGRESSION,
        ),  # type: ignore[arg-type]
    )
    conn.commit()

    report = build_production_baseline(conn, limit=100)

    assert report["sample"]["duplicate_promoted_product_events"] == 1
    assert report["product_windows"]["promoted_execution"]["assist"]["observation_count"] == 0
