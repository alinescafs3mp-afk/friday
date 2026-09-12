"""R10 native boundary tests: real OS isolation, no deployed model calls."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import document_contour_live_battery as lifecycle
from tools import release_1_0_native as native
from tools.release_1_0_live_cases import WORD_VARIANTS, word_fixture


def _certified_wheel_fixture(tmp_path, monkeypatch, *, wheel_version="0.208.57"):
    _, gate, _, _ = native._dependencies()
    candidate = "1" * 40
    base = "2" * 40
    tree = "3" * 40
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    source.chmod(0o700)
    (source / "pyproject.toml").write_text('[project]\nname = "friday"\nversion = "0.208.57"\n')
    monkeypatch.setattr(native, "ROOT", source)
    monkeypatch.setattr(gate, "_git_output", lambda root, *args: tree)

    custody = tmp_path / "custody"
    custody.mkdir(mode=0o700)
    custody.chmod(0o700)
    wheel = custody / f"friday-{wheel_version}-py3-none-any.whl"
    dist_info = f"friday-{wheel_version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.4\nName: friday\nVersion: {wheel_version}\n",
        )
        archive.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\n")
    wheel.chmod(0o600)
    wheel_sha = native._sha(wheel.read_bytes())
    receipt = {
        "schema": "friday.quality-gate-summary.v2",
        "result": "passed",
        "certification_eligible": True,
        "candidate_sha": candidate,
        "candidate_tree": tree,
        "base_sha": base,
        "tier": "exact-release",
        "inventory_sha256": "4" * 64,
        "invariant_identity": "semantic-function+exact-parameter-set",
        "wheel_sha256": wheel_sha,
        "test_runtime_wheel_sha256": wheel_sha,
        "comparison_wheel": {
            "epoch_commit": None,
            "expected_sha256": None,
            "observed_sha256": None,
            "build_profile": None,
        },
        "topology": {},
        "release_host_capacity": {},
        "completed_steps": [
            "candidate wheel build",
            "candidate wheel verifier",
            "clean-install candidate wheel",
            "one authoritative candidate collection",
            "exact-release non-UI tests",
        ],
        "partition": [],
        "executed": [],
        "scratch_groups": [],
        "workload_metrics_before_evidence": {"retry_count": 0},
        "active_deadlines": {},
        "owned_commands": [],
        "auxiliary_commands": [],
    }
    receipt_path = custody / "quality-gate-summary.json"

    def write_receipt():
        receipt_path.write_bytes(native._canonical(receipt))
        receipt_path.chmod(0o600)
        return native._sha(receipt_path.read_bytes())

    return {
        "candidate": candidate,
        "base": base,
        "tree": tree,
        "source": source,
        "wheel": wheel,
        "wheel_sha": wheel_sha,
        "receipt": receipt,
        "receipt_path": receipt_path,
        "receipt_sha": write_receipt(),
        "write_receipt": write_receipt,
        "run_dir": tmp_path / "future-run",
    }


def test_certified_wheel_matches_exact_receipt_without_mutating_custody(tmp_path, monkeypatch):
    fixture = _certified_wheel_fixture(tmp_path, monkeypatch)
    wheel_before = fixture["wheel"].stat()
    receipt_before = fixture["receipt_path"].stat()

    observed = native._validate_certified_wheel(
        path=fixture["wheel"],
        sha256=fixture["wheel_sha"],
        receipt_path=fixture["receipt_path"],
        receipt_sha256=fixture["receipt_sha"],
        candidate_sha=fixture["candidate"],
        base_sha=fixture["base"],
        run_dir=fixture["run_dir"],
    )

    assert observed.sha256 == fixture["wheel_sha"]
    assert observed.version == "0.208.57"
    assert observed.content == fixture["wheel"].read_bytes()
    assert (fixture["wheel"].stat().st_ino, fixture["wheel"].stat().st_mode) == (
        wheel_before.st_ino,
        wheel_before.st_mode,
    )
    assert (fixture["receipt_path"].stat().st_ino, fixture["receipt_path"].stat().st_mode) == (
        receipt_before.st_ino,
        receipt_before.st_mode,
    )


def test_certified_wheel_is_privately_installed_from_retained_exact_bytes(tmp_path, monkeypatch):
    fixture = _certified_wheel_fixture(tmp_path, monkeypatch)
    certified = native._validate_certified_wheel(
        path=fixture["wheel"],
        sha256=fixture["wheel_sha"],
        receipt_path=fixture["receipt_path"],
        receipt_sha256=fixture["receipt_sha"],
        candidate_sha=fixture["candidate"],
        base_sha=fixture["base"],
        run_dir=fixture["run_dir"],
    )
    _, gate, _, battery = native._dependencies()
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    retained = run / fixture["wheel"].name
    battery._secure_write_bytes(retained, certified.content)
    scratch = run / "scratch"
    scratch.mkdir(mode=0o700)
    commands = []

    def execute(command):
        commands.append(command)
        assert str(retained) in command.argv
        return 0

    monkeypatch.setattr(gate, "run_command", execute)
    site = native._install_certified_wheel(
        certified,
        retained,
        source=fixture["source"],
        scratch=scratch,
        gate=gate,
    )

    assert [command.name for command in commands] == [
        "certified wheel verifier",
        "clean-install certified wheel",
    ]
    assert site == scratch / "certified-runtime/site-packages"
    assert site.stat().st_mode & 0o777 == 0o700
    assert retained.stat().st_mode & 0o777 == 0o600
    assert native._retained_wheel_sha256(retained) == fixture["wheel_sha"]


@pytest.mark.parametrize(
    "fault,code",
    [
        ("wrong-hash", "native_certified_wheel_digest_mismatch"),
        ("wrong-source", "native_certified_wheel_source_mismatch"),
        ("wrong-version", "native_certified_wheel_version_mismatch"),
        ("missing", "native_certified_wheel_invalid"),
        ("unsafe-mode", "native_certified_wheel_invalid"),
        ("unsafe-link", "native_certified_wheel_invalid"),
    ],
)
def test_certified_wheel_rejects_unbound_or_unsafe_artifact(tmp_path, monkeypatch, fault, code):
    fixture = _certified_wheel_fixture(
        tmp_path,
        monkeypatch,
        wheel_version="0.208.58" if fault == "wrong-version" else "0.208.57",
    )
    supplied_sha = fixture["wheel_sha"]
    if fault == "wrong-hash":
        supplied_sha = "0" * 64
    elif fault == "wrong-source":
        fixture["receipt"]["candidate_sha"] = "9" * 40
        fixture["receipt_sha"] = fixture["write_receipt"]()
    elif fault == "missing":
        fixture["wheel"].unlink()
    elif fault == "unsafe-mode":
        fixture["wheel"].chmod(0o640)
    elif fault == "unsafe-link":
        target = fixture["wheel"].with_name("foreign.whl")
        fixture["wheel"].rename(target)
        fixture["wheel"].symlink_to(target)

    with pytest.raises(native.NativeError, match=f"^{code}$"):
        native._validate_certified_wheel(
            path=fixture["wheel"],
            sha256=supplied_sha,
            receipt_path=fixture["receipt_path"],
            receipt_sha256=fixture["receipt_sha"],
            candidate_sha=fixture["candidate"],
            base_sha=fixture["base"],
            run_dir=fixture["run_dir"],
        )


@pytest.mark.parametrize(
    "fault",
    [
        "",
        "artifact_missing",
        "identity",
        "pass_with_failure",
        "request_changed",
        "retained_missing",
        "retained_wrong",
        "bool_count",
        "runtime_identity",
        "probe_missing",
        "probe_invalid",
        "probe_nonzero",
        "probe_fifo",
        "probe_cleanup_uncertain",
        "controller_model_environment",
        "host_relay_cleanup",
        "live_exception",
        "cleanup_uncertain",
        "reader_uncertain",
        "secondary",
        "secondary_wrong_mode",
        "secondary_counts_missing",
        "secondary_ca",
        "secondary_ca_source_changed",
        "secondary_ca_origin_mismatch",
        "secondary_report_mode_mismatch",
    ],
)
def test_case_controller_requires_bound_observed_result_and_preserves_receipt(tmp_path, monkeypatch, fault):
    _, gate, _, battery = native._dependencies()
    source = tmp_path / "source"
    site = tmp_path / "site"
    run = tmp_path / "run"
    for path in (source, site, run):
        path.mkdir(mode=0o700)
    (source / "input.txt").write_text("frozen source")
    snapshot = SimpleNamespace(root=source, relative_paths=("input.txt",))
    identity = {
        key: value
        for key, value in _request().items()
        if key
        in {
            "candidate_sha",
            "candidate_tree",
            "candidate_source_sha256",
            "wheel_sha256",
            "installed_site_sha256",
            "suite_sha256",
            "model_environment_sha256",
        }
    }
    identity["candidate_source_sha256"] = battery._candidate_source_digest(
        root=source, relative_paths=snapshot.relative_paths
    )
    identity["installed_site_sha256"] = gate._projection_digest(site)
    commands = []
    secondary = fault.startswith("secondary")
    use_ca = fault.startswith("secondary_ca")
    ca_path = tmp_path / "selected-ca.pem"
    if use_ca:
        ca_path.write_bytes(b"-----BEGIN CERTIFICATE-----\nU1lOVEhFVElDLUNB\n-----END CERTIFICATE-----\n")
        ca_path.chmod(0o644)
    descriptors = []

    def child_command(**kwargs):
        commands.append("probe" if kwargs.get("probe") else "live")
        request_path = kwargs["request_path"]
        request = json.loads(request_path.read_bytes())
        if use_ca:
            descriptor = request["secondary_ca"]
            assert descriptor == kwargs["secondary_ca"]
            staged = native._assets().verify_staged(descriptor, request_path.parent)
            assert staged.read_bytes() == ca_path.read_bytes()
            assert str(staged) != str(ca_path)
            descriptors.append(dict(descriptor))
        program = "import signal;signal.pthread_sigmask(signal.SIG_UNBLOCK,{signal.SIGINT,signal.SIGTERM});"
        if kwargs.get("probe"):
            probe_report = {
                "schema": native.PROBE_PROTOCOL,
                "status": "PASS",
                "identity": request,
                "effective_runtime_sha256": "8" * 64,
                "worker_environment_sha256": request["worker_environment_sha256"],
            }
            if fault == "probe_missing":
                return [sys.executable, "-I", "-c", program]
            if fault == "probe_invalid":
                probe_report.pop("effective_runtime_sha256")
            if fault == "probe_nonzero":
                program += "raise SystemExit(7);"
            if fault == "probe_fifo":
                program += (
                    f"import os;os.unlink({str(request_path)!r});os.mkfifo({str(request_path)!r},0o600);"
                )
            return [
                sys.executable,
                "-I",
                "-c",
                program + f"print({json.dumps(probe_report)!r})",
            ]
        expected_runtime = {
            "effective_runtime_sha256": request["expected_effective_runtime_sha256"],
            "worker_environment_sha256": request["expected_worker_environment_sha256"],
            "runtime_binding_sha256": request["expected_runtime_binding_sha256"],
        }
        report = {
            "schema": native.PROTOCOL,
            "identity": request,
            "id": request["case_id"],
            "status": "PASS",
            "failure_codes": [],
            "layer": "isolated-live",
            "attempt": 1,
            "chat_submissions": 1,
            "duration_ms": 0,
            "python_version": sys.version,
            "root_class": None,
            "secondary_enabled": False,
            "runtime_sha256": request["expected_effective_runtime_sha256"],
            "runtime_identity_before": dict(expected_runtime),
            "runtime_identity_live_after": dict(expected_runtime),
            "runtime_identity_reloaded_after": dict(expected_runtime),
            "model_http_counts": {"model": 1, "embedding": 0, "reranker": 0, "other": 0},
            "observed_safe": {
                "fixture_sha256": request["fixture_sha256"],
                "artifact_sha256": native._sha(b"x"),
                "artifact_size_bytes": 1,
            },
        }
        if secondary:
            report["secondary_enabled"] = fault != "secondary_wrong_mode"
            report["secondary_runtime"] = {
                "startup": {"mode": "assist", "state": "probing", "configured": True, "available": False},
                "after_case": {"mode": "assist", "state": "healthy", "configured": True, "available": True},
            }
            if fault != "secondary_counts_missing":
                report["model_http_counts"].update(secondary=0, secondary_health=2)
        if fault == "secondary_report_mode_mismatch":
            report["secondary_runtime"]["after_case"]["mode"] = "shadow"
        if fault == "secondary_ca_source_changed":
            ca_path.write_bytes(ca_path.read_bytes().replace(b"U1lOVEhFVElDLUNB", b"QUxURVJOQVRFQ0E="))
        if fault == "artifact_missing":
            report.pop("observed_safe")
        elif fault == "identity":
            report["identity"]["candidate_sha"] = "0" * 40
        elif fault == "pass_with_failure":
            report["failure_codes"] = ["observed_damage"]
        elif fault == "bool_count":
            report["chat_submissions"] = True
        elif fault == "runtime_identity":
            report["runtime_sha256"] = "9" * 64
            report["runtime_identity_live_after"]["effective_runtime_sha256"] = "9" * 64
            report["runtime_identity_live_after"]["runtime_binding_sha256"] = native._runtime_binding(
                request,
                effective_runtime_sha256="9" * 64,
                worker_environment_sha256=request["expected_worker_environment_sha256"],
                probe_receipt_sha256=request["runtime_probe_receipt_sha256"],
            )
        fixture = word_fixture(request["case_id"], request["run_id"])
        battery._secure_write_bytes(request_path.parent / f"artifact-{fixture.sha256}.bin", fixture.content)
        if fault != "retained_missing":
            battery._secure_write_bytes(
                request_path.parent / f"artifact-{native._sha(b'x')}.bin",
                b"wrong" if fault == "retained_wrong" else b"x",
            )
        if fault == "request_changed":
            program += f"from pathlib import Path;Path({str(request_path)!r}).write_bytes(b'changed');"
        return [sys.executable, "-I", "-c", program + f"print({json.dumps(report)!r})"]

    monkeypatch.setattr(native, "_sandbox_command", child_command)
    if fault == "host_relay_cleanup":
        monkeypatch.setattr(native, "_host_relays_cleanup_clear", lambda *args: False)
    if fault in {
        "cleanup_uncertain",
        "reader_uncertain",
        "probe_cleanup_uncertain",
        "live_exception",
    }:
        from dataclasses import replace

        original_process = lifecycle._run_worker_process
        process_calls = 0

        def uncertain_process(*args, **kwargs):
            nonlocal process_calls
            process_calls += 1
            if fault == "live_exception" and process_calls == 2:
                raise RuntimeError("injected live process failure")
            outcome = original_process(*args, **kwargs)
            probe_fault = fault == "probe_cleanup_uncertain" and process_calls == 1
            live_fault = fault != "probe_cleanup_uncertain" and process_calls == 2
            if not (probe_fault or live_fault):
                return outcome
            if live_fault:
                request_path = run / "case-001/evidence/worker-request.json"
                request_path.unlink()
                request_path.mkdir()
            return replace(
                outcome,
                process_group_clear=fault == "reader_uncertain",
                cleanup_failure_codes=(
                    "worker_stdout_reader_not_clear"
                    if fault == "reader_uncertain"
                    else "worker_process_group_not_clear",
                ),
            )

        monkeypatch.setattr(lifecycle, "_run_worker_process", uncertain_process)
    model_env = {
        key: "http://127.0.0.1:9"
        for key in ("FRIDAY_LLM_BASE_URL", "FRIDAY_EMBEDDINGS_BASE_URL", "FRIDAY_RERANK_BASE_URL")
    }
    if secondary:
        model_env.update(
            FRIDAY_SECONDARY_LLM_ENABLED="1",
            FRIDAY_SECONDARY_LLM_BASE_URL="http://127.0.0.1:19004/v1",
            FRIDAY_SECONDARY_LLM_MODE="assist",
        )
    if use_ca:
        model_env["JERICHO_SECONDARY_LLM_CA_FILE"] = str(ca_path)
    identity["model_environment_sha256"] = native._sha(native._canonical(model_env))
    if fault == "controller_model_environment":
        model_env["FRIDAY_LLM_MODEL"] = "changed-after-freeze"
    case_kwargs = dict(
        spec={"id": WORD_VARIANTS[0][0], "timeout_s": 2},
        index=1,
        run_id="a" * 32,
        source=source,
        snapshot=snapshot,
        site=site,
        run_dir=run,
        model_env=model_env,
        identity=identity,
        signals=None,
    )
    if fault == "secondary_ca_origin_mismatch":
        other_ca = tmp_path / "other-ca.pem"
        other_ca.write_bytes(ca_path.read_bytes())
        other_ca.chmod(0o644)
        case_kwargs["captured_ca"] = native._assets().capture_ca(other_ca)
        with pytest.raises(native.NativeError, match="native_secondary_ca_binding_mismatch"):
            native._run_case(**case_kwargs)
        assert commands == []
        assert not (run / "case-001/evidence/secondary-ca.pem").exists()
        return
    if fault == "live_exception":
        with pytest.raises(RuntimeError, match="injected live process failure"):
            native._run_case(**case_kwargs)
        assert (run / "case-001/home").is_dir()
        assert commands == ["probe", "live"]
        return
    report = native._run_case(**case_kwargs)
    assert report["status"] == (
        "PASS"
        if not fault or fault in {"secondary", "secondary_ca"}
        else "FAIL"
        if fault
        in {
            "request_changed",
            "retained_missing",
            "retained_wrong",
            "cleanup_uncertain",
            "reader_uncertain",
            "probe_cleanup_uncertain",
            "host_relay_cleanup",
            "secondary_ca_source_changed",
        }
        else "NOT_RUN"
    ), report
    uncertain = fault in {
        "cleanup_uncertain",
        "reader_uncertain",
        "probe_cleanup_uncertain",
        "host_relay_cleanup",
    }
    assert report["process_cleanup_clear"] is not uncertain
    assert (run / "case-001/home").exists() is uncertain
    receipt = run / ("case-001-receipt.json" if uncertain else "case-001/evidence/case-receipt.json")
    assert json.loads(receipt.read_bytes()) == report
    if use_ca:
        assert len(descriptors) == 2 and descriptors[0] == descriptors[1]
        assert model_env["JERICHO_SECONDARY_LLM_CA_FILE"] == str(ca_path)
        assert "FRIDAY_SECONDARY_LLM_CA_FILE" not in model_env
        if fault == "secondary_ca_source_changed":
            assert "native_secondary_ca_changed" in report["failure_codes"]
    if fault in {"secondary", "secondary_ca"}:
        assert report["secondary_enabled"] is True
        assert report["model_http_counts"]["secondary"] == 0
        assert report["model_http_counts"]["secondary_health"] == 2
    assert commands == (
        []
        if fault == "controller_model_environment"
        else ["probe"]
        if fault
        in {"probe_missing", "probe_invalid", "probe_nonzero", "probe_fifo", "probe_cleanup_uncertain"}
        else ["probe", "live"]
    )


def _request():
    case_id = WORD_VARIANTS[0][0]
    run_id = "a" * 32
    request = {
        "protocol": native.PROTOCOL,
        "case_id": case_id,
        "run_id": run_id,
        "candidate_sha": "1" * 40,
        "candidate_tree": "2" * 40,
        "candidate_source_sha256": "3" * 64,
        "candidate_files": ["tools/release_1_0_native.py"],
        "wheel_sha256": "4" * 64,
        "installed_site_sha256": "5" * 64,
        "suite_sha256": "6" * 64,
        "model_environment_sha256": "7" * 64,
        "expected_effective_runtime_sha256": "8" * 64,
        "expected_worker_environment_sha256": "9" * 64,
        "runtime_probe_receipt_sha256": "a" * 64,
        "fixture_sha256": word_fixture(case_id, run_id).sha256,
    }
    request["expected_runtime_binding_sha256"] = native._runtime_binding(
        request,
        effective_runtime_sha256=request["expected_effective_runtime_sha256"],
        worker_environment_sha256=request["expected_worker_environment_sha256"],
        probe_receipt_sha256=request["runtime_probe_receipt_sha256"],
    )
    return request


@pytest.mark.parametrize("arbitrary", ["0" * 64, "f" * 64])
def test_one_sealed_request_rejects_distinct_arbitrary_runtime_hashes(arbitrary):
    request = _request()
    expected = {
        "effective_runtime_sha256": request["expected_effective_runtime_sha256"],
        "worker_environment_sha256": request["expected_worker_environment_sha256"],
        "runtime_binding_sha256": request["expected_runtime_binding_sha256"],
    }
    after = {
        **expected,
        "effective_runtime_sha256": arbitrary,
        "runtime_binding_sha256": native._runtime_binding(
            request,
            effective_runtime_sha256=arbitrary,
            worker_environment_sha256=request["expected_worker_environment_sha256"],
            probe_receipt_sha256=request["runtime_probe_receipt_sha256"],
        ),
    }
    report = {
        "schema": native.PROTOCOL,
        "identity": request,
        "id": request["case_id"],
        "status": "PASS",
        "failure_codes": [],
        "layer": "isolated-live",
        "attempt": 1,
        "chat_submissions": 1,
        "duration_ms": 0,
        "python_version": sys.version,
        "root_class": None,
        "secondary_enabled": False,
        "runtime_sha256": arbitrary,
        "runtime_identity_before": expected,
        "runtime_identity_live_after": after,
        "runtime_identity_reloaded_after": expected,
        "model_http_counts": {"model": 1, "embedding": 0, "reranker": 0, "other": 0},
        "observed_safe": {
            "fixture_sha256": request["fixture_sha256"],
            "artifact_sha256": "b" * 64,
            "artifact_size_bytes": 1,
        },
    }
    assert not native._worker_result_valid(report, request)


@pytest.mark.parametrize(
    "field,value",
    [
        ("chat_submissions", True),
        ("observed_safe", "arbitrary"),
        ("model_http_counts", "arbitrary"),
        ("secondary_enabled", "arbitrary"),
        ("python_version", "arbitrary"),
        ("observed_artifact_sha256", "arbitrary"),
        ("observed_artifact_size_bytes", True),
    ],
)
def test_worker_result_rejects_malformed_fail_common_fields(field, value):
    request = _request()
    expected = {
        "effective_runtime_sha256": request["expected_effective_runtime_sha256"],
        "worker_environment_sha256": request["expected_worker_environment_sha256"],
        "runtime_binding_sha256": request["expected_runtime_binding_sha256"],
    }
    report = {
        "schema": native.PROTOCOL,
        "identity": request,
        "id": request["case_id"],
        "status": "FAIL",
        "failure_codes": ["observed_damage"],
        "layer": "isolated-live",
        "attempt": 1,
        "chat_submissions": 1,
        "duration_ms": 0,
        "python_version": sys.version,
        "root_class": "product",
        "secondary_enabled": False,
        "runtime_sha256": request["expected_effective_runtime_sha256"],
        "runtime_identity_before": dict(expected),
        "runtime_identity_live_after": dict(expected),
        "runtime_identity_reloaded_after": dict(expected),
        "model_http_counts": {"model": 1, "embedding": 0, "reranker": 0, "other": 0},
        "observed_safe": {"fixture_sha256": request["fixture_sha256"]},
    }
    assert native._worker_result_valid(report, request)
    if field.startswith("observed_artifact_"):
        report["observed_safe"].update(
            artifact_sha256="b" * 64,
            artifact_size_bytes=1,
        )
        report["observed_safe"][field.removeprefix("observed_")] = value
    else:
        report[field] = value
    assert not native._worker_result_valid(report, request)


@pytest.mark.parametrize(
    "fault", ["extra_field", "fixture", "duplicate_source", "bad_identity", "ca_descriptor"]
)
def test_worker_rejects_unbound_request_before_execution(fault):
    request = _request()
    if fault == "extra_field":
        request["replace_model"] = True
    elif fault == "fixture":
        request["fixture_sha256"] = "b" * 64
    elif fault == "duplicate_source":
        request["candidate_files"] *= 2
    elif fault == "ca_descriptor":
        request["secondary_ca"] = {"schema": "friday.r10-native-ca.v1", "arbitrary": True}
    else:
        request["candidate_sha"] = "short"
    with pytest.raises(native.NativeError):
        native._validate_request(request)


def test_worker_read_rejects_wrong_digest_without_starting_case(tmp_path, monkeypatch, capsys):
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    path = evidence / "worker-request.json"
    path.write_bytes(native._canonical(_request()))
    path.chmod(0o600)
    monkeypatch.setenv("FRIDAY_LIVE_BATTERY_EVIDENCE", str(evidence / "observed.json"))
    assert native.worker_main(str(path), "0" * 64) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "NOT_RUN"
    assert report["failure_codes"] == ["native_worker_request_digest_invalid"]


@pytest.mark.parametrize("replacement", ["fifo", "symlink", "new_inode"])
def test_sealed_file_stamp_rejects_nonregular_or_replaced_path(tmp_path, replacement):
    path = tmp_path / "worker-request.json"
    expected = b'{"sealed":true}'
    path.write_bytes(expected)
    path.chmod(0o600)
    original = native._sealed_file_stamp(path, expected)
    assert original is not None
    path.unlink()
    if replacement == "fifo":
        os.mkfifo(path, 0o600)
    elif replacement == "symlink":
        target = tmp_path / "target"
        os.mkfifo(target, 0o600)
        path.symlink_to(target)
    else:
        path.write_bytes(expected)
        path.chmod(0o600)
    assert native._sealed_file_stamp(path, expected) != original


def test_host_relay_cleanup_audit_rejects_live_owned_thread(tmp_path):
    release = threading.Event()
    started = threading.Event()

    def linger():
        started.set()
        release.wait(2)

    thread = threading.Thread(target=linger)
    thread.start()
    assert started.wait(1)
    stopped = threading.Event()
    stopped.set()
    relay = SimpleNamespace(
        _lock=threading.Lock(),
        _threads=[thread],
        _connections=set(),
        _listener=None,
        _stop=stopped,
        socket_path=tmp_path / "gone.sock",
    )
    owner = SimpleNamespace(directory=None, _temporary=None, _relays=[])
    try:
        assert not native._host_relays_cleanup_clear(owner, tmp_path / "gone", (relay,))
    finally:
        release.set()
        thread.join(timeout=1)


def test_model_file_is_authority_and_cannot_set_runtime_home(tmp_path, monkeypatch):
    path = tmp_path / "models.env"
    path.write_text(
        "FRIDAY_LLM_MODEL=selected\nFRIDAY_RERANK_BASE_URL=http://127.0.0.1:9\nFRIDAY_HOME=/forbidden\n"
    )
    path.chmod(0o600)
    monkeypatch.setenv("FRIDAY_LLM_MODEL", "ambient-poison")
    environment = native._model_environment(path)
    assert environment["FRIDAY_LLM_MODEL"] == "selected"
    assert "FRIDAY_HOME" not in environment and "ambient-poison" not in str(environment)


def test_enabled_secondary_keeps_selected_mode_and_requires_closed_endpoint(tmp_path, monkeypatch):
    _, _, _, battery = native._dependencies()
    path = tmp_path / "models.env"
    path.write_text("JERICHO_SECONDARY_LLM_ENABLED=custom-true\nFRIDAY_RERANK_BASE_URL=http://127.0.0.1:9\n")
    path.chmod(0o600)
    monkeypatch.setenv("FRIDAY_SECONDARY_LLM_BASE_URL", "http://127.0.0.1:19999/v1")
    with pytest.raises(battery.BatteryContractError, match="worker_relay_endpoint_invalid"):
        native._model_environment(path)
    path.write_text(path.read_text() + "JERICHO_SECONDARY_LLM_BASE_URL=http://127.0.0.1:19004/v1\n")
    environment = native._model_environment(path)
    assert native._secondary_enabled(environment, battery=battery)
    assert environment["JERICHO_SECONDARY_LLM_ENABLED"] == "custom-true"
    assert native._native_endpoints(environment, battery=battery)["secondary"] == "http://127.0.0.1:19004/v1"
    for value in ("", "0", "false", "no", "off"):
        assert not native._secondary_enabled(
            {"FRIDAY_SECONDARY_LLM_ENABLED": value, **environment}, battery=battery
        )


def test_actual_sandbox_denies_host_file_network_and_product_writes(tmp_path):
    snapshot = tmp_path / "snapshot"
    site = tmp_path / "site"
    case = tmp_path / "case"
    relays = tmp_path / "relays"
    for path in (snapshot, site, case, relays, case / "home"):
        path.mkdir(mode=0o700)
    outside = tmp_path / "outside-private-marker"
    outside.write_text("synthetic outside data")
    (snapshot / "visible").write_text("source")
    with socket.socket() as listener, (tmp_path / "process.log").open("wb") as log:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        command = native._sandbox_command(
            snapshot=snapshot,
            site=site,
            case_root=case,
            relays=relays,
            request_path=case / "evidence/worker-request.json",
            request_sha="0" * 64,
        )
        program = f"""
