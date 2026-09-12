"""Read one externally bound application-soak attempt without executing it."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CASE_ID = "R10-LIVE-APP-SOAK"
ROOT = Path(__file__).resolve().parents[1]


def _require(condition: bool, code: str) -> None:
    from tools.release_1_0_acceptance import AcceptanceError

    if not condition:
        raise AcceptanceError("app_soak_" + code)


def _read(path: Path, digest: str, maximum: int = 4 << 20) -> dict[str, Any]:
    from tools.release_1_0_acceptance import AcceptanceError, _read_bound_receipt_bytes

    def pairs(items):
        value = dict(items)
        if len(value) != len(items):
            raise ValueError("duplicate key")
        return value

    def constant(_value):
        raise ValueError("nonfinite constant")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite decoded float")
        return number

    try:
        raw = _read_bound_receipt_bytes(path, digest, max_bytes=maximum)
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float)
    except (AcceptanceError, ValueError, UnicodeError, RecursionError) as exc:
        raise AcceptanceError("app_soak_bound_json_invalid") from exc
    _require(isinstance(value, dict), "json_object_required")
    return value


def read_context(path: Path, digest: str) -> dict[str, Any]:
    context = _read(path, digest, 65536)
    _require(context.get("schema") == "friday.app-soak-expected-context.v1", "context_schema")
    _require(context.get("profile") in {"short", "frozen"}, "context_profile")
    root = Path(str(context.get("output_root", "")))
    _require(root.is_absolute() and root.resolve() == root, "output_root")
    _require(not path.resolve().is_relative_to(root), "context_not_independent")
    return context


def audit_app_soak_execution(
    *,
    receipt_path: Path,
    receipt_sha256: str,
    context_path: Path,
    context_sha256: str,
    expected_identity: Mapping[str, str],
    expected_suite: str,
) -> dict[str, Any]:
    """Registration and offline control success cannot provide hour-run credit."""
    from tools import release_1_0_app_soak as soak
    from tools.release_1_0_acceptance import AcceptanceError

    context_ref = {"path": str(context_path), "sha256": context_sha256}
    context = read_context(context_path, context_sha256)
    _require(context.get("candidate_identity") == dict(expected_identity), "candidate_identity")
    root = Path(context["output_root"])
    _require(receipt_path.parent == root, "receipt_outside_attempt")
    result = _read(receipt_path, receipt_sha256)

    def linked(item: Any, *, inside: bool, maximum: int = 4 << 20) -> dict[str, Any]:
        _require(isinstance(item, dict) and set(item) == {"path", "sha256"}, "reference_shape")
        path = Path(str(item["path"]))
        _require(path.is_absolute() and path.resolve() == path, "reference_path")
        _require(path.is_relative_to(root) is inside, "reference_scope")
        return _read(path, item["sha256"], maximum)

    manifest = linked(context.get("candidate"), inside=False, maximum=16 << 20)
    _require(manifest.get("source_root") == str(ROOT), "candidate_source")
    _require(manifest.get("candidate_sha") == expected_identity["candidate_sha"], "candidate_commit")
    _require(manifest.get("candidate_tree") == expected_identity["candidate_tree"], "candidate_tree")
    _require(manifest.get("suite_revision") == expected_suite, "candidate_suite")
    _require(result.get("candidate") == context["candidate"], "result_candidate")
    load_path = ROOT / "tools/release_1_0_app_soak_load.json"
    load_raw = load_path.read_bytes()
    load = linked(context.get("load"), inside=False)
    _require(hashlib.sha256(load_raw).hexdigest() == context["load"]["sha256"], "load_candidate_binding")
    _require(load == json.loads(load_raw), "load_identity")
    try:
        policy = soak.SoakPolicy.from_value(context["policy"])
    except (KeyError, soak.AppSoakError) as exc:
        raise AcceptanceError("app_soak_expected_policy_invalid") from exc
    _require(policy.run_id == context.get("run_id"), "run_identity")
    _require(policy.candidate_sha256 == context["candidate"]["sha256"], "policy_candidate")
    _require(policy.suite_revision == expected_suite, "policy_suite")
    _require(policy.config_sha256 == soak._digest(context.get("config_identity")), "config_identity")
    if context["profile"] == "frozen":
        payload = policy.to_payload()
        _require(
            all(
                payload[name] == load[name]
                for name in (
                    "duration_sec",
                    "cadence_sec",
                    "deadline_sec",
                    "max_requests",
                    "request_concurrency",
                    "resources",
                )
            ),
            "policy_load",
        )
    after = linked(result.get("after"), inside=True)
    _require(after.get("status") == "TERMINAL", "outer_terminal_missing")
    _require(after.get("candidate") == context["candidate"], "outer_candidate")
    _require(after.get("run_id") == policy.run_id, "outer_run_identity")
    _require(after.get("expected_context") == context_ref, "outer_context_identity")
    _require(after.get("runner") == context.get("runner"), "outer_runner_identity")
    runner = context.get("runner")
    _require(isinstance(runner, dict) and set(runner) == {"path", "sha256"}, "runner_shape")
    _require(
        hashlib.sha256(Path(runner["path"]).read_bytes()).hexdigest() == runner["sha256"], "runner_changed"
    )
    plan = linked(after.get("plan"), inside=False)
    _require(result.get("plan") == after["plan"], "result_plan_identity")
    _require(
        plan.get("expected_context") == context_ref and plan.get("run_id") == policy.run_id, "plan_context"
    )
    _require(
        plan.get("candidate") == context["candidate"] and plan.get("load") == context["load"],
        "plan_candidate_load",
    )
    _require(plan.get("root") == str(root) and plan.get("profile") == context["profile"], "plan_profile_root")
    terminal = after.get("outer_owner")
    _require(isinstance(terminal, dict), "outer_owner_missing")
    cleanup = terminal.get("cleanup")
    _require(isinstance(cleanup, dict), "outer_cleanup_missing")
    clean = (
        result.get("cleanup_clear") is True
        and not after.get("integrity_errors", ["missing"])
        and all(
            after.get(key) is True
            for key in (
                "candidate_source_git_unchanged",
                "tools_unchanged",
                "protected_indexes_unchanged",
                "office_runtime_and_admission_unchanged",
                "run_inputs_unchanged",
            )
        )
        and all(cleanup.get(key) is True for key in ("kernel_echild", "leader_reaped", "subreaper_restored"))
        and all(
            cleanup.get(key) is False for key in ("forced_leader", "forced_descendants", "initial_uncertain")
        )
        and cleanup.get("fault_codes") == []
        and terminal.get("outcome") in {"completed", "child_failed"}
        and terminal.get("leader_returncode") in {0, 4}
        and terminal.get("timed_out") is False
        and terminal.get("error_code") is None
        and terminal.get("cancelled_signals") == []
        and terminal.get("stop_unconfirmed_emitted") is False
    )
    baseline = {
        "status": "BLOCKED",
        "case_layers": {},
        "case_statuses": {CASE_ID: "BLOCKED"},
        "root_failure": "app_soak_owner_incomplete",
        "go_emitted": False,
    }
    if not clean:
        return baseline
    observation = linked(result.get("profile"), inside=True)
    _require(observation.get("expected_context") == context_ref, "observed_context")
    _require(observation.get("source_manifest") == context["candidate"], "observed_candidate")
    _require(observation.get("load_scope") == context["load"], "observed_load")
    _require(
        soak._digest(observation.get("actual_policy")) == soak._digest(policy.to_payload()), "observed_policy"
    )
    report = observation.get("report")
    _require(isinstance(report, dict), "observed_report")
    try:
        expected = soak.build_expected(policy, local_day=context["local_day"])
        raw_check = soak.independent_evidence_check(
            root / "actual-evidence", policy=policy, expected=expected, provisional=report
        )
    except (soak.AppSoakError, KeyError, OSError, ValueError) as exc:
        raise AcceptanceError("app_soak_raw_evidence_invalid") from exc
    if raw_check.get("status") != "EVIDENCE_COMPLETE":
        return {
            **baseline,
            "root_failure": "app_soak_raw_evidence_incomplete",
            "evidence_status": "EVIDENCE_INCOMPLETE",
        }
    _require(report.get("independent_evidence") == raw_check, "reported_independent_verdict")
    status = raw_check.get("observed_profile_status")
    _require(status in {"PASS", "FAIL", "BLOCKED"}, "observed_status")
    _require(report.get("functional_status") == status, "reported_functional_status")
    _require((terminal["leader_returncode"] == 0) == (status == "PASS"), "exit_functional_status")
    elapsed = report.get("elapsed_sec")
    _require(type(elapsed) in {int, float} and math.isfinite(elapsed) and elapsed >= 0, "elapsed_invalid")
    _require(
        type(after.get("seconds")) in {int, float} and after["seconds"] >= elapsed, "outer_elapsed_invalid"
    )
    # A reported duration alone cannot establish a sustained observation.
    samples, sample_errors = soak._resource_observations(root / "actual-evidence", policy=policy)
    _require(not sample_errors and bool(samples), "raw_resource_evidence")
    starts = {sample.monotonic_ns - sample.elapsed_ns for sample in samples}
    _require(
        len(starts) == 1
        and next(iter(starts)) >= 0
        and all(
            left.monotonic_ns < right.monotonic_ns for left, right in zip(samples, samples[1:], strict=False)
        ),
        "raw_resource_clock",
    )
    observed_duration = samples[-1].elapsed_ns / 1_000_000_000
    _require(observed_duration <= elapsed + 0.000001, "raw_duration_exceeds_report")
    full = context["profile"] == "frozen" and len(samples) >= 2 and observed_duration >= load["duration_sec"]
    coverage = report.get("duration_coverage", {})
    mixed = full and coverage.get("full_planned_mixed_soak") is True
    case_status = status if status != "PASS" or mixed else "NOT_RUN"
    return {
        "status": case_status,
        "case_layers": {CASE_ID: "isolated-live"} if case_status == "PASS" else {},
        "case_statuses": {CASE_ID: case_status},
        "profile_status": status,
        "evidence_status": "EVIDENCE_COMPLETE",
        "frozen_duration_observed": full,
        "observed_duration_sec": observed_duration,
        "mixed_workload_observed": mixed,
        "operation_outcomes": report.get("operation_outcomes"),
        "root_failure": None
        if case_status == "PASS"
        else "app_soak_functional_fail"
        if status == "FAIL"
        else "app_soak_scope_incomplete",
        "go_emitted": False,
    }
