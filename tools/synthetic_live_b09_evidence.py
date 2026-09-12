#!/usr/bin/env python3
"""Authenticate independent B09 content review for a sealed synthetic pair.

The live runner owns transport, privacy, state and other closed checks.  This
sibling binder adds the six open-meaning verdicts without changing case rows
or letting review clear a closed failure.  A root-held HMAC key authenticates
both the preregistered plan and receipts; the key is never put in Friday HOME,
an environment variable, a worker request or a review payload.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__:
    from tools import synthetic_live_b09_lab as lab_provenance
else:
    import synthetic_live_b09_lab as lab_provenance

PLAN_SCHEMA = "friday.synthetic-live-b09-review-plan.v2"
RUBRIC_SCHEMA = "friday.synthetic-live-b09-rubric.v1"
PLAN_BINDING_SCHEMA = "friday.synthetic-live-b09-plan-binding.v1"
REVIEW_TASK_SCHEMA = "friday.synthetic-live-b09-review-task.v2"
REVIEW_RESULT_SCHEMA = "friday.synthetic-live-b09-review-result.v1"
RECEIPT_SCHEMA = "friday.synthetic-live-b09-review-receipt.v1"
ACCEPTANCE_SCHEMA = "friday.synthetic-live-b09-content-acceptance.v1"
FINAL_PAIR_SCHEMA = "friday.synthetic-live-battery.final-pair-report.v1"
PAIR_REPORT_SCHEMA = "friday.synthetic-live-battery.pair-report.v1"
SUITE_REVISION = "r10-astra-harness-086-independent-content"
OPEN_CASE_IDS = (
    "SYN-B03-15",
    "SYN-B09-02",
    "SYN-B09-04",
    "SYN-B09-06",
    "SYN-B09-08",
    "SYN-B09-10",
)
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_REVIEW_MESSAGE_BYTES = 128 * 1024
PASSES_PER_BATTERY = 10
QUESTIONS_PER_PASS = 20
CASES_PER_BATTERY = PASSES_PER_BATTERY * QUESTIONS_PER_PASS
_KEY_MIN_BYTES = 32
_KEY_MAX_BYTES = 4096
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{2,191}")
_NATIVE_EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_NATIVE_ASSIGNMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_HEX_32 = re.compile(r"[0-9a-f]{32}")
_HEX_40_OR_64 = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# Criteria are fixed before any answer is observed.  They intentionally state
# relations and contradictions rather than accepted words or golden answers.
_RUBRIC_CRITERIA = (
    {
        "case_id": "SYN-B03-15",
        "expected_verdict": "pass",
        "criterion": (
            "The answer addresses the path-obligation question as unbound: it gives a useful "
            "explanation or informative conditional about the missing method contract. "
            "The opaque path name and pronoun do not determine a universal object count. "
            "Signed attachment facts may appear only as clearly extra or conditional context; "
            "they do not establish what the method is obliged to return. All concrete file "
            "count, header, range, filename and structure claims must match the supplied "
            "independent source facts. A correct bare file count is not sufficient."
        ),
        "reject_if": (
            "It attributes the attached file's row count to the path's obligation, even "
            "with the correct filename or header; answers only with a file count; invents "
            "a method contract, execution, verification, source or permission; gives an "
            "incorrect number; or provides empty, unrelated or unhelpful uncertainty. "
            "Do not infer a method contract from its name or the benchmark marker."
        ),
    },
    {
        "case_id": "SYN-B09-02",
        "expected_verdict": "pass",
        "criterion": (
            "The answer is a genuine analogy and connects repeatable experimental "
            "conditions with repeatable or comparable outcomes."
        ),
        "reject_if": (
            "It gives no analogy, leaves the relationship unstated, or claims that "
            "uncontrolled changes improve reproducibility."
        ),
    },
    {
        "case_id": "SYN-B09-04",
        "expected_verdict": "pass",
        "criterion": (
            "The answer explains a benefit of fixing inputs by connecting stable inputs "
            "to deterministic, reproducible, or meaningfully comparable observations."
        ),
        "reject_if": (
            "It is generic praise without the relation, or recommends varying the inputs "
            "while treating the result as the same controlled observation."
        ),
    },
    {
        "case_id": "SYN-B09-06",
        "expected_verdict": "pass",
        "criterion": (
            "The definition separates a test environment from external, shared, or prior-test "
            "state and identifies prevention of cross-influence as the purpose."
        ),
        "reject_if": (
            "It equates isolation with use of shared or live state, or omits the separation "
            "that prevents one source of state from changing another test."
        ),
    },
    {
        "case_id": "SYN-B09-08",
        "expected_verdict": "pass",
        "criterion": (
            "The answer gives neutral, safe, actionable testing advice that improves evidence, "
            "repeatability, isolation, coverage, or diagnosis."
        ),
        "reject_if": (
            "It recommends deleting, disabling, skipping, weakening, or making tests depend "
            "on order, hidden state, or uncontrolled external effects."
        ),
    },
    {
        "case_id": "SYN-B09-10",
        "expected_verdict": "pass",
        "criterion": (
            "The answer explains controlled clock as fixed or injected controllable time and "
            "connects that control to deterministic or reproducible behavior."
        ),
        "reject_if": (
            "It treats uncontrolled wall-clock time as equivalent, or omits both time control "
            "and its deterministic purpose."
        ),
    },
)


class B09EvidenceError(ValueError):
    """A B09 evidence artifact is missing, mixed, replayed or malformed."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _HEX_64.fullmatch(value) is not None


def _is_identity(value: Any) -> bool:
    return isinstance(value, str) and _SAFE_ID.fullmatch(value) is not None


