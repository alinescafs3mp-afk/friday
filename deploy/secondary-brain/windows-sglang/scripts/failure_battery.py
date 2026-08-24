#!/usr/bin/env python3
"""Run deterministic mocked failure contracts and emit candidate-bound evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from endpoint_common import EndpointError, configure_expected_model, evidence_identity

SCHEMA = "friday.secondary-failure-battery.v1"
EVIDENCE_SCOPE = "deterministic_mock_contract"
REPO_ROOT = Path(__file__).resolve().parents[4]
JOURNEY_TESTS = {
    "startup_laptop_off": "test_server_stays_healthy_with_secondary_disabled_or_laptop_off",
    "ordinary_primary_chat_laptop_off": "test_laptop_failure_preserves_primary_advice_once",
    "readmit_without_friday_restart": "test_laptop_is_readmitted_on_demand_without_primary_restart",
    "disappear_before_admission": "test_optional_failure_never_duplicates_primary_or_retains_exception",
    "disappear_after_submission": "test_mid_submission_disconnect_falls_back_exactly_once",
    "http_503": "test_clients_have_independent_circuits",
    "deadline_hang": "test_deadline_hang_is_bounded_and_falls_back_exactly_once",
    "malformed_json": "test_malformed_json_falls_back_exactly_once",
    "wrong_model_alias": "test_wrong_alias_uses_one_primary_fallback_without_retaining_raw_value",
    "invalid_tool_markup": "test_structured_result_is_typed_and_tool_output_is_rejected",
    "secondary_busy": "test_secondary_admission_is_immediate_and_does_not_queue",
    "half_open_restart_recovery": "test_cooldown_admits_only_one_half_open_probe",
    "no_effect_replay": "test_mutating_request_runs_primary_effect_once_without_secondary_replay",
    "v12_readiness_unchanged": "test_enabled_secondary_cannot_change_primary_v12_identity",
    "one_flag_primary_only": "test_disabled_builds_no_client_and_required_falls_back_exactly_once",
}
SUITE_FILES = (
    "friday/agent_runtime/__init__.py",
    "friday/config/__init__.py",
    "friday/ingestion/_advice.py",
    "friday/ingestion/_core.py",
    "friday/ingestion/_secondary_advice.py",
    "friday/model_input_hygiene.py",
    "friday/secondary_brain/client.py",
    "friday/secondary_brain/contracts.py",
    "friday/secondary_brain/profiles.py",
    "friday/secondary_brain/gpt_oss.py",
    "friday/secondary_brain/scheduler.py",
    "friday/server.py",
    "tests/test_secondary_brain.py",
    "tests/test_secondary_inbox_advice.py",
)
TEST_FILES = ("tests/test_secondary_brain.py", "tests/test_secondary_inbox_advice.py")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class FailureBatteryError(RuntimeError):
    """One content-free certification failure."""


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise FailureBatteryError("battery source file is unavailable") from exc
    return digest.hexdigest()


def journey_contract_sha256() -> str:
    return hashlib.sha256(_canonical(JOURNEY_TESTS)).hexdigest()


def _git_head() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FailureBatteryError("source revision is unavailable") from exc
    head = result.stdout.strip()
    if result.returncode != 0 or _COMMIT.fullmatch(head) is None:
        raise FailureBatteryError("source revision is unavailable")
    return head


def _require_clean_suite() -> None:
    try:
        result = subprocess.run(
            [
                "git",
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--",
                *SUITE_FILES,
                str(Path(__file__).relative_to(REPO_ROOT)),
            ],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FailureBatteryError("failure suite identity is unavailable") from exc
    if result.returncode != 0 or result.stdout:
        raise FailureBatteryError("failure suite differs from the committed source")


def _test_names(junit_path: Path) -> tuple[set[str], int]:
    try:
        if not junit_path.is_file() or junit_path.stat().st_size > 8 * 1024 * 1024:
            raise FailureBatteryError("pytest receipt is absent or oversized")
        root = ET.parse(junit_path).getroot()  # noqa: S314  # local pytest output only
    except (OSError, ET.ParseError) as exc:
        raise FailureBatteryError("pytest receipt is invalid") from exc
    cases = root.findall(".//testcase")
    if not cases or any(
        case.find("failure") is not None or case.find("error") is not None or case.find("skipped") is not None
        for case in cases
    ):
        raise FailureBatteryError("failure battery contains a failed assertion")
    names = {str(case.attrib.get("name", "")).split("[")[0] for case in cases}
    missing = set(JOURNEY_TESTS.values()) - names
    if missing:
        raise FailureBatteryError("failure battery did not execute every journey assertion")
    return names, len(cases)


def run_battery(*, candidate: Path, ca_file: Path, output: Path) -> dict[str, Any]:
    try:
        configure_expected_model(candidate, ca_file)
    except EndpointError as exc:
        raise FailureBatteryError("candidate identity is invalid") from exc
    _require_clean_suite()
    suite_hashes = {relative: _sha256(REPO_ROOT / relative) for relative in SUITE_FILES}
    runner_hash = _sha256(Path(__file__))
    if any(_SHA256.fullmatch(value) is None for value in (*suite_hashes.values(), runner_hash)):
        raise FailureBatteryError("failure suite identity is invalid")
    with tempfile.TemporaryDirectory(prefix="friday-secondary-failure-") as temporary:
        temp = Path(temporary)
        environment = dict(os.environ)
        environment.pop("FRIDAY_DATABASE_PATH", None)
        environment["FRIDAY_HOME"] = str(temp / "home")
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "--junitxml",
                    str(temp / "pytest.xml"),
                    *TEST_FILES,
                ],
                cwd=REPO_ROOT,
                env=environment,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise FailureBatteryError("failure journey assertions could not complete") from exc
        if result.returncode != 0:
            raise FailureBatteryError("failure journey assertions did not pass")
        _names, test_count = _test_names(temp / "pytest.xml")
    evidence = {
        "schema": SCHEMA,
        "status": "passed",
        "evidence_scope": EVIDENCE_SCOPE,
        "live_physical_journeys_observed": False,
        **evidence_identity(),
        "source_head": _git_head(),
        "runner_sha256": runner_hash,
        "journey_contract_sha256": journey_contract_sha256(),
        "suite_file_sha256": suite_hashes,
        "test_count": test_count,
        "journeys": {
            journey: {"status": "passed", "assertion_test": test}
            for journey, test in sorted(JOURNEY_TESTS.items())
        },
        "primary_fallback_exactly_once": True,
        "effect_replay_observed": False,
        "v12_readiness_changed": False,
        "primary_only_flag_verified": True,
        "raw_content_retained": False,
        "credentials_retained": False,
    }
    if output.exists() or output.is_symlink() or not output.absolute().parent.is_dir():
        raise FailureBatteryError("failure evidence output path is not new")
    try:
        with output.open("xb") as stream:
            stream.write(_canonical(evidence))
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise FailureBatteryError("failure evidence could not be created") from exc
    return {
        "status": "deterministic_contract_passed",
        "live_physical_journeys_observed": False,
        "test_count": test_count,
        "output_sha256": _sha256(output),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_battery(candidate=args.candidate, ca_file=args.ca_file, output=args.output)
    except FailureBatteryError as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
