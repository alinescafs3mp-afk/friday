#!/usr/bin/env python3
"""Run the sealed synthetic pre-release acceptance slices without retries.

The runner reuses the live battery's process, network, privacy and reconciliation
boundaries.  It adds only deterministic suite selection and closed aggregate
accounting for the two release-blocking slices:

* ``p06``: A-P06 plus B-P06, 40 cases;
* ``focused``: A-P01/P02/P04/P08/P09/P10, 120 cases;
* ``all``: both slices, dispatched from one immutable candidate snapshot.

Raw questions and responses stay below the ignored private run directory.  Stdout
contains only synthetic case IDs, closed failure codes, hashes and counters.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_battery as battery  # noqa: E402

RUNNER_PATH = Path(__file__).resolve()
RUNNER_RELATIVE_PATH = RUNNER_PATH.relative_to(ROOT).as_posix()
FOCUSED_PASS_INDEXES = (1, 2, 4, 8, 9, 10)
P06_PASS_KEYS = (("A", 6), ("B", 6))
FOCUSED_PASS_KEYS = tuple(("A", index) for index in FOCUSED_PASS_INDEXES)
SUITE_PASS_KEYS = {
    "p06": P06_PASS_KEYS,
    "focused": FOCUSED_PASS_KEYS,
    "all": (*FOCUSED_PASS_KEYS, *P06_PASS_KEYS),
}
SUITE_CASE_COUNTS = {"p06": 40, "focused": 120, "all": 160}
SUMMARY_SCHEMA = "friday.synthetic-live-battery.pre-release.v1"
P06_SCHEMA = "friday.synthetic-live-battery.p06-final.v1"
FOCUSED_SCHEMA = "friday.synthetic-live-battery.focused-final.v1"


@dataclass(frozen=True)
class SealedPass:
    manifest: Mapping[str, Any]
    pass_spec: Mapping[str, Any]
    cases: tuple[battery.ExpandedCase, ...]
    context: battery.PassContext

    @property
    def key(self) -> tuple[str, int]:
        return self.context.battery_id, self.context.pass_index


@dataclass(frozen=True)
class ExecutionResult:
    results: Mapping[tuple[str, int], Mapping[str, Any]]
    worker_codes: Mapping[tuple[str, int], str]
    dispatches: Mapping[str, int]
    candidate_files: tuple[str, ...]
    candidate_pre_sha256: str
    candidate_sealed_sha256: str
    candidate_post_sha256: str

    @property
    def candidate_identity(self) -> bool:
        return bool(self.candidate_pre_sha256 == self.candidate_sealed_sha256 == self.candidate_post_sha256)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _private_tree(root: Path) -> bool:
    """Require 0700 directories, 0600 files and no symlinks."""

    try:
        if root.is_symlink() or not root.is_dir() or stat.S_IMODE(root.stat().st_mode) != 0o700:
            return False
        for path in root.rglob("*"):
            if path.is_symlink():
                return False
            mode = stat.S_IMODE(path.stat().st_mode)
            if path.is_dir() and mode != 0o700:
                return False
            if path.is_file() and mode != 0o600:
                return False
            if not path.is_dir() and not path.is_file():
                return False
        return True
    except OSError:
        return False


def _read_reconciliation(
    path: Path,
    *,
    kind: str,
) -> tuple[bool, str, dict[str, bool], str]:
    """Read only the closed reconciliation record and validate its own digest."""

    try:
        if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            return False, "", {}, "reconciliation_evidence_not_private"
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, "", {}, "reconciliation_evidence_invalid"
    if not isinstance(value, Mapping):
        return False, "", {}, "reconciliation_shape_invalid"
    if kind == "pass":
        components = {
            "api_exact",
            "audit_exact",
            "counters_exact",
            "files_exact",
            "http_exact",
            "storage_exact",
            "tools_exact",
        }
        expected = {"schema", "clear", "snapshot_sha256", *components}
        schema = battery.RECONCILIATION_SCHEMA
    elif kind == "tail":
        components = {"probe_exact", "files_exact", "database_exact"}
        expected = {"schema", "clear", "snapshot_sha256", *components}
        schema = "friday.synthetic-live-battery.tail-reconciliation.v1"
    else:
        return False, "", {}, "reconciliation_kind_invalid"
    if (
        set(value) != expected
        or value.get("schema") != schema
        or any(type(value.get(key)) is not bool for key in components | {"clear"})
        or not battery._is_sha256(value.get("snapshot_sha256"))
    ):
        return False, "", {}, "reconciliation_shape_invalid"
    unsigned = {key: value[key] for key in value if key != "snapshot_sha256"}
    snapshot_exact = value["snapshot_sha256"] == battery._sha256_bytes(
        battery._canonical_json_bytes(unsigned)
    )
    clear_exact = value["clear"] is all(value[key] is True for key in components)
    clear = bool(snapshot_exact and clear_exact and value["clear"] is True)
    component_values = {key: value[key] is True for key in components}
    full_hash = battery._sha256_bytes(battery._canonical_json_bytes(value))
    return clear, str(value["snapshot_sha256"]), component_values, full_hash


def _suite_keys(suite: str) -> tuple[tuple[str, int], ...]:
    try:
        return tuple(SUITE_PASS_KEYS[suite])
    except KeyError:
        raise battery.BatteryContractError("acceptance_suite_invalid") from None


def _load_manifests() -> dict[str, tuple[str, Mapping[str, Any]]]:
    audit = battery.audit_frozen_manifests()
    if audit.get("valid") is not True:
        raise battery.BatteryContractError("manifest_audit_failed")
    manifests: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for battery_id in ("A", "B"):
        path = battery.MANIFEST_PATHS[battery_id]
        digest = battery.file_sha256(path)
        manifest = battery.load_manifest(path)
        if digest != battery.FROZEN_MANIFEST_SHA256[battery_id] or battery.manifest_complaints(
            manifest, expected_battery=battery_id
        ):
            raise battery.BatteryContractError("manifest_audit_failed")
        manifests[battery_id] = digest, manifest
    return manifests


def inventory_for_suite(suite: str) -> dict[str, Any]:
    """Return a closed, model-free inventory for tests and operator preflight."""

    manifests = _load_manifests()
    case_ids: list[str] = []
    questions: list[str] = []
    pass_ids: list[str] = []
    for battery_id, pass_index in _suite_keys(suite):
        _manifest_hash, manifest = manifests[battery_id]
        pass_spec = list(manifest["passes"])[pass_index - 1]
        cases = [case for case in battery.expand_manifest_cases(manifest) if case.pass_index == pass_index]
        expected_pass_id = f"{battery_id}-P{pass_index:02d}"
        if (
            len(cases) != battery.QUESTIONS_PER_PASS
            or str(pass_spec.get("pass_id") or "") != expected_pass_id
            or any(case.pass_id != expected_pass_id for case in cases)
        ):
            raise battery.BatteryContractError("acceptance_pass_inventory_invalid")
        if (battery_id, pass_index) in P06_PASS_KEYS and any(
            case.oracle_profile != "tenant_privacy" for case in cases
        ):
            raise battery.BatteryContractError("p06_profile_invalid")
        pass_ids.append(expected_pass_id)
        case_ids.extend(case.id for case in cases)
        questions.extend(case.question for case in cases)
    expected_cases = SUITE_CASE_COUNTS[suite]
    if (
        len(pass_ids) != len(set(pass_ids))
        or len(case_ids) != expected_cases
        or len(case_ids) != len(set(case_ids))
        or len(questions) != len(set(questions))
    ):
        raise battery.BatteryContractError("acceptance_suite_inventory_invalid")
    candidate_files = battery._candidate_source_paths(instrument_path=RUNNER_PATH)
    if RUNNER_RELATIVE_PATH not in candidate_files:
        raise battery.BatteryContractError("acceptance_runner_not_candidate_bound")
    return {
        "schema": "friday.synthetic-live-battery.pre-release-audit.v1",
        "valid": True,
        "suite": suite,
        "passes": len(pass_ids),
        "cases": len(case_ids),
        "pass_ids": pass_ids,
        "manifest_sha256": {
            battery_id: manifest_hash for battery_id, (manifest_hash, _manifest) in manifests.items()
        },
        "candidate_source_sha256": battery._candidate_source_digest(relative_paths=candidate_files),
        "runner_sha256": battery.file_sha256(RUNNER_PATH),
    }


def _make_private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    battery._require_private_directory(path)


def _preseal_passes(
    suite: str,
    run_root: Path,
    manifests: Mapping[str, tuple[str, Mapping[str, Any]]],
) -> tuple[SealedPass, ...]:
    """Create every isolated home/evidence path before the first dispatch."""

    sealed: list[SealedPass] = []
    passes_root = run_root / "passes"
    _make_private_directory(passes_root)
    for battery_id, pass_index in _suite_keys(suite):
        manifest_hash, manifest = manifests[battery_id]
        pass_spec = list(manifest["passes"])[pass_index - 1]
        cases = tuple(
            case for case in battery.expand_manifest_cases(manifest) if case.pass_index == pass_index
        )
        pass_id = f"{battery_id}-P{pass_index:02d}"
        if len(cases) != battery.QUESTIONS_PER_PASS or pass_spec.get("pass_id") != pass_id:
            raise battery.BatteryContractError("acceptance_pass_inventory_invalid")
        pass_root = passes_root / pass_id
        home = pass_root / "home"
        evidence_dir = pass_root / "evidence"
        for directory in (pass_root, home, evidence_dir):
            _make_private_directory(directory)
        battery._prepare_process_scratch(home)
        sealed.append(
            SealedPass(
                manifest=manifest,
                pass_spec=pass_spec,
                cases=cases,
                context=battery.PassContext(
                    battery_id=battery_id,
                    pass_id=pass_id,
                    pass_index=pass_index,
                    seed=int(manifest["seed"]) + pass_index,
                    clock=str(manifest["clock"]),
                    timezone=str(manifest["timezone"]),
                    manifest_sha256=manifest_hash,
                    home=home.resolve(),
                    evidence_path=(evidence_dir / "raw-responses.jsonl").resolve(),
                ),
            )
        )
    if len(sealed) != len(_suite_keys(suite)):
        raise battery.BatteryContractError("acceptance_preseal_incomplete")
    return tuple(sealed)


def _execute_sealed(
    sealed: Sequence[SealedPass],
    *,
    concurrency: int,
) -> ExecutionResult:
    if type(concurrency) is not int or not (1 <= concurrency <= battery.MAX_CONCURRENCY):
        raise battery.BatteryContractError("concurrency_out_of_range")
    candidate_files = battery._candidate_source_paths(instrument_path=RUNNER_PATH)
    if RUNNER_RELATIVE_PATH not in candidate_files:
        raise battery.BatteryContractError("acceptance_runner_not_candidate_bound")
    candidate_pre = battery._candidate_source_digest(relative_paths=candidate_files)
    results: dict[tuple[str, int], Mapping[str, Any]] = {}
    worker_codes: dict[tuple[str, int], str] = {}
    dispatches = {item.context.pass_id: 0 for item in sealed}
    dispatch_lock = threading.Lock()
    with battery.SubprocessPassExecutor(
        battery._inherit_model_environment(),
        instrument_path=RUNNER_PATH,
    ) as executor:
        candidate_sealed = str(executor._candidate_source_sha256)
        if executor._candidate_files != candidate_files or candidate_sealed != candidate_pre:
            raise battery.BatteryContractError("candidate_preseal_identity_invalid")

        def execute_one(item: SealedPass) -> tuple[tuple[str, int], Mapping[str, Any], str]:
            with dispatch_lock:
                dispatches[item.context.pass_id] += 1
            code = ""
            try:
                value = executor(
                    item.manifest,
                    item.pass_spec,
                    item.cases,
                    item.context,
                )
            except Exception:  # noqa: BLE001 - raw detail stays inside private worker evidence
                value = battery._pass_failure(item.cases, "pass_worker_error")
                code = "pass_worker_error"
            if not battery._validate_pass_result(value, item.cases):
                value = battery._pass_failure(item.cases, "pass_result_invalid")
                code = "pass_result_invalid"
            return item.key, dict(value), code

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(execute_one, item) for item in sealed]
            for future in concurrent.futures.as_completed(futures):
                key, value, code = future.result()
                results[key] = value
                worker_codes[key] = code
        executor._assert_candidate_unchanged()
    candidate_post_files = battery._candidate_source_paths(instrument_path=RUNNER_PATH)
    candidate_post = battery._candidate_source_digest(relative_paths=candidate_post_files)
    if candidate_post_files != candidate_files:
        raise battery.BatteryContractError("candidate_source_changed_during_acceptance")
    return ExecutionResult(
        results=results,
        worker_codes=worker_codes,
        dispatches=dispatches,
        candidate_files=candidate_files,
        candidate_pre_sha256=candidate_pre,
        candidate_sealed_sha256=candidate_sealed,
        candidate_post_sha256=candidate_post,
    )


def _summarize_pass(item: SealedPass, execution: ExecutionResult) -> dict[str, Any]:
    result = execution.results.get(item.key, {})
    result_valid = battery._validate_pass_result(result, item.cases)
    evidence_dir = item.context.evidence_path.parent
    pass_clear, pass_snapshot, pass_components, pass_full_hash = _read_reconciliation(
        evidence_dir / "pass-reconciliation.json",
        kind="pass",
    )
    tail_clear, tail_snapshot, tail_components, _tail_full_hash = _read_reconciliation(
        evidence_dir / "tail-reconciliation.json",
        kind="tail",
    )
    expected_combined = battery._sha256_bytes(
        battery._canonical_json_bytes(
            {
                "pass_reconciliation_sha256": pass_full_hash,
                "tail_reconciliation_sha256": tail_snapshot,
            }
        )
    )
    combined_clear = bool(
        pass_clear
        and tail_clear
        and result.get("pass_reconciliation_clear") is True
        and result.get("pass_reconciliation_sha256") == expected_combined
    )
    rows = result.get("case_results") if isinstance(result.get("case_results"), list) else []
    privacy_clear = bool(rows) and all(
        isinstance(row, Mapping) and row.get("privacy_canary_clear") is True for row in rows
    )
    pass_root = item.context.home.parent
    evidence_private = bool(
        item.context.evidence_path.is_file()
        and stat.S_IMODE(item.context.evidence_path.stat().st_mode) == 0o600
        and _private_tree(pass_root)
    )
    evidence_digest_match = bool(
        evidence_private
        and battery._is_sha256(result.get("evidence_sha256"))
        and result.get("evidence_sha256") == battery.file_sha256(item.context.evidence_path)
    )
    failure_codes = sorted(
        {
            str(code)
            for row in rows
            if isinstance(row, Mapping)
            for code in (row.get("failure_codes") or [])
            if isinstance(code, str)
        }
    )
    failed_case_ids = [
        str(row.get("case_id")) for row in rows if isinstance(row, Mapping) and row.get("passed") is False
    ]
    lifecycle_exact = bool(
        all(
            pass_components.get(key) is True
            for key in (
                "api_exact",
                "audit_exact",
                "counters_exact",
                "files_exact",
                "http_exact",
                "storage_exact",
                "tools_exact",
            )
        )
        and all(tail_components.get(key) is True for key in ("probe_exact", "files_exact", "database_exact"))
    )
    all_gates_exact = bool(
        result_valid
        and pass_clear
        and tail_clear
        and combined_clear
        and privacy_clear
        and evidence_digest_match
        and lifecycle_exact
        and not execution.worker_codes.get(item.key)
        and execution.dispatches.get(item.context.pass_id) == 1
    )
    return {
        "pass_id": item.context.pass_id,
        "cases": int(result.get("cases") or 0),
        "passed": int(result.get("passed") or 0),
        "failed": int(result.get("failed") or 0),
        "failed_case_ids": failed_case_ids,
        "failure_codes": failure_codes,
        "result_valid": result_valid,
        "pass_reconciliation_clear": pass_clear,
        "tail_reconciliation_clear": tail_clear,
        "combined_reconciliation_clear": combined_clear,
        "privacy_canaries_clear": privacy_clear,
        "evidence_private_and_bound": evidence_digest_match,
        "api_exact": bool(pass_components.get("api_exact")),
        "audit_exact": bool(pass_components.get("audit_exact")),
        "counters_exact": bool(pass_components.get("counters_exact")),
        "files_exact": bool(pass_components.get("files_exact")),
        "http_exact": bool(pass_components.get("http_exact")),
        "storage_exact": bool(pass_components.get("storage_exact")),
        "tools_exact": bool(pass_components.get("tools_exact")),
        "tail_probe_exact": bool(tail_components.get("probe_exact")),
        "tail_files_exact": bool(tail_components.get("files_exact")),
        "tail_database_exact": bool(tail_components.get("database_exact")),
        "all_gates_exact": all_gates_exact,
        "worker_error_code": str(execution.worker_codes.get(item.key) or ""),
        "runtime_sha256": str(result.get("runtime_hash") or ""),
        "evidence_sha256": str(result.get("evidence_sha256") or ""),
        "pass_snapshot_prefix": pass_snapshot[:12],
        "tail_snapshot_prefix": tail_snapshot[:12],
    }


def _runtime_identity(rows: Sequence[Mapping[str, Any]]) -> tuple[bool, str]:
    hashes = [str(row.get("runtime_sha256") or "") for row in rows]
    consistent = bool(hashes and all(battery._is_sha256(value) for value in hashes) and len(set(hashes)) == 1)
    return consistent, hashes[0] if consistent else ""


def _focused_summary(
    rows: Sequence[Mapping[str, Any]],
    execution: ExecutionResult,
    *,
    artifact_id: str,
) -> dict[str, Any]:
    focused = [
        row
        for row in rows
        if str(row.get("pass_id") or "") in {f"A-P{index:02d}" for index in FOCUSED_PASS_INDEXES}
    ]
    runtime_consistent, runtime_sha256 = _runtime_identity(focused)
    cases = sum(int(row.get("cases") or 0) for row in focused)
    passed = sum(int(row.get("passed") or 0) for row in focused)
    failed = sum(int(row.get("failed") or 0) for row in focused)
    green = bool(
        len(focused) == len(FOCUSED_PASS_INDEXES)
        and [row.get("pass_id") for row in focused] == [f"A-P{index:02d}" for index in FOCUSED_PASS_INDEXES]
        and cases == passed == 120
        and failed == 0
        and all(row.get("all_gates_exact") is True for row in focused)
        and runtime_consistent
        and execution.candidate_identity
    )
    return {
        "schema": FOCUSED_SCHEMA,
        "artifact_id": artifact_id,
        "status": "green" if green else "red",
        "passes_requested": len(FOCUSED_PASS_INDEXES),
        "passes_completed": len(focused),
        "cases": cases,
        "passed": passed,
        "failed": failed,
        "all_results_valid": all(row.get("result_valid") is True for row in focused),
        "all_pass_reconciliation_clear": all(row.get("pass_reconciliation_clear") is True for row in focused),
        "all_tail_reconciliation_clear": all(row.get("tail_reconciliation_clear") is True for row in focused),
        "all_combined_reconciliation_clear": all(
            row.get("combined_reconciliation_clear") is True for row in focused
        ),
        "privacy_canaries_clear": all(row.get("privacy_canaries_clear") is True for row in focused),
        "all_evidence_private_and_bound": all(
            row.get("evidence_private_and_bound") is True for row in focused
        ),
        "all_lifecycle_components_exact": all(row.get("all_gates_exact") is True for row in focused),
        "runtime_identity_consistent": runtime_consistent,
        "runtime_sha256": runtime_sha256,
        "candidate_digest_identity": execution.candidate_identity,
        "candidate_pre_sha256": execution.candidate_pre_sha256,
        "candidate_sealed_sha256": execution.candidate_sealed_sha256,
        "candidate_post_sha256": execution.candidate_post_sha256,
        "passes": focused,
    }


def _p06_summary(
    sealed: Sequence[SealedPass],
    rows: Sequence[Mapping[str, Any]],
    execution: ExecutionResult,
    *,
    artifact_id: str,
) -> dict[str, Any]:
    p06_ids = {"A-P06", "B-P06"}
    p06_rows = [row for row in rows if str(row.get("pass_id") or "") in p06_ids]
    rows_by_pass = {str(row.get("pass_id") or ""): row for row in p06_rows}
    exact_zero_expected = 0
    exact_zero_observed = 0
    control_expected = 0
    control_observed = 0
    tenant_control_cases_exact = 0
    for item in sealed:
        if item.context.pass_id not in p06_ids:
            continue
        result = execution.results.get(item.key, {})
        case_rows = result.get("case_results") if isinstance(result.get("case_results"), list) else []
        row_by_id = {str(row.get("case_id") or ""): row for row in case_rows if isinstance(row, Mapping)}
        for case in item.cases:
            equals = battery.oracle_for_case(case)["state"]["equals"]
            zero_keys = [key for key, value in equals.items() if type(value) is int and value == 0]
            control_keys = [
                key for key in equals if key.startswith("tenant_control_") and key != "tenant_control_exact"
            ]
            if (
                len(zero_keys) != 72
                or len(control_keys) != 44
                or equals.get("tenant_control_exact") is not True
            ):
                raise battery.BatteryContractError("p06_closed_oracle_shape_invalid")
            exact_zero_expected += len(zero_keys)
            control_expected += len(control_keys)
            row = row_by_id.get(case.id)
            if isinstance(row, Mapping) and row.get("passed") is True:
                exact_zero_observed += len(zero_keys)
                control_observed += len(control_keys)
                tenant_control_cases_exact += 1
    runtime_consistent, runtime_sha256 = _runtime_identity(p06_rows)
    cases = sum(int(row.get("cases") or 0) for row in p06_rows)
    passed = sum(int(row.get("passed") or 0) for row in p06_rows)
    failed = sum(int(row.get("failed") or 0) for row in p06_rows)
    ordered_rows = [rows_by_pass[pass_id] for pass_id in ("A-P06", "B-P06") if pass_id in rows_by_pass]
    green = bool(
        len(ordered_rows) == 2
        and cases == passed == 40
        and failed == 0
        and all(row.get("all_gates_exact") is True for row in ordered_rows)
        and exact_zero_expected == exact_zero_observed == 2880
        and control_expected == control_observed == 1760
        and tenant_control_cases_exact == 40
        and runtime_consistent
        and execution.candidate_identity
    )
    return {
        "schema": P06_SCHEMA,
        "artifact_id": artifact_id,
        "status": "green" if green else "red",
        "cases": cases,
        "passed": passed,
        "failed": failed,
        "exact_zero_expected": exact_zero_expected,
        "exact_zero_observed": exact_zero_observed,
        "tenant_control_fields_expected": control_expected,
        "tenant_control_fields_observed": control_observed,
        "tenant_control_cases_exact": tenant_control_cases_exact,
        "dispatches": {
            battery_id: execution.dispatches.get(f"{battery_id}-P06", 0) for battery_id in ("A", "B")
        },
        "runtime_identity_consistent": runtime_consistent,
        "runtime_sha256": runtime_sha256,
        "candidate_digest_identity": execution.candidate_identity,
        "candidate_pre_sha256": execution.candidate_pre_sha256,
        "candidate_sealed_sha256": execution.candidate_sealed_sha256,
        "candidate_post_sha256": execution.candidate_post_sha256,
        "passes": ordered_rows,
    }


def run_acceptance(
    suite: str,
    *,
    run_directory: Path,
    concurrency: int,
    artifact_id: str,
) -> tuple[int, dict[str, Any]]:
    """Run one sealed suite and return only a closed aggregate."""

    if not re.fullmatch(r"PRE-RELEASE-(?:ALL|P06|FOCUSED)-[0-9a-f]{16}", artifact_id):
        raise battery.BatteryContractError("artifact_id_invalid")
    manifests = _load_manifests()
    inventory_for_suite(suite)
    battery._assert_ignored_or_external(run_directory)
    if run_directory.exists():
        raise battery.BatteryContractError("run_directory_already_exists")
    _make_private_directory(run_directory)
    battery._preflight_private_filesystem(run_directory)
    sealed = _preseal_passes(suite, run_directory, manifests)
    execution = _execute_sealed(sealed, concurrency=concurrency)
    pass_rows_by_id = {item.context.pass_id: _summarize_pass(item, execution) for item in sealed}
    pass_rows = [pass_rows_by_id[item.context.pass_id] for item in sealed]
    suite_summaries: dict[str, dict[str, Any]] = {}
    if suite in {"focused", "all"}:
        suite_summaries["focused"] = _focused_summary(
            pass_rows,
            execution,
            artifact_id=artifact_id,
        )
        battery._secure_write_json(
            run_directory / "focused-sanitized-summary.json",
            suite_summaries["focused"],
        )
    if suite in {"p06", "all"}:
        suite_summaries["p06"] = _p06_summary(
            sealed,
            pass_rows,
            execution,
            artifact_id=artifact_id,
        )
        battery._secure_write_json(
            run_directory / "p06-sanitized-summary.json",
            suite_summaries["p06"],
        )
    runtime_consistent, runtime_sha256 = _runtime_identity(pass_rows)
    cases = sum(int(row.get("cases") or 0) for row in pass_rows)
    passed = sum(int(row.get("passed") or 0) for row in pass_rows)
    failed = sum(int(row.get("failed") or 0) for row in pass_rows)
    dispatches_exact = bool(
        set(execution.dispatches) == {item.context.pass_id for item in sealed}
        and all(value == 1 for value in execution.dispatches.values())
    )
    green = bool(
        set(suite_summaries) == ({"focused", "p06"} if suite == "all" else {suite})
        and all(summary.get("status") == "green" for summary in suite_summaries.values())
        and cases == passed == SUITE_CASE_COUNTS[suite]
        and failed == 0
        and all(row.get("all_gates_exact") is True for row in pass_rows)
        and execution.candidate_identity
        and runtime_consistent
        and dispatches_exact
        and _private_tree(run_directory)
    )
    summary = {
        "schema": SUMMARY_SCHEMA,
        "artifact_id": artifact_id,
        "status": "green" if green else "red",
        "suite": suite,
        "passes": len(pass_rows),
        "cases": cases,
        "passed": passed,
        "failed": failed,
        "suite_status": {name: str(value.get("status") or "red") for name, value in suite_summaries.items()},
        "dispatches_exact_once": dispatches_exact,
        "privacy_evidence_private": _private_tree(run_directory),
        "candidate_digest_identity": execution.candidate_identity,
        "candidate_pre_sha256": execution.candidate_pre_sha256,
        "candidate_sealed_sha256": execution.candidate_sealed_sha256,
        "candidate_post_sha256": execution.candidate_post_sha256,
        "runtime_identity_consistent": runtime_consistent,
        "runtime_sha256": runtime_sha256,
        "runner_sha256": battery.file_sha256(RUNNER_PATH),
        "manifest_sha256": dict(battery.FROZEN_MANIFEST_SHA256),
    }
    battery._secure_write_json(run_directory / "pre-release-sanitized-summary.json", summary)
    if not _private_tree(run_directory):
        summary["status"] = "red"
        summary["privacy_evidence_private"] = False
        return 4, summary
    return (0 if green else 4), summary


def _default_run_directory(artifact_id: str) -> Path:
    return ROOT / "data" / "live-battery-runs" / artifact_id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=tuple(SUITE_PASS_KEYS),
        default="all",
        help="Acceptance slice (default: all, one shared immutable snapshot)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=battery.DEFAULT_CONCURRENCY,
        help=f"Independent pass workers (1-{battery.MAX_CONCURRENCY})",
    )
    parser.add_argument(
        "--run-directory",
        type=Path,
        help="New ignored/external directory; existing paths are refused",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Private operator config for live execution; never written to evidence",
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Validate manifests, inventory and candidate binding; run no model turns",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    args = _parser().parse_args(argv)
    if not (1 <= int(args.concurrency) <= battery.MAX_CONCURRENCY):
        raise SystemExit(f"--concurrency must be between 1 and {battery.MAX_CONCURRENCY}")
    if args.audit_only:
        try:
            audit = inventory_for_suite(str(args.suite))
        except Exception as exc:  # noqa: BLE001 - never print possibly private exception text
            print(
                json.dumps(
                    {
                        "schema": "friday.synthetic-live-battery.pre-release-audit.v1",
                        "valid": False,
                        "code": "pre_release_audit_failed",
                        "error_class_sha256": _sha256_text(type(exc).__name__),
                    },
                    sort_keys=True,
                )
            )
            return 2
        print(json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    artifact_id = f"PRE-RELEASE-{str(args.suite).upper()}-{secrets.token_hex(8)}"
    run_directory = (
        args.run_directory.resolve()
        if args.run_directory is not None
        else _default_run_directory(artifact_id)
    )
    run_directory_existed = run_directory.exists()
    try:
        if args.env_file is not None:
            battery._select_live_env_file(args.env_file)
        return_code, summary = run_acceptance(
            str(args.suite),
            run_directory=run_directory,
            concurrency=int(args.concurrency),
            artifact_id=artifact_id,
        )
    except Exception as exc:  # noqa: BLE001 - raw detail stays in private evidence
        failure = {
            "schema": SUMMARY_SCHEMA,
            "artifact_id": artifact_id,
            "status": "red",
            "code": "pre_release_runner_failed",
            "error_class_sha256": _sha256_text(type(exc).__name__),
        }
        try:
            if not run_directory_existed and run_directory.is_dir() and _private_tree(run_directory):
                battery._secure_write_json(
                    run_directory / "pre-release-sanitized-failure.json",
                    failure,
                )
        except Exception:
            pass
        print(json.dumps(failure, sort_keys=True))
        return 4
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
