"""Model-free adversarial checks for the new B09 receipt binder."""

from __future__ import annotations

import copy
import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_b09_evidence as evidence  # noqa: E402
import synthetic_live_battery as battery  # noqa: E402

KEY = b"synthetic-root-review-key-only-for-tests"
NOW = "2026-09-11T10:00:00Z"


def _questions() -> dict[str, str]:
    manifest = battery.load_manifest(battery.MANIFEST_PATHS["B"])
    return {
        case.id: case.question
        for case in battery.expand_manifest_cases(manifest)
        if case.id in evidence.OPEN_CASE_IDS
    }


def _reviewer() -> dict[str, Any]:
    return {
        "reviewer_id": "independent-reviewer",
        "reviewer_generation": "inst_" + "3" * 32,
        "reviewer_thread": "12345678-1234-4234-9234-123456789abc",
        "owner_generation": "owner-epoch-001",
        "owner_thread": "owner-thread-001",
        "authoring_session_id": "author-session-001",
        "implementer_session_id": "implementer-session-001",
        "assignment_id": "B09-CONTENT-REVIEW-001",
        "assignment_generation": 1,
        "task_event_id": "B09-CONTENT-REVIEW-001-T1",
        "result_event_id": "B09-CONTENT-REVIEW-001-R1",
    }


def _plan(
    *, lab_root: Path | None = None, pair_directory: str = "/var/tmp/synthetic-b09-unit/pair"
) -> dict[str, Any]:
    transport = {
        "protocol": "friday-lab/2",
        "root": "/var/tmp/unavailable-synthetic-lab",
        "device": 0,
        "inode": 0,
        "job_id": "job_" + "4" * 32,
        "instance_id": _reviewer()["reviewer_generation"],
        "session_id": _reviewer()["reviewer_thread"],
        "epoch": 14,
    }
    if lab_root is not None:
        transport = evidence.lab_provenance.preregister(lab_root, transport["job_id"])
    return evidence.create_plan(
        key=KEY,
        run_id="1" * 32,
        candidate_sha="c" * 64,
        manifest_sha256=battery.FROZEN_MANIFEST_SHA256,
        questions=_questions(),
        source_facts=evidence.expected_source_facts(battery),
        reviewer=_reviewer(),
        lab_transport=transport,
        pair_directory=pair_directory,
        issued_at=NOW,
    )


def _response(case_id: str) -> dict[str, Any]:
    return {
        "conversation_id": f"conversation-{case_id}",
        "message": f"Independent synthetic answer for {case_id} with adequate detail.",
        "message_id": f"message-{case_id}",
        "tools_used": [],
    }