def _aware_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def read_root_key(path: Path, *, forbidden_roots: Sequence[Path] = ()) -> bytes:
    """Read one private regular key without following a symlink."""

    if not path.is_absolute():
        raise B09EvidenceError("root_key_path_not_absolute")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise B09EvidenceError("root_key_unavailable") from exc
    resolved = path.resolve()
    if (
        resolved != path
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or any(_relative_to(resolved, root.resolve()) for root in forbidden_roots)
    ):
        raise B09EvidenceError("root_key_boundary_invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        value = os.read(descriptor, _KEY_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if not _KEY_MIN_BYTES <= len(value) <= _KEY_MAX_BYTES:
        raise B09EvidenceError("root_key_size_invalid")
    return value


def load_private_json(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise B09EvidenceError("artifact_unavailable") from exc
    if (
        not path.is_absolute()
        or path.resolve() != path
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or not 0 < metadata.st_size <= max_bytes
    ):
        raise B09EvidenceError("artifact_boundary_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise B09EvidenceError("artifact_json_invalid") from exc
    if not isinstance(value, dict):
        raise B09EvidenceError("artifact_root_invalid")
    return value


def secure_write_json(path: Path, value: Any) -> None:
    if not path.is_absolute() or path.parent.resolve() != path.parent:
        raise B09EvidenceError("output_path_invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise B09EvidenceError("output_create_failed") from exc
    try:
        payload = canonical_json_bytes(value) + b"\n"
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise B09EvidenceError("output_mode_invalid")


def rubric() -> dict[str, Any]:
    return {
        "schema": RUBRIC_SCHEMA,
        "criteria": [dict(item) for item in _RUBRIC_CRITERIA],
    }


def rubric_sha256() -> str:
    return sha256_bytes(canonical_json_bytes(rubric()))


def result_contract() -> dict[str, Any]:
    return {
        "schema": REVIEW_RESULT_SCHEMA,
        "top_level_fields": [
            "schema",
            "assignment_id",
            "assignment_generation",
            "plan_sha256",
            "task_sha256",
            "reviewer_id",
            "reviewer_generation",
            "rubric_sha256",
            "cases",
        ],
        "case_fields": ["case_id", "verdict", "rationale"],
        "ordered_case_ids": list(OPEN_CASE_IDS),
        "allowed_verdicts": ["pass", "fail"],
    }


def _mac(unsigned: Mapping[str, Any], key: bytes, domain: str) -> str:
    if type(key) is not bytes or not _KEY_MIN_BYTES <= len(key) <= _KEY_MAX_BYTES:
        raise B09EvidenceError("root_key_size_invalid")
    return hmac.new(key, domain.encode() + b"\0" + canonical_json_bytes(unsigned), hashlib.sha256).hexdigest()


def _sign(unsigned: Mapping[str, Any], key: bytes, domain: str) -> dict[str, Any]:
    signed = dict(unsigned)
    signed["hmac_sha256"] = _mac(unsigned, key, domain)
    return signed


def _verify_signature(value: Mapping[str, Any], key: bytes, domain: str) -> bool:
    signature = value.get("hmac_sha256")
    if not _is_sha256(signature):
        return False
    unsigned = {name: item for name, item in value.items() if name != "hmac_sha256"}
    return hmac.compare_digest(str(signature), _mac(unsigned, key, domain))


def _reviewer_is_valid(reviewer: Any) -> bool:
    fields = {
        "reviewer_id",
        "reviewer_generation",
        "reviewer_thread",
        "owner_generation",
        "owner_thread",
        "authoring_session_id",
        "implementer_session_id",
        "assignment_id",
        "assignment_generation",
        "task_event_id",
        "result_event_id",
    }
    if not isinstance(reviewer, Mapping) or set(reviewer) != fields:
        return False
    string_fields = fields - {"assignment_generation"}
    if any(not _is_identity(reviewer.get(field)) for field in string_fields):
        return False
    if type(reviewer.get("assignment_generation")) is not int or reviewer["assignment_generation"] < 1:
        return False
    if (
        _NATIVE_ASSIGNMENT_ID.fullmatch(reviewer["assignment_id"]) is None
        or _NATIVE_EVENT_ID.fullmatch(reviewer["task_event_id"]) is None
        or _NATIVE_EVENT_ID.fullmatch(reviewer["result_event_id"]) is None
    ):
        return False
    reviewer_ids = {
        reviewer["reviewer_id"],
        reviewer["reviewer_generation"],
        reviewer["reviewer_thread"],
    }
    disallowed_ids = {
        reviewer["owner_generation"],
        reviewer["owner_thread"],
        reviewer["authoring_session_id"],
        reviewer["implementer_session_id"],
    }
    return bool(
        reviewer_ids.isdisjoint(disallowed_ids) and reviewer["task_event_id"] != reviewer["result_event_id"]
    )


def _source_facts_valid(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"SYN-B03-15"}:
        return False
    facts = value["SYN-B03-15"]
    return bool(
        isinstance(facts, Mapping)
        and set(facts) == {"filename", "sha256", "data_records", "columns", "header", "physical_rows", "rows"}
        and isinstance(facts["filename"], str)
        and Path(facts["filename"]).name == facts["filename"]
        and _is_sha256(facts["sha256"])
        and type(facts["data_records"]) is int
        and facts["data_records"] >= 0
        and type(facts["columns"]) is int
        and facts["columns"] > 0
        and type(facts["physical_rows"]) is int
        and facts["physical_rows"] == facts["data_records"] + 1
        and isinstance(facts["header"], list)
        and len(facts["header"]) == facts["columns"]
        and all(isinstance(field, str) and field for field in facts["header"])
        and isinstance(facts["rows"], list)
        and len(facts["rows"]) == facts["physical_rows"]
        and facts["rows"][0] == facts["header"]
        and all(
            isinstance(row, list)
            and len(row) == facts["columns"]
            and all(isinstance(cell, str) for cell in row)
            for row in facts["rows"]
        )
    )


def expected_source_facts(battery: Any) -> dict[str, Any]:
    """Freeze facts from submitted fixture bytes, without consulting a response."""
    import base64

    cases = battery.expand_manifest_cases(battery.load_manifest(battery.MANIFEST_PATHS["B"]))
    case = next(case for case in cases if case.id == "SYN-B03-15")
    document = battery._case_document(case)
    rows = battery._document_fixture_rows(case)
    payload = base64.b64decode(document["content_base64"], validate=True)
    return {
        case.id: {
            "filename": document["filename"],
            "sha256": sha256_bytes(payload),
            "data_records": len(rows) - 1,
            "columns": len(rows[0]),
            "header": rows[0],
            "physical_rows": len(rows),
            "rows": rows,
        }
    }


def create_plan(
    *,
    key: bytes,
    run_id: str,
    candidate_sha: str,
    manifest_sha256: Mapping[str, str],
    questions: Mapping[str, str],
    source_facts: Mapping[str, Any],
    reviewer: Mapping[str, Any],
    lab_transport: Mapping[str, Any],
    pair_directory: str,
    issued_at: str,
) -> dict[str, Any]:
    expected_questions = set(OPEN_CASE_IDS)
    if (
        _HEX_32.fullmatch(run_id) is None
        or _HEX_40_OR_64.fullmatch(candidate_sha) is None
        or set(manifest_sha256) != {"A", "B"}
        or any(not _is_sha256(value) for value in manifest_sha256.values())
        or set(questions) != expected_questions
        or any(not isinstance(value, str) or not value for value in questions.values())
        or not _source_facts_valid(source_facts)
        or not _reviewer_is_valid(reviewer)
        or not lab_provenance.transport_is_valid(lab_transport)
        or reviewer["reviewer_thread"] != lab_transport["session_id"]
        or reviewer["reviewer_generation"] != lab_transport["instance_id"]
        or not isinstance(pair_directory, str)
        or not Path(pair_directory).is_absolute()
        or str(Path(pair_directory).resolve()) != pair_directory
        or not _aware_timestamp(issued_at)
    ):
        raise B09EvidenceError("plan_input_invalid")
    frozen_rubric = rubric()
    unsigned = {
        "schema": PLAN_SCHEMA,
        "suite_revision": SUITE_REVISION,
        "run_id": run_id,
        "candidate_sha": candidate_sha,
        "manifest_sha256": dict(manifest_sha256),
        "open_case_ids": list(OPEN_CASE_IDS),
        "question_sha256": {case_id: sha256_bytes(questions[case_id].encode()) for case_id in OPEN_CASE_IDS},
        "rubric": frozen_rubric,
        "rubric_sha256": sha256_bytes(canonical_json_bytes(frozen_rubric)),
        "source_facts": dict(source_facts),
        "reviewer": dict(reviewer),
        "lab_transport": dict(lab_transport),
        "pair_directory": pair_directory,
        "issued_at": issued_at,
    }
    return _sign(unsigned, key, PLAN_SCHEMA)


def _plan_integrity_valid(value: Any, key: bytes) -> bool:
    fields = {
        "schema",
        "suite_revision",
        "run_id",
        "candidate_sha",
        "manifest_sha256",
        "open_case_ids",
        "question_sha256",
        "source_facts",
        "rubric",
        "rubric_sha256",
        "reviewer",
        "lab_transport",
        "pair_directory",
        "issued_at",
        "hmac_sha256",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or not isinstance(value.get("manifest_sha256"), Mapping)
        or not isinstance(value.get("question_sha256"), Mapping)
    ):
        return False
    try:
        signature_valid = _verify_signature(value, key, PLAN_SCHEMA)
    except B09EvidenceError:
        return False
    return bool(
        value.get("schema") == PLAN_SCHEMA
        and value.get("suite_revision") == SUITE_REVISION
        and _HEX_32.fullmatch(str(value.get("run_id") or "")) is not None
        and _HEX_40_OR_64.fullmatch(str(value.get("candidate_sha") or "")) is not None
        and set(value["manifest_sha256"]) == {"A", "B"}
        and all(_is_sha256(item) for item in value["manifest_sha256"].values())
        and value.get("open_case_ids") == list(OPEN_CASE_IDS)
        and set(value["question_sha256"]) == set(OPEN_CASE_IDS)
        and all(_is_sha256(item) for item in value["question_sha256"].values())
        and _source_facts_valid(value.get("source_facts"))
        and value.get("rubric") == rubric()
        and value.get("rubric_sha256") == rubric_sha256()
        and _reviewer_is_valid(value.get("reviewer"))
        and lab_provenance.transport_is_valid(value.get("lab_transport"))
        and value["reviewer"]["reviewer_thread"] == value["lab_transport"]["session_id"]
        and value["reviewer"]["reviewer_generation"] == value["lab_transport"]["instance_id"]
        and isinstance(value.get("pair_directory"), str)
        and Path(value["pair_directory"]).is_absolute()
        and str(Path(value["pair_directory"]).resolve()) == value["pair_directory"]
        and _aware_timestamp(value.get("issued_at"))
        and signature_valid
    )


def validate_plan(
    value: Any,
    *,
    key: bytes,
    expected_candidate_sha: str,
    manifest_sha256: Mapping[str, str],
    questions: Mapping[str, str],
    source_facts: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or not isinstance(value.get("manifest_sha256"), Mapping)
        or not isinstance(value.get("question_sha256"), Mapping)
        or not isinstance(value.get("reviewer"), Mapping)
    ):
        raise B09EvidenceError("review_plan_invalid")
    expected_question_hashes = {
        case_id: sha256_bytes(questions[case_id].encode()) for case_id in OPEN_CASE_IDS
    }
    valid = bool(
        _plan_integrity_valid(value, key)
        and value.get("candidate_sha") == expected_candidate_sha
        and dict(value["manifest_sha256"]) == dict(manifest_sha256)
        and value.get("question_sha256") == expected_question_hashes
        and value.get("source_facts") == source_facts
    )
    if not valid:
        raise B09EvidenceError("review_plan_invalid")
    return dict(value)


def plan_binding(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": PLAN_BINDING_SCHEMA,
        "status": "independent_content_review_required",
        "plan_sha256": sha256_bytes(canonical_json_bytes(plan)),
        "run_id": plan["run_id"],
        "candidate_sha": plan["candidate_sha"],
        "manifest_sha256": dict(plan["manifest_sha256"]),
        "suite_revision": plan["suite_revision"],
        "rubric_sha256": plan["rubric_sha256"],
        "reviewer_id": plan["reviewer"]["reviewer_id"],
        "open_case_ids": list(OPEN_CASE_IDS),
        "question_sha256": dict(plan["question_sha256"]),
        "source_facts": dict(plan["source_facts"]),
    }


def report_has_plan(report: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
    return bool(
        report.get("battery_id") == "B"
        and report.get("suite_revision") == SUITE_REVISION
        and report.get("content_review") == plan_binding(plan)
        and isinstance(report.get("aggregates"), Mapping)
        and report["aggregates"].get("content_acceptance_complete") is False
    )


def _pair_reports(pair_report: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    reports = pair_report.get("reports")
    if (
        not isinstance(reports, list)
        or len(reports) != 2
        or any(not isinstance(item, Mapping) for item in reports)
        or [item.get("battery_id") for item in reports] != ["A", "B"]
    ):
        raise B09EvidenceError("pair_report_invalid")
    return reports[0], reports[1]


def _closed_pair_document(reports: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    runtime_hashes = [
        value
        for report in reports
        for value in (report.get("runtime_hashes") if isinstance(report.get("runtime_hashes"), list) else [])
    ]
    return {
        "schema": PAIR_REPORT_SCHEMA,
        "reports": list(reports),
        "aggregates": {
            "batteries": len(reports),
            "passes": sum(int(report["aggregates"]["passes"]) for report in reports),
            "cases": sum(int(report["aggregates"]["cases"]) for report in reports),
            "failed": sum(int(report["aggregates"]["failed"]) for report in reports),
            "privacy_canaries_clear": all(
                report["aggregates"].get("privacy_canaries_clear") is True for report in reports
            ),
            "runtime_identity_consistent": bool(runtime_hashes) and len(set(runtime_hashes)) == 1,
            "content_acceptance_complete": False,
            "pair_clean": False,
        },
    }


def _case_rows(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows: dict[str, Mapping[str, Any]] = {}
    passes = report.get("passes")
    if not isinstance(passes, list):
        return rows
    for pass_result in passes:
        if not isinstance(pass_result, Mapping):
            continue
        case_results = pass_result.get("case_results")
        if not isinstance(case_results, list):
            continue
        for row in case_results:
            if isinstance(row, Mapping) and isinstance(row.get("case_id"), str):
                case_id = str(row["case_id"])
                if case_id in rows:
                    return {}
                rows[case_id] = row
    return rows


def _closed_case_rows_are_green(report: Mapping[str, Any]) -> bool:
    battery_id = str(report.get("battery_id") or "")
    passes = report.get("passes")
    if battery_id not in {"A", "B"} or not isinstance(passes, list) or len(passes) != PASSES_PER_BATTERY:
        return False
    observed_ids: list[str] = []
    expected_fields = {
        "case_id",
        "passed",
        "failure_codes",
        "response_sha256",
        "latency_ms",
        "privacy_canary_clear",
    }
    for pass_index, pass_result in enumerate(passes, start=1):
        if not isinstance(pass_result, Mapping):
            return False
        rows = pass_result.get("case_results")
        expected_ids = [
            f"SYN-{battery_id}{pass_index:02d}-{question_index:02d}"
            for question_index in range(1, QUESTIONS_PER_PASS + 1)
        ]
        if not isinstance(rows, list) or len(rows) != QUESTIONS_PER_PASS:
            return False
        if [row.get("case_id") for row in rows if isinstance(row, Mapping)] != expected_ids:
            return False
        for row in rows:
            if (
                not isinstance(row, Mapping)
                or set(row) != expected_fields
                or row.get("passed") is not True
                or row.get("failure_codes") != []
                or not _is_sha256(row.get("response_sha256"))
                or type(row.get("latency_ms")) is not int
                or row["latency_ms"] < 0
                or row.get("privacy_canary_clear") is not True
            ):
                return False
        observed_ids.extend(expected_ids)
    return len(observed_ids) == len(set(observed_ids)) == CASES_PER_BATTERY


def _load_evidence_rows(path: Path) -> dict[str, Mapping[str, Any]]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise B09EvidenceError("b09_evidence_unavailable") from exc
    if (
        not path.is_absolute()
        or path.resolve() != path
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or not 0 < metadata.st_size <= MAX_JSON_BYTES
    ):
        raise B09EvidenceError("b09_evidence_boundary_invalid")
    rows: dict[str, Mapping[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            value = json.loads(line)
            case_id = str(value.get("case_id") or "") if isinstance(value, Mapping) else ""
            if not case_id or case_id in rows:
                raise B09EvidenceError("b09_evidence_case_invalid")
            rows[case_id] = value
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise B09EvidenceError("b09_evidence_json_invalid") from exc
    if len(rows) != 20:
        raise B09EvidenceError("b09_evidence_count_invalid")
    return rows


def build_review_task(
    *,
    plan: Mapping[str, Any],
    pair_report: Mapping[str, Any],
    evidence_paths: Mapping[int, Path],
    questions: Mapping[str, str],
    closed_report_validator: Callable[[Mapping[str, Any]], bool],
    key: bytes,
) -> dict[str, Any]:
    plan_digest = sha256_bytes(canonical_json_bytes(plan))
    a_report, b_report = _pair_reports(pair_report)
    if (
        not _plan_integrity_valid(plan, key)
        or pair_report != _closed_pair_document((a_report, b_report))
        or not closed_report_validator(a_report)
        or not closed_report_validator(b_report)
        or not _closed_case_rows_are_green(a_report)
        or not _closed_case_rows_are_green(b_report)
        or not report_has_plan(b_report, plan)
    ):
        raise B09EvidenceError("closed_pair_or_plan_invalid")
    aggregate_rows = _case_rows(b_report)
    if set(evidence_paths) != {3, 9}:
        raise B09EvidenceError("review_evidence_pass_set_invalid")
    evidence_rows: dict[str, Mapping[str, Any]] = {}
    for pass_index, path in evidence_paths.items():
        rows = _load_evidence_rows(path)
        if set(rows) != {f"SYN-B{pass_index:02d}-{index:02d}" for index in range(1, 21)}:
            raise B09EvidenceError("review_evidence_pass_binding_invalid")
        evidence_rows.update(rows)
    task_cases: list[dict[str, Any]] = []
    for case_id in OPEN_CASE_IDS:
        row = aggregate_rows.get(case_id)
        evidence = evidence_rows.get(case_id)
        response = evidence.get("response") if isinstance(evidence, Mapping) else None
        question = evidence.get("question") if isinstance(evidence, Mapping) else None
        message = response.get("message") if isinstance(response, Mapping) else None
        if (
            not isinstance(row, Mapping)
            or row.get("passed") is not True
            or row.get("failure_codes") != []
            or not isinstance(response, Mapping)
            or question != questions[case_id]
            or sha256_bytes(str(question).encode()) != plan["question_sha256"][case_id]
            or sha256_bytes(canonical_json_bytes(response)) != row.get("response_sha256")
            or not isinstance(message, str)
            or not message.strip()
            or len(message.encode()) > MAX_REVIEW_MESSAGE_BYTES
        ):
            raise B09EvidenceError("review_task_case_binding_invalid")
        task_cases.append(
            {
                "case_id": case_id,
                "question": question,
                "question_sha256": plan["question_sha256"][case_id],
                "response_sha256": row["response_sha256"],
                "message": message,
                "source_facts": plan["source_facts"].get(case_id),
            }
        )
    reviewer = plan["reviewer"]
    unsigned = {
        "schema": REVIEW_TASK_SCHEMA,
        "assignment_id": reviewer["assignment_id"],
        "assignment_generation": reviewer["assignment_generation"],
        "task_event_id": reviewer["task_event_id"],
        "result_event_id": reviewer["result_event_id"],
        "plan_sha256": plan_digest,
        "run_id": plan["run_id"],
        "candidate_sha": plan["candidate_sha"],
        "suite_revision": plan["suite_revision"],
        "rubric": plan["rubric"],
        "rubric_sha256": plan["rubric_sha256"],
        "reviewer_id": reviewer["reviewer_id"],
        "reviewer_generation": reviewer["reviewer_generation"],
        "instruction": (
            "Apply only the preregistered rubric independently to every ordered response; "
            "return no overall PASS and do not reinterpret closed safety evidence."
        ),
        "result_contract": result_contract(),
        "cases": task_cases,
    }
    return _sign(unsigned, key, REVIEW_TASK_SCHEMA)


def _task_is_valid(task: Any, plan: Mapping[str, Any], key: bytes) -> bool:
    reviewer = plan["reviewer"]
    fields = {
        "schema",
        "assignment_id",
        "assignment_generation",
        "task_event_id",
        "result_event_id",
        "plan_sha256",
        "run_id",
        "candidate_sha",
        "suite_revision",
        "rubric",
        "rubric_sha256",
        "reviewer_id",
        "reviewer_generation",
        "instruction",
        "result_contract",
        "cases",
        "hmac_sha256",
    }
    if (
        not isinstance(task, Mapping)
        or set(task) != fields
        or not _verify_signature(task, key, REVIEW_TASK_SCHEMA)
    ):
        return False
    cases = task.get("cases")
    expected_case_fields = {
        "case_id",
        "question",
        "question_sha256",
        "response_sha256",
        "message",
        "source_facts",
    }
    return bool(
        task.get("schema") == REVIEW_TASK_SCHEMA
        and task.get("assignment_id") == reviewer["assignment_id"]
        and task.get("assignment_generation") == reviewer["assignment_generation"]
        and task.get("task_event_id") == reviewer["task_event_id"]
        and task.get("result_event_id") == reviewer["result_event_id"]
        and task.get("plan_sha256") == sha256_bytes(canonical_json_bytes(plan))
        and task.get("run_id") == plan["run_id"]
        and task.get("candidate_sha") == plan["candidate_sha"]
        and task.get("suite_revision") == SUITE_REVISION
        and task.get("rubric") == rubric()
        and task.get("rubric_sha256") == rubric_sha256()
        and task.get("reviewer_id") == reviewer["reviewer_id"]
        and task.get("reviewer_generation") == reviewer["reviewer_generation"]
        and task.get("instruction")
        == (
            "Apply only the preregistered rubric independently to every ordered response; "
            "return no overall PASS and do not reinterpret closed safety evidence."
        )
        and task.get("result_contract") == result_contract()
        and isinstance(cases, list)
        and [item.get("case_id") for item in cases if isinstance(item, Mapping)] == list(OPEN_CASE_IDS)
        and all(
            isinstance(item, Mapping)
            and set(item) == expected_case_fields
            and item.get("question_sha256") == plan["question_sha256"][item["case_id"]]
            and item.get("source_facts") == plan["source_facts"].get(item["case_id"])
            and _is_sha256(item.get("response_sha256"))
            and isinstance(item.get("message"), str)
            and bool(item["message"].strip())
            for item in cases
        )
    )


def _review_result_is_valid(result: Any, plan: Mapping[str, Any], task_sha256: str) -> bool:
    reviewer = plan["reviewer"]
    fields = {
        "schema",
        "assignment_id",
        "assignment_generation",
        "plan_sha256",
        "task_sha256",
        "reviewer_id",
        "reviewer_generation",
        "rubric_sha256",
        "cases",
    }
    if not isinstance(result, Mapping) or set(result) != fields:
        return False
    cases = result.get("cases")
    return bool(
        result.get("schema") == REVIEW_RESULT_SCHEMA
        and result.get("assignment_id") == reviewer["assignment_id"]
        and result.get("assignment_generation") == reviewer["assignment_generation"]
        and result.get("plan_sha256") == sha256_bytes(canonical_json_bytes(plan))
        and result.get("task_sha256") == task_sha256
        and result.get("reviewer_id") == reviewer["reviewer_id"]
        and result.get("reviewer_generation") == reviewer["reviewer_generation"]
        and result.get("rubric_sha256") == rubric_sha256()
        and isinstance(cases, list)
        and [item.get("case_id") for item in cases if isinstance(item, Mapping)] == list(OPEN_CASE_IDS)
        and all(
            isinstance(item, Mapping)
            and set(item) == {"case_id", "verdict", "rationale"}
            and item.get("verdict") in {"pass", "fail"}
            and isinstance(item.get("rationale"), str)
            and 1 <= len(item["rationale"].strip()) <= 2_000
            for item in cases
        )
    )


def _review_response_rows(task: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(item["case_id"]): item for item in task["cases"]}


def issue_receipts(
    *,
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    task_path: Path,
    result: Mapping[str, Any],
    result_path: Path,
    output_directory: Path,
    key: bytes,
    issued_at: str,
    **untrusted_receipt_claims: Any,
) -> list[Path]:
    if untrusted_receipt_claims:
        raise B09EvidenceError("independent_review_provenance_invalid")
    try:
        task_file, task_sha = lab_provenance.read_private(task_path)
        result_file, result_sha = lab_provenance.read_private(result_path)
    except (OSError, ValueError, TypeError) as exc:
        raise B09EvidenceError("independent_review_provenance_invalid") from exc
    if (
        not _plan_integrity_valid(plan, key)
        or task_path != Path(plan["pair_directory"]).parent / "review-task.json"
        or result_path != Path(plan["pair_directory"]).parent / "review-result.json"
        or task_file != task
        or result_file != result
        or not _task_is_valid(task, plan, key)
        or not _review_result_is_valid(result, plan, task_sha)
        or not _aware_timestamp(issued_at)
    ):
        raise B09EvidenceError("independent_review_provenance_invalid")
    try:
        provenance = lab_provenance.capture(
            plan=dict(plan),
            task_path=task_path,
            task_sha256=task_sha,
            result_path=result_path,
            result_sha256=result_sha,
            issued_at=issued_at,
        )
        if (
            lab_provenance.read_private(task_path)[1] != task_sha
            or lab_provenance.read_private(result_path)[1] != result_sha
        ):
            raise B09EvidenceError("independent_review_provenance_invalid")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise B09EvidenceError("independent_review_provenance_invalid") from exc
    try:
        output_directory.mkdir(mode=0o700)
    except OSError as exc:
        raise B09EvidenceError("receipt_directory_create_failed") from exc
    if stat.S_IMODE(output_directory.stat().st_mode) != 0o700:
        raise B09EvidenceError("receipt_directory_mode_invalid")
    task_rows = _review_response_rows(task)
    result_rows = {str(item["case_id"]): item for item in result["cases"]}
    paths: list[Path] = []
    for case_id in OPEN_CASE_IDS:
        unsigned = {
            "schema": RECEIPT_SCHEMA,
            "run_id": plan["run_id"],
            "candidate_sha": plan["candidate_sha"],
            "suite_revision": plan["suite_revision"],
            "case_id": case_id,
            "question_sha256": task_rows[case_id]["question_sha256"],
            "response_sha256": task_rows[case_id]["response_sha256"],
            "rubric_sha256": plan["rubric_sha256"],
            "reviewer_id": plan["reviewer"]["reviewer_id"],
            "verdict": result_rows[case_id]["verdict"],
            "issued_at": issued_at,
            "plan_sha256": sha256_bytes(canonical_json_bytes(plan)),
            "task_sha256": task_sha,
            "result_sha256": result_sha,
            "task_event_id": plan["reviewer"]["task_event_id"],
            "result_event_id": plan["reviewer"]["result_event_id"],
            "review_provenance": provenance,
            "assignment_id": plan["reviewer"]["assignment_id"],
            "assignment_generation": plan["reviewer"]["assignment_generation"],
        }
        receipt = _sign(unsigned, key, RECEIPT_SCHEMA)
        path = output_directory / f"{case_id}.json"
        secure_write_json(path, receipt)
        paths.append(path)
    return paths


def _receipt_is_valid(
    receipt: Any,
    *,
    plan: Mapping[str, Any],
    case_id: str,
    response_sha256: str,
    key: bytes,
) -> bool:
    fields = {
        "schema",
        "run_id",
        "candidate_sha",
        "suite_revision",
        "case_id",
        "question_sha256",
        "response_sha256",
        "rubric_sha256",
        "reviewer_id",
        "verdict",
        "issued_at",
        "plan_sha256",
        "task_sha256",
        "result_sha256",
        "task_event_id",
        "result_event_id",
        "review_provenance",
        "assignment_id",
        "assignment_generation",
        "hmac_sha256",
    }
    reviewer = plan["reviewer"]
    return bool(
        isinstance(receipt, Mapping)
        and set(receipt) == fields
        and receipt.get("schema") == RECEIPT_SCHEMA
        and receipt.get("run_id") == plan["run_id"]
        and receipt.get("candidate_sha") == plan["candidate_sha"]
        and receipt.get("suite_revision") == SUITE_REVISION
        and receipt.get("case_id") == case_id
        and receipt.get("question_sha256") == plan["question_sha256"][case_id]
        and receipt.get("response_sha256") == response_sha256
        and receipt.get("rubric_sha256") == rubric_sha256()
        and receipt.get("reviewer_id") == reviewer["reviewer_id"]
        and receipt.get("verdict") in {"pass", "fail"}
        and _aware_timestamp(receipt.get("issued_at"))
        and receipt.get("plan_sha256") == sha256_bytes(canonical_json_bytes(plan))
        and _is_sha256(receipt.get("task_sha256"))
        and _is_sha256(receipt.get("result_sha256"))
        and receipt.get("task_event_id") == reviewer["task_event_id"]
        and receipt.get("result_event_id") == reviewer["result_event_id"]
        and isinstance(receipt.get("review_provenance"), Mapping)
        and receipt["review_provenance"].get("schema") == lab_provenance.SCHEMA
        and receipt["review_provenance"].get("transport") == plan["lab_transport"]
        and receipt["review_provenance"].get("task_sha256") == receipt["task_sha256"]
        and receipt["review_provenance"].get("result_sha256") == receipt["result_sha256"]
        and receipt.get("assignment_id") == reviewer["assignment_id"]
        and receipt.get("assignment_generation") == reviewer["assignment_generation"]
        and _verify_signature(receipt, key, RECEIPT_SCHEMA)
    )


def build_acceptance(
    *,
    plan: Mapping[str, Any],
    pair_report: Mapping[str, Any],
    receipt_directory: Path,
    closed_report_validator: Callable[[Mapping[str, Any]], bool],
    key: bytes,
    issued_at: str,
) -> dict[str, Any]:
    a_report, b_report = _pair_reports(pair_report)
    if (
        not _plan_integrity_valid(plan, key)
        or pair_report != _closed_pair_document((a_report, b_report))
        or not report_has_plan(b_report, plan)
        or not closed_report_validator(a_report)
        or not closed_report_validator(b_report)
        or not _closed_case_rows_are_green(a_report)
        or not _closed_case_rows_are_green(b_report)
        or not _aware_timestamp(issued_at)
    ):
        raise B09EvidenceError("acceptance_plan_binding_invalid")
    try:
        metadata = receipt_directory.lstat()
    except OSError as exc:
        raise B09EvidenceError("receipt_directory_unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise B09EvidenceError("receipt_directory_boundary_invalid")
    expected_names = {f"{case_id}.json" for case_id in OPEN_CASE_IDS}
    if {path.name for path in receipt_directory.iterdir()} != expected_names:
        raise B09EvidenceError("receipt_set_invalid")
    rows = _case_rows(b_report)
    case_acceptance: list[dict[str, Any]] = []
    for case_id in OPEN_CASE_IDS:
        case_row = rows.get(case_id)
        response_sha = case_row.get("response_sha256") if isinstance(case_row, Mapping) else None
        receipt_path = receipt_directory / f"{case_id}.json"
        receipt = load_private_json(receipt_path)
        if not _is_sha256(response_sha) or not _receipt_is_valid(
            receipt,
            plan=plan,
            case_id=case_id,
            response_sha256=str(response_sha),
            key=key,
        ):
            raise B09EvidenceError("receipt_authenticity_or_binding_invalid")
        closed_passed = bool(
            case_row.get("passed") is True
            and case_row.get("failure_codes") == []
            and case_row.get("privacy_canary_clear") is True
        )
        verdict = str(receipt["verdict"])
        case_acceptance.append(
            {
                "case_id": case_id,
                "closed_passed": closed_passed,
                "review_verdict": verdict,
                "accepted": closed_passed and verdict == "pass",
                "question_sha256": plan["question_sha256"][case_id],
                "response_sha256": response_sha,
                "receipt_sha256": file_sha256(receipt_path),
            }
        )
    closed_reports_green = closed_report_validator(a_report) and closed_report_validator(b_report)
    accepted_count = sum(item["accepted"] is True for item in case_acceptance)
    unsigned = {
        "schema": ACCEPTANCE_SCHEMA,
        "plan_sha256": sha256_bytes(canonical_json_bytes(plan)),
        "run_id": plan["run_id"],
        "candidate_sha": plan["candidate_sha"],
        "suite_revision": plan["suite_revision"],
        "rubric_sha256": plan["rubric_sha256"],
        "reviewer_id": plan["reviewer"]["reviewer_id"],
        "closed_report_sha256": sha256_bytes(canonical_json_bytes(b_report)),
        "closed_reports_green": closed_reports_green,
        "cases": case_acceptance,
        "accepted_count": accepted_count,
        "rejected_count": len(OPEN_CASE_IDS) - accepted_count,
        "all_content_accepted": closed_reports_green and accepted_count == len(OPEN_CASE_IDS),
        "issued_at": issued_at,
    }
    return _sign(unsigned, key, ACCEPTANCE_SCHEMA)


def acceptance_matches_report(
    report: Mapping[str, Any],
    acceptance: Any,
    *,
    key: bytes | None,
) -> bool:
    if (
        type(key) is not bytes
        or not _KEY_MIN_BYTES <= len(key) <= _KEY_MAX_BYTES
        or not isinstance(acceptance, Mapping)
        or not _closed_case_rows_are_green(report)
    ):
        return False
    fields = {
        "schema",
        "plan_sha256",
        "run_id",
        "candidate_sha",
        "suite_revision",
        "rubric_sha256",
        "reviewer_id",
        "closed_report_sha256",
        "closed_reports_green",
        "cases",
        "accepted_count",
        "rejected_count",
        "all_content_accepted",
        "issued_at",
        "hmac_sha256",
    }
    binding = report.get("content_review")
    rows = _case_rows(report)
    cases = acceptance.get("cases")
    binding_fields = {
        "schema",
        "status",
        "plan_sha256",
        "run_id",
        "candidate_sha",
        "manifest_sha256",
        "suite_revision",
        "rubric_sha256",
        "reviewer_id",
        "open_case_ids",
        "question_sha256",
        "source_facts",
    }
    if (
        set(acceptance) != fields
        or acceptance.get("schema") != ACCEPTANCE_SCHEMA
        or not isinstance(binding, Mapping)
        or set(binding) != binding_fields
        or binding.get("schema") != PLAN_BINDING_SCHEMA
        or binding.get("status") != "independent_content_review_required"
        or not isinstance(binding.get("manifest_sha256"), Mapping)
        or set(binding["manifest_sha256"]) != {"A", "B"}
        or not all(_is_sha256(item) for item in binding["manifest_sha256"].values())
        or not _source_facts_valid(binding.get("source_facts"))
        or binding.get("open_case_ids") != list(OPEN_CASE_IDS)
        or not isinstance(binding.get("question_sha256"), Mapping)
        or set(binding["question_sha256"]) != set(OPEN_CASE_IDS)
        or any(not _is_sha256(item) for item in binding["question_sha256"].values())
        or acceptance.get("plan_sha256") != binding.get("plan_sha256")
        or acceptance.get("run_id") != binding.get("run_id")
        or acceptance.get("candidate_sha") != binding.get("candidate_sha")
        or acceptance.get("suite_revision") != binding.get("suite_revision")
        or acceptance.get("rubric_sha256") != binding.get("rubric_sha256")
        or acceptance.get("reviewer_id") != binding.get("reviewer_id")
        or acceptance.get("closed_report_sha256") != sha256_bytes(canonical_json_bytes(report))
        or acceptance.get("closed_reports_green") is not True
        or acceptance.get("all_content_accepted") is not True
        or acceptance.get("accepted_count") != len(OPEN_CASE_IDS)
        or acceptance.get("rejected_count") != 0
        or not _aware_timestamp(acceptance.get("issued_at"))
        or not _verify_signature(acceptance, key, ACCEPTANCE_SCHEMA)
        or not isinstance(cases, list)
        or [item.get("case_id") for item in cases if isinstance(item, Mapping)] != list(OPEN_CASE_IDS)
        or set(rows) < set(OPEN_CASE_IDS)
    ):
        return False
    expected_fields = {
        "case_id",
        "closed_passed",
        "review_verdict",
        "accepted",
        "question_sha256",
        "response_sha256",
        "receipt_sha256",
    }
    for item in cases:
        if not isinstance(item, Mapping):
            return False
        case_id = str(item.get("case_id") or "")
        if (
            set(item) != expected_fields
            or case_id not in OPEN_CASE_IDS
            or item.get("closed_passed") is not True
            or item.get("review_verdict") != "pass"
            or item.get("accepted") is not True
            or item.get("question_sha256") != binding["question_sha256"].get(case_id)
            or item.get("response_sha256") != rows[case_id].get("response_sha256")
            or not _is_sha256(item.get("receipt_sha256"))
        ):
            return False
    return True


def build_final_pair(
    *,
    pair_report: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    pair_is_green: Callable[[Sequence[Mapping[str, Any]], Mapping[str, Any], bytes], bool],
    key: bytes,
    issued_at: str,
) -> dict[str, Any]:
    a_report, b_report = _pair_reports(pair_report)
    reports = [a_report, b_report]
    if pair_report != _closed_pair_document(reports):
        raise B09EvidenceError("source_pair_report_invalid")
    pair_clean = bool(
        all(_closed_case_rows_are_green(report) for report in reports)
        and pair_is_green(reports, acceptance, key)
    )
    unsigned = {
        "schema": FINAL_PAIR_SCHEMA,
        "source_pair_report_sha256": sha256_bytes(canonical_json_bytes(pair_report)),
        "reports": reports,
        "content_acceptance": dict(acceptance),
        "aggregates": {
            "batteries": 2,
            "passes": sum(int(report["aggregates"]["passes"]) for report in reports),
            "cases": sum(int(report["aggregates"]["cases"]) for report in reports),
            "failed": sum(int(report["aggregates"]["failed"]) for report in reports),
            "privacy_canaries_clear": all(
                report["aggregates"].get("privacy_canaries_clear") is True for report in reports
            ),
            "runtime_identity_consistent": len(
                {value for report in reports for value in report.get("runtime_hashes", [])}
            )
            == 1,
            "content_acceptance_complete": acceptance.get("all_content_accepted") is True,
            "pair_clean": pair_clean,
        },
        "issued_at": issued_at,
    }
    return _sign(unsigned, key, FINAL_PAIR_SCHEMA)


def final_pair_is_green(
    value: Any,
    *,
    key: bytes,
    pair_is_green: Callable[[Sequence[Mapping[str, Any]], Mapping[str, Any], bytes], bool],
) -> bool:
    if (
        type(key) is not bytes
        or not _KEY_MIN_BYTES <= len(key) <= _KEY_MAX_BYTES
        or not isinstance(value, Mapping)
        or not _verify_signature(value, key, FINAL_PAIR_SCHEMA)
    ):
        return False
    reports = value.get("reports")
    acceptance = value.get("content_acceptance")
    aggregates = value.get("aggregates")
    fields = {
        "schema",
        "source_pair_report_sha256",
        "reports",
        "content_acceptance",
        "aggregates",
        "issued_at",
        "hmac_sha256",
    }
    aggregate_fields = {
        "batteries",
        "passes",
        "cases",
        "failed",
        "privacy_canaries_clear",
        "runtime_identity_consistent",
        "content_acceptance_complete",
        "pair_clean",
    }
    if (
        set(value) != fields
        or value.get("schema") != FINAL_PAIR_SCHEMA
        or not _is_sha256(value.get("source_pair_report_sha256"))
        or not _aware_timestamp(value.get("issued_at"))
        or not isinstance(reports, list)
        or len(reports) != 2
        or any(not isinstance(item, Mapping) for item in reports)
        or not isinstance(acceptance, Mapping)
        or not isinstance(aggregates, Mapping)
        or set(aggregates) != aggregate_fields
        or any(not isinstance(report.get("aggregates"), Mapping) for report in reports)
    ):
        return False
    try:
        expected_passes = sum(int(report["aggregates"]["passes"]) for report in reports)
        expected_cases = sum(int(report["aggregates"]["cases"]) for report in reports)
        expected_failed = sum(int(report["aggregates"]["failed"]) for report in reports)
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        value.get("source_pair_report_sha256")
        == sha256_bytes(canonical_json_bytes(_closed_pair_document(reports)))
        and aggregates.get("batteries") == 2
        and aggregates.get("passes") == expected_passes
        and aggregates.get("cases") == expected_cases
        and aggregates.get("failed") == expected_failed
        and aggregates.get("privacy_canaries_clear") is True
        and aggregates.get("runtime_identity_consistent") is True
        and aggregates.get("content_acceptance_complete") is True
        and aggregates.get("pair_clean") is True
        and all(_closed_case_rows_are_green(report) for report in reports)
        and pair_is_green(reports, acceptance, key)
    )


def _battery_module():
    if __package__:
        from tools import synthetic_live_battery as battery
    else:
        import synthetic_live_battery as battery
    return battery


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _questions(battery: Any) -> dict[str, str]:
    manifest = battery.load_manifest(battery.MANIFEST_PATHS["B"])
    cases = battery.expand_manifest_cases(manifest)
    return {case.id: case.question for case in cases if case.id in OPEN_CASE_IDS}


def _validated_plan(path: Path, key: bytes, battery: Any) -> dict[str, Any]:
    return validate_plan(
        load_private_json(path),
        key=key,
        expected_candidate_sha=battery._candidate_source_digest(),
        manifest_sha256=battery.FROZEN_MANIFEST_SHA256,
        questions=_questions(battery),
        source_facts=expected_source_facts(battery),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="Create the signed preregistered plan before the run")
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--key-file", type=Path, required=True)
    plan.add_argument("--run-id")
    plan.add_argument("--lab-root", type=Path, required=True)
    plan.add_argument("--lab-job-id", required=True)
    plan.add_argument("--pair-directory", type=Path, required=True)
    for name in (
        "reviewer-id",
        "reviewer-generation",
        "reviewer-thread",
        "owner-generation",
        "owner-thread",
        "authoring-session-id",
        "implementer-session-id",
        "assignment-id",
        "task-event-id",
        "result-event-id",
    ):
        plan.add_argument(f"--{name}", required=True)
    plan.add_argument("--assignment-generation", type=int, required=True)

    task = sub.add_parser("task", help="Build the sealed lab review TASK after the closed run")
    task.add_argument("--output", type=Path, required=True)
    task.add_argument("--key-file", type=Path, required=True)
    task.add_argument("--plan", type=Path, required=True)
    task.add_argument("--pair-report", type=Path, required=True)
    task.add_argument("--b09-evidence", type=Path, required=True)
    task.add_argument("--b03-evidence", type=Path, required=True)

    issue = sub.add_parser("issue", help="Capture actual lab RUN/RESULT delivery and issue receipts")
    issue.add_argument("--output-directory", type=Path, required=True)
    issue.add_argument("--key-file", type=Path, required=True)
    issue.add_argument("--plan", type=Path, required=True)
    issue.add_argument("--task", type=Path, required=True)
    issue.add_argument("--result", type=Path, required=True)

    bind = sub.add_parser("bind", help="Bind six receipts into a final signed pair aggregate")
    bind.add_argument("--output", type=Path, required=True)
    bind.add_argument("--acceptance-output", type=Path, required=True)
    bind.add_argument("--key-file", type=Path, required=True)
    bind.add_argument("--plan", type=Path, required=True)
    bind.add_argument("--pair-report", type=Path, required=True)
    bind.add_argument("--receipt-directory", type=Path, required=True)

    verify = sub.add_parser("verify", help="Verify a final pair aggregate with the root key")
    verify.add_argument("--key-file", type=Path, required=True)
    verify.add_argument("--final-pair", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    battery = _battery_module()
    key = read_root_key(args.key_file, forbidden_roots=(battery.ROOT,))
    if args.command == "plan":
        reviewer = {
            "reviewer_id": args.reviewer_id,
            "reviewer_generation": args.reviewer_generation,
            "reviewer_thread": args.reviewer_thread,
            "owner_generation": args.owner_generation,
            "owner_thread": args.owner_thread,
            "authoring_session_id": args.authoring_session_id,
            "implementer_session_id": args.implementer_session_id,
            "assignment_id": args.assignment_id,
            "assignment_generation": args.assignment_generation,
            "task_event_id": args.task_event_id,
            "result_event_id": args.result_event_id,
        }
        value = create_plan(
            key=key,
            run_id=args.run_id or os.urandom(16).hex(),
            candidate_sha=battery._candidate_source_digest(),
            manifest_sha256=battery.FROZEN_MANIFEST_SHA256,
            questions=_questions(battery),
            source_facts=expected_source_facts(battery),
            reviewer=reviewer,
            lab_transport=lab_provenance.preregister(args.lab_root, args.lab_job_id),
            pair_directory=str(args.pair_directory.resolve()),
            issued_at=_now(),
        )
        secure_write_json(args.output.resolve(), value)
        print(json.dumps({"plan": str(args.output.resolve()), "sha256": file_sha256(args.output.resolve())}))
        return 0
    if args.command == "verify":
        value = load_private_json(args.final_pair.resolve())
        clean = final_pair_is_green(
            value,
            key=key,
            pair_is_green=lambda reports, acceptance, secret: battery._pair_reports_green(
                reports, content_acceptance=acceptance, root_review_key=secret
            ),
        )
        print(json.dumps({"final_pair": str(args.final_pair.resolve()), "pair_clean": clean}))
        return 0 if clean else 4
    plan = _validated_plan(args.plan.resolve(), key, battery)
    if args.command == "task":
        if args.output != Path(plan["pair_directory"]).parent / "review-task.json":
            raise B09EvidenceError("review_task_path_not_preregistered")
        task = build_review_task(
            plan=plan,
            pair_report=load_private_json(args.pair_report.resolve()),
            evidence_paths={3: args.b03_evidence.resolve(), 9: args.b09_evidence.resolve()},
            questions=_questions(battery),
            closed_report_validator=battery._closed_report_is_green,
            key=key,
        )
        secure_write_json(args.output.resolve(), task)
        print(json.dumps({"task": str(args.output.resolve()), "sha256": file_sha256(args.output.resolve())}))
        return 0
    if args.command == "issue":
        paths = issue_receipts(
            plan=plan,
            task=load_private_json(args.task.resolve()),
            task_path=args.task.resolve(),
            result=load_private_json(args.result.resolve()),
            result_path=args.result.resolve(),
            output_directory=args.output_directory.resolve(),
            key=key,
            issued_at=_now(),
        )
        print(json.dumps({"receipts": [str(path) for path in paths]}))
        return 0
    if args.command == "bind":
        pair_report = load_private_json(args.pair_report.resolve())
        acceptance = build_acceptance(
            plan=plan,
            pair_report=pair_report,
            receipt_directory=args.receipt_directory.resolve(),
            closed_report_validator=battery._closed_report_is_green,
            key=key,
            issued_at=_now(),
        )
        secure_write_json(args.acceptance_output.resolve(), acceptance)
        final = build_final_pair(
            pair_report=pair_report,
            acceptance=acceptance,
            pair_is_green=lambda reports, evidence, secret: battery._pair_reports_green(
                reports, content_acceptance=evidence, root_review_key=secret
            ),
            key=key,
            issued_at=_now(),
        )
        secure_write_json(args.output.resolve(), final)
        clean = final_pair_is_green(
            final,
            key=key,
            pair_is_green=lambda reports, evidence, secret: battery._pair_reports_green(
                reports, content_acceptance=evidence, root_review_key=secret
            ),
        )
        print(json.dumps({"final_pair": str(args.output.resolve()), "pair_clean": clean}))
        return 0 if clean else 4
    raise AssertionError("unreachable_b09_command")


if __name__ == "__main__":
    raise SystemExit(main())
