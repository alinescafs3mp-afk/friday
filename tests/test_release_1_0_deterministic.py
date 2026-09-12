"""Actual child deadlines and retained deterministic R10 evidence."""

from __future__ import annotations

import json
import signal
import sys
import time
import xml.etree.ElementTree as ET

import pytest

from tools import release_1_0_deterministic as driver
from tools import release_1_0_live_journeys as journeys


def _case_settings(settings, tmp_path):
    root = tmp_path / "case"
    root.mkdir(mode=0o700)
    return journeys._settings_in_home(settings, root / "home")


def test_full_settings_roundtrip_has_no_importable_type_selector(settings):
    encoded = driver._encode(settings)
    assert driver._decode(json.loads(driver._bytes(encoded))) == settings
    encoded["dataclass"] = "subprocess.Popen"
    with pytest.raises(ValueError, match="deterministic_settings_shape"):
        driver._decode(encoded)


@pytest.mark.parametrize(
    "error, expected_code",
    [
        (ValueError("deterministic_gate_runtime_mismatch"), "deterministic_gate_runtime_mismatch"),
        (ValueError("deterministic_settings_type"), "deterministic_settings_type"),
        (ValueError("deterministic_gate_runtime_mismatch: private-canary"), None),
        (ValueError("deterministic_gate_runtime_mismatch", "private-canary"), None),
        (RuntimeError("private-canary"), None),
    ],
)
def test_pre_request_failure_retains_only_closed_error_code_and_fences_remaining_cases(
    settings, tmp_path, monkeypatch, error, expected_code, child_gate_evidence
):
    # Exercise the real pre-worker fence before the public exception oracle.
    # Controlled identity differences test evidence, never stand in for a live
    # journey or the unresolved full-suite mismatch.
    if expected_code == "deterministic_gate_runtime_mismatch":
        from tools import quality_gate as gate
        from tools import synthetic_live_battery as battery

        context = child_gate_evidence[2]
        for fault in ("absent-site", "source-root", "python", "product", "suite", "write-failed"):
            root = tmp_path / ("mismatch-" + fault)
            root.mkdir(mode=0o700)
            scoped = journeys._settings_in_home(settings, root / "home")
            observed = json.loads(json.dumps(context["runtime"]))
            if fault == "python":
                observed["python"] = "private-version-canary"
            elif fault == "product":
                observed["product_sha256"]["friday"] = "7" * 64
            elif fault == "suite":
                observed["suite_sha256"][driver.SUITE_PATHS[0]] = "8" * 64
            with monkeypatch.context() as patch:
                site = (
                    None
                    if fault in {"absent-site", "write-failed"}
                    else (driver.ROOT if fault == "source-root" else tmp_path / "installed")
                )
                patch.setattr(gate, "_validated_installed_site", lambda env, site=site: site)
                patch.setattr(driver, "_identity", lambda product_root, observed=observed: observed)

                def no_worker(*args, **kwargs):
                    pytest.fail("mismatch must stop before request or worker")

                patch.setattr(driver, "_command", no_worker)
                if fault == "write-failed":

                    def failed_write(*args, **kwargs):
                        raise OSError("private-write-canary")

                    patch.setattr(battery, "_secure_write_bytes", failed_write)
                with pytest.raises(ValueError) as raised:
                    driver.run_case("R10-FILE-MISSING", scoped, 30, gate_context=context)
                assert raised.value.args == ("deterministic_gate_runtime_mismatch",)
            assert not scoped.home.exists() and not (root / "evidence/request.json").exists()
            path = root / "evidence/runtime-mismatch-digest.json"
            if fault == "write-failed":
                assert not path.exists()
                continue
            raw = path.read_bytes()
            digest = json.loads(raw)
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.parent.stat().st_mode & 0o777 == 0o700
            assert digest["case_id"] == "R10-FILE-MISSING" and digest["nonce"] == context["nonce"]
            assert digest["absent_site"] is (fault == "absent-site")
            assert digest["product_is_root"] is (fault in {"absent-site", "source-root"})
            equality = digest["field_equal"]
            assert equality["python"] is (fault != "python")
            assert equality["product_sha256"]["friday"] is (fault != "product")
            assert equality["suite_sha256"][driver.SUITE_PATHS[0]] is (fault != "suite")
            for side in ("claimed_sha256", "observed_sha256"):
                for value in digest[side].values():
                    values = value.values() if isinstance(value, dict) else (value,)
                    assert all(driver._hex(item) for item in values)
            for private in ("private-version-canary", "private-write-canary", str(tmp_path), sys.version):
                assert private.encode() not in raw

    dispatched = []
    scratch = tmp_path / "diagnostic-case"
    scratch.mkdir(mode=0o700)
    monkeypatch.setattr(journeys.tempfile, "mkdtemp", lambda **kwargs: str(scratch))

    def fail(case_id, *args, **kwargs):
        dispatched.append(case_id)
        raise error

    monkeypatch.setattr(driver, "run_case", fail)
    report = journeys.run_deterministic_suite(settings, ["R10-FILE-MISSING", "R10-AUTH-FORGED"])
    assert dispatched == ["R10-FILE-MISSING"]
    assert report["status"] == "FAIL" and report["executed"] == 0
    assert report["go_emitted"] is False and report["not_run"] == 1
    failure, unexecuted = report["results"]
    assert failure["failure_codes"] == ["harness_exception"]
    expected = {"error_type": type(error).__name__}
    if expected_code is not None:
        expected["error_code"] = expected_code
    assert failure["observed_safe"] == expected
    assert unexecuted["reason"] == "prior_cleanup_uncertain"
    assert not (scratch / "evidence/request.json").exists()
    assert "private-canary" not in json.dumps(report)


