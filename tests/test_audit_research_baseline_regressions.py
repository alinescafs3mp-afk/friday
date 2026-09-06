"""Source-derived regressions for the September 2026 reliability corrections."""

from __future__ import annotations

import json
import sqlite3

import pytest

from friday.execution_kernel.web_research_gates import merge_unique_research_sources
from friday.orchestration.supervisor_production_baseline import (
    SUPERVISOR_TRACE_EVENT,
    SUPERVISOR_TRACE_JOIN_SCHEMA,
    build_production_baseline,
)
from friday.web_research_contract import MAX_RESEARCH_SOURCES


def _source(url: object) -> dict[str, object]:
    return {"url": url, "text": "An observed public source.", "error": ""}


@pytest.mark.parametrize(
    ("original", "other", "count"),
    (
        ("https://docs.python.org/report?year=2025", "https://docs.python.org/report?year=2026", 2),
        ("https://docs.python.org/report?id=1", "https://docs.python.org/report?id=2", 2),
        ("http://docs.python.org/report", "https://docs.python.org/report", 2),
        ("https://docs.python.org:8443/report", "https://docs.python.org/report", 2),
        ("https://docs.python.org:443/report#one", "https://DOCS.PYTHON.ORG/report#two", 1),
        ("https://docs.python.org/%7Euser", "https://docs.python.org/~user", 1),
    ),
)
def test_research_merge_uses_shared_resource_identity(original: str, other: str, count: int) -> None:
    report = {"sources": [_source(original)]}
    merge_unique_research_sources(report, [_source(other)], bound=MAX_RESEARCH_SOURCES)
    assert len(report["sources"]) == count


@pytest.mark.parametrize("url", ("https://[", "https://docs.python.org:bad/a", None, [], {}))
def test_corrupt_original_source_does_not_abort_merge(url: object) -> None:
    corrupt = _source(url)
    report = {"sources": [corrupt]}
    added = _source("https://docs.python.org/3/")
    merge_unique_research_sources(report, [added], bound=MAX_RESEARCH_SOURCES)
    assert report["sources"] == [corrupt, added]


@pytest.mark.parametrize("url", ("https://[", "https://localhost/", "https://127.0.0.1/", [], {}))
def test_merge_never_admits_private_or_malformed_new_source(url: object) -> None:
    report = {"sources": []}
    merge_unique_research_sources(report, [_source(url)], bound=MAX_RESEARCH_SOURCES)
    assert report["sources"] == []


@pytest.mark.parametrize("bound", (True, False, None, 0, -1, 1.5, MAX_RESEARCH_SOURCES + 1))
def test_invalid_merge_bound_cannot_expand_source_population(bound: object) -> None:
    report = {"sources": []}
    merge_unique_research_sources(report, [_source("https://docs.python.org/3/")], bound=bound)
    assert report["sources"] == []


def test_merge_preserves_failure_accounting_and_honors_bound() -> None:
    report = {"sources": [], "completed_sources": 0, "failed_sources": 2, "timed_out_sources": 1}
    merge_unique_research_sources(
        report,
        [_source(f"https://docs.python.org/report?id={index}") for index in range(5)],
        bound=2,
    )
    assert len(report["sources"]) == report["completed_sources"] == 2
    assert report["requested_sources"] == 5


@pytest.fixture
def baseline_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE messages(role TEXT, metadata_json TEXT);"
        "CREATE TABLE runtime_events(event_type TEXT, payload TEXT);"
    )
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize("payload", ("not JSON", "{", "null", '"PRIVATE-BODY"', "[]"))
def test_invalid_join_json_is_an_anomaly_not_an_absent_row(baseline_conn, payload: str) -> None:
    baseline_conn.execute("INSERT INTO runtime_events VALUES (?, ?)", (SUPERVISOR_TRACE_EVENT, payload))
    report = build_production_baseline(baseline_conn)
    assert report["sample"]["malformed_joined_events"] == 1
    assert report["sample"]["joined_supervisor_events"] == 0
    assert "PRIVATE-BODY" not in json.dumps(report)


@pytest.mark.parametrize(
    "metadata",
    ("broken", '{"interaction_trace":null}', '{"interaction_trace":[]}', '{"interaction_trace":1}'),
)
def test_invalid_trace_metadata_cannot_disappear(baseline_conn, metadata: str) -> None:
    baseline_conn.execute("INSERT INTO messages VALUES (?, ?)", ("assistant", metadata))
    report = build_production_baseline(baseline_conn)
    assert report["sample"]["malformed_turn_traces"] == 1
    assert report["sample"]["turn_traces"] == 0


def test_uninstrumented_and_nonassistant_messages_are_not_malformed_traces(baseline_conn) -> None:
    baseline_conn.executemany("INSERT INTO messages VALUES (?, ?)", [("assistant", "{}"), ("user", "broken")])
    assert build_production_baseline(baseline_conn)["sample"]["malformed_turn_traces"] == 0


@pytest.mark.parametrize("task", ([], {}, 1, None))
def test_unhashable_task_class_does_not_crash_body_free_baseline(baseline_conn, task: object) -> None:
    event = {"schema": SUPERVISOR_TRACE_JOIN_SCHEMA, "supervisor": {"task_class": task}, "primary_trace": {}}
    baseline_conn.execute(
        "INSERT INTO runtime_events VALUES (?, ?)", (SUPERVISOR_TRACE_EVENT, json.dumps(event))
    )
    report = build_production_baseline(baseline_conn)
    assert report["supervisor_join"]["task_counts"] == {"unknown": 1}
    assert report["sample"]["unmatched_shadow_product_events"] == 1


def test_duplicate_join_keys_are_counted_instead_of_using_last_value(baseline_conn) -> None:
    payload = json.dumps({"schema": SUPERVISOR_TRACE_JOIN_SCHEMA, "supervisor": {}, "primary_trace": {}})
    payload = payload[:-1] + ', "supervisor": {}}'
    baseline_conn.execute("INSERT INTO runtime_events VALUES (?, ?)", (SUPERVISOR_TRACE_EVENT, payload))
    assert build_production_baseline(baseline_conn)["sample"]["malformed_joined_events"] == 1