def _closed_report(battery_id: str, *, plan: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime_hash = "a" * 64
    passes: list[dict[str, Any]] = []
    manifest = battery.load_manifest(battery.MANIFEST_PATHS[battery_id])
    cases = battery.expand_manifest_cases(manifest)
    for pass_index in range(1, battery.PASSES_PER_BATTERY + 1):
        pass_cases = [case for case in cases if case.pass_index == pass_index]
        rows = [
            {
                "case_id": case.id,
                "passed": True,
                "failure_codes": [],
                "response_sha256": evidence.sha256_bytes(evidence.canonical_json_bytes(_response(case.id))),
                "latency_ms": 1,
                "privacy_canary_clear": True,
            }
            for case in pass_cases
        ]
        passes.append(
            {
                "pass_id": f"{battery_id}-P{pass_index:02d}",
                "cases": battery.QUESTIONS_PER_PASS,
                "passed": battery.QUESTIONS_PER_PASS,
                "failed": 0,
                "case_results": rows,
                "pass_reconciliation_clear": True,
                "pass_reconciliation_sha256": "d" * 64,
                "runtime_hash": runtime_hash,
                "evidence_sha256": "e" * 64,
            }
        )
    report: dict[str, Any] = {
        "schema": battery.REPORT_SCHEMA,
        "battery_id": battery_id,
        "manifest_sha256": battery.FROZEN_MANIFEST_SHA256[battery_id],
        "passes": passes,
        "runtime_hashes": [runtime_hash] * battery.PASSES_PER_BATTERY,
        "evidence_hashes": ["e" * 64] * battery.PASSES_PER_BATTERY,
        "aggregates": {
            "passes": battery.PASSES_PER_BATTERY,
            "cases": battery.CASES_PER_BATTERY,
            "passed": battery.CASES_PER_BATTERY,
            "failed": 0,
            "privacy_canaries_clear": True,
            "all_passes_complete": True,
            "runtime_identity_consistent": True,
        },
    }
    if battery_id == "B":
        report["aggregates"]["content_acceptance_complete"] = False
    if plan is not None:
        report["suite_revision"] = evidence.SUITE_REVISION
        report["content_review"] = evidence.plan_binding(plan)
    return report


def _pair(plan: dict[str, Any]) -> dict[str, Any]:
    return evidence._closed_pair_document([_closed_report("A"), _closed_report("B", plan=plan)])


def _write_private(path: Path, value: Any) -> None:
    path.write_bytes(evidence.canonical_json_bytes(value) + b"\n")
    path.chmod(0o600)


def _write_b09_evidence(path: Path, *, pass_index: int = 9) -> None:
    manifest = battery.load_manifest(battery.MANIFEST_PATHS["B"])
    cases = [case for case in battery.expand_manifest_cases(manifest) if case.pass_index == pass_index]
    rows = [
        {
            "schema": battery.EVIDENCE_SCHEMA,
            "case_id": case.id,
            "question": case.question,
            "status_code": 200,
            "raw_response": _response(case.id),
            "response": _response(case.id),
            "state": {},
            "executor_error_class": "",
            "latency_ms": 1,
        }
        for case in cases
    ]
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _native_receipt(
    plan: dict[str, Any],
    *,
    artifact_sha256: str,
    artifact_size: int,
    direction: str,
) -> dict[str, Any]:
    reviewer = plan["reviewer"]
    if direction == "task":
        event_id = reviewer["task_event_id"]
        recipient_thread = reviewer["reviewer_thread"]
        recipient_generation = reviewer["reviewer_generation"]
        sender_generation = reviewer["owner_generation"]
        action = "active"
    else:
        event_id = reviewer["result_event_id"]
        recipient_thread = reviewer["owner_thread"]
        recipient_generation = reviewer["owner_generation"]
        sender_generation = reviewer["reviewer_generation"]
        action = "result"
    return {
        "event_id": event_id,
        "sha256": artifact_sha256,
        "recipient_thread": recipient_thread,
        "recipient_generation": recipient_generation,
        "sender_generation": sender_generation,
        "assignment_id": reviewer["assignment_id"],
        "assignment_generation": reviewer["assignment_generation"],
        "assignment_action": action,
        "state": "ENQUEUED",
        "notification_bytes": 400,
        "contract_bytes": artifact_size,
        "large_reason": "synthetic large content review" if artifact_size > 2_000 else None,
        "peer_receipt": "unconfirmed",
        "dispatch": "native_idle_boundary",
        "submission_id": "12345678-1234-4234-9234-123456789abc",
        "elapsed_ms": 10,
    }


def test_root_issuer_rejects_unsigned_fabricated_enqueue_claims(tmp_path: Path) -> None:
    """A real signed task does not authenticate an invented reviewer response.

    `_native_receipt` intentionally constructs public JSON without any native
    operation or root secret. Retain it as an unsigned forgery fixture.
    """
    root = tmp_path / "unsigned-claims"
    root.mkdir(mode=0o700)
    plan = _plan()
    raw = root / "raw.jsonl"
    _write_b09_evidence(raw)
    raw_b03 = root / "raw-b03.jsonl"
    _write_b09_evidence(raw_b03, pass_index=3)
    task = evidence.build_review_task(
        plan=plan,
        pair_report=_pair(plan),
        evidence_paths={3: raw_b03, 9: raw},
        questions=_questions(),
        closed_report_validator=_closed_green,
        key=KEY,
    )
    task_path = root / "task.json"
    _write_private(task_path, task)
    result = {
        "schema": evidence.REVIEW_RESULT_SCHEMA,
        "assignment_id": plan["reviewer"]["assignment_id"],
        "assignment_generation": 1,
        "plan_sha256": evidence.sha256_bytes(evidence.canonical_json_bytes(plan)),
        "task_sha256": evidence.file_sha256(task_path),
        "reviewer_id": plan["reviewer"]["reviewer_id"],
        "reviewer_generation": plan["reviewer"]["reviewer_generation"],
        "rubric_sha256": plan["rubric_sha256"],
        "cases": [
            {"case_id": case_id, "verdict": "pass", "rationale": "Invented review."}
            for case_id in evidence.OPEN_CASE_IDS
        ],
    }
    result_path = root / "result.json"
    _write_private(result_path, result)
    claims = {}
    for direction, path in [("task", task_path), ("result", result_path)]:
        claim = _native_receipt(
            plan,
            artifact_sha256=evidence.file_sha256(path),
            artifact_size=path.stat().st_size,
            direction=direction,
        )
        assert claim["state"] == "ENQUEUED" and claim["peer_receipt"] == "unconfirmed"
        assert "hmac_sha256" not in claim
        claim_path = root / (direction + "-claim.json")
        _write_private(claim_path, claim)
        claims[direction] = (claim, claim_path)
    with pytest.raises(evidence.B09EvidenceError, match="provenance"):
        evidence.issue_receipts(
            plan=plan,
            task=task,
            task_path=task_path,
            task_native_receipt=claims["task"][0],
            task_native_receipt_path=claims["task"][1],
            result=result,
            result_path=result_path,
            result_native_receipt=claims["result"][0],
            result_native_receipt_path=claims["result"][1],
            output_directory=root / "receipts",
            key=KEY,
            issued_at=NOW,
        )


def test_root_pair_without_preregistered_review_fails_before_executor(monkeypatch) -> None:
    def forbidden_executor(*args, **kwargs):
        del args, kwargs
        raise AssertionError("Uncertifiable pair must not construct a live executor")

    monkeypatch.setattr(battery, "SubprocessPassExecutor", forbidden_executor)
    with pytest.raises(SystemExit, match="b09-review-plan"):
        battery.main(["--both"])


def _closed_green(report: dict[str, Any]) -> bool:
    return battery._closed_report_is_green(report)


def _pair_green(reports: list[dict[str, Any]], acceptance: dict[str, Any], key: bytes) -> bool:
    return battery._pair_reports_green(
        reports,
        content_acceptance=acceptance,
        root_review_key=key,
    )


class _ClosedPassExecutor:
    def __init__(self) -> None:
        self.suite_revisions: list[str | None] = []

    def __call__(self, _manifest, _pass_spec, cases, context):  # noqa: ANN001, ANN204
        self.suite_revisions.append(context.suite_revision)
        rows = [
            {
                "case_id": case.id,
                "passed": True,
                "failure_codes": [],
                "response_sha256": evidence.sha256_bytes(evidence.canonical_json_bytes(_response(case.id))),
                "latency_ms": 1,
                "privacy_canary_clear": True,
            }
            for case in cases
        ]
        return {
            "pass_id": context.pass_id,
            "block": cases[0].block,
            "cases": battery.QUESTIONS_PER_PASS,
            "passed": battery.QUESTIONS_PER_PASS,
            "failed": 0,
            "case_results": rows,
            "evidence_sha256": "e" * 64,
            "runtime_hash": "a" * 64,
            "pass_reconciliation_clear": True,
            "pass_reconciliation_sha256": "d" * 64,
        }


def _make_lab_root(root: Path) -> None:
    root.mkdir(mode=0o700)
    for child in ("jobs", "messages", "deliveries", "artifacts"):
        (root / child).mkdir(mode=0o700)
    _write_private(
        root / "control.json",
        {
            "attachment": {
                "instance_id": _reviewer()["reviewer_generation"],
                "session_id": _reviewer()["reviewer_thread"],
                "epoch": 14,
            }
        },
    )


def _publish_synthetic_review_store(plan, task_path, result_path, mutation=None):
    """Protocol fixtures only: no model review or live acceptance is claimed."""
    transport, reviewer = plan["lab_transport"], plan["reviewer"]
    root = Path(transport["root"])
    job_id = transport["job_id"]
    run_id, result_id, delivery_id = "msg_" + "5" * 32, "msg_" + "6" * 32, "del_" + "7" * 32
    binding = {
        "assignment_id": reviewer["assignment_id"],
        "assignment_generation": 1,
        "plan_sha256": evidence.sha256_bytes(evidence.canonical_json_bytes(plan)),
        "task_event_id": reviewer["task_event_id"],
        "result_event_id": reviewer["result_event_id"],
        "task_path": str(task_path),
        "task_sha256": evidence.file_sha256(task_path),
    }
    payload = {
        "job_id": job_id,
        "goal": reviewer["assignment_id"],
        "execution_mode": "ATTACHED_TUI_MONITOR",
        "content_review": binding,
    }
    result_payload = {
        "job_id": job_id,
        "content_review": {
            **binding,
            "result_path": str(result_path),
            "result_sha256": evidence.file_sha256(result_path),
        },
    }

    def envelope(message_id, kind, sender, recipient, timestamp, **extra):
        import os

        return {
            "protocol": "friday-lab/2",
            "message_id": message_id,
            "type": kind,
            "from": sender,
            "to": recipient,
            "created_at": timestamp,
            "auth": {"euid": os.getuid()},
            **extra,
        }

    run = envelope(run_id, "RUN", "astra", "grok-lab", "2026-09-11T10:01:00Z", job_id=job_id, payload=payload)
    result = envelope(
        result_id,
        "RESULT",
        "grok-lab",
        "astra",
        "2026-09-11T10:04:00Z",
        job_id=job_id,
        payload=result_payload,
    )
    identity = {k: transport[k] for k in ("instance_id", "session_id")}
    claim = {
        **identity,
        "delivery_id": delivery_id,
        "source": "tui-monitor",
        "executor_ack": True,
        "claimed_at": "2026-09-11T10:01:01Z",
        "executor_ack_at": "2026-09-11T10:02:00Z",
        "waiting_current": False,
    }
    job = {
        "protocol": "friday-lab/2",
        "job_id": job_id,
        "from": "astra",
        "state": "DONE",
        "kind": "source-review",
        "identity": "owner",
        "task_revision": 1,
        "cancel_requested": False,
        "expired": False,
        "effect_unknown": False,
        "waiting_for": None,
        "run_message_id": run_id,
        "result_message_id": result_id,
        "payload": payload,
        "claim": claim,
        "created_at": run["created_at"],
        "updated_at": result["created_at"],
        "deadline_at": "2026-09-11T10:10:00Z",
    }
    delivery = {
        **identity,
        "protocol": "friday-lab/2",
        "delivery_id": delivery_id,
        "job_id": job_id,
        "message_id": run_id,
        "kind": "RUN",
        "epoch": 14,
        "executor_ack": True,
        "executor_ack_at": claim["executor_ack_at"],
        "created_at": claim["claimed_at"],
        "claim_source": "tui-monitor",
        "claimed": True,
        "stdout_confirmed": True,
        "submit_admitted": True,
        "needs_executor_ack": False,
    }
    ack = envelope(
        "msg_" + "8" * 32,
        "ACK",
        "grok-lab",
        "grok-lab",
        "2026-09-11T10:02:01Z",
        in_reply_to=run_id,
        payload={},
    )
    completion = envelope(
        "msg_" + "9" * 32,
        "ACK_RESULT",
        "grok-lab",
        "grok-lab",
        "2026-09-11T10:04:01Z",
        job_id=job_id,
        in_reply_to=result_id,
        payload={},
    )
    artifact = copy.deepcopy(result_payload)
    records = copy.deepcopy(
        {
            "job": job,
            "run": run,
            "result": result,
            "delivery": delivery,
            "artifact": artifact,
            "ack": ack,
            "completion": completion,
        }
    )
    if mutation:
        mutation(records)
    paths = {
        "job": root / "jobs" / f"{job_id}.json",
        "run": root / "messages" / f"{run_id}.json",
        "result": root / "messages" / f"{result_id}.json",
        "delivery": root / "deliveries" / f"{delivery_id}.json",
        "artifact": root / "artifacts" / job_id / "result.json",
        "ack": root / "messages" / f"{ack['message_id']}.json",
        "completion": root / "messages" / f"{completion['message_id']}.json",
    }
    (root / "artifacts" / job_id).mkdir(mode=0o700)
    for name, value in records.items():
        if value is not None:
            _write_private(paths[name], value)
    _write_private(
        root / "artifacts" / job_id / "submit_receipt.json",
        {
            "protocol": "friday-lab/2",
            "job_id": job_id,
            "message_id": run_id,
            "receipt_sha256": evidence.file_sha256(paths["run"]),
            "state": "QUEUED",
        },
    )


def _pipeline(
    tmp_path: Path,
    *,
    verdict: str = "pass",
    b15_verdict: str | None = None,
    mutate_store: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path]:
    tmp_path = tmp_path / "pipeline"
    tmp_path.mkdir(parents=True, mode=0o700)
    lab_root = tmp_path / "lab"
    _make_lab_root(lab_root)
    plan = _plan(lab_root=lab_root, pair_directory=str(tmp_path / "pair"))
    pair = _pair(plan)
    evidence_path = tmp_path / "raw-responses.jsonl"
    _write_b09_evidence(evidence_path)
    b03_path = tmp_path / "b03-responses.jsonl"
    _write_b09_evidence(b03_path, pass_index=3)
    task = evidence.build_review_task(
        plan=plan,
        pair_report=pair,
        evidence_paths={3: b03_path, 9: evidence_path},
        questions=_questions(),
        closed_report_validator=_closed_green,
        key=KEY,
    )
    task_path = tmp_path / "review-task.json"
    _write_private(task_path, task)
    result = {
        "schema": evidence.REVIEW_RESULT_SCHEMA,
        "assignment_id": plan["reviewer"]["assignment_id"],
        "assignment_generation": plan["reviewer"]["assignment_generation"],
        "plan_sha256": evidence.sha256_bytes(evidence.canonical_json_bytes(plan)),
        "task_sha256": evidence.file_sha256(task_path),
        "reviewer_id": plan["reviewer"]["reviewer_id"],
        "reviewer_generation": plan["reviewer"]["reviewer_generation"],
        "rubric_sha256": plan["rubric_sha256"],
        "cases": [
            {
                "case_id": case_id,
                "verdict": b15_verdict if case_id == "SYN-B03-15" and b15_verdict is not None else verdict,
                "rationale": "Fixed rubric applied.",
            }
            for case_id in evidence.OPEN_CASE_IDS
        ],
    }
    result_path = tmp_path / "review-result.json"
    _write_private(result_path, result)
    _publish_synthetic_review_store(plan, task_path, result_path, mutate_store)
    receipt_directory = tmp_path / "receipts"
    evidence.issue_receipts(
        plan=plan,
        task=task,
        task_path=task_path,
        result=result,
        result_path=result_path,
        output_directory=receipt_directory,
        key=KEY,
        issued_at="2026-09-11T10:05:00Z",
    )
    acceptance = evidence.build_acceptance(
        plan=plan,
        pair_report=pair,
        receipt_directory=receipt_directory,
        closed_report_validator=_closed_green,
        key=KEY,
        issued_at="2026-09-11T10:05:00Z",
    )
    return plan, pair, acceptance, receipt_directory


def test_plan_freezes_open_ids_rubric_questions_candidate_and_reviewer() -> None:
    plan = _plan()
    validated = evidence.validate_plan(
        plan,
        key=KEY,
        expected_candidate_sha="c" * 64,
        manifest_sha256=battery.FROZEN_MANIFEST_SHA256,
        questions=_questions(),
        source_facts=evidence.expected_source_facts(battery),
    )
    assert validated["open_case_ids"] == list(evidence.OPEN_CASE_IDS)
    assert validated["rubric"] == evidence.rubric()
    for field in ("candidate_sha", "run_id", "rubric_sha256", "question_sha256", "reviewer", "source_facts"):
        mutated = copy.deepcopy(plan)
        mutated[field] = {} if isinstance(mutated[field], dict) else "f" * 64
        with pytest.raises(evidence.B09EvidenceError, match="review_plan_invalid"):
            evidence.validate_plan(
                mutated,
                key=KEY,
                expected_candidate_sha="c" * 64,
                manifest_sha256=battery.FROZEN_MANIFEST_SHA256,
                questions=_questions(),
                source_facts=evidence.expected_source_facts(battery),
            )


def test_new_suite_opens_only_five_b09_meaning_checks_and_preserves_a09() -> None:
    b09_cases = [
        case
        for case in battery.expand_manifest_cases(battery.load_manifest(battery.MANIFEST_PATHS["B"]))
        if case.pass_index == 9 and case.id in evidence.OPEN_CASE_IDS
    ]
    assert [case.id for case in b09_cases] == [
        case_id for case_id in evidence.OPEN_CASE_IDS if case_id.startswith("SYN-B09-")
    ]
    for case in b09_cases:
        legacy = battery.oracle_for_case(case)
        revised = battery.oracle_for_case(case, suite_revision=evidence.SUITE_REVISION)
        assert legacy["content"]["contains_any"] or legacy["content"]["semantic_groups"]
        assert revised["content"]["contains_any"] == []
        assert revised["content"]["semantic_groups"] == []
        assert revised["content"]["min_chars"] == legacy["content"]["min_chars"] == 24
        assert revised["content"]["min_words"] == legacy["content"]["min_words"] == 4
        assert revised["content"]["excludes_all"] == legacy["content"]["excludes_all"]
        assert revised["state"] == legacy["state"]
        assert revised["structural"] == legacy["structural"]
    a09 = [
        case
        for case in battery.expand_manifest_cases(battery.load_manifest(battery.MANIFEST_PATHS["A"]))
        if case.pass_index == 9
    ]
    assert all(
        battery.oracle_for_case(case) == battery.oracle_for_case(case, suite_revision=evidence.SUITE_REVISION)
        for case in a09
    )


def test_closed_b_rows_cannot_make_report_or_pair_green() -> None:
    plan = _plan()
    pair = _pair(plan)
    assert battery._closed_report_is_green(pair["reports"][1]) is True
    assert battery._report_is_green(pair["reports"][1]) is False
    assert battery._pair_reports_green(pair["reports"]) is False


def test_b03_meta_oracle_changes_only_the_unbound_case_and_keeps_closed_guards() -> None:
    cases = [
        case
        for name in ("A", "B")
        for case in battery.expand_manifest_cases(battery.load_manifest(battery.MANIFEST_PATHS[name]))
        if case.pass_index == 3
    ]
    for case in cases:
        legacy = battery.oracle_for_case(case)
        current = battery.oracle_for_case(case, suite_revision=evidence.SUITE_REVISION)
        if case.id != "SYN-B03-15":
            assert current == legacy
            assert current["state"]["equals"]["office_exact_owned"] is True
            continue
        assert legacy["content"]["standalone_integer"] == 36
        assert current["content"]["standalone_integer"] is None
        assert current["content"]["min_chars"] == 24
        assert current["content"]["min_words"] == 4
        assert current["structural"] == legacy["structural"]
        assert current["state"]["min"] == legacy["state"]["min"]
        assert current["state"]["max"] == legacy["state"]["max"]
        state = current["state"]["equals"]
        assert state["office_exact_owned"] is False
        assert state["model_spoke"] is True
        assert state["llm_failed"] is False
        for key, value in legacy["state"]["equals"].items():
            if key != "office_exact_owned":
                assert state[key] == value


def test_b03_full_fixture_facts_are_signed_before_any_response() -> None:
    plan = _plan()
    facts = plan["source_facts"]["SYN-B03-15"]
    assert facts["filename"] == "syn-b03-15.csv"
    assert facts["data_records"] == 36
    assert facts["physical_rows"] == 37 and facts["columns"] == 2
    assert facts["rows"][0] == facts["header"] == ["ID", "Статус"]
    assert facts["rows"][1] == ["SYN-ROW-B03-15-01", "синтетика"]
    assert facts["rows"][-1] == ["SYN-ROW-B03-15-36", "синтетика"]
    assert evidence._source_facts_valid(plan["source_facts"])
    facts["rows"][1][0] = "INVENTED-SOURCE-ROW"
    assert not evidence._plan_integrity_valid(plan, KEY)


def test_b03_unsupported_content_cannot_be_hidden_by_five_passed_b09_reviews(tmp_path: Path) -> None:
    plan, pair, acceptance, receipt_directory = _pipeline(tmp_path, b15_verdict="fail")
    assert acceptance["accepted_count"] == 5
    assert acceptance["rejected_count"] == 1
    assert acceptance["all_content_accepted"] is False
    assert not battery._report_is_green(
        pair["reports"][1], content_acceptance=acceptance, root_review_key=KEY
    )
    task = evidence.load_private_json(receipt_directory.parent / "review-task.json")
    assert task["cases"][0]["case_id"] == "SYN-B03-15"
    assert task["cases"][0]["source_facts"] == plan["source_facts"]["SYN-B03-15"]
    assert all(case["source_facts"] is None for case in task["cases"][1:])
    task["cases"][0]["source_facts"] = None
    assert not evidence._task_is_valid(task, plan, KEY)


def test_missing_b03_receipt_and_changed_source_facts_cannot_use_old_acceptance(tmp_path: Path) -> None:
    plan, pair, acceptance, receipt_directory = _pipeline(tmp_path)
    report = copy.deepcopy(pair["reports"][1])
    report["content_review"]["source_facts"]["SYN-B03-15"]["rows"][1][0] = "FOREIGN-ROW"
    assert not battery._report_is_green(report, content_acceptance=acceptance, root_review_key=KEY)
    (receipt_directory / "SYN-B03-15.json").unlink()
    with pytest.raises(evidence.B09EvidenceError, match="receipt_set_invalid"):
        evidence.build_acceptance(
            plan=plan,
            pair_report=pair,
            receipt_directory=receipt_directory,
            closed_report_validator=_closed_green,
            key=KEY,
            issued_at=NOW,
        )


def test_new_battery_run_binds_plan_but_remains_closed_only(tmp_path: Path) -> None:
    plan = _plan(pair_directory=str(tmp_path / "new-b09-suite"))
    executor = _ClosedPassExecutor()
    report = battery.run_battery(
        battery.load_manifest(battery.MANIFEST_PATHS["B"]),
        manifest_sha256=battery.FROZEN_MANIFEST_SHA256["B"],
        run_directory=tmp_path / "new-b09-suite" / "battery-b",
        pass_executor=executor,
        review_plan=plan,
        root_review_key=KEY,
        candidate_sha="c" * 64,
    )
    assert executor.suite_revisions == [evidence.SUITE_REVISION] * battery.PASSES_PER_BATTERY
    assert report["suite_revision"] == evidence.SUITE_REVISION
    assert report["content_review"] == evidence.plan_binding(plan)
    assert report["aggregates"]["content_acceptance_complete"] is False
    assert battery._closed_report_is_green(report) is True
    assert battery._report_is_green(report) is False
    assert KEY.decode() not in json.dumps(report)


def test_authentic_full_receipt_set_makes_signed_final_pair_green(tmp_path: Path) -> None:
    plan, pair, acceptance, receipt_directory = _pipeline(tmp_path)
    assert acceptance["all_content_accepted"] is True
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in receipt_directory.iterdir())
    assert battery._report_is_green(pair["reports"][1], content_acceptance=acceptance, root_review_key=KEY)
    final = evidence.build_final_pair(
        pair_report=pair,
        acceptance=acceptance,
        pair_is_green=_pair_green,
        key=KEY,
        issued_at=NOW,
    )
    assert evidence.final_pair_is_green(final, key=KEY, pair_is_green=_pair_green) is True
    assert KEY.hex() not in json.dumps({"plan": plan, "final": final})


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.__setitem__("run_id", "2" * 32),
        lambda value: value.__setitem__("candidate_sha", "d" * 64),
        lambda value: value.__setitem__("suite_revision", "foreign-suite"),
        lambda value: value.__setitem__("question_sha256", "0" * 64),
        lambda value: value.__setitem__("response_sha256", "0" * 64),
        lambda value: value.__setitem__("rubric_sha256", "0" * 64),
        lambda value: value.__setitem__("reviewer_id", "foreign-reviewer"),
        lambda value: value.__setitem__("hmac_sha256", "0" * 64),
    ],
)
def test_mixed_replayed_or_forged_receipt_fails_binder(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    plan, pair, _acceptance, receipt_directory = _pipeline(tmp_path)
    path = receipt_directory / f"{evidence.OPEN_CASE_IDS[0]}.json"
    receipt = evidence.load_private_json(path)
    mutation(receipt)
    _write_private(path, receipt)
    with pytest.raises(evidence.B09EvidenceError, match="receipt_authenticity_or_binding_invalid"):
        evidence.build_acceptance(
            plan=plan,
            pair_report=pair,
            receipt_directory=receipt_directory,
            closed_report_validator=_closed_green,
            key=KEY,
            issued_at=NOW,
        )


def test_missing_receipt_fails_and_fail_verdict_cannot_be_green(tmp_path: Path) -> None:
    plan, pair, acceptance, receipt_directory = _pipeline(tmp_path / "fail", verdict="fail")
    assert acceptance["all_content_accepted"] is False
    assert not battery._report_is_green(
        pair["reports"][1], content_acceptance=acceptance, root_review_key=KEY
    )
    missing_root = tmp_path / "missing"
    plan, pair, _acceptance, receipt_directory = _pipeline(missing_root)
    (receipt_directory / f"{evidence.OPEN_CASE_IDS[-1]}.json").unlink()
    with pytest.raises(evidence.B09EvidenceError, match="receipt_set_invalid"):
        evidence.build_acceptance(
            plan=plan,
            pair_report=pair,
            receipt_directory=receipt_directory,
            closed_report_validator=_closed_green,
            key=KEY,
            issued_at=NOW,
        )


def test_pass_receipt_cannot_clear_closed_privacy_or_lifecycle_failure(tmp_path: Path) -> None:
    plan, pair, _acceptance, receipt_directory = _pipeline(tmp_path)
    row = evidence._case_rows(pair["reports"][1])[evidence.OPEN_CASE_IDS[0]]
    row["passed"] = False
    row["failure_codes"] = ["pass_lifecycle_unreconciled", "privacy_canary_exposed"]
    row["privacy_canary_clear"] = False
    with pytest.raises(evidence.B09EvidenceError, match="acceptance_plan_binding_invalid"):
        evidence.build_acceptance(
            plan=plan,
            pair_report=pair,
            receipt_directory=receipt_directory,
            closed_report_validator=_closed_green,
            key=KEY,
            issued_at=NOW,
        )


def test_issuer_rejects_wrong_lab_result_direction(tmp_path: Path) -> None:
    def mutate(store: dict[str, Any]) -> None:
        store["result"]["to"] = "grok-lab"

    with pytest.raises(evidence.B09EvidenceError, match="independent_review_provenance_invalid"):
        _pipeline(tmp_path, mutate_store=mutate)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda store: store["job"].__setitem__("state", "RUNNING"),
        lambda store: store["job"].__setitem__("expired", True),
        lambda store: store["job"].__setitem__("cancel_requested", True),
        lambda store: store["job"].__setitem__("effect_unknown", True),
        lambda store: store["job"].__setitem__("task_revision", 2),
        lambda store: store["job"].__setitem__("job_id", "job_" + "a" * 32),
        lambda store: store["job"]["claim"].__setitem__("executor_ack", False),
        lambda store: store["delivery"].__setitem__("executor_ack", False),
        lambda store: store["delivery"].__setitem__("session_id", "another-thread"),
        lambda store: store["job"]["claim"].__setitem__("instance_id", "inst_" + "a" * 32),
        lambda store: store["delivery"].__setitem__("stdout_confirmed", False),
        lambda store: store["job"].__setitem__("deadline_at", "2026-09-11T10:03:00Z"),
        lambda store: store["run"]["payload"]["content_review"].__setitem__("task_sha256", "0" * 64),
        lambda store: store["artifact"]["content_review"].__setitem__("result_sha256", "0" * 64),
        lambda store: store["result"].__setitem__("from", "astra"),
        lambda store: store.__setitem__("ack", None),
        lambda store: store.__setitem__("completion", None),
        lambda store: store["completion"].__setitem__("in_reply_to", "msg_" + "a" * 32),
        lambda store: store["ack"].__setitem__("auth", {"euid": -1}),
        lambda store: store["ack"].__setitem__("to", "astra"),
        lambda store: store["completion"].__setitem__("to", "astra"),
    ],
)
def test_issuer_rejects_incomplete_or_mixed_lab_execution(tmp_path: Path, mutation) -> None:
    with pytest.raises(evidence.B09EvidenceError, match="independent_review_provenance_invalid"):
        _pipeline(tmp_path, mutate_store=mutation)