def test_actual_journey_child_preserves_private_bound_evidence(settings, tmp_path):
    scoped = _case_settings(settings, tmp_path)
    report = driver.run_case("R10-FILE-MISSING", scoped, 30)
    assert report["status"] == "PASS", report
    assert report["cleanup_clear"] is True and not scoped.home.exists()
    receipt = scoped.home.parent / "evidence/case-receipt.json"
    assert json.loads(receipt.read_text()) == report
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert report["identity"]["requires_canonical_gate_receipt"] is True
    assert "A" * 48 not in json.dumps(report)


def test_actual_hung_child_is_stopped_and_home_is_removed_only_after_audit(settings, tmp_path, monkeypatch):
    scoped = _case_settings(settings, tmp_path)
    monkeypatch.setattr(
        driver, "_command", lambda *args: (sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)")
    )
    started = time.monotonic()
    report = driver.run_case("R10-FILE-MISSING", scoped, 1)
    assert report["status"] == "FAIL" and "worker_timeout" in report["failure_codes"]
    assert time.monotonic() - started < 10
    assert report["cleanup_clear"] is True and not scoped.home.exists()


def test_uncertain_cleanup_retains_every_worker_path(settings, tmp_path, monkeypatch):
    from tools import document_contour_live_battery as lifecycle

    scoped = _case_settings(settings, tmp_path)
    outcome = lifecycle.WorkerProcessOutcome(
        b"{}", 0, True, True, True, False, ("worker_stdout_reader_failed",)
    )
    monkeypatch.setattr(lifecycle, "_run_worker_process", lambda *args, **kwargs: outcome)
    report = driver.run_case("R10-FILE-MISSING", scoped, 30)
    assert report["status"] == "FAIL" and report["cleanup_clear"] is False
    assert scoped.home.is_dir() and (scoped.home.parent / "environment").is_dir()


@pytest.mark.parametrize("signal_number", [signal.SIGINT, signal.SIGTERM])
def test_actual_controller_signal_stops_child_and_keeps_interrupted_receipt(
    settings, tmp_path, monkeypatch, signal_number
):
    from tools import document_contour_live_battery as lifecycle

    scoped = _case_settings(settings, tmp_path)
    command = (
        sys.executable,
        "-I",
        "-B",
        "-c",
        f"import os,time; os.kill(os.getppid(),{int(signal_number)}); time.sleep(30)",
    )
    monkeypatch.setattr(driver, "_command", lambda *args: command)
    previous = signal.getsignal(signal_number)
    with pytest.raises(lifecycle.ControllerSignal) as raised:
        driver.run_case("R10-FILE-MISSING", scoped, 30)
    assert raised.value.signal_number == signal_number
    assert raised.value.worker_cleanup_clear is True
    report = json.loads((scoped.home.parent / "evidence/case-receipt.json").read_text())
    assert report["status"] == "FAIL"
    assert report["failure_codes"] == ["deterministic_controller_interrupted"]
    assert report["cleanup_clear"] is True and not scoped.home.exists()
    assert signal.getsignal(signal_number) == previous