import json,pathlib,signal,socket
signal.pthread_sigmask(signal.SIG_UNBLOCK,{{signal.SIGINT,signal.SIGTERM}})
result={{"host_file":pathlib.Path({str(outside)!r}).exists(),"source_visible":pathlib.Path('/workspace/visible').read_text()=='source'}}
try:
 s=socket.create_connection(('127.0.0.1',{port}),timeout=0.1);s.close();result['host_network']=True
except OSError:result['host_network']=False
try:pathlib.Path('/candidate-site/write').write_text('bad');result['product_write']=True
except OSError:result['product_write']=False
pathlib.Path({str(case / "home/owned-write")!r}).write_text('owned')
print(json.dumps(result))
"""
        # Test-only probe replaces the closed worker entry while retaining the
        # exact production sandbox mounts/namespaces/process lifecycle.
        command = (*command[: command.index("--") + 1], sys.executable, "-I", "-c", program)
        outcome = lifecycle._run_worker_process(
            command, environment={}, private_log=log, timeout_sec=3, stdout_limit_bytes=2048
        )
    assert outcome.returncode == 0, (tmp_path / "process.log").read_text()
    assert outcome.worker_reaped and outcome.process_group_clear
    assert not outcome.cleanup_failure_codes
    assert json.loads(outcome.stdout) == {
        "host_file": False,
        "source_visible": True,
        "host_network": False,
        "product_write": False,
    }
    assert (case / "home/owned-write").read_text() == "owned"
    assert outside.read_text() == "synthetic outside data"


@pytest.mark.parametrize(
    "fault,code",
    [
        ("digest", "native_worker_request_digest_invalid"),
        ("mode", "native_worker_request_metadata_invalid"),
        ("tool_shadow", "native_worker_request_digest_invalid"),
    ],
)
def test_native_bootstrap_reaches_closed_validation_inside_sandbox(tmp_path, fault, code):
    site = tmp_path / "site"
    case = tmp_path / "case"
    relays = tmp_path / "relays"
    for path in (site, case, relays, case / "home", case / "evidence"):
        path.mkdir(mode=0o700)
    # Minimal package-origin fixtures: no model/product execution is possible.
    # This checks the actual bootstrap, tool imports and request boundary.
    for name in ("friday", "friday_host_agent", "friday_package_broker"):
        package = site / name
        package.mkdir(mode=0o700)
        (package / "__init__.py").write_text("# synthetic origin fixture\n")
    if fault == "tool_shadow":
        (site / "tools").mkdir()
        (site / "tools/__init__.py").write_text("raise RuntimeError('dependency tools shadow executed')\n")
    request = case / "evidence/worker-request.json"
    raw = native._canonical(_request())
    request.write_bytes(raw)
    request.chmod(0o644 if fault == "mode" else 0o600)
    command = native._sandbox_command(
        snapshot=native.ROOT,
        site=site,
        case_root=case,
        relays=relays,
        request_path=request,
        request_sha="0" * 64 if fault in {"digest", "tool_shadow"} else native._sha(raw),
    )
    environment = {
        "FRIDAY_QUALITY_GATE_INSTALLED_SITE": "/candidate-site",
        "FRIDAY_LIVE_BATTERY_EVIDENCE": str(case / "evidence/observed.json"),
    }
    with (tmp_path / "process.log").open("wb") as log:
        outcome = lifecycle._run_worker_process(
            command, environment=environment, private_log=log, timeout_sec=5, stdout_limit_bytes=4096
        )
    assert outcome.returncode == 0, (tmp_path / "process.log").read_text()
    assert outcome.worker_reaped and outcome.process_group_clear and not outcome.cleanup_failure_codes
    report = json.loads(outcome.stdout)
    assert report["status"] == "NOT_RUN" and report["failure_codes"] == [code]


def _actual_sandbox_runtime_probe(tmp_path):
    _, gate, _, battery = native._dependencies()
    source = native.ROOT
    # An owned installed-package fixture must not inherit checkout permissions
    # or bind the source root as the package authority.
    site = tmp_path / "site"
    site.mkdir(mode=0o700)
    for package in gate._WHEEL_NAMESPACES:
        shutil.copytree(
            source / package,
            site / package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    case = tmp_path / "case"
    home = case / "home"
    evidence_dir = case / "evidence"
    empty_relays = case / "empty-relays"
    for path in (case, home, evidence_dir, empty_relays):
        path.mkdir(mode=0o700)
    evidence = evidence_dir / "observed.json"
    suite_sha = native._suite_digest(source)
    context = battery.PassContext(
        "R10",
        WORD_VARIANTS[0][0],
        1,
        0,
        battery.FIXED_CLOCK,
        battery.FIXED_TIMEZONE,
        suite_sha,
        home,
        evidence,
    )
    model_env = {
        "FRIDAY_LLM_BASE_URL": "http://127.0.0.1:19001/v1",
        "FRIDAY_LLM_MODEL": "probe-primary",
        "FRIDAY_LLM_ENABLED": "1",
        "FRIDAY_EMBEDDINGS_BASE_URL": "http://127.0.0.1:19002/v1",
        "FRIDAY_EMBEDDINGS_MODEL": "probe-embeddings",
        "FRIDAY_EMBEDDINGS_ENABLED": "1",
        "FRIDAY_RERANK_BASE_URL": "http://127.0.0.1:19003/v1",
        "FRIDAY_RERANK_MODEL": "probe-reranker",
        "FRIDAY_RERANK_TOP": "5",
        "FRIDAY_SECONDARY_LLM_ENABLED": "0",
    }
    environment = battery._worker_environment(
        {**model_env, "OMP_NUM_THREADS": "8", "OPENBLAS_NUM_THREADS": "8"}, context
    )
    environment.update(
        {
            "FRIDAY_QUALITY_GATE_INSTALLED_SITE": "/candidate-site",
            "FRIDAY_LIVE_BATTERY_PRODUCT_ROOT": "/candidate-site",
            "FRIDAY_TELEGRAM_OWNER_CHAT_IDS": environment["FRIDAY_LIVE_BATTERY_MAIN_CHAT"],
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PWD": str(home),
        }
    )
    for key in battery._PROCESS_SCRATCH_PATHS:
        Path(environment[key]).mkdir(parents=True, mode=0o700)
    candidate_files = ["tools/release_1_0_native.py"]
    request = {
        "protocol": native.PROBE_PROTOCOL,
        "probe_nonce": "a" * 32,
        "case_id": WORD_VARIANTS[0][0],
        "run_id": "b" * 32,
        "candidate_sha": "1" * 40,
        "candidate_tree": "2" * 40,
        "candidate_source_sha256": battery._candidate_source_digest(
            root=source, relative_paths=candidate_files
        ),
        "candidate_files": candidate_files,
        "wheel_sha256": "3" * 64,
        "installed_site_sha256": gate._projection_digest(site),
        "suite_sha256": suite_sha,
        "model_environment_sha256": native._sha(native._canonical(model_env)),
        "worker_environment_sha256": native._worker_environment_digest(environment),
    }
    request_path = evidence_dir / "runtime-probe-request.json"
    raw = native._canonical(request)
    battery._secure_write_bytes(request_path, raw)
    command = native._sandbox_command(
        snapshot=source,
        site=site,
        case_root=case,
        relays=empty_relays,
        request_path=request_path,
        request_sha=native._sha(raw),
        probe=True,
    )
    with (tmp_path / "probe-process.log").open("wb") as log:
        outcome = lifecycle._run_worker_process(
            command,
            environment=environment,
            private_log=log,
            timeout_sec=10,
            stdout_limit_bytes=1 << 20,
        )
    assert outcome.returncode == 0 and outcome.cleanup_clear, (tmp_path / "probe-process.log").read_text()
    result = json.loads(outcome.stdout)
    assert native._runtime_probe_result_valid(result, request)
    assert result["worker_environment_sha256"] == native._worker_environment_digest(environment)
    assert list(empty_relays.iterdir()) == []
    return source, site, case, environment, request, result, outcome.stdout


def test_actual_sandbox_runtime_probe_derives_installed_settings_without_relays(tmp_path):
    _actual_sandbox_runtime_probe(tmp_path)


@pytest.mark.parametrize("surviving_thread", [False, True])
def test_actual_worker_startup_shutdown_preserves_strict_thread_census(tmp_path, surviving_thread):
    """Exercise worker_main with a model-free handler; never mint a live PASS."""
    source, site, case, environment, probe, observed, probe_raw = _actual_sandbox_runtime_probe(tmp_path)
    _, _, _, battery = native._dependencies()
    request = {
        key: value
        for key, value in probe.items()
        if key not in {"protocol", "probe_nonce", "worker_environment_sha256"}
    }
    request.update(
        protocol=native.PROTOCOL,
        expected_effective_runtime_sha256=observed["effective_runtime_sha256"],
        expected_worker_environment_sha256=observed["worker_environment_sha256"],
        runtime_probe_receipt_sha256=native._sha(probe_raw),
        fixture_sha256=word_fixture(request["case_id"], request["run_id"]).sha256,
    )
    request["expected_runtime_binding_sha256"] = native._runtime_binding(
        request,
        effective_runtime_sha256=request["expected_effective_runtime_sha256"],
        worker_environment_sha256=request["expected_worker_environment_sha256"],
        probe_receipt_sha256=request["runtime_probe_receipt_sha256"],
    )
    request_path = case / "evidence" / "worker-request.json"
    raw = native._canonical(request)
    battery._secure_write_bytes(request_path, raw)
    # Short owned paths avoid AF_UNIX's path limit under long pytest node names.
    # Listening sockets have no upstream: even a regression cannot call models.
    with (
        tempfile.TemporaryDirectory(prefix="r10-census-", dir="/var/tmp") as relay_name,
        contextlib.ExitStack() as stack,
    ):
        for name in ("model.sock", "embedding.sock", "reranker.sock"):
            listener = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            path = Path(relay_name) / name
            listener.bind(str(path))
            path.chmod(0o600)
            listener.listen(4)
        command = list(
            native._sandbox_command(
                snapshot=source,
                site=site,
                case_root=case,
                relays=Path(relay_name),
                request_path=request_path,
                request_sha=native._sha(raw),
            )
        )
        prefix, entrypoint = command[-1].rsplit("raise SystemExit(", 1)
        # Replace only the journey handler. All import guards, request and
        # runtime checks, app lifespan, census and containment teardown are
        # the unmodified actual worker implementation.
        command[-1] = (
            prefix
            + f"""