@pytest.mark.parametrize("damage", ["duplicate-result", "rewritten-answer", "foreign-store", "missing-store"])
def test_issuer_cannot_reissue_from_forged_or_replaced_store(tmp_path: Path, damage: str) -> None:
    plan, _pair_value, _acceptance, receipts = _pipeline(tmp_path)
    root = receipts.parent
    lab_root = Path(plan["lab_transport"]["root"])
    task_path, result_path = root / "review-task.json", root / "review-result.json"
    task, result = evidence.load_private_json(task_path), evidence.load_private_json(result_path)
    if damage == "duplicate-result":
        message = evidence.load_private_json(lab_root / "messages" / ("msg_" + "6" * 32 + ".json"))
        message["message_id"] = "msg_" + "a" * 32
        _write_private(lab_root / "messages" / (message["message_id"] + ".json"), message)
    elif damage == "rewritten-answer":
        result["cases"][0]["verdict"] = "fail"
        _write_private(result_path, result)
    elif damage == "missing-store":
        (lab_root / "jobs" / (plan["lab_transport"]["job_id"] + ".json")).unlink()
    else:
        lab_root.rename(root / "original-lab")
        _make_lab_root(lab_root)
        _publish_synthetic_review_store(plan, task_path, result_path)
    with pytest.raises(evidence.B09EvidenceError, match="independent_review_provenance_invalid"):
        evidence.issue_receipts(
            plan=plan,
            task=task,
            task_path=task_path,
            result=result,
            result_path=result_path,
            output_directory=root / "forged-receipts",
            key=KEY,
            issued_at="2026-09-11T10:06:00Z",
        )