def test_interruption_with_unconfirmed_cleanup_keeps_private_paths(settings, tmp_path, monkeypatch):
    from tools import document_contour_live_battery as lifecycle

    scoped = _case_settings(settings, tmp_path)

    def uncertain(*args, **kwargs):
        exc = lifecycle.ControllerSignal(signal.SIGTERM)
        exc.worker_cleanup_clear = False
        raise exc

    monkeypatch.setattr(lifecycle, "_run_worker_process", uncertain)
    with pytest.raises(lifecycle.ControllerSignal):
        driver.run_case("R10-FILE-MISSING", scoped, 30)
    report = json.loads((scoped.home.parent / "evidence/case-receipt.json").read_text())
    assert report["status"] == "FAIL" and report["cleanup_clear"] is False
    assert scoped.home.is_dir() and (scoped.home.parent / "environment").is_dir()


def test_suite_fences_dispatch_after_unconfirmed_child_cleanup(settings, monkeypatch):
    selected = ["R10-FILE-MISSING", "R10-AUTH-FORGED"]
    dispatched = []

    def uncertain(case_id, case_settings, timeout_s):
        dispatched.append(case_id)
        return {
            "id": case_id,
            "status": "FAIL",
            "failure_codes": ["deterministic_cleanup_uncertain"],
            "cleanup_clear": False,
            "execution_observed": False,
        }

    monkeypatch.setattr(driver, "run_case", uncertain)
    result = journeys.run_deterministic_suite(settings, selected)
    assert result["status"] == "FAIL"
    assert dispatched == selected[:1]
    assert result["planned"] == 2 and result["attempted"] == 1
    assert result["executed"] == 0 and result["not_run"] == 1
    assert result["results"][1] == {
        "id": selected[1],
        "status": "NOT_RUN",
        "failure_codes": [],
        "reason": "prior_cleanup_uncertain",
    }


@pytest.fixture
def child_gate_evidence(tmp_path):
    from tools import quality_gate as gate

    matrix = journeys.acceptance.load_matrix()
    case = next(row for row in matrix["cases"] if row["id"] == "R10-FILE-MISSING")
    node = case["node_ids"][0]
    context = {
        "schema": driver.GATE_SCHEMA,
        "nonce": "a" * 64,
        "release": {
            "base_sha": "0" * 40,
            "candidate_sha": "1" * 40,
            "candidate_tree": "2" * 40,
            "wheel_sha256": "3" * 64,
            "inventory_sha256": "4" * 64,
        },
        "runtime": {
            "schema": driver.SCHEMA,
            "python": sys.version,
            "requires_canonical_gate_receipt": True,
            "product_sha256": dict.fromkeys(gate._WHEEL_NAMESPACES, "5" * 64),
            "suite_sha256": dict.fromkeys(driver.SUITE_PATHS, "6" * 64),
        },
    }
    home = tmp_path / "case"
    evidence = home / "evidence"
    evidence.mkdir(parents=True, mode=0o700)
    receipt = evidence / "case-receipt.json"
    request = {
        "schema": driver.SCHEMA,
        "case_id": case["id"],
        "settings": {"fixture": True},
        "identity": context["runtime"],
        "gate_context": context,
    }
    request_path = evidence / "request.json"
    request_path.write_bytes(driver._bytes(request))
    request_path.chmod(0o600)
    data = {
        "id": case["id"],
        "status": "PASS",
        "failure_codes": [],
        "attempt": 1,
        "process_schema": driver.SCHEMA,
        "cleanup_clear": True,
        "execution_observed": True,
        "go_emitted": False,
        "gate_context": context,
        "identity": context["runtime"],
        "evidence_dir": str(evidence),
        "request_sha256": driver._sha(request_path.read_bytes()),
        "settings_sha256": driver._sha(driver._bytes(request["settings"])),
        "timeout_s": case["timeout_s"],
        "duration_ns": 100,
    }
    receipt.write_bytes(driver._bytes(data))
    receipt.chmod(0o600)
    report = tmp_path / "results.xml"
    root = ET.Element("testsuite", tests="1", failures="0", errors="0", skipped="0")
    item = ET.SubElement(root, "testcase", name="observed", time="1")
    props = ET.SubElement(item, "properties")
    ET.SubElement(props, "property", name=gate._NODEID_PROPERTY, value=node)
    ET.SubElement(props, "property", name="r10_case_receipt", value=str(receipt))
    ET.SubElement(props, "property", name="r10_case_receipt_sha256", value=driver._sha(receipt.read_bytes()))
    ET.ElementTree(root).write(report)
    return matrix, node, context, receipt, data, report


