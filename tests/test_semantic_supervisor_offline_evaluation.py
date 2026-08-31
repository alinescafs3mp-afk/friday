from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from friday.orchestration.supervisor_contracts import canonical_dumps, canonical_sha256
from friday.orchestration.supervisor_offline_evaluation import (
    OFFLINE_EVALUATION_SCHEMA,
    OFFLINE_EVIDENCE_KIND,
    OfflineEvaluationError,
    evaluate_offline_fixture_set,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "semantic_supervisor_offline_v1.json"
CLI = ROOT / "tools" / "evaluate_semantic_supervisor_offline.py"


def _fixture_set() -> dict[str, Any]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def _case(report: dict[str, Any], case_id: str) -> dict[str, Any]:
    return next(item for item in report["cases"] if item["case_id"] == case_id)


def _string_values(value: object) -> set[str]:
    if isinstance(value, dict):
        return {item for child in value.values() for item in _string_values(child)}
    if isinstance(value, list):
        return {item for child in value for item in _string_values(child)}
    return {value} if isinstance(value, str) else set()


def test_offline_replay_reports_closed_p1_metrics_and_zero_activity() -> None:
    report = evaluate_offline_fixture_set(_fixture_set())

    assert report["schema"] == OFFLINE_EVALUATION_SCHEMA
    assert report["evidence"] == {
        "kind": OFFLINE_EVIDENCE_KIND,
        "network_used": False,
        "live_shadow_evidence": False,
        "live_canary_evidence": False,
        "promotion_evidence": False,
        "acceptance_authority": "none",
        "warning": "synthetic_offline_only_not_live_shadow_or_canary_acceptance",
    }
    assert report["fixture_set"]["case_count"] == 10
    assert report["metrics"]["valid_proposals"] == {
        "count": 2,
        "denominator": "invocations",
        "denominator_count": 5,
        "rate": 0.4,
    }
    assert report["metrics"]["policy_rejections"]["count"] == 2
    assert report["metrics"]["stale_manifest_rejections"]["count"] == 1
    assert report["metrics"]["unknown_capability_rejections"]["count"] == 1
    assert report["metrics"]["stale_or_unknown_capability_rejections"]["count"] == 2
    assert report["metrics"]["unnecessary_invocations"]["count"] == 0
    assert report["metrics"]["exact_lane_bypasses"]["count"] == 2
    assert report["metrics"]["exact_lane_bypasses"]["rate"] == 1.0
    assert report["metrics"]["primary_fallback_parity"]["count"] == 10
    assert report["metrics"]["primary_fallback_parity"]["rate"] == 1.0
    assert report["metrics"]["fixture_conformance"]["count"] == 10
    assert report["runtime_harness"] == {
        "runtime_exercised": True,
        "fixture_primary_trace_used": False,
        "in_memory_model_adapter": True,
        "network_endpoint_installed": False,
        "primary_call_count": 10,
        "shadow_call_count": 5,
        "shadow_dispatch_count": 5,
        "runtime_invariant_conformance": {
            "count": 10,
            "denominator": "fixtures",
            "denominator_count": 10,
            "rate": 1.0,
        },
    }
    assert report["non_owning_counts"] == {
        "execution_count": 0,
        "publication_count": 0,
        "effect_count": 0,
    }
    for case in report["cases"]:
        assert case["primary_call_count"] == 1
        assert case["primary_response_identity_unchanged"] is True
        assert case["primary_response_value_unchanged"] is True
        assert case["shadow_started_after_primary"] is True
        assert case["opaque_surfaces_forwarded_unchanged"] is True
        assert case["lifecycle_owner_unchanged"] is True
        assert case["runtime_status_non_owning"] is True
        assert case["observed_execution_count"] == 0
        assert case["observed_publication_count"] == 0
        assert case["observed_effect_count"] == 0
        assert case["non_owning_conforms"] is True
        assert case["runtime_invariant_conforms"] is True


def test_replay_exercises_valid_stale_unknown_malformed_and_exact_outcomes() -> None:
    report = evaluate_offline_fixture_set(_fixture_set())

    assert _case(report, "file_web_valid")["policy_verdict"] == "valid"
    assert _case(report, "archive_web_valid")["policy_reason"] == "admitted"
    assert _case(report, "stale_manifest_rejected")["policy_reason"] == "stale_manifest"
    assert _case(report, "unknown_capability_rejected")["policy_reason"] == "unknown_capability"
    malformed = _case(report, "malformed_proposal_primary")
    assert malformed["proposal_parse_status"] == "malformed"
    assert malformed["policy_verdict"] == "not_evaluated"
    for case_id in ("exact_pending_ordinal", "exact_cancel"):
        exact = _case(report, case_id)
        assert exact["invoked"] is False
        assert exact["lane"] == "exact_lane"
        assert exact["skip_reason"] == "exact_lane"


def test_report_is_deterministic_canonical_and_contains_no_fixture_bodies() -> None:
    fixture_set = _fixture_set()
    first = evaluate_offline_fixture_set(fixture_set)
    second = evaluate_offline_fixture_set(deepcopy(fixture_set))

    assert first == second
    unsigned = dict(first)
    report_sha256 = unsigned.pop("report_sha256")
    assert report_sha256 == canonical_sha256(unsigned)
    assert first["fixture_set"]["digest"] == canonical_sha256(fixture_set)
    encoded = canonical_dumps(first)
    assert encoded == canonical_dumps(json.loads(encoded))
    report_strings = _string_values(first)
    for fixture in fixture_set["fixtures"]:
        assert fixture["turn"]["message"] not in report_strings
    for forbidden_key in ('"message"', '"goal"', '"steps"', '"purpose"', '"input"'):
        assert forbidden_key not in encoded


def test_fixture_contract_is_closed_and_rejects_raw_proposal_fields() -> None:
    fixture_set = _fixture_set()
    fixture_set["fixtures"][0]["raw_proposal"] = {"execute_now": True}
    with pytest.raises(OfflineEvaluationError, match="keys are not closed"):
        evaluate_offline_fixture_set(fixture_set)

    duplicate = _fixture_set()
    duplicate["fixtures"][1]["id"] = duplicate["fixtures"][0]["id"]
    with pytest.raises(OfflineEvaluationError, match="fixture ids must be unique"):
        evaluate_offline_fixture_set(duplicate)

    claimed_primary_trace = _fixture_set()
    claimed_primary_trace["fixtures"][0]["primary_trace"] = "primary_once_unchanged"
    with pytest.raises(OfflineEvaluationError, match="keys are not closed"):
        evaluate_offline_fixture_set(claimed_primary_trace)

    invalid_bounds = _fixture_set()
    invalid_bounds["settings"]["max_steps"] = 5
    with pytest.raises(OfflineEvaluationError, match="admitted P1 bound 6"):
        evaluate_offline_fixture_set(invalid_bounds)


def test_cli_emits_the_same_canonical_body_free_report_twice() -> None:
    commands = [sys.executable, str(CLI), "--fixtures", str(FIXTURES)]
    first = subprocess.run(commands, cwd=ROOT, check=True, capture_output=True, text=True)
    second = subprocess.run(commands, cwd=ROOT, check=True, capture_output=True, text=True)

    assert first.stderr == ""
    assert first.stdout == second.stdout
    assert first.stdout == canonical_dumps(json.loads(first.stdout)) + "\n"
    report = json.loads(first.stdout)
    assert report["evidence"]["acceptance_authority"] == "none"
    assert report["non_owning_counts"]["execution_count"] == 0

    rejected = subprocess.run(
        commands,
        cwd=ROOT,
        env={**os.environ, "FRIDAY_QUALITY_GATE_INSTALLED_SITE": "relative/wheel-site"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert rejected.stdout == ""