import threading
from pathlib import Path
def diagnostic_handler(session, storage, fixture):
    b._secure_write_bytes(Path({str(case / "evidence" / "handler-reached")!r}), b'yes')
    if {surviving_thread!r}:
        started = threading.Event()
        def survivor():
            started.set()
            threading.Event().wait()
        threading.Thread(target=survivor, daemon=True).start()
        assert started.wait(2)
    return {{'id': {request["case_id"]!r}, 'status': 'PASS', 'failure_codes': []}}
n.live_handlers = lambda: {{{request["case_id"]!r}: diagnostic_handler}}
raise SystemExit({entrypoint}
"""
        )
        with (tmp_path / "worker-process.log").open("wb") as log:
            outcome = lifecycle._run_worker_process(
                command,
                environment=environment,
                private_log=log,
                timeout_sec=30,
                stdout_limit_bytes=1 << 20,
            )
    assert outcome.returncode == 0 and outcome.cleanup_clear, (tmp_path / "worker-process.log").read_text()
    report = json.loads(outcome.stdout)
    assert report["layer"] == "isolated-live", report
    assert (case / "evidence" / "handler-reached").read_bytes() == b"yes"
    assert report["status"] == "FAIL" and report["identity"] == request
    assert report["runtime_identity_before"] == native._expected_runtime_identity(request)
    assert report["runtime_identity_live_after"] == report["runtime_identity_before"]
    assert report["runtime_identity_reloaded_after"] == report["runtime_identity_before"]
    assert not any(report["model_http_counts"].values())
    expected_codes = ["native_primary_http_not_observed"]
    if surviving_thread:
        expected_codes += ["native_unowned_worker_thread", "native_worker_relay_cleanup_uncertain"]
    assert report["failure_codes"] == sorted(expected_codes), report
    assert environment["OMP_NUM_THREADS"] == environment["OPENBLAS_NUM_THREADS"] == "1"


def test_worker_guard_blocks_product_import_and_atexit_direct_relay_access(tmp_path):
    _, gate, _, battery = native._dependencies()
    source = native.ROOT
    site = tmp_path / "site"
    package = site / "friday"
    package.mkdir(parents=True, mode=0o700)
    site.chmod(0o700)
    (package / "__init__.py").write_text(
        """
import atexit
import os
import socket
from pathlib import Path

Path(os.environ['FRIDAY_LIVE_BATTERY_EVIDENCE']).with_name('candidate-imported').write_text('yes')

def attempt(payload):
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect('/run/friday-relays/model.sock')
        connection.sendall(payload)
        connection.close()
    except OSError:
        pass

attempt(b'import-relay-canary')
atexit.register(attempt, b'atexit-relay-canary')
"""
    )
    case = tmp_path / "case"
    home = case / "home"
    evidence_dir = case / "evidence"
    relay_temporary = tempfile.TemporaryDirectory(prefix="r10i-", dir="/tmp")
    relay_root = Path(relay_temporary.name)
    for path in (case, home, evidence_dir):
        path.mkdir(mode=0o700)
    evidence = evidence_dir / "observed.json"
    suite_sha = native._suite_digest(source)
    context = battery.PassContext(
        "R10",
        WORD_VARIANTS[0][0],
        1,
        0,
        battery.FIXED_CLOCK,
        battery.FIXED_TIMEZONE,
        suite_sha,
        home,
        evidence,
    )
    model_env = {
        "FRIDAY_LLM_BASE_URL": "http://127.0.0.1:19001/v1",
        "FRIDAY_LLM_MODEL": "sealed-primary",
        "FRIDAY_LLM_ENABLED": "1",
        "FRIDAY_EMBEDDINGS_BASE_URL": "http://127.0.0.1:19002/v1",
        "FRIDAY_EMBEDDINGS_MODEL": "sealed-embeddings",
        "FRIDAY_EMBEDDINGS_ENABLED": "1",
        "FRIDAY_RERANK_BASE_URL": "http://127.0.0.1:19003/v1",
        "FRIDAY_RERANK_MODEL": "sealed-reranker",
        "FRIDAY_RERANK_TOP": "5",
        "FRIDAY_SECONDARY_LLM_ENABLED": "0",
    }
    environment = battery._worker_environment(model_env, context)
    environment.update(
        {
            "FRIDAY_QUALITY_GATE_INSTALLED_SITE": "/candidate-site",
            "FRIDAY_LIVE_BATTERY_PRODUCT_ROOT": "/candidate-site",
            "FRIDAY_TELEGRAM_OWNER_CHAT_IDS": environment["FRIDAY_LIVE_BATTERY_MAIN_CHAT"],
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PWD": str(home),
        }
    )
    for key in battery._PROCESS_SCRATCH_PATHS:
        Path(environment[key]).mkdir(parents=True, mode=0o700)
    fixture = word_fixture(WORD_VARIANTS[0][0], "b" * 32)
    request = {
        "protocol": native.PROTOCOL,
        "case_id": WORD_VARIANTS[0][0],
        "run_id": "b" * 32,
        "candidate_sha": "1" * 40,
        "candidate_tree": "2" * 40,
        "candidate_source_sha256": "0" * 64,
        "candidate_files": ["tools/release_1_0_native.py"],
        "wheel_sha256": "3" * 64,
        "installed_site_sha256": gate._projection_digest(site),
        "suite_sha256": suite_sha,
        "model_environment_sha256": native._sha(native._canonical(model_env)),
        "expected_effective_runtime_sha256": "8" * 64,
        "expected_worker_environment_sha256": native._worker_environment_digest(environment),
        "runtime_probe_receipt_sha256": "a" * 64,
        "fixture_sha256": fixture.sha256,
    }
    request["expected_runtime_binding_sha256"] = native._runtime_binding(
        request,
        effective_runtime_sha256=request["expected_effective_runtime_sha256"],
        worker_environment_sha256=request["expected_worker_environment_sha256"],
        probe_receipt_sha256=request["runtime_probe_receipt_sha256"],
    )
    request_path = evidence_dir / "worker-request.json"
    raw = native._canonical(request)
    battery._secure_write_bytes(request_path, raw)
    listeners = []
    try:
        for name in ("model.sock", "embedding.sock", "reranker.sock"):
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(relay_root / name))
            os.chmod(relay_root / name, 0o600)
            listener.listen(4)
            listeners.append(listener)
        command = native._sandbox_command(
            snapshot=source,
            site=site,
            case_root=case,
            relays=relay_root,
            request_path=request_path,
            request_sha=native._sha(raw),
        )
        with (tmp_path / "worker-process.log").open("wb") as log:
            outcome = lifecycle._run_worker_process(
                command,
                environment=environment,
                private_log=log,
                timeout_sec=10,
                stdout_limit_bytes=1 << 20,
            )
        assert outcome.returncode == 0 and outcome.cleanup_clear, (
            tmp_path / "worker-process.log"
        ).read_text()
        report = json.loads(outcome.stdout)
        assert (evidence_dir / "candidate-imported").read_text() == "yes"
        assert report["status"] in {"NOT_RUN", "FAIL"}
        assert set(report["failure_codes"]) in (
            {"native_source_changed_before_case"},
            {"native_source_changed_before_case", "native_worker_relay_cleanup_uncertain"},
        )
        for listener in listeners:
            listener.setblocking(False)
            with pytest.raises(BlockingIOError):
                listener.accept()
    finally:
        for listener in listeners:
            listener.close()
        relay_temporary.cleanup()


@pytest.mark.parametrize(
    "fault",
    [
        "build",
        "readiness",
        "dispatch",
        "signal",
        "wheel_changed",
        "wheel_missing",
        "",
        "context",
        "context-build",
        "context-readiness",
        "context-drift",
        "context-secondary",
    ],
)
def test_run_setup_failure_keeps_denominator_one_root_and_private_evidence(tmp_path, monkeypatch, fault):
    from contextlib import nullcontext

    context_variant = fault if fault.startswith("context") else None
    if context_variant:
        fault = {"context-build": "build", "context-readiness": "readiness"}.get(context_variant, "")

    _, gate, acceptance, battery = native._dependencies()
    source = tmp_path / "source"
    source.mkdir()
    site = tmp_path / "site"
    site.mkdir()
    wheel = tmp_path / "synthetic.whl"
    wheel.write_bytes(b"synthetic build fixture only")
    wheel.chmod(0o600)
    model_env = {"FRIDAY_SECONDARY_LLM_ENABLED": "0"}
    if context_variant == "context-secondary":
        model_env = {"FRIDAY_SECONDARY_LLM_ENABLED": "1", "FRIDAY_SECONDARY_LLM_MODE": "assist"}
    candidate = "1" * 40
    monkeypatch.setattr(
        gate,
        "_git_output",
        lambda root, *args: "" if args[0] == "status" else "d" * 40 if args[0] == "merge-base" else candidate,
    )
    if context_variant:
        monkeypatch.setattr(gate, "_projection_digest", lambda site: "4" * 64)
    monkeypatch.setattr(native, "_model_environment", lambda path: model_env)
    monkeypatch.setattr(acceptance, "_assert_configured_model_environment", lambda env: None)
    # Real kernel serialization, isolated from the host acceptance lease.
    protocol = "friday.native.owner.unit." + native._sha(str(tmp_path).encode())[:24]
    monkeypatch.setattr(acceptance, "_ACCEPTANCE_LOCK_PROTOCOL", protocol.encode("ascii"))
    monkeypatch.setattr(
        acceptance, "_acceptance_lock_path", lambda: tmp_path / "runtime/locks/acceptance.lock"
    )
    monkeypatch.setattr(gate, "_candidate_projection", lambda *args, **kwargs: nullcontext(source))

    def build(*args, **kwargs):
        if fault == "build":
            raise RuntimeError("private injected build canary")
        return wheel, native._sha(wheel.read_bytes()), None, site, sys.executable

    monkeypatch.setattr(gate, "_build_reusable_wheel", build)
    monkeypatch.setattr(battery, "_validated_quality_gate_installed_site", lambda env: site)
    monkeypatch.setattr(battery, "_candidate_source_paths", lambda **kwargs: ("fixture",))
    monkeypatch.setattr(
        battery,
        "_CandidateSourceSnapshot",
        lambda **kwargs: SimpleNamespace(
            root=source, relative_paths=("fixture",), sha256="2" * 64, close=lambda: None
        ),
    )
    monkeypatch.setattr(native, "_suite_digest", lambda path: "3" * 64)
    monkeypatch.setattr(
        acceptance,
        "_model_readiness_barrier",
        lambda env: SimpleNamespace(dispatch_clear=fault != "readiness"),
    )
    import tools.release_1_0_acceptance as registry

    fixture_ids = [row[0] for row in WORD_VARIANTS] + ["fixture-required-not-selected"]
    monkeypatch.setattr(
        registry,
        "load_matrix",
        lambda: {
            "cases": [
                {"id": case_id, "release_required": True, "layer": "isolated-live", "timeout_s": 2}
                for case_id in fixture_ids
            ]
        },
    )
    submissions = []

    def dispatch(**kwargs):
        submissions.append(kwargs["spec"]["id"])
        if len(submissions) == 3:
            if fault == "wheel_changed":
                (kwargs["run_dir"] / wheel.name).write_bytes(b"dispatch-time mutation")
            elif fault == "wheel_missing":
                (kwargs["run_dir"] / wheel.name).unlink()
        if fault == "dispatch":
            raise RuntimeError("private injected transport canary")
        if fault == "signal":
            raise lifecycle.ControllerSignal(15)
        return {
            "id": submissions[-1],
            "status": "PASS",
            "attempt": 1,
            "process_cleanup_clear": True,
            "failure_codes": [],
        }

    monkeypatch.setattr(native, "_run_case", dispatch)
    run_dir = tmp_path / "new-run"
    explicit = {}
    observed_contexts = []
    if context_variant:
        external = tmp_path / "context-owner"
        external.mkdir(mode=0o700)
        context_path = external / "context.json"
        selected = [WORD_VARIANTS[1][0], WORD_VARIANTS[0][0]]
        explicit = {
            "run_id": "a" * 32,
            "base_sha": "d" * 40,
            "context_path": context_path,
            "case_ids": selected,
        }
        expected_context = {
            "schema": "friday.r10-native-context.v1",
            "base_sha": "d" * 40,
            "run_id": "a" * 32,
            "case_ids": selected,
            "secondary_enabled": context_variant == "context-secondary",
            "secondary_mode": "assist" if context_variant == "context-secondary" else "disabled",
            "identity": {
                "candidate_sha": "1" * 40,
                "candidate_tree": "1" * 40,
                "candidate_source_sha256": "2" * 64,
                "wheel_sha256": "0ee6f5582505e2d31919ccfda43ded862f4b7bcef102d2498c9bbea9cc26b6bc",
                "installed_site_sha256": "4" * 64,
                "suite_sha256": "3" * 64,
                "model_environment_sha256": "ff423d3f1a8ec32f24c42a97d740f23b96703e5e8b85e6c4c4c768d51489fe4a"
                if context_variant == "context-secondary"
                else "df2db74cdc7c69222c6d45521c26faa4c3f9de7e96cd964503631f154cd3d74a",
            },
        }

        def readiness(environment):
            assert environment == model_env and submissions == []
            digest = Path(str(context_path) + ".sha256").read_text()
            assert len(digest) == 65 and digest.endswith("\n")
            observed = registry._read_native_context(context_path, digest.rstrip("\n"))
            assert observed == expected_context
            assert context_path.stat().st_mode & 0o777 == 0o600
            assert Path(str(context_path) + ".sha256").stat().st_mode & 0o777 == 0o600
            observed_contexts.append(observed)
            if context_variant == "context-drift":
                context_path.write_bytes(context_path.read_bytes() + b" ")
            return SimpleNamespace(dispatch_clear=fault != "readiness")

        monkeypatch.setattr(acceptance, "_model_readiness_barrier", readiness)
        original_dispatch = dispatch

        def context_dispatch(**kwargs):
            assert observed_contexts == [expected_context]
            assert kwargs["run_id"] == "a" * 32
            assert kwargs["spec"]["id"] == selected[len(submissions)]
            return original_dispatch(**kwargs)

        monkeypatch.setattr(native, "_run_case", context_dispatch)
    publications_under_lease = []
    secure_write = battery._secure_write_json

    def publish_under_lease(path, value):
        if path.parent == run_dir and path.name in {"summary.json", "root-failure.json"}:
            with (
                pytest.raises(
                    acceptance.battery.BatteryContractError, match="^acceptance_run_already_active$"
                ),
                acceptance._ExclusiveAcceptanceRun(acceptance._acceptance_lock_path()),
            ):
                pytest.fail("another run entered during final publication")
            publications_under_lease.append(path.name)
        secure_write(path, value)

    monkeypatch.setattr(battery, "_secure_write_json", publish_under_lease)
    options = dict(env_file=tmp_path / "unused.env", candidate_sha=candidate, run_dir=run_dir, **explicit)
    if not fault and not context_variant:
        # An owning parent uses the same validated body without reacquiring its
        # lease. The other variants exercise the unchanged public entry point.
        inputs = native._prepare_native_run(**options)
        with acceptance._ExclusiveAcceptanceRun(acceptance._acceptance_lock_path()):
            report = native._run_native_locked(inputs)
    else:
        report = native.run_native(**options)
    assert "summary.json" in publications_under_lease
    with acceptance._ExclusiveAcceptanceRun(acceptance._acceptance_lock_path()):
        pass  # The public/native body has finished all publication and teardown.
    if context_variant:
        assert report["run_id"] == "a" * 32
        assert report["planned"] == len(report["results"]) == 2
        assert report["required_denominator"] == len(report["required_results"]) == 4
        assert [row["id"] for row in report["results"]] == selected
        assert all(row["status"] == "NOT_RUN" for row in report["required_results"] if not row["selected"])
        assert report["status"] == "FAIL" and report["go_emitted"] is False
        if fault == "build":
            assert not context_path.exists() and not Path(str(context_path) + ".sha256").exists()
            assert observed_contexts == [] and submissions == []
        elif fault == "readiness" or context_variant == "context-drift":
            assert context_path.exists() and len(observed_contexts) == 1 and submissions == []
            assert report["root_failure"]["code"] == (
                "native_model_readiness_failed" if fault == "readiness" else "native_context_changed"
            )
            assert all(row["status"] == "NOT_RUN" and row["attempt"] == 0 for row in report["results"])
        else:
            assert submissions == selected and observed_contexts == [expected_context]
            assert report["root_failure"]["code"] == "native_evidence_pass_incomplete"
        return
    assert report["planned"] == len(report["results"]) == 3
    assert report["required_denominator"] == 4
    assert len(report["required_results"]) == 4
    assert {row["id"] for row in report["required_results"]} == set(fixture_ids)
    assert all(row["status"] == "NOT_RUN" for row in report["required_results"] if not row["selected"])
    # The synthetic dispatch stub supplies no retained case evidence. Even its
    # successful outcome cannot make the controller's final report pass.
    assert report["status"] == "FAIL"
    if not fault:
        assert report["root_failure"]["code"] == "native_evidence_pass_incomplete"
    assert report["go_emitted"] is False
    assert "private injected" not in json.dumps(report)
    assert json.loads((run_dir / "summary.json").read_bytes()) == report
    if fault in {"wheel_changed", "wheel_missing"}:
        assert report["root_failure"]["code"] == "native_retained_wheel_identity_mismatch"
        assert all(row["status"] == "PASS" for row in report["results"])
    elif fault:
        assert report["root_failure"]["id"] == "native-run-root"
        assert all(
            row["status"] == "NOT_RUN" and row["root_ref"] == "native-run-root" for row in report["results"]
        )
        assert sum(row["attempt"] for row in report["results"]) == (
            1 if fault in {"dispatch", "signal"} else 0
        )
    if fault not in {"build", "wheel_changed", "wheel_missing"}:
        assert (run_dir / wheel.name).read_bytes() == wheel.read_bytes()
        assert (run_dir / wheel.name).stat().st_mode & 0o777 == 0o600
    assert len(submissions) == (
        3 if fault in {"", "wheel_changed", "wheel_missing"} else 1 if fault in {"dispatch", "signal"} else 0
    )


@pytest.mark.parametrize("fault,exit_code", [("", 0), ("missing", 5), ("exception", 5), ("signal", 143)])
def test_cli_live_dispatch_consumes_explicit_inputs_and_redacts_setup_failures(
    tmp_path, monkeypatch, capsys, fault, exit_code
):
    from tools import release_1_0_live_journeys as journeys

    received = []

    def execute(**kwargs):
        received.append(kwargs)
        if fault == "exception":
            raise native.NativeError("private injected failure canary")
        if fault == "signal":
            raise lifecycle.ControllerSignal(15)
        return {"status": "PASS", "go_emitted": False}

    monkeypatch.setattr(native, "run_native", execute)
    arguments = ["--run-live"]
    if fault != "missing":
        arguments += [
            "--env-file",
            str(tmp_path / "models.env"),
            "--candidate-sha",
            "1" * 40,
            "--evidence-dir",
            str(tmp_path / "evidence"),
        ]
    assert journeys.main(arguments) == exit_code
    report = json.loads(capsys.readouterr().out)
    assert report["go_emitted"] is False
    assert "private injected" not in json.dumps(report)
    assert len(received) == (0 if fault == "missing" else 1)
    if received:
        assert received[0] == {
            "env_file": tmp_path / "models.env",
            "candidate_sha": "1" * 40,
            "run_dir": tmp_path / "evidence",
        }


def test_worker_startup_does_not_execute_dependency_pth(tmp_path):
    """Run the real native interpreter flags against a private poisoned venv."""
    import subprocess
    import venv

    env_path = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(env_path)
    package_path = env_path / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    marker = tmp_path / "hook-ran"
    (package_path / "startup.pth").write_text(
        f"import pathlib;pathlib.Path({str(marker)!r}).write_text('untrusted hook ran')\n"
    )
    command = native._sandbox_command(
        snapshot=tmp_path,
        site=tmp_path,
        case_root=tmp_path,
        relays=tmp_path,
        request_path=tmp_path / "worker-request.json",
        request_sha="0" * 64,
    )
    argv = list(command[command.index("--") + 1 :])
    argv[0] = str(env_path / "bin/python")
    argv[-1] = "import json,sys;print(json.dumps({'site_loaded':'site' in sys.modules}))"
    outcome = subprocess.run(argv, env={}, capture_output=True, timeout=10, check=True)
    assert not marker.exists(), "Python executed a .pth hook before the harness bootstrap"
    assert json.loads(outcome.stdout) == {"site_loaded": False}


def _http_unit_thread_census():
    """Model this HTTP unit's Python thread ownership, independent of pytest TID churn.

    The dedicated actual sandbox tests retain the real kernel census. A joined
    Python helper can remain briefly in /proc while an ambient pytest thread
    can disappear; neither is this unit's HTTP/lifespan observation.
    """

    def alive_ids():
        return frozenset(
            thread.native_id
            for thread in threading.enumerate()
            if thread.native_id is not None and thread.is_alive()
        )

    baseline = alive_ids()
    return lambda: baseline | alive_ids()


@pytest.mark.parametrize("scenario", ["joined", "ambient_disappeared", "alive_extra"])
def test_http_unit_census_preserves_alive_extra_thread_rejection(monkeypatch, scenario):
    def fake_thread(native_id, alive=True):
        return SimpleNamespace(native_id=native_id, is_alive=lambda: alive)

    initial = [fake_thread(101)]
    if scenario == "ambient_disappeared":
        initial.append(fake_thread(303))
    current = list(initial)
    monkeypatch.setattr(threading, "enumerate", lambda: current)
    census = _http_unit_thread_census()
    baseline = census()
    if scenario == "joined":
        current.append(fake_thread(202, False))
        kernel_ids = ["101", "202"]
    elif scenario == "ambient_disappeared":
        current[:] = [fake_thread(101)]
        kernel_ids = ["101"]
    else:
        current.append(fake_thread(202))
        kernel_ids = ["101", "202"]
    real_listdir = os.listdir
    monkeypatch.setattr(
        os, "listdir", lambda path: kernel_ids if path == "/proc/self/task" else real_listdir(path)
    )
    assert native._worker_task_ids() != baseline
    monkeypatch.setattr(native, "_worker_task_ids", census)
    if scenario == "alive_extra":
        with pytest.raises(native.NativeError, match="^native_unowned_worker_thread$"):
            native._require_worker_task_ids(baseline)
    else:
        native._require_worker_task_ids(baseline)


@pytest.mark.parametrize(
    ("expected_task_ids", "observed_task_ids"),
    [
        (frozenset({101}), frozenset({101, 202})),
        (frozenset({101, 202}), frozenset({101})),
    ],
    ids=["added", "disappeared"],
)
def test_worker_task_census_rejects_added_and_disappeared_ids(
    monkeypatch, expected_task_ids, observed_task_ids
):
    """Both census directions remain strict without timing or real helper threads."""
    monkeypatch.setattr(native, "_worker_task_ids", lambda: observed_task_ids)
    with pytest.raises(native.NativeError, match="^native_unowned_worker_thread$"):
        native._require_worker_task_ids(expected_task_ids)

    stopped = threading.Event()
    stopped.set()
    relay = SimpleNamespace(
        _lock=threading.Lock(),
        _threads=[],
        _connections=set(),
        _stop=stopped,
        _listeners=[],
    )
    containment = {
        "relay_owner": contextlib.nullcontext(relay),
        "relay": relay,
        "root_task_ids": expected_task_ids,
    }
    assert native._leave_worker_containment(containment, restore_guards=False) is False


@pytest.mark.parametrize(
    "scenario",
    [
        "clear",
        "joined_settles",
        "joined_stuck",
        "unknown_extra",
        "missing_root",
        "live_relay",
        "unclosed_listener",
        "unclosed_connection",
        "not_stopped",
    ],
)
def test_worker_relay_cleanup_settles_only_joined_owned_ids_within_shared_budget(monkeypatch, scenario):
    clock = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(native, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    stopped = threading.Event()
    listener = SimpleNamespace(fileno=lambda: 7 if scenario == "unclosed_listener" else -1)
    connection = SimpleNamespace(fileno=lambda: 8 if scenario == "unclosed_connection" else -1)
    thread = SimpleNamespace(native_id=202, is_alive=lambda: scenario == "live_relay")
    relay = SimpleNamespace(
        _lock=threading.Lock(),
        _threads=[thread],
        _connections=[connection],
        _stop=stopped,
        _listeners=[listener],
    )

    @contextlib.contextmanager
    def owner():
        yield relay
        clock[0] = 1.99  # Joining consumed almost all the existing two seconds.
        if scenario != "not_stopped":
            stopped.set()
        relay._listeners.clear()

    calls = []

    def census():
        calls.append(True)
        if scenario == "unknown_extra":
            return frozenset({101, 999})
        if scenario == "missing_root":
            return frozenset()
        if scenario == "joined_stuck" or scenario == "joined_settles" and len(calls) == 1:
            return frozenset({101, 202})
        return frozenset({101})

    monkeypatch.setattr(native, "_worker_task_ids", census)
    relay_owner = owner()
    relay_owner.__enter__()
    result = native._leave_worker_containment(
        {"relay_owner": relay_owner, "relay": relay, "root_task_ids": frozenset({101})},
        restore_guards=False,
    )
    assert result is (scenario in {"clear", "joined_settles"})
    assert clock[0] <= 2.0
    if scenario in {"joined_settles", "joined_stuck"}:
        assert sleeps and all(0 < delay <= 0.005 for delay in sleeps)
        assert len(calls) >= 2
    else:
        assert sleeps == []


@pytest.mark.parametrize(
    "traffic_phase",
    [
        "none",
        "startup",
        "shutdown",
        "environment_change",
        "live_settings_change",
        "settings_change",
        "profile_change",
        "secondary",
        "secondary_live_change",
        "secondary_fallback",
        "secondary_scheduler_mode_mismatch",
    ],
)
def test_worker_http_boundary_includes_lifespan_traffic(tmp_path, monkeypatch, settings, traffic_phase):
    """Actual TestClient lifespan and HTTP probe, with no model/network transport."""
    import contextlib
    from dataclasses import replace

    import httpx
    from fastapi import FastAPI

    import friday.config
    import friday.server

    _, gate, _, battery = native._dependencies()
    secondary = traffic_phase.startswith("secondary")
    settings = replace(
        settings,
        llm_base_url="http://127.0.0.1:9",
        embeddings_base_url="http://127.0.0.1:9",
        rerank_base_url="http://127.0.0.1:9",
        secondary_llm_enabled=secondary,
        secondary_llm_mode="assist" if secondary else "disabled",
        secondary_llm_base_url="http://127.0.0.1:19004/v1",
    )
    if secondary:
        monkeypatch.setattr(type(settings), "secondary_llm_configured", property(lambda self: True))
    monkeypatch.setenv("FRIDAY_SECONDARY_LLM_ENABLED", "1" if secondary else "0")
    monkeypatch.setenv("FRIDAY_SECONDARY_LLM_MODE", settings.secondary_llm_mode)
    monkeypatch.setenv("FRIDAY_SECONDARY_LLM_BASE_URL", settings.secondary_llm_base_url)
    home = tmp_path / "home"
    home.mkdir()
    evidence = tmp_path / "evidence/observed.json"
    evidence.parent.mkdir()
    monkeypatch.setenv("FRIDAY_HOME", str(home))
    monkeypatch.setenv("FRIDAY_LIVE_BATTERY_EVIDENCE", str(evidence))
    monkeypatch.setenv("FRIDAY_LIVE_BATTERY_MAIN_CHAT", "42")
    monkeypatch.setenv("FRIDAY_LLM_BASE_URL", str(settings.llm_base_url))
    monkeypatch.setenv("FRIDAY_EMBEDDINGS_BASE_URL", str(settings.embeddings_base_url))
    monkeypatch.setenv("FRIDAY_RERANK_BASE_URL", str(settings.rerank_base_url))
    request = _request()
    monkeypatch.setattr(native, "ROOT", type(tmp_path)("/workspace"))
    monkeypatch.setattr(native, "_suite_digest", lambda root: request["suite_sha256"])
    monkeypatch.setattr(battery, "_candidate_source_digest", lambda **kw: request["candidate_source_sha256"])
    monkeypatch.setattr(gate, "_projection_digest", lambda site: request["installed_site_sha256"])
    for name in (
        "_assert_worker_product_authority",
        "_assert_worker_paths",
        "_assert_live_model_runtime",
        "_install_no_exec_seccomp",
    ):
        monkeypatch.setattr(battery, name, lambda *a, **kw: None)
    monkeypatch.setattr(gate, "_require_installed_wheel_imports", lambda site: None)
    settings_after = (
        replace(settings, profile=replace(settings.profile, max_steps=settings.profile.max_steps + 1))
        if traffic_phase == "profile_change"
        else replace(settings, llm_model="changed-after-probe")
    )
    settings_loads = iter(
        [settings, settings_after]
        if traffic_phase in {"settings_change", "profile_change"}
        else [settings, replace(settings)]
    )
    monkeypatch.setattr(friday.config, "load_settings", lambda: next(settings_loads))
    monkeypatch.setattr(friday.config, "ensure_runtime_dirs", lambda settings: None)
    stopped = threading.Event()
    stopped.set()
    fake_relay = SimpleNamespace(
        routes={},
        _lock=threading.Lock(),
        _threads=[],
        _connections=set(),
        _stop=stopped,
        _listeners=[],
    )
    bridge_modes = []

    def bridge(settings, *, include_secondary=False):
        bridge_modes.append(include_secondary)
        if secondary:
            assert settings.secondary_llm_base_url == "http://127.0.0.1:19004/v1"
        return contextlib.nullcontext(fake_relay)

    monkeypatch.setattr(battery._UnixRelayLoopbackBridge, "from_settings", bridge)
    monkeypatch.setattr(
        battery.LocalEndpointNetworkGuard,
        "from_settings",
        lambda *a, **kw: contextlib.nullcontext(
            SimpleNamespace(
                denied_attempts=0,
                _require_address=lambda *args: None,
                _deny=lambda: (_ for _ in ()).throw(PermissionError("denied")),
            )
        ),
    )

    if secondary:

        def guard(endpoint_urls, *, relay_routes):
            assert tuple(endpoint_urls) == (
                settings.llm_base_url,
                settings.embeddings_base_url,
                settings.rerank_base_url,
                settings.secondary_llm_base_url,
            )
            assert relay_routes == fake_relay.routes
            return contextlib.nullcontext(
                SimpleNamespace(
                    denied_attempts=0,
                    _require_address=lambda *args: None,
                    _deny=lambda: (_ for _ in ()).throw(PermissionError("denied")),
                )
            )

        monkeypatch.setattr(battery, "LocalEndpointNetworkGuard", guard)

    async def observed_http(path):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        ) as client:
            await client.post(settings.llm_base_url + path)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if traffic_phase == "startup":
            await observed_http("/unexpected-lifespan")
        yield
        if traffic_phase == "shutdown":
            await observed_http("/unexpected-lifespan")

    app = FastAPI(lifespan=lifespan)
    app.state.storage = object()
    if secondary:
        from friday.secondary_brain import SecondaryBrainScheduler

        app.state.secondary_brain = object.__new__(SecondaryBrainScheduler)
        monkeypatch.setattr(
            SecondaryBrainScheduler,
            "public_status",
            lambda self: {
                "mode": "shadow" if traffic_phase == "secondary_scheduler_mode_mismatch" else "assist",
                "state": "misconfigured" if traffic_phase == "secondary_fallback" else "probing",
                "configured": traffic_phase != "secondary_fallback",
                "available": False,
            },
        )
    monkeypatch.setattr(friday.server, "create_app", lambda settings: app)

    def handler(*args):
        import asyncio

        asyncio.run(observed_http("/chat/completions"))
        if secondary:

            async def observed_secondary():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
                ) as client:
                    await client.get(settings.secondary_llm_base_url + "/friday-profile")
                    await client.get(settings.secondary_llm_base_url + "/models")
                    await client.post(settings.secondary_llm_base_url + "/chat/completions")

            asyncio.run(observed_secondary())
        if traffic_phase == "secondary_live_change":
            object.__setattr__(
                settings, "secondary_llm_read_timeout_sec", settings.secondary_llm_read_timeout_sec + 1
            )
        if traffic_phase == "environment_change":
            monkeypatch.setenv("FRIDAY_LLM_MODEL", "changed-after-probe")
        if traffic_phase == "live_settings_change":
            object.__setattr__(settings, "llm_model", "model-actually-used")
        return {"failure_codes": []}

    monkeypatch.setattr(native, "live_handlers", lambda: {request["case_id"]: handler})
    request["expected_effective_runtime_sha256"] = native._effective_runtime_hash(
        settings, candidate_source_sha256=request["candidate_source_sha256"], battery=battery
    )
    request["expected_worker_environment_sha256"] = native._worker_environment_digest(dict(os.environ))
    request["expected_runtime_binding_sha256"] = native._runtime_binding(
        request,
        effective_runtime_sha256=request["expected_effective_runtime_sha256"],
        worker_environment_sha256=request["expected_worker_environment_sha256"],
        probe_receipt_sha256=request["runtime_probe_receipt_sha256"],
    )
    monkeypatch.setattr(native, "_worker_task_ids", _http_unit_thread_census())
    if traffic_phase in {"secondary_fallback", "secondary_scheduler_mode_mismatch"}:
        with pytest.raises(native.NativeError, match="native_secondary_scheduler_unavailable"):
            native._worker(request)
        assert bridge_modes == [True]
        return
    result = native._worker(request)
    assert bridge_modes == [secondary]
    expected_failure_codes = {
        "none": [],
        "startup": ["native_network_boundary_violation"],
        "shutdown": ["native_network_boundary_violation"],
        "environment_change": ["native_runtime_identity_changed"],
        "live_settings_change": ["native_runtime_identity_changed"],
        "settings_change": ["native_runtime_identity_changed"],
        "profile_change": ["native_runtime_identity_changed"],
        "secondary": [],
        "secondary_live_change": ["native_runtime_identity_changed"],
    }
    assert result["failure_codes"] == expected_failure_codes[traffic_phase], result
    assert result["status"] == ("FAIL" if result["failure_codes"] else "PASS"), result
    if secondary:
        assert result["model_http_counts"]["secondary"] == 1
        assert result["model_http_counts"]["secondary_health"] == 2
        assert result["secondary_runtime"]["after_case"]["state"] == "probing"
        assert result["secondary_runtime"]["after_case"]["available"] is False
    assert result["model_http_counts"]["model"] == 1
    assert result["model_http_counts"]["other"] == (1 if traffic_phase in {"startup", "shutdown"} else 0)
    assert ("native_network_boundary_violation" in result["failure_codes"]) is (
        traffic_phase in {"startup", "shutdown"}
    )
    assert ("native_runtime_identity_changed" in result["failure_codes"]) is (
        traffic_phase
        in {
            "environment_change",
            "live_settings_change",
            "settings_change",
            "profile_change",
            "secondary_live_change",
        }
    )


def test_secondary_runtime_identity_binds_effective_settings_and_ca_bytes(tmp_path, settings, monkeypatch):
    from dataclasses import fields, replace

    _, _, _, battery = native._dependencies()
    monkeypatch.setattr(type(settings), "secondary_llm_configured", property(lambda self: True))
    enabled = replace(settings, secondary_llm_enabled=True, secondary_llm_ca_file="")

    def digest(value):
        return native._effective_runtime_hash(value, candidate_source_sha256="1" * 64, battery=battery)

    baseline = digest(enabled)
    observed_names = []
    for field in fields(enabled):
        name = field.name
        if (
            not (
                name.startswith("secondary_llm_")
                or name
                in {
                    "semantic_supervisor_mode",
                    "semantic_supervisor_tasks",
                    "semantic_supervisor_max_steps",
                    "semantic_supervisor_max_review_rounds",
                    "semantic_supervisor_timeout_sec",
                    "semantic_supervisor_effect_mode",
                }
            )
            or name == "secondary_llm_ca_file"
        ):
            continue
        old = getattr(enabled, name)
        changed = (
            not old
            if type(old) is bool
            else old + 1
            if isinstance(old, (int, float))
            else old + ("synthetic-change",)
            if isinstance(old, tuple)
            else str(old) + "-changed"
        )
        assert digest(replace(enabled, **{name: changed})) != baseline, name
        observed_names.append(name)
    assert len(observed_names) == 23
    ca = tmp_path / "ca.pem"
    ca.write_bytes(b"-----BEGIN CERTIFICATE-----\nU1lOVEhFVElDLUNB\n-----END CERTIFICATE-----\n")
    ca.chmod(0o644)
    with_ca = replace(enabled, secondary_llm_ca_file=str(ca))
    before = digest(with_ca)
    ca.write_bytes(ca.read_bytes().replace(b"U1lOVEhFVElDLUNB", b"QUxURVJOQVRFQ0E="))
    assert digest(with_ca) != before
    monkeypatch.setattr(type(settings), "secondary_llm_configured", property(lambda self: False))
    assert digest(enabled) != baseline
    monkeypatch.setenv("FRIDAY_SECONDARY_LLM_ENABLED", "1")
    monkeypatch.setenv("FRIDAY_RERANK_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FRIDAY_SECONDARY_LLM_BASE_URL", "http://127.0.0.1:19004/v1")
    with pytest.raises(native.NativeError, match="native_secondary_runtime_incomplete"):
        native._require_secondary_runtime(enabled, battery=battery)
    with pytest.raises(native.NativeError, match="native_secondary_mode_mismatch"):
        native._require_secondary_runtime(settings, battery=battery)


@pytest.mark.parametrize("expected_mode", ["shadow", "assist"])
@pytest.mark.parametrize(
    "scenario,state,configured,available",
    [
        ("missing", "probing", True, False),
        ("disabled", "disabled", True, False),
        ("disabled_mode", "probing", True, False),
        ("misconfigured", "misconfigured", True, False),
        ("wrong_mode", "healthy", True, True),
        ("unconfigured", "healthy", False, False),
        ("probing", "probing", True, False),
        ("healthy", "healthy", True, True),
        ("healthy_unavailable", "healthy", True, False),
        ("degraded", "degraded", True, False),
        ("cooldown", "cooldown", True, False),
    ],
    ids=[
        "missing",
        "disabled",
        "disabled_mode",
        "misconfigured",
        "wrong_mode",
        "unconfigured",
        "probing",
        "healthy",
        "healthy_unavailable",
        "degraded",
        "cooldown",
    ],
)
def test_secondary_observation_preserves_state_and_requires_configured_expected_mode(
    monkeypatch, expected_mode, scenario, state, configured, available
):
    """Exercise the observation boundary; synthetic status is no profile admission proof."""
    from friday.secondary_brain import SecondaryBrainScheduler

    app = SimpleNamespace(state=SimpleNamespace())
    if scenario == "missing":
        for attributes in ({}, {"secondary_brain": None}, {"secondary_brain": object()}):
            app.state = SimpleNamespace(**attributes)
            with pytest.raises(native.NativeError, match="^native_secondary_scheduler_missing$"):
                native._secondary_observation(app, expected_mode=expected_mode)
        return

    mode = expected_mode
    if scenario == "disabled_mode":
        mode = "disabled"
    elif scenario == "wrong_mode":
        mode = "assist" if expected_mode == "shadow" else "shadow"
    public = {
        "mode": mode,
        "state": state,
        "configured": configured,
        "available": available,
        "schema": "friday.optional-secondary-health.v1",
    }
    scheduler = object.__new__(SecondaryBrainScheduler)
    app.state.secondary_brain = scheduler
    calls = []

    def public_status(self):
        assert self is scheduler
        calls.append(self)
        return dict(public)

    monkeypatch.setattr(SecondaryBrainScheduler, "public_status", public_status)
    if scenario in {"disabled", "disabled_mode", "misconfigured", "wrong_mode", "unconfigured"}:
        with pytest.raises(native.NativeError, match="^native_secondary_scheduler_unavailable$"):
            native._secondary_observation(app, expected_mode=expected_mode)
    else:
        observed = native._secondary_observation(app, expected_mode=expected_mode)
        assert observed == {
            "mode": expected_mode,
            "state": state,
            "configured": True,
            "available": available,
        }
        assert observed is not public
    assert calls == [scheduler]


def test_sandbox_ca_copy_is_readonly_and_origin_directory_is_absent(tmp_path):
    import subprocess

    secret_dir = tmp_path / "origin"
    secret_dir.mkdir(mode=0o700)
    ca = secret_dir / "ca.pem"
    ca.write_bytes(b"-----BEGIN CERTIFICATE-----\nU1lOVEhFVElDLUNB\n-----END CERTIFICATE-----\n")
    ca.chmod(0o644)
    (secret_dir / "unrelated").write_text("must remain outside")
    case = tmp_path / "case"
    (case / "home").mkdir(parents=True, mode=0o700)
    (case / "evidence").mkdir(mode=0o700)
    relays = tmp_path / "relays"
    relays.mkdir(mode=0o700)
    descriptor = native._assets().stage_ca(native._assets().capture_ca(ca), case / "evidence")
    command = list(
        native._sandbox_command(
            snapshot=native.ROOT,
            site=native.ROOT,
            case_root=case,
            relays=relays,
            request_path=case / "request.json",
            request_sha="0" * 64,
            secondary_ca=descriptor,
        )
    )
    staged = descriptor["staged_path"]
    program = f"""from pathlib import Path
import hashlib,json,os
p=Path({staged!r})
assert hashlib.sha256(p.read_bytes()).hexdigest()=={descriptor["sha256"]!r}
assert not Path({str(secret_dir)!r}).exists()
for op in (lambda:p.write_bytes(b'bad'),lambda:p.unlink(),lambda:os.replace(p,p.with_name('moved'))):
 try: op()
 except OSError: pass
 else: raise AssertionError('read-only CA changed')
print(json.dumps({{'readonly':True}}))
"""
    command[-1] = program
    result = subprocess.run(command, env={"LANG": "C.UTF-8"}, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout) == {"readonly": True}
    assert native._assets().verify_staged(descriptor, case / "evidence").read_bytes() == ca.read_bytes()


def _retained_native_evidence_fixture(tmp_path):
    """Synthetic byte transport only; these JSON stubs are not live receipts."""
    run = tmp_path / "native-run"
    evidence = run / "case-001" / "evidence"
    evidence.mkdir(parents=True, mode=0o700)

    def write(path, value):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode() + b"\n"
        path.write_bytes(raw)
        path.chmod(0o600)
        return raw

    first, second = (row[0] for row in WORD_VARIANTS[:2])
    fixture, artifact = b"fixture transport bytes", b"artifact transport bytes"
    observed = {
        "fixture_sha256": native._sha(fixture),
        "artifact_sha256": native._sha(artifact),
        "artifact_size_bytes": len(artifact),
    }
    result = {
        "id": first,
        "status": "PASS",
        "attempt": 1,
        "process_cleanup_clear": True,
        "duration_ms": 30,
        "observed_safe": observed,
    }
    write(run / "frozen-identity.json", {"candidate_sha": "a" * 40})
    write(run / "candidate.whl", b"synthetic wheel transport bytes")
    write(evidence / "case-receipt.json", result)
    write(evidence / "runtime-probe-request.json", {"synthetic": "probe request"})
    write(run / "case-001-runtime-probe-response.json", {"synthetic": "probe stdout"})
    write(evidence / "worker-request.json", {"synthetic": "worker request"})
    write(run / "case-001-worker-response.json", {"status": "PASS", "duration_ms": 3})
    write(evidence / f"artifact-{native._sha(fixture)}.bin", fixture)
    write(evidence / f"artifact-{native._sha(artifact)}.bin", artifact)
    return run, [first, second], [result, {"id": second, "status": "NOT_RUN", "attempt": 0}]


def test_retained_native_evidence_binds_selection_and_distinct_controller_worker_bytes(tmp_path):
    run, selected, results = _retained_native_evidence_fixture(tmp_path)
    retained = native._retained_evidence_index(run, selected, results, retained_wheel=run / "candidate.whl")
    assert retained["schema"] == "friday.r10-native-evidence.v1"
    assert retained["selected_cases"] == selected
    assert [row["id"] for row in retained["cases"]] == selected
    assert [row["index"] for row in retained["cases"]] == [1, 2]
    first, second = retained["cases"]
    assert set(first) == {
        "id",
        "index",
        "receipt",
        "probe_request",
        "probe_response",
        "worker_request",
        "worker_response",
        "artifacts",
    }
    refs = [
        retained["identity"],
        retained["wheel"],
        *(
            first[key]
            for key in ("receipt", "probe_request", "probe_response", "worker_request", "worker_response")
        ),
        *first["artifacts"],
    ]
    for ref in refs:
        raw = (run / ref["path"]).read_bytes()
        assert ref["sha256"] == native._sha(raw)
        assert ref["size_bytes"] == len(raw)
    assert json.loads((run / first["receipt"]["path"]).read_bytes())["duration_ms"] == 30
    assert json.loads((run / first["worker_response"]["path"]).read_bytes())["duration_ms"] == 3
    assert first["receipt"]["sha256"] != first["worker_response"]["sha256"]
    assert second == {
        "id": selected[1],
        "index": 2,
        "receipt": None,
        "probe_request": None,
        "probe_response": None,
        "worker_request": None,
        "worker_response": None,
        "artifacts": [],
    }
    assert "status" not in retained and "case_layers" not in retained
    assert results[1]["status"] == "NOT_RUN"


@pytest.mark.parametrize(
    "fault",
    [
        "missing-wheel",
        "missing-receipt",
        "missing-request",
        "missing-worker",
        "artifact-mutated",
        "public-request",
        "symlink-request",
        "linked-request",
        "empty-request",
        "duplicate-selection",
    ],
)
def test_retained_native_evidence_refuses_missing_or_unsafe_pass_proof(tmp_path, fault):
    run, selected, results = _retained_native_evidence_fixture(tmp_path)
    evidence = run / "case-001" / "evidence"
    request = evidence / "worker-request.json"
    if fault == "missing-wheel":
        (run / "candidate.whl").unlink()
    elif fault == "missing-receipt":
        (evidence / "case-receipt.json").unlink()
    elif fault == "missing-request":
        request.unlink()
    elif fault == "missing-worker":
        (run / "case-001-worker-response.json").unlink()
    elif fault == "artifact-mutated":
        (evidence / f"artifact-{results[0]['observed_safe']['artifact_sha256']}.bin").write_bytes(b"corrupt")
    elif fault == "public-request":
        request.chmod(0o644)
    elif fault == "symlink-request":
        moved = evidence / "moved.json"
        request.rename(moved)
        request.symlink_to(moved)
    elif fault == "linked-request":
        os.link(request, evidence / "linked.json")
    elif fault == "empty-request":
        request.write_bytes(b"")
    elif fault == "duplicate-selection":
        selected[1] = selected[0]
    with pytest.raises((native.NativeError, RuntimeError)):
        native._retained_evidence_index(run, selected, results, retained_wheel=run / "candidate.whl")


def test_retained_native_evidence_does_not_traverse_uncertain_worker_directory(tmp_path, monkeypatch):
    run, selected, results = _retained_native_evidence_fixture(tmp_path)
    _, gate, _, _ = native._dependencies()
    results[0].update(status="FAIL", process_cleanup_clear=False)
    receipt = run / "case-001-receipt.json"
    receipt.write_bytes(json.dumps(results[0]).encode() + b"\n")
    receipt.chmod(0o600)
    checked = []
    original = gate._bounded_file_identity

    def observe(path, **kwargs):
        assert run / "case-001" not in path.parents
        checked.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(gate, "_bounded_file_identity", observe)
    retained = native._retained_evidence_index(run, selected, results, retained_wheel=run / "candidate.whl")
    assert checked == [run / "frozen-identity.json", run / "candidate.whl", receipt]
    assert retained["cases"][0]["receipt"]["path"] == "case-001-receipt.json"
    assert retained["cases"][0]["worker_response"] is None
    assert retained["cases"][0]["artifacts"] == []
    assert results[0]["status"] == "FAIL"


@pytest.mark.parametrize("status", ["FAIL", "NOT_RUN"])
def test_retained_native_evidence_preserves_empty_failed_worker_response(tmp_path, status):
    run, selected, results = _retained_native_evidence_fixture(tmp_path)
    results[0]["status"] = status
    worker = run / "case-001-worker-response.json"
    worker.write_bytes(b"")
    retained = native._retained_evidence_index(run, selected, results, retained_wheel=run / "candidate.whl")
    assert retained["cases"][0]["worker_response"] == {
        "path": "case-001-worker-response.json",
        "sha256": native._sha(b""),
        "size_bytes": 0,
    }
    assert results[0]["status"] == status
    assert results[1]["status"] == "NOT_RUN"


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "context-exists",
        "digest-exists",
        "symlink",
        "fifo",
        "parent-mode",
        "short-write",
        "sidecar-race",
        "drift",
    ],
)
def test_native_context_publication_preserves_foreign_entries_and_binds_bytes(tmp_path, monkeypatch, fault):
    from tools import release_1_0_acceptance as acceptance

    parent = tmp_path / "owner"
    parent.mkdir(mode=0o700)
    path = parent / "context.json"
    sidecar = parent / "context.json.sha256"
    context = {
        "schema": "friday.r10-native-context.v1",
        "base_sha": "d" * 40,
        "identity": {
            "candidate_sha": "1" * 40,
            "candidate_tree": "2" * 40,
            "candidate_source_sha256": "3" * 64,
            "wheel_sha256": "4" * 64,
            "installed_site_sha256": "5" * 64,
            "suite_sha256": "6" * 64,
            "model_environment_sha256": "7" * 64,
        },
        "run_id": "a" * 32,
        "case_ids": [WORD_VARIANTS[0][0]],
        "secondary_enabled": False,
        "secondary_mode": "disabled",
    }
    sentinel = parent / "foreign"
    sentinel.write_bytes(b"FOREIGN-SENTINEL")
    sentinel.chmod(0o640)
    if fault == "context-exists":
        path.write_bytes(b"EXISTING-CONTEXT")
        path.chmod(0o640)
    elif fault == "digest-exists":
        sidecar.write_bytes(b"EXISTING-DIGEST")
        sidecar.chmod(0o640)
    elif fault == "symlink":
        path.symlink_to(sentinel)
    elif fault == "fifo":
        os.mkfifo(path, 0o600)
    elif fault == "parent-mode":
        parent.chmod(0o750)
    elif fault == "short-write":
        monkeypatch.setattr(native.os, "write", lambda fd, raw: 0)
    elif fault == "sidecar-race":
        original_open = os.open

        def raced_open(name, flags, *args, **kwargs):
            if name == sidecar.name and flags & os.O_CREAT:
                sidecar.write_bytes(b"RACED-FOREIGN-DIGEST")
                raise FileExistsError("synthetic other writer won")
            return original_open(name, flags, *args, **kwargs)

        monkeypatch.setattr(native.os, "open", raced_open)
    before = {p.name: (p.lstat().st_ino, p.lstat().st_mode) for p in parent.iterdir()}
    parent_mode = parent.stat().st_mode
    if fault in {"none", "drift"}:
        retained = native._publish_native_context(path, context)
        digest = sidecar.read_text()
        assert len(digest) == 65 and digest.endswith("\n")
        assert acceptance._read_native_context(path, digest[:-1]) == context
        assert path.read_bytes() == json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
        assert path.stat().st_mode & 0o777 == sidecar.stat().st_mode & 0o777 == 0o600
        native._check_native_context(path, retained)
        if fault == "drift":
            sidecar.write_bytes(b"0" * 64 + b"\n")
            with pytest.raises(native.NativeError, match="^native_context_changed$"):
                native._check_native_context(path, retained)
    else:
        code = "native_context_path_invalid" if fault == "parent-mode" else "native_context_publish_failed"
        with pytest.raises(native.NativeError, match=f"^{code}$"):
            native._publish_native_context(path, context)
        for name, identity in before.items():
            entry = parent / name
            assert (entry.lstat().st_ino, entry.lstat().st_mode) == identity
        if fault == "context-exists":
            assert path.read_bytes() == b"EXISTING-CONTEXT" and not sidecar.exists()
        elif fault == "digest-exists":
            assert sidecar.read_bytes() == b"EXISTING-DIGEST" and not path.exists()
        elif fault == "sidecar-race":
            assert sidecar.read_bytes() == b"RACED-FOREIGN-DIGEST" and not path.exists()
        elif fault in {"parent-mode", "short-write"}:
            assert not path.exists() and not sidecar.exists()
    assert sentinel.read_bytes() == b"FOREIGN-SENTINEL"
    assert parent.stat().st_mode == parent_mode


@pytest.mark.parametrize(
    "fault", ["partial", "run-id", "base-equal", "ancestor", "selection", "inside-run", "inside-source"]
)
def test_native_context_preflight_refuses_before_model_or_run_effects(tmp_path, monkeypatch, fault):
    _, gate, _, _ = native._dependencies()
    owner = tmp_path / "owner"
    owner.mkdir(mode=0o700)
    run_dir = tmp_path / "run"
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    monkeypatch.setattr(native, "ROOT", source)
    candidate = "1" * 40
    monkeypatch.setattr(
        gate,
        "_git_output",
        lambda root, *args: (
            ""
            if args[0] == "status"
            else ("e" * 40 if fault == "ancestor" else "d" * 40)
            if args[0] == "merge-base"
            else candidate
        ),
    )
    observed = []

    def forbidden(path):
        observed.append(path)
        raise AssertionError("model configuration must not be read")

    monkeypatch.setattr(native, "_model_environment", forbidden)
    options = {
        "env_file": tmp_path / "unused.env",
        "candidate_sha": candidate,
        "run_dir": run_dir,
        "run_id": "a" * 32,
        "base_sha": "d" * 40,
        "context_path": owner / "context.json",
    }
    if fault == "partial":
        del options["base_sha"]
    elif fault == "run-id":
        options["run_id"] = "A" * 32
    elif fault == "base-equal":
        options["base_sha"] = candidate
    elif fault == "selection":
        options["case_ids"] = [[WORD_VARIANTS[0][0]]]
    elif fault == "inside-run":
        options["context_path"] = run_dir / "context.json"
    elif fault == "inside-source":
        options["context_path"] = source / "context.json"
    expected = (
        "native_context_inputs_invalid"
        if fault in {"partial", "run-id", "base-equal"}
        else "native_context_base_invalid"
        if fault == "ancestor"
        else "native_case_selection_invalid"
        if fault == "selection"
        else "native_context_path_invalid"
    )
    with pytest.raises(native.NativeError, match=f"^{expected}$"):
        native.run_native(**options)
    assert observed == [] and not run_dir.exists() and list(owner.iterdir()) == []


@pytest.mark.parametrize("mode", ["explicit", "partial", "audit"])
def test_cli_native_context_group_and_ordered_selection(tmp_path, monkeypatch, capsys, mode):
    from tools import release_1_0_live_journeys as journeys

    received = []
    monkeypatch.setattr(
        native,
        "run_native",
        lambda **kwargs: received.append(kwargs) or {"status": "PASS", "go_emitted": False},
    )
    selected = [WORD_VARIANTS[1][0], WORD_VARIANTS[0][0]]
    args = [
        "--run-live",
        "--env-file",
        str(tmp_path / "models.env"),
        "--candidate-sha",
        "1" * 40,
        "--evidence-dir",
        str(tmp_path / "run"),
        "--run-id",
        "a" * 32,
        "--base-sha",
        "d" * 40,
        "--context-out",
        str(tmp_path / "context.json"),
    ]
    for case_id in selected:
        args += ["--case-id", case_id]
    if mode == "partial":
        at = args.index("--base-sha")
        del args[at : at + 2]
    elif mode == "audit":
        args[0] = "--audit-only"
    if mode != "explicit":
        with pytest.raises(SystemExit) as error:
            journeys.main(args)
        assert error.value.code == 2 and received == []
        assert capsys.readouterr().out == ""
    else:
        assert journeys.main(args) == 0
        assert received == [
            {
                "env_file": tmp_path / "models.env",
                "candidate_sha": "1" * 40,
                "run_dir": tmp_path / "run",
                "run_id": "a" * 32,
                "base_sha": "d" * 40,
                "context_path": tmp_path / "context.json",
                "case_ids": selected,
            }
        ]
        assert json.loads(capsys.readouterr().out) == {"status": "PASS", "go_emitted": False}


@pytest.mark.parametrize(
    "fault", ["parent-fstat", "child-fstat", "close-error", "write-signal", "parent-signal"]
)
def test_native_context_descriptor_failures_close_all_known_fds(tmp_path, monkeypatch, fault):
    from collections import Counter

    owner = tmp_path / "owner"
    owner.mkdir(mode=0o700)
    path = owner / "context.json"
    sentinel = owner / "foreign"
    sentinel.write_bytes(b"FOREIGN-DESCRIPTOR-SENTINEL")
    sentinel.chmod(0o640)
    original_sentinel = sentinel.stat()
    original_parent_mode = owner.stat().st_mode
    original_open, original_fstat, original_close = os.open, os.fstat, os.close
    opened, closed = [], []
    writer_fds = set()
    injected = []

    def tracked_open(name, flags, *args, **kwargs):
        fd = original_open(name, flags, *args, **kwargs)
        opened.append(fd)
        if flags & os.O_CREAT:
            writer_fds.add(fd)
        return fd

    def failed_fstat(fd):
        if not injected and (
            (fault in {"parent-fstat", "parent-signal"} and fd == opened[0])
            or (fault == "child-fstat" and fd in writer_fds)
        ):
            injected.append(fault)
            if fault == "parent-signal":
                raise lifecycle.ControllerSignal(15)
            raise OSError("synthetic descriptor metadata failure")
        return original_fstat(fd)

    def failed_close(fd):
        closed.append(fd)
        original_close(fd)
        if fault == "close-error" and fd in writer_fds and not injected:
            injected.append(fault)
            raise OSError("synthetic post-close error")

    monkeypatch.setattr(native.os, "open", tracked_open)
    monkeypatch.setattr(native.os, "fstat", failed_fstat)
    monkeypatch.setattr(native.os, "close", failed_close)
    if fault == "write-signal":

        def interrupted_write(fd, raw):
            assert fd in writer_fds and raw
            injected.append(fault)
            raise lifecycle.ControllerSignal(15)

        monkeypatch.setattr(native.os, "write", interrupted_write)
    code = "native_context_path_invalid" if fault == "parent-fstat" else "native_context_publish_failed"
    if fault in {"write-signal", "parent-signal"}:
        with pytest.raises(lifecycle.ControllerSignal) as interrupted:
            native._publish_native_context(path, {"descriptor_fixture": True})
        assert interrupted.value.signal_number == 15
    else:
        with pytest.raises(native.NativeError, match=f"^{code}$"):
            native._publish_native_context(path, {"descriptor_fixture": True})
    assert injected == [fault]
    assert Counter(opened) == Counter(closed)
    for fd in set(opened):
        with pytest.raises(OSError):
            original_fstat(fd)
    assert sentinel.read_bytes() == b"FOREIGN-DESCRIPTOR-SENTINEL"
    assert (sentinel.stat().st_ino, sentinel.stat().st_mode) == (
        original_sentinel.st_ino,
        original_sentinel.st_mode,
    )
    assert owner.stat().st_mode == original_parent_mode
    if fault in {"parent-fstat", "write-signal", "parent-signal"}:
        assert not path.exists() and not Path(str(path) + ".sha256").exists()
    elif fault == "child-fstat":
        # Unknown inode identity is deliberately retained; it cannot authorize unlink.
        assert path.read_bytes() == b"" and not Path(str(path) + ".sha256").exists()
    else:
        # A close failure prevents readiness even after successful byte publication.
        assert path.read_bytes() == b'{"descriptor_fixture":true}'
        assert Path(str(path) + ".sha256").is_file()