def _collect(value):
    matrix, node, context, _path, _data, report = value
    return driver.collect_gate_journeys(report, [node], context, matrix)


def test_canonical_collector_observes_private_receipt_and_projects_no_payload(child_gate_evidence):
    matrix, node, context, path, _data, _report = child_gate_evidence
    rows = _collect(child_gate_evidence)
    assert len(rows) == 1 and rows[0]["receipt_sha256"] == driver._sha(path.read_bytes())
    assert "evidence_dir" not in rows[0] and "identity" not in rows[0]
    accepted = driver.validate_gate_journeys(
        {"context": context, "records": rows}, context["release"], matrix, {node: 1000}
    )
    assert set(accepted) == {"R10-FILE-MISSING"}


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "digest",
        "symlink",
        "hardlink",
        "public",
        "fifo",
        "duplicate-key",
        "oversized",
        "request-missing",
        "request-edited",
        "status",
        "cleanup",
        "unobserved",
        "retry",
        "attempt-bool",
        "case",
        "nonce",
        "candidate",
        "wheel",
        "runtime",
        "runtime-bool",
        "settings",
        "duration",
        "duration-bool",
        "timeout",
        "extra-property",
        "missing-property",
        "duplicate-property",
    ],
)
def test_collector_rejects_observed_receipt_or_junit_corruption(child_gate_evidence, fault):
    import os

    from tools import release_1_0_acceptance as acceptance

    _matrix, _node, _context, path, data, report = child_gate_evidence
    data = json.loads(driver._bytes(data))  # Mutation must not also change the trusted context.
    xml = ET.parse(report)
    props = xml.find("testcase/properties")
    if fault == "request-missing":
        (path.parent / "request.json").unlink()
    elif fault == "request-edited":
        (path.parent / "request.json").write_bytes(b"{}")
    elif fault == "missing":
        path.unlink()
    elif fault == "digest":
        path.write_bytes(b"{}")
    elif fault == "symlink":
        target = path.with_name("other")
        path.rename(target)
        path.symlink_to(target)
    elif fault == "hardlink":
        os.link(path, path.with_name("other"))
    elif fault == "public":
        path.chmod(0o644)
    elif fault == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif fault == "duplicate-key":
        raw = driver._bytes(data).replace(b'"status":"PASS"', b'"status":"PASS","status":"PASS"')
        path.write_bytes(raw)
        props[-1].set("value", driver._sha(raw))
    elif fault == "oversized":
        path.write_bytes(b" " * (driver.MAX_REQUEST + 1))
    elif fault in {"extra-property", "duplicate-property"}:
        ET.SubElement(
            props,
            "property",
            name="r10_case_receipt" if fault == "duplicate-property" else "r10_forged",
            value="x",
        )
    elif fault == "missing-property":
        props.remove(props[-1])
    else:
        mutations = {
            "status": (data, "status", "FAIL"),
            "cleanup": (data, "cleanup_clear", False),
            "unobserved": (data, "execution_observed", False),
            "retry": (data, "attempt", 2),
            "attempt-bool": (data, "attempt", True),
            "case": (data, "id", "R10-AUTH-FORGED"),
            "nonce": (data["gate_context"], "nonce", "b" * 64),
            "candidate": (data["gate_context"]["release"], "candidate_sha", "b" * 40),
            "wheel": (data["gate_context"]["release"], "wheel_sha256", "b" * 64),
            "runtime": (data["identity"]["product_sha256"], "friday", "b" * 64),
            "runtime-bool": (data["identity"], "requires_canonical_gate_receipt", 1),
            "settings": (data, "settings_sha256", "bad"),
            "duration": (data, "duration_ns", 31_000_000_000),
            "duration-bool": (data, "duration_ns", True),
            "timeout": (data, "timeout_s", 31),
        }
        obj, key, value = mutations[fault]
        obj[key] = value
        path.write_bytes(driver._bytes(data))
        props[-1].set("value", driver._sha(path.read_bytes()))
    xml.write(report)
    with pytest.raises((ValueError, acceptance.AcceptanceError), match="(deterministic_gate_|gate_receipt_)"):
        _collect(child_gate_evidence)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "duplicate",
        "node",
        "case",
        "nonce",
        "candidate",
        "digest",
        "duration",
        "duration-bool",
        "timeout",
        "artifact-missing",
        "artifact-edited",
    ],
)
def test_reader_rejects_bound_child_projection_drift(child_gate_evidence, fault):
    from tools import release_1_0_acceptance as acceptance

    matrix, node, context, path, _data, _report = child_gate_evidence
    evidence = json.loads(driver._bytes({"context": context, "records": _collect(child_gate_evidence)}))
    row = evidence["records"][0]
    if fault == "artifact-missing":
        path.unlink()
    elif fault == "artifact-edited":
        path.write_bytes(b"{}")
    elif fault == "missing":
        evidence["records"] = []
    elif fault == "duplicate":
        evidence["records"].append(dict(row))
    elif fault == "nonce":
        evidence["context"]["nonce"] = ""
    elif fault == "candidate":
        evidence["context"]["release"]["candidate_sha"] = "c" * 40
    else:
        key, value = {
            "node": ("nodeid", "unknown"),
            "case": ("case_id", "unknown"),
            "digest": ("receipt_sha256", "bad"),
            "duration": ("duration_ns", 1001),
            "duration-bool": ("duration_ns", True),
            "timeout": ("timeout_s", 31),
        }[fault]
        row[key] = value
    with pytest.raises((ValueError, acceptance.AcceptanceError), match="(deterministic_gate_|gate_receipt_)"):
        driver.validate_gate_journeys(evidence, context["release"], matrix, {node: 1000})


