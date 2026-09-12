"""Check complete and forged synthetic receipts; fixtures never claim an app execution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools import release_1_0_acceptance as acceptance
from tools import release_1_0_app_soak as soak
from tools import release_1_0_soak_receipts as receipts


def _write(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _metadata(tmp_path):
    """Only a forged envelope; no raw observation or positive receipt exists."""
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    load_path = receipts.ROOT / "tools/release_1_0_app_soak_load.json"
    load = json.loads(load_path.read_bytes())
    load_ref = _write(tmp_path / "load.json", load)
    # Preserve the actual frozen bytes: its digest is bound into the candidate.
    Path(load_ref["path"]).write_bytes(load_path.read_bytes())
    load_ref["sha256"] = hashlib.sha256(load_path.read_bytes()).hexdigest()
    identity = {
        "candidate_sha": "1" * 40,
        "candidate_tree": "2" * 40,
        "base_sha": "3" * 40,
        "wheel_sha256": "4" * 64,
        "inventory_sha256": "5" * 64,
    }
    candidate = _write(
        tmp_path / "candidate.json",
        {
            "source_root": str(receipts.ROOT),
            "candidate_sha": identity["candidate_sha"],
            "candidate_tree": identity["candidate_tree"],
            "suite_revision": "fixture-suite",
        },
    )
    run_id = "6" * 32
    policy = {
        "schema": soak.POLICY_SCHEMA,
        "run_id": run_id,
        "candidate_sha256": candidate["sha256"],
        "suite_revision": "fixture-suite",
        "config_sha256": soak._digest({}),
        **{
            name: load[name]
            for name in (
                "duration_sec",
                "cadence_sec",
                "deadline_sec",
                "max_requests",
                "request_concurrency",
                "resources",
            )
        },
        "principals": [
            {"user_id": "owner", "chat_id": 5001, "preset_key": "owner"},
            *({"user_id": f"user-{n}", "chat_id": 5001 + n, "preset_key": "user"} for n in range(1, 4)),
        ],
    }
    runner = _write(tmp_path / "never-executed-runner.json", {"fixture": "metadata only"})
    context = {
        "schema": "friday.app-soak-expected-context.v1",
        "profile": "frozen",
        "output_root": str(attempt),
        "candidate_identity": identity,
        "candidate": candidate,
        "load": load_ref,
        "run_id": run_id,
        "policy": policy,
        "config_identity": {},
        "local_day": "2026-09-11",
        "runner": runner,
    }
    context_ref = _write(tmp_path / "context.json", context)
    plan_ref = _write(
        tmp_path / "plan.json",
        {
            "expected_context": context_ref,
            "run_id": run_id,
            "candidate": candidate,
            "load": load_ref,
            "root": str(attempt),
            "profile": "frozen",
        },
    )
    after = {
        "status": "TERMINAL",
        "candidate": candidate,
        "run_id": run_id,
        "expected_context": context_ref,
        "runner": runner,
        "plan": plan_ref,
        "integrity_errors": [],
        **{
            name: True
            for name in (
                "candidate_source_git_unchanged",
                "tools_unchanged",
                "protected_indexes_unchanged",
                "office_runtime_and_admission_unchanged",
                "run_inputs_unchanged",
            )
        },
        "outer_owner": {
            "outcome": "completed",
            "leader_returncode": 0,
            "timed_out": False,
            "error_code": None,
            "cancelled_signals": [],
            "stop_unconfirmed_emitted": False,
            "cleanup": {
                "kernel_echild": True,
                "leader_reaped": True,
                "subreaper_restored": True,
                "forced_leader": False,
                "forced_descendants": False,
                "initial_uncertain": False,
                "fault_codes": [],
            },
        },
    }
    after_ref = _write(attempt / "after.json", after)
    result = {
        "candidate": candidate,
        "after": after_ref,
        "plan": plan_ref,
        "cleanup_clear": True,
        "profile": None,
    }
    return attempt, context_ref, identity, after, result


def _audit(attempt, context_ref, identity, result):
    receipt = _write(attempt / "result.json", result)
    return receipts.audit_app_soak_execution(
        receipt_path=Path(receipt["path"]),
        receipt_sha256=receipt["sha256"],
        context_path=Path(context_ref["path"]),
        context_sha256=context_ref["sha256"],
        expected_identity=identity,
        expected_suite="fixture-suite",
    )


def test_soak_registration_points_to_actual_runner_and_does_not_credit_execution():
    case = next(item for item in acceptance.load_matrix()["cases"] if item["id"] == receipts.CASE_ID)
    handler, layer, nodes, driver = acceptance.registered_case_handlers()[receipts.CASE_ID]
    assert handler is soak.run_actual_app_soak
    assert driver == "app-soak" and layer == "isolated-live"
    assert case["release_required"] is True and case["timeout_s"] == 3780
    assert case["node_ids"] == list(nodes)
    assert case["execution_driver"] != "canonical-pytest"


def test_partial_soak_cli_evidence_is_refused_before_audit():
    with pytest.raises(SystemExit) as raised:
        acceptance.main(["--audit-only", "--app-soak-receipt", "/var/tmp/missing-soak.json"])
    assert raised.value.code == 2


@pytest.mark.parametrize("field", ["run_id", "expected_context", "runner", "candidate"])
def test_rehashed_outer_metadata_cannot_change_expected_attempt(tmp_path, field):
    attempt, context_ref, identity, after, result = _metadata(tmp_path)
    after[field] = "wrong"
    result["after"] = _write(attempt / "after.json", after)
    with pytest.raises(acceptance.AcceptanceError, match="app_soak_outer_"):
        _audit(attempt, context_ref, identity, result)


def test_green_outer_metadata_without_actual_profile_is_not_a_pass(tmp_path):
    attempt, context_ref, identity, _after, result = _metadata(tmp_path)
    with pytest.raises(acceptance.AcceptanceError, match="app_soak_reference_shape"):
        _audit(attempt, context_ref, identity, result)


@pytest.mark.parametrize("failure", ["cleanup", "integrity", "timeout"])
def test_incomplete_owned_attempt_is_blocked_without_profile_credit(tmp_path, failure):
    attempt, context_ref, identity, after, result = _metadata(tmp_path)
    if failure == "cleanup":
        after["outer_owner"]["cleanup"]["kernel_echild"] = False
    elif failure == "integrity":
        after["run_inputs_unchanged"] = False
    else:
        after["outer_owner"]["timed_out"] = True
    result["after"] = _write(attempt / "after.json", after)
    observed = _audit(attempt, context_ref, identity, result)
    assert observed["status"] == "BLOCKED"
    assert observed["case_layers"] == {}
    assert observed["case_statuses"] == {receipts.CASE_ID: "BLOCKED"}


def test_expected_context_must_be_outside_the_attempt(tmp_path):
    context = _write(
        tmp_path / "context.json",
        {
            "schema": "friday.app-soak-expected-context.v1",
            "profile": "frozen",
            "output_root": str(tmp_path),
        },
    )
    with pytest.raises(acceptance.AcceptanceError, match="app_soak_context_not_independent"):
        receipts.read_context(Path(context["path"]), context["sha256"])


@pytest.mark.parametrize("raw", [b'{"schema":1,"schema":2}', b'{"number":NaN}', b'{"number":1e400}'])
def test_duplicate_or_nonfinite_bound_context_is_not_accepted(tmp_path, raw):
    path = tmp_path / "context.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    with pytest.raises(acceptance.AcceptanceError, match="app_soak_bound_json_invalid"):
        receipts.read_context(path, hashlib.sha256(raw).hexdigest())


def _complete_synthetic_red_packet(tmp_path, monkeypatch, *, frozen=False):
    # Reuse the raw request/operation fixture, never mock either receipt reader.
    from tests import test_release_1_0_app_soak as mechanics

    attempt, context_ref, identity, after, result = _metadata(tmp_path)
    context = json.loads(Path(context_ref["path"]).read_text())
    payload = mechanics._policy_payload()
    payload.update(candidate_sha256=context["candidate"]["sha256"], suite_revision="fixture-suite")
    if frozen:
        load = json.loads(Path(context["load"]["path"]).read_text())
        for name in (
            "duration_sec",
            "cadence_sec",
            "deadline_sec",
            "max_requests",
            "request_concurrency",
            "resources",
        ):
            payload[name] = load[name]
    monkeypatch.setattr(mechanics, "_policy_payload", lambda: payload.copy())
    evidence = attempt / "actual-evidence"
    evidence.mkdir(mode=0o700)
    policy, expected, report = mechanics._truthful_red_evidence(evidence)
    report["elapsed_sec"] = 3600.0 if frozen else 1.0
    report["operation_outcomes"] = json.loads((evidence / "operations.json").read_text())["items"]
    report["independent_evidence"] = soak.independent_evidence_check(
        evidence, policy=policy, expected=expected, provisional=report
    )
    assert report["independent_evidence"]["status"] == "EVIDENCE_COMPLETE"
    context.update(
        profile="frozen" if frozen else "short",
        run_id=policy.run_id,
        policy=policy.to_payload(),
        config_identity=json.loads((evidence / "config-identity.json").read_text()),
        local_day="2035-01-02",
    )
    context_ref = _write(Path(context_ref["path"]), context)
    plan = json.loads(Path(after["plan"]["path"]).read_text())
    plan.update(expected_context=context_ref, run_id=policy.run_id, profile=context["profile"])
    result["plan"] = _write(Path(after["plan"]["path"]), plan)
    after.update(
        plan=result["plan"],
        expected_context=context_ref,
        run_id=policy.run_id,
        seconds=report["elapsed_sec"] + 2,
    )
    after["outer_owner"].update(outcome="child_failed", leader_returncode=4)
    result["after"] = _write(attempt / "after.json", after)
    observation = {
        "expected_context": context_ref,
        "source_manifest": context["candidate"],
        "load_scope": context["load"],
        "actual_policy": policy.to_payload(),
        "report": report,
    }
    result["profile"] = _write(attempt / "profile.json", observation)
    return attempt, context_ref, identity, result


def test_complete_raw_red_packet_is_ingested_without_hour_or_pass_credit(tmp_path, monkeypatch):
    attempt, context_ref, identity, result = _complete_synthetic_red_packet(tmp_path, monkeypatch)
    observed = _audit(attempt, context_ref, identity, result)
    assert observed["status"] == observed["profile_status"] == "FAIL"
    assert observed["evidence_status"] == "EVIDENCE_COMPLETE"
    assert observed["root_failure"] == "app_soak_functional_fail"
    assert observed["case_layers"] == {}
    assert observed["case_statuses"] == {receipts.CASE_ID: "FAIL"}
    assert observed["frozen_duration_observed"] is False
    assert observed["mixed_workload_observed"] is False
    assert observed["go_emitted"] is False
    assert any(item["status"] == "NOT_RUN" for item in observed["operation_outcomes"])


@pytest.mark.parametrize("corruption", ["missing_response", "changed_quote", "promoted_summary"])
def test_complete_packet_rechecks_raw_evidence_and_never_promotes_failure(tmp_path, monkeypatch, corruption):
    attempt, context_ref, identity, result = _complete_synthetic_red_packet(tmp_path, monkeypatch)
    evidence = attempt / "actual-evidence"
    if corruption == "missing_response":
        (evidence / "session-owner/request-000006.response.bin").unlink()
    elif corruption == "changed_quote":
        path = evidence / "session-owner/request-000004.response.bin"
        response = json.loads(path.read_text())
        response["items"][0]["excerpt"] = "wrong source content"
        body = json.dumps(response).encode()
        path.write_bytes(body)
        terminal = evidence / "session-owner/request-000004.terminal.json"
        data = json.loads(terminal.read_text())
        data.update(response_sha256=hashlib.sha256(body).hexdigest(), response_bytes=len(body))
        _write(terminal, data)
    else:
        profile = Path(result["profile"]["path"])
        data = json.loads(profile.read_text())
        data["report"].update(status="APP_SOAK_PROFILE_OBSERVED", functional_status="PASS")
        result["profile"] = _write(profile, data)
    observed = _audit(attempt, context_ref, identity, result)
    assert observed["status"] == "BLOCKED"
    assert observed["evidence_status"] == "EVIDENCE_INCOMPLETE"
    assert observed["root_failure"] == "app_soak_raw_evidence_incomplete"
    assert observed["case_layers"] == {}
    assert observed["go_emitted"] is False


def test_hour_duration_cannot_be_asserted_by_summary_with_one_nanosecond_raw_sample(tmp_path, monkeypatch):
    attempt, context_ref, identity, result = _complete_synthetic_red_packet(
        tmp_path, monkeypatch, frozen=True
    )
    observed = _audit(attempt, context_ref, identity, result)
    assert observed.get("frozen_duration_observed", False) is False
    assert observed["case_layers"] == {}
    assert observed["go_emitted"] is False


@pytest.mark.parametrize("corrupt_clock", [False, True])
def test_duration_uses_raw_clock_samples_and_never_creates_mixed_workload_credit(
    tmp_path, monkeypatch, corrupt_clock
):
    # Synthetic raw clocks prove reader mechanics, never a physical hour run.
    attempt, context_ref, identity, result = _complete_synthetic_red_packet(
        tmp_path, monkeypatch, frozen=True
    )
    evidence = attempt / "actual-evidence"
    sample = json.loads((evidence / "resource-000001.json").read_text())
    sample.update(
        sequence=2, elapsed_ns=3_600_000_000_000, monotonic_ns=3_600_000_000_001 + int(corrupt_clock)
    )
    _write(evidence / "resource-000002.json", sample)
    path = Path(result["profile"]["path"])
    observation = json.loads(path.read_text())
    observation["report"]["resource_samples"] = 2
    result["profile"] = _write(path, observation)
    if corrupt_clock:
        with pytest.raises(acceptance.AcceptanceError, match="app_soak_raw_resource_clock"):
            _audit(attempt, context_ref, identity, result)
    else:
        observed = _audit(attempt, context_ref, identity, result)
        assert observed["frozen_duration_observed"] is True
        assert observed["observed_duration_sec"] == 3600.0
        assert observed["mixed_workload_observed"] is False
        assert observed["status"] == "FAIL"
        assert observed["case_layers"] == {}
        assert observed["go_emitted"] is False