def test_same_review_plan_cannot_run_in_another_physical_pair(tmp_path: Path) -> None:
    plan = _plan(pair_directory=str(tmp_path / "preregistered"))
    executor = _ClosedPassExecutor()
    with pytest.raises(battery.BatteryContractError, match="b09_review_run_directory_mismatch"):
        battery.run_battery(
            battery.load_manifest(battery.MANIFEST_PATHS["B"]),
            manifest_sha256=battery.FROZEN_MANIFEST_SHA256["B"],
            run_directory=tmp_path / "another-pair" / "battery-b",
            pass_executor=executor,
            review_plan=plan,
            root_review_key=KEY,
            candidate_sha="c" * 64,
        )
    assert executor.suite_revisions == []
    assert not (tmp_path / "another-pair").exists()


def test_invalid_preregistered_plan_fails_before_live_executor(tmp_path: Path, monkeypatch) -> None:
    plan_path, key_path = tmp_path / "plan.json", tmp_path / "root.key"
    _write_private(plan_path, _plan())
    key_path.write_bytes(b"wrong-but-well-sized-private-key-for-negative")
    key_path.chmod(0o600)
    monkeypatch.setattr(battery, "_candidate_source_digest", lambda: "c" * 64)

    def refuse(*args, **kwargs):
        raise AssertionError("Invalid plan must fail before the live executor")

    monkeypatch.setattr(battery, "SubprocessPassExecutor", refuse)
    with pytest.raises(battery.b09_evidence.B09EvidenceError, match="review_plan_invalid"):
        battery.main(
            [
                "--both",
                "--b09-review-plan",
                str(plan_path),
                "--root-review-key",
                str(key_path),
                "--run-directory",
                str(tmp_path / "pair"),
            ]
        )
    assert not (tmp_path / "pair").exists()


def test_tampered_acceptance_and_final_aggregate_fail_closed(tmp_path: Path) -> None:
    _plan_value, pair, acceptance, _receipt_directory = _pipeline(tmp_path)
    tampered = copy.deepcopy(acceptance)
    tampered["run_id"] = "2" * 32
    assert not battery._report_is_green(pair["reports"][1], content_acceptance=tampered, root_review_key=KEY)
    final = evidence.build_final_pair(
        pair_report=pair,
        acceptance=acceptance,
        pair_is_green=_pair_green,
        key=KEY,
        issued_at=NOW,
    )
    final["aggregates"]["pair_clean"] = False
    assert evidence.final_pair_is_green(final, key=KEY, pair_is_green=_pair_green) is False