@pytest.mark.parametrize("foreign", [False, True])
def test_isolated_gate_loads_only_the_candidate_tools_namespace(tmp_path, foreign):
    import subprocess

    script = f"""
import importlib.util,json,sys,types
from pathlib import Path
root=Path({str(driver.ROOT)!r})
assert str(root) not in sys.path and 'tools' not in sys.modules
spec=importlib.util.spec_from_file_location('probe_gate',root/'tools/quality_gate.py')
gate=importlib.util.module_from_spec(spec);sys.modules[spec.name]=gate;spec.loader.exec_module(gate)
if {foreign!r}:
    namespace=types.ModuleType('tools');namespace.__path__=[{str(tmp_path)!r}];sys.modules['tools']=namespace
try:
    acceptance,driver=gate._candidate_r10_modules(root)
except RuntimeError as exc:
    assert {foreign!r} and 'foreign authority' in str(exc)
    print('FOREIGN_REFUSED')
else:
    assert not {foreign!r} and Path(driver.__file__).parent==root/'tools'
    assert sys.modules['tools.quality_gate'] is gate
    assert acceptance.load_matrix()['cases'] and str(root) not in sys.path
    assert not any(name=='friday' or name.startswith('friday.') for name in sys.modules)
    print('OWNED_TOOLS_LOADED')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("FOREIGN_REFUSED" if foreign else "OWNED_TOOLS_LOADED")
