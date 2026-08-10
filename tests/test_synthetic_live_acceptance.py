"""Model-free regressions for the sealed pre-release live acceptance runner."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_acceptance as acceptance  # noqa: E402
import synthetic_live_battery as battery  # noqa: E402


def _private_root(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def test_pre_release_inventory_is_exact_unique_and_candidate_bound() -> None:
    focused = acceptance.inventory_for_suite("focused")
    p06 = acceptance.inventory_for_suite("p06")
    combined = acceptance.inventory_for_suite("all")

    assert focused["pass_ids"] == ["A-P01", "A-P02", "A-P04", "A-P08", "A-P09", "A-P10"]
    assert focused["passes"] == 6
    assert focused["cases"] == 120
    assert p06["pass_ids"] == ["A-P06", "B-P06"]
    assert p06["passes"] == 2
    assert p06["cases"] == 40
    assert combined["passes"] == 8
    assert combined["cases"] == 160
    assert len(combined["pass_ids"]) == len(set(combined["pass_ids"]))
    assert set(focused["pass_ids"]).isdisjoint(p06["pass_ids"])
    assert battery._is_sha256(combined["candidate_source_sha256"])
    assert battery._is_sha256(combined["runner_sha256"])
    candidate_files = battery._candidate_source_paths(instrument_path=acceptance.RUNNER_PATH)
    assert acceptance.RUNNER_RELATIVE_PATH in candidate_files
    assert "tools/synthetic_live_battery.py" in candidate_files
    assert "sol/LIVE_TEST_2026-08-08.md" not in candidate_files
    assert "start.txt" not in candidate_files


def test_all_pass_homes_are_private_and_presealed_before_dispatch(tmp_path: Path) -> None:
    run_root = _private_root(tmp_path / "acceptance")
    sealed = acceptance._preseal_passes("all", run_root, acceptance._load_manifests())

    assert [item.context.pass_id for item in sealed] == [
        "A-P01",
        "A-P02",
        "A-P04",
        "A-P08",
        "A-P09",
        "A-P10",
        "A-P06",
        "B-P06",
    ]
    assert len({case.id for item in sealed for case in item.cases}) == 160
    assert len({case.question for item in sealed for case in item.cases}) == 160
    for item in sealed:
        assert item.context.home.is_dir()
        assert item.context.evidence_path.parent.is_dir()
        assert stat.S_IMODE(item.context.home.stat().st_mode) == 0o700
        assert stat.S_IMODE(item.context.evidence_path.parent.stat().st_mode) == 0o700
    assert acceptance._private_tree(run_root) is True


def test_every_selected_pass_is_dispatched_once_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = _private_root(tmp_path / "acceptance")
    sealed = acceptance._preseal_passes("all", run_root, acceptance._load_manifests())
    candidate_files = (
        "tools/synthetic_live_acceptance.py",
        "tools/synthetic_live_battery.py",
    )
    candidate_digest = "a" * 64
    calls: dict[str, int] = {}
    lock = threading.Lock()

    class FakeExecutor:
        def __init__(self, environment: dict[str, str], *, instrument_path: Path) -> None:
            assert environment == {}
            assert instrument_path == acceptance.RUNNER_PATH
            self._candidate_files = candidate_files
            self._candidate_source_sha256 = candidate_digest

        def __enter__(self) -> FakeExecutor:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def _assert_candidate_unchanged(self) -> None:
            return None

        def __call__(self, _manifest: Any, _pass_spec: Any, cases: Any, context: Any) -> Any:
            # Both homes must exist before either worker is allowed to dispatch.
            assert all(item.context.home.is_dir() for item in sealed)
            with lock:
                calls[context.pass_id] = calls.get(context.pass_id, 0) + 1
            raise RuntimeError("private worker detail must not escape")

    monkeypatch.setattr(battery, "_candidate_source_paths", lambda **_kwargs: candidate_files)
    monkeypatch.setattr(battery, "_candidate_source_digest", lambda **_kwargs: candidate_digest)
    monkeypatch.setattr(battery, "_inherit_model_environment", lambda: {})
    monkeypatch.setattr(battery, "SubprocessPassExecutor", FakeExecutor)

    result = acceptance._execute_sealed(sealed, concurrency=2)

    assert calls == {item.context.pass_id: 1 for item in sealed}
    assert result.dispatches == {item.context.pass_id: 1 for item in sealed}
    assert result.worker_codes == {item.key: "pass_worker_error" for item in sealed}
    assert result.candidate_identity is True


def _closed_reconciliation(kind: str) -> dict[str, Any]:
    if kind == "pass":
        value: dict[str, Any] = {
            "schema": battery.RECONCILIATION_SCHEMA,
            "clear": True,
            "api_exact": True,
            "audit_exact": True,
            "counters_exact": True,
            "files_exact": True,
            "http_exact": True,
            "storage_exact": True,
            "tools_exact": True,
        }
    else:
        value = {
            "schema": "friday.synthetic-live-battery.tail-reconciliation.v1",
            "clear": True,
            "probe_exact": True,
            "files_exact": True,
            "database_exact": True,
        }
    value["snapshot_sha256"] = battery._sha256_bytes(battery._canonical_json_bytes(value))
    return value


def test_missing_tail_and_wrong_combined_digest_fail_closed(tmp_path: Path) -> None:
    run_root = _private_root(tmp_path / "acceptance")
    item = acceptance._preseal_passes("p06", run_root, acceptance._load_manifests())[0]
    evidence_path = item.context.evidence_path
    battery._secure_write_bytes(evidence_path, b"closed synthetic evidence\n")
    pass_reconciliation = _closed_reconciliation("pass")
    tail_reconciliation = _closed_reconciliation("tail")
    battery._secure_write_json(
        evidence_path.parent / "pass-reconciliation.json",
        pass_reconciliation,
    )
    tail_path = evidence_path.parent / "tail-reconciliation.json"
    battery._secure_write_json(tail_path, tail_reconciliation)
    pass_full_hash = battery._sha256_bytes(battery._canonical_json_bytes(pass_reconciliation))
    combined_hash = battery._sha256_bytes(
        battery._canonical_json_bytes(
            {
                "pass_reconciliation_sha256": pass_full_hash,
                "tail_reconciliation_sha256": tail_reconciliation["snapshot_sha256"],
            }
        )
    )
    result = {
        "pass_id": item.context.pass_id,
        "block": str(item.pass_spec["block"]),
        "cases": 20,
        "passed": 20,
        "failed": 0,
        "case_results": [
            {
                "case_id": case.id,
                "passed": True,
                "failure_codes": [],
                "response_sha256": "b" * 64,
                "latency_ms": 1,
                "privacy_canary_clear": True,
            }
            for case in item.cases
        ],
        "evidence_sha256": battery.file_sha256(evidence_path),
        "runtime_hash": "c" * 64,
        "pass_reconciliation_clear": True,
        "pass_reconciliation_sha256": combined_hash,
    }
    execution = acceptance.ExecutionResult(
        results={item.key: result},
        worker_codes={item.key: ""},
        dispatches={item.context.pass_id: 1},
        candidate_files=(acceptance.RUNNER_RELATIVE_PATH,),
        candidate_pre_sha256="d" * 64,
        candidate_sealed_sha256="d" * 64,
        candidate_post_sha256="d" * 64,
    )
    assert acceptance._summarize_pass(item, execution)["all_gates_exact"] is True

    forged = dict(result)
    forged["pass_reconciliation_sha256"] = "e" * 64
    forged_execution = replace(execution, results={item.key: forged})
    assert acceptance._summarize_pass(item, forged_execution)["all_gates_exact"] is False

    tail_path.rename(evidence_path.parent / "tail-reconciliation.missing")
    assert acceptance._summarize_pass(item, execution)["all_gates_exact"] is False


def test_reconciliation_and_private_tree_fail_closed(tmp_path: Path) -> None:
    root = _private_root(tmp_path / "evidence")
    components = {
        "api_exact": True,
        "audit_exact": True,
        "counters_exact": True,
        "files_exact": True,
        "http_exact": True,
        "storage_exact": True,
        "tools_exact": True,
    }
    value: dict[str, Any] = {
        "schema": battery.RECONCILIATION_SCHEMA,
        "clear": True,
        **components,
    }
    value["snapshot_sha256"] = battery._sha256_bytes(battery._canonical_json_bytes(value))
    path = root / "pass-reconciliation.json"
    battery._secure_write_json(path, value)

    clear, snapshot, observed, full_hash = acceptance._read_reconciliation(path, kind="pass")
    assert clear is True
    assert snapshot == value["snapshot_sha256"]
    assert observed == components
    assert battery._is_sha256(full_hash)
    assert acceptance._private_tree(root) is True

    path.chmod(0o644)
    assert acceptance._read_reconciliation(path, kind="pass")[0] is False
    assert acceptance._private_tree(root) is False

    path.chmod(0o600)
    fifo = root / "unexpected-fifo"
    os.mkfifo(fifo, mode=0o600)
    assert acceptance._private_tree(root) is False


def test_cli_has_no_retry_resume_resubmit_or_repair_path() -> None:
    parser = acceptance._parser()
    option_strings = {option for action in parser._actions for option in action.option_strings}

    assert not option_strings.intersection(
        {
            "--retry",
            "--retries",
            "--resume",
            "--resubmit",
            "--repair",
            "--rerun-failed",
        }
    )
    assert "--env-file" in option_strings


def test_acceptance_audit_only_does_not_select_or_read_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_path = tmp_path / "do-not-read-this-config.env"

    def refuse(_path: Path) -> None:
        raise AssertionError("audit-only must not select an environment file")

    monkeypatch.setattr(battery, "_select_live_env_file", refuse)

    assert acceptance.main(["--suite", "all", "--audit-only", "--env-file", str(private_path)]) == 0
    output = capsys.readouterr().out
    assert private_path.name not in output
    assert json.loads(output)["valid"] is True


def test_acceptance_selects_explicit_env_without_publishing_its_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_path = tmp_path / "operator-private-config-name.env"
    selected: Path | None = None

    def fake_select(path: Path) -> None:
        nonlocal selected
        selected = path

    def fake_run(
        suite: str,
        *,
        run_directory: Path,
        concurrency: int,
        artifact_id: str,
    ) -> tuple[int, dict[str, Any]]:
        assert suite == "all"
        assert run_directory.parent == ROOT / "data" / "live-battery-runs"
        assert concurrency == battery.DEFAULT_CONCURRENCY
        return 4, {
            "schema": acceptance.SUMMARY_SCHEMA,
            "artifact_id": artifact_id,
            "status": "red",
        }

    monkeypatch.setattr(battery, "_select_live_env_file", fake_select)
    monkeypatch.setattr(acceptance, "run_acceptance", fake_run)

    assert acceptance.main(["--env-file", str(private_path)]) == 4
    output = capsys.readouterr().out
    assert selected == private_path
    assert private_path.name not in output
    assert str(private_path) not in output
    assert "env_file" not in json.loads(output)


def test_acceptance_env_preflight_failure_is_sanitized(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_path = tmp_path / "private-config-basename.env"

    assert acceptance.main(["--env-file", str(private_path)]) == 4

    streams = capsys.readouterr()
    public = json.loads(streams.out)
    assert public["status"] == "red"
    assert public["code"] == "pre_release_runner_failed"
    assert private_path.name not in streams.out
    assert private_path.name not in streams.err


@pytest.mark.parametrize(
    "key",
    ["FRIDAY_LLM_BASE_URL", "FRIDAY_EMBEDDINGS_BASE_URL", "FRIDAY_RERANK_BASE_URL"],
)
def test_release_endpoints_require_numeric_local_addresses(key: str) -> None:
    safe = {
        "FRIDAY_LLM_BASE_URL": "http://127.0.0.1:8001/v1",
        "FRIDAY_EMBEDDINGS_BASE_URL": "http://127.0.0.1:8002/v1",
        "FRIDAY_RERANK_BASE_URL": "http://192.168.1.20:8003/v1",
    }
    assert set(battery._configured_model_endpoint_urls(safe)) == {"model", "embedding", "reranker"}

    hostname = dict(safe)
    hostname[key] = "http://localhost:8001/v1"
    with pytest.raises(battery.BatteryContractError, match="worker_relay_endpoint_invalid"):
        battery._configured_model_endpoint_urls(hostname)

    public = dict(safe)
    public[key] = "http://8.8.8.8:8001/v1"
    with pytest.raises(battery.BatteryContractError, match="worker_relay_endpoint_invalid"):
        battery._configured_model_endpoint_urls(public)


def test_custom_run_directory_name_never_reaches_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_name = "customer-secret-project-name"
    run_directory = tmp_path / secret_name
    captured_artifact_id = ""

    def fake_run(
        suite: str,
        *,
        run_directory: Path,
        concurrency: int,
        artifact_id: str,
    ) -> tuple[int, dict[str, Any]]:
        nonlocal captured_artifact_id
        assert suite == "p06"
        assert run_directory.name == secret_name
        assert concurrency == 1
        captured_artifact_id = artifact_id
        return 4, {
            "schema": acceptance.SUMMARY_SCHEMA,
            "artifact_id": artifact_id,
            "status": "red",
        }

    monkeypatch.setattr(acceptance, "run_acceptance", fake_run)

    assert (
        acceptance.main(
            [
                "--suite",
                "p06",
                "--concurrency",
                "1",
                "--run-directory",
                str(run_directory),
            ]
        )
        == 4
    )
    output = capsys.readouterr().out
    assert secret_name not in output
    assert re.fullmatch(r"PRE-RELEASE-P06-[0-9a-f]{16}", captured_artifact_id)
    assert json.loads(output)["artifact_id"] == captured_artifact_id


def test_default_artifact_id_is_the_default_directory_locator(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured_directory: Path | None = None

    def fake_run(
        suite: str,
        *,
        run_directory: Path,
        concurrency: int,
        artifact_id: str,
    ) -> tuple[int, dict[str, Any]]:
        nonlocal captured_directory
        assert suite == "all"
        assert concurrency == battery.DEFAULT_CONCURRENCY
        captured_directory = run_directory
        return 4, {
            "schema": acceptance.SUMMARY_SCHEMA,
            "artifact_id": artifact_id,
            "status": "red",
        }

    monkeypatch.setattr(acceptance, "run_acceptance", fake_run)

    assert acceptance.main([]) == 4
    public = json.loads(capsys.readouterr().out)
    assert captured_directory is not None
    assert captured_directory.parent == ROOT / "data" / "live-battery-runs"
    assert captured_directory.name == public["artifact_id"]


def test_existing_run_directory_is_refused_without_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_directory = _private_root(tmp_path / "existing-private-directory")
    before = tuple(run_directory.iterdir())

    assert acceptance.main(["--suite", "p06", "--run-directory", str(run_directory)]) == 4

    assert tuple(run_directory.iterdir()) == before
    public = json.loads(capsys.readouterr().out)
    assert public["status"] == "red"
    assert public["code"] == "pre_release_runner_failed"
    assert run_directory.name not in json.dumps(public)
