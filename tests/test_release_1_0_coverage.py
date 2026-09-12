"""Coverage requires executable, layer-correct, collected evidence links."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from tools import release_1_0_acceptance as acceptance
from tools import release_1_0_live_journeys as journeys


@pytest.fixture
def native_receipt_transport(tmp_path):
    """Synthetic retained bytes for reader policy; no product or live credit."""
    import hashlib

    from tools import release_1_0_native as native
    from tools.release_1_0_live_cases import WORD_VARIANTS, word_fixture

    matrix = acceptance.load_matrix()
    case_id, run_id = WORD_VARIANTS[0][0], "a" * 32
    root = tmp_path / "native-receipt"
    root.mkdir(mode=0o700)

    def save(name, value):
        raw = value if isinstance(value, bytes) else json.dumps(value, indent=2).encode() + b"\n"
        path = root / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o600)
        return {"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}

    wheel = save("synthetic-0-py3-none-any.whl", b"reader-transport-fixture-wheel")
    identity = {
        "candidate_sha": "1" * 40,
        "candidate_tree": "2" * 40,
        "candidate_source_sha256": "3" * 64,
        "wheel_sha256": wheel["sha256"],
        "installed_site_sha256": "5" * 64,
        "suite_sha256": "6" * 64,
        "model_environment_sha256": "7" * 64,
    }
    probe_request = {
        "protocol": native.PROBE_PROTOCOL,
        "probe_nonce": "b" * 32,
        "case_id": case_id,
        "run_id": run_id,
        **identity,
        "candidate_files": ["tools/release_1_0_native.py"],
        "worker_environment_sha256": "9" * 64,
    }
    probe = {
        "schema": native.PROBE_PROTOCOL,
        "status": "PASS",
        "identity": probe_request,
        "effective_runtime_sha256": "8" * 64,
        "worker_environment_sha256": "9" * 64,
    }
    probe_response = save("case-001-runtime-probe-response.json", probe)
    fixture = word_fixture(case_id, run_id)
    request = {
        "protocol": native.PROTOCOL,
        "case_id": case_id,
        "run_id": run_id,
        **identity,
        "candidate_files": probe_request["candidate_files"],
        "expected_effective_runtime_sha256": "8" * 64,
        "expected_worker_environment_sha256": "9" * 64,
        "runtime_probe_receipt_sha256": probe_response["sha256"],
        "fixture_sha256": fixture.sha256,
    }
    request["expected_runtime_binding_sha256"] = native._runtime_binding(
        request,
        effective_runtime_sha256="8" * 64,
        worker_environment_sha256="9" * 64,
        probe_receipt_sha256=probe_response["sha256"],
    )
    runtime = {
        "effective_runtime_sha256": "8" * 64,
        "worker_environment_sha256": "9" * 64,
        "runtime_binding_sha256": request["expected_runtime_binding_sha256"],
    }
    output = b"synthetic-reader-output-bytes"
    worker = {
        "schema": native.PROTOCOL,
        "identity": request,
        "id": case_id,
        "status": "PASS",
        "failure_codes": [],
        "layer": "isolated-live",
        "attempt": 1,
        "chat_submissions": 1,
        "duration_ms": 5,
        "python_version": sys.version,
        "root_class": None,
        "secondary_enabled": False,
        "runtime_sha256": "8" * 64,
        "runtime_identity_before": runtime,
        "runtime_identity_live_after": runtime,
        "runtime_identity_reloaded_after": runtime,
        "model_http_counts": {"model": 1, "embedding": 0, "reranker": 0, "other": 0},
        "observed_safe": {
            "fixture_sha256": fixture.sha256,
            "artifact_sha256": hashlib.sha256(output).hexdigest(),
            "artifact_size_bytes": len(output),
        },
    }
    final = {
        **worker,
        "duration_ms": 10,
        "started_at": "2026-09-09T00:00:00+00:00",
        "ended_at": "2026-09-09T00:00:00.010000+00:00",
        "process_cleanup_clear": True,
        "go_emitted": False,
    }
    entry = {
        "id": case_id,
        "index": 1,
        "receipt": save("case-001/evidence/case-receipt.json", final),
        "probe_request": save("case-001/evidence/runtime-probe-request.json", probe_request),
        "probe_response": probe_response,
        "worker_request": save("case-001/evidence/worker-request.json", request),
        "worker_response": save("case-001-worker-response.json", worker),
        "artifacts": [
            {"kind": kind, **save(f"case-001/evidence/artifact-{hashlib.sha256(raw).hexdigest()}.bin", raw)}
            for kind, raw in (("fixture_sha256", fixture.content), ("artifact_sha256", output))
        ],
    }
    summary = {
        "schema": native.SCHEMA,
        "run_id": run_id,
        "planned": 1,
        "results": [],
        "status": "PASS",
        "root_failure": None,
        "go_emitted": False,
        "required_denominator": sum(case["release_required"] for case in matrix["cases"]),
        "required_results": [],
        "scope": "selected isolated-live cases only; not complete release acceptance",
        "evidence": {
            "schema": "friday.r10-native-evidence.v1",
            "selected_cases": [case_id],
            "identity": save("frozen-identity.json", identity),
            "wheel": wheel,
            "cases": [entry],
        },
    }
    arguments = {
        "receipt_path": root / "summary.json",
        "expected_identity": dict(identity),
        "expected_run_id": run_id,
        "expected_case_ids": [case_id],
        "expected_secondary": False,
        "expected_secondary_mode": None,
    }

    def publish():
        summary["results"] = [
            {
                key: value
                for key, value in final.items()
                if key
                in {
                    "id",
                    "status",
                    "failure_codes",
                    "attempt",
                    "duration_ms",
                    "root_class",
                    "process_cleanup_clear",
                    "root_ref",
                }
            }
        ]
        summary["required_results"] = [
            {
                "id": case["id"],
                "required_layer": case["layer"],
                "status": final["status"] if case["id"] == case_id else "NOT_RUN",
                "selected": case["id"] == case_id,
            }
            for case in matrix["cases"]
            if case["release_required"]
        ]
        entry["receipt"] = save(entry["receipt"]["path"], final) if entry["receipt"] else None
        arguments["receipt_sha256"] = save("summary.json", summary)["sha256"]

    publish()
    return {
        "matrix": matrix,
        "root": root,
        "summary": summary,
        "entry": entry,
        "identity": identity,
        "final": final,
        "worker": worker,
        "request": request,
        "save": save,
        "publish": publish,
        "arguments": arguments,
        "case_id": case_id,
    }


def test_native_receipt_audit_credits_only_selected_validated_case(native_receipt_transport):
    bundle = native_receipt_transport
    result = acceptance.audit_native_execution(bundle["matrix"], **bundle["arguments"])
    assert result["status"] == "PASS"
    assert result["case_layers"] == {bundle["case_id"]: "isolated-live"}
    assert all(
        status == "NOT_RUN" for cid, status in result["case_statuses"].items() if cid != bundle["case_id"]
    )
    assert result["go_emitted"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "tamper",
        "missing",
        "crossrun",
        "selection",
        "identity",
        "artifact",
        "deadline",
        "cleanup",
        "duplicate_json",
        "worker_mismatch",
        "wheel",
        "path",
        "mode",
        "secondary",
        "summary-status-type",
        "root-class-type",
        "artifact-kind-type",
        "worker-status-type",
        "worker-root-class-type",
        "final-cleanup-type",
        "final-count-type",
        "required-selected-type",
        "nonfinite-json",
    ],
)
def test_native_receipt_audit_rejects_bound_transport_faults(native_receipt_transport, fault):
    bundle = native_receipt_transport
    entry, args, final = bundle["entry"], bundle["arguments"], bundle["final"]
    if fault == "crossrun":
        args["expected_run_id"] = "c" * 32
    elif fault == "selection":
        args["expected_case_ids"] = []
    elif fault == "identity":
        args["expected_identity"]["installed_site_sha256"] = "c" * 64
    elif fault == "artifact":
        entry["artifacts"] = entry["artifacts"][:1]
    elif fault == "deadline":
        final["ended_at"] = "2026-09-09T01:00:00+00:00"
        final["duration_ms"] = 3_600_000
    elif fault == "cleanup":
        final["process_cleanup_clear"] = False
        entry["receipt"]["path"] = "case-001-receipt.json"
    elif fault == "duplicate_json":
        raw = json.dumps(bundle["request"]).encode()
        entry["worker_request"] = bundle["save"](
            entry["worker_request"]["path"], raw[:-1] + b', "run_id": "' + b"a" * 32 + b'"}'
        )
    elif fault == "worker_mismatch":
        final["model_http_counts"] = {"model": 99, "embedding": 0, "reranker": 0, "other": 0}
    elif fault == "wheel":
        bundle["summary"]["evidence"]["wheel"] = bundle["save"](
            "synthetic-0-py3-none-any.whl", b"different-retained-wheel"
        )
    elif fault == "path":
        entry["probe_response"]["path"] = "../outside.json"
    elif fault == "secondary":
        args["expected_secondary"] = True
        args["expected_secondary_mode"] = "shadow"
    elif fault == "root-class-type":
        bundle["summary"]["root_failure"] = {
            "id": "native-run-root",
            "code": "native_controller_interrupted",
            "root_class": [],
        }
    elif fault == "artifact-kind-type":
        entry["artifacts"][0]["kind"] = []
    elif fault in {"worker-status-type", "worker-root-class-type"}:
        key = "status" if fault == "worker-status-type" else "root_class"
        worker = {**bundle["worker"], key: []}
        entry["worker_response"] = bundle["save"](entry["worker_response"]["path"], worker)
    bundle["publish"]()
    if fault in {"final-cleanup-type", "final-count-type"}:
        # Reseal only the controller receipt, keeping the summary's valid bool
        # and the raw worker's integer count as independent typed references.
        if fault == "final-cleanup-type":
            final["process_cleanup_clear"] = 1
        else:
            final["model_http_counts"] = {**final["model_http_counts"], "model": 1.0}
        entry["receipt"] = bundle["save"](entry["receipt"]["path"], final)
        args["receipt_sha256"] = bundle["save"]("summary.json", bundle["summary"])["sha256"]
    elif fault in {"summary-status-type", "required-selected-type"}:
        if fault == "summary-status-type":
            bundle["summary"]["results"][0]["status"] = []
        else:
            selected = next(
                row for row in bundle["summary"]["required_results"] if row["id"] == bundle["case_id"]
            )
            selected["selected"] = 1
        args["receipt_sha256"] = bundle["save"]("summary.json", bundle["summary"])["sha256"]
    elif fault == "nonfinite-json":
        path = bundle["root"] / "summary.json"
        raw = path.read_bytes()
        marker = b'"scope": "selected isolated-live cases only; not complete release acceptance"'
        assert raw.count(marker) == 1
        args["receipt_sha256"] = bundle["save"]("summary.json", raw.replace(marker, b'"scope": 1e309'))[
            "sha256"
        ]
    raw_path = bundle["root"] / entry["worker_response"]["path"]
    if fault == "tamper":
        raw_path.write_bytes(b"{}")
    elif fault == "missing":
        raw_path.unlink()
    elif fault == "mode":
        raw_path.chmod(0o644)
    code = "^native_receipt_json_invalid$" if fault == "nonfinite-json" else "native_"
    with pytest.raises(acceptance.AcceptanceError, match=code):
        acceptance.audit_native_execution(bundle["matrix"], **args)


@pytest.mark.parametrize("status", ["FAIL", "NOT_RUN"])
def test_native_receipt_audit_preserves_nonpass_and_raw_diagnostics(native_receipt_transport, status):
    bundle = native_receipt_transport
    bundle["final"].update(
        status=status, failure_codes=["native_worker_response_invalid"], root_class="harness"
    )
    bundle["summary"]["status"] = "FAIL"
    bundle["entry"]["worker_response"] = bundle["save"]("case-001-worker-response.json", b"")
    bundle["publish"]()
    result = acceptance.audit_native_execution(bundle["matrix"], **bundle["arguments"])
    assert result["case_statuses"][bundle["case_id"]] == status
    assert result["case_layers"] == {}
    assert result["status"] == "FAIL"


def test_native_receipt_audit_root_prevented_case_needs_no_worker_traversal(native_receipt_transport):
    bundle = native_receipt_transport
    bundle["final"].clear()
    bundle["final"].update(
        id=bundle["case_id"],
        status="NOT_RUN",
        attempt=0,
        failure_codes=["native_run_root_prevented_completion"],
        root_class="environment",
        root_ref="native-run-root",
    )
    bundle["summary"].update(
        status="FAIL",
        root_failure={
            "id": "native-run-root",
            "code": "native_controller_interrupted",
            "root_class": "environment",
        },
    )
    for key in ("receipt", "probe_request", "probe_response", "worker_request", "worker_response"):
        bundle["entry"][key] = None
    bundle["entry"]["artifacts"] = []
    bundle["publish"]()
    result = acceptance.audit_native_execution(bundle["matrix"], **bundle["arguments"])
    assert result["case_layers"] == {}
    assert result["case_statuses"][bundle["case_id"]] == "NOT_RUN"
    assert result["root_failure"] == bundle["summary"]["root_failure"]


def test_native_receipt_audit_preserves_interrupted_root_receipt(native_receipt_transport):
    bundle = native_receipt_transport
    interrupted = {
        "id": bundle["case_id"],
        "identity": bundle["request"],
        "status": "NOT_RUN",
        "attempt": 1,
        "failure_codes": ["native_controller_interrupted"],
        "root_class": "environment",
        "process_cleanup_clear": False,
        "duration_ms": 10,
        "go_emitted": False,
    }
    bundle["final"].clear()
    bundle["final"].update(
        id=bundle["case_id"],
        status="NOT_RUN",
        attempt=1,
        failure_codes=["native_run_root_prevented_completion"],
        root_class="environment",
        root_ref="native-run-root",
    )
    bundle["summary"].update(
        status="FAIL",
        root_failure={
            "id": "native-run-root",
            "code": "native_controller_interrupted",
            "root_class": "environment",
        },
    )
    for key in ("receipt", "probe_request", "probe_response", "worker_request", "worker_response"):
        bundle["entry"][key] = None
    bundle["entry"]["artifacts"] = []
    bundle["publish"]()
    bundle["entry"]["receipt"] = bundle["save"]("case-001-receipt.json", interrupted)
    bundle["arguments"]["receipt_sha256"] = bundle["save"]("summary.json", bundle["summary"])["sha256"]
    result = acceptance.audit_native_execution(bundle["matrix"], **bundle["arguments"])
    assert result["case_layers"] == {}
    assert result["case_statuses"][bundle["case_id"]] == "NOT_RUN"


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("registry", "executable_handler_missing"),
        ("callable", "executable_handler_missing"),
        ("nodes", "executable_nodes_mismatch"),
        ("collection", "case_nodes_missing_or_uncollected"),
        ("source", "executable_test_source_missing"),
        ("driver", "executable_driver_mismatch"),
        ("surfaces", "executable_surfaces_mismatch"),
        ("layer", "executable_layer_mismatch"),
        ("handler", "executable_handler_mismatch"),
    ],
)
def test_canonical_pytest_bindings_require_actual_driver_source_and_exact_nodes(monkeypatch, fault, code):
    from pathlib import Path

    from tools import quality_gate as gate

    matrix = acceptance.load_matrix()
    case = next(case for case in matrix["cases"] if case["id"] == "R10-ADMIN-FILES-PAGE")
    nodes = _declared_nodes(matrix)
    if fault == "registry":
        monkeypatch.delitem(acceptance.PYTEST_CASE_BINDINGS, case["id"])
    elif fault == "callable":
        monkeypatch.setattr(gate, "execute_tier", None)
    elif fault == "nodes":
        case["node_ids"] = case["node_ids"][1:]
    elif fault == "collection":
        nodes = tuple(node for node in nodes if node != case["node_ids"][0])
    elif fault == "source":
        original = Path.read_text
        module, function = case["node_ids"][0].split("::")

        def changed_source(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            return (
                text.replace(f"def {function}(", "def removed_test(", 1)
                if path == acceptance.ROOT / module
                else text
            )

        monkeypatch.setattr(Path, "read_text", changed_source)
    elif fault == "driver":
        case["execution_driver"] = "journey"
    elif fault == "surfaces":
        case["covered_surfaces"].append("api:GET /api/health")
    elif fault == "layer":
        case["layer"] = "user-ui"
    else:
        case["handler"] = "tools/release_1_0_live_journeys.py:run_j01"
    result = acceptance.audit_case_bindings(matrix, nodes)
    assert not result["valid"]
    assert case["id"] not in result["verified_case_ids"]
    assert f"{code}:{case['id']}" in result["complaints"]


@pytest.mark.parametrize("fault", ["none", "rename", "duplicate", "syntax", "symlink", "hardlink", "class"])
def test_pytest_source_binding_is_static_and_cannot_import_test_side_effects(tmp_path, monkeypatch, fault):
    import os

    root = tmp_path / "source"
    (root / "tests").mkdir(parents=True)
    path = root / "tests/test_owned.py"
    source = (
        "raise AssertionError('controller must never import tests')\ndef test_bound():\n    assert True\n"
    )
    if fault == "rename":
        source = source.replace("test_bound", "removed")
    elif fault == "duplicate":
        source += "def test_bound():\n    assert False\n"
    elif fault == "syntax":
        source += "def ("
    path.write_text(source)
    if fault in {"symlink", "hardlink"}:
        original = tmp_path / "other.py"
        path.rename(original)
        if fault == "symlink":
            path.symlink_to(original)
        else:
            os.link(original, path)
    monkeypatch.setattr(acceptance, "ROOT", root)
    node = "tests/test_owned.py::" + ("MissingClass::" if fault == "class" else "") + "test_bound"
    assert acceptance._pytest_source_bindings_exist([node]) is (fault == "none")


def test_journey_driver_keeps_canonical_pytest_cases_explicitly_not_run(settings, monkeypatch):
    from tools import release_1_0_deterministic as deterministic

    monkeypatch.setattr(
        deterministic,
        "run_case",
        lambda case_id, _settings, _timeout: {
            "id": case_id,
            "status": "PASS",
            "cleanup_clear": True,
            "execution_observed": True,
        },
    )
    report = journeys.run_deterministic_suite(settings)
    bindings = journeys.acceptance.PYTEST_CASE_BINDINGS
    expected = {case_id for case_id, (layer, _, _) in bindings.items() if layer == "deterministic"}
    browser = {case_id for case_id, (layer, _, _) in bindings.items() if layer == "user-ui"}
    assert report["status"] == "INCOMPLETE" and report["go_emitted"] is False
    assert report["planned"] == len(journeys.RUNNERS) + len(expected)
    assert report["executed"] == len(journeys.RUNNERS)
    assert report["not_run"] == len(expected)
    assert {row["id"] for row in report["results"] if row["status"] == "NOT_RUN"} == expected
    assert browser and browser.isdisjoint(row["id"] for row in report["results"])


@pytest.mark.parametrize("fault", ["explicit", "missing_registry"])
def test_journey_driver_cannot_run_or_silently_drop_canonical_pytest_cases(settings, monkeypatch, fault):
    case_id = "R10-ADMIN-FILES-PAGE"
    if fault == "missing_registry":
        monkeypatch.delitem(journeys.acceptance.PYTEST_CASE_BINDINGS, case_id)
    with pytest.raises(
        journeys.acceptance.AcceptanceError,
        match="case_requires_canonical_gate" if fault == "explicit" else "executable_handler_missing",
    ):
        journeys.run_deterministic_suite(settings, [case_id] if fault == "explicit" else None)


@pytest.mark.parametrize("fault", ["not_executable", "wrong_layer", "wrong_surface"])
def test_existing_case_id_alone_does_not_cover_a_surface(fault):
    matrix = acceptance.load_matrix()
    case = next(case for case in matrix["cases"] if case["id"] == "R10-J01-UPLOAD-SEARCH-RESTART")
    surface = "api:POST /api/files"
    case["covered_surfaces"] = [surface]
    if fault == "not_executable":
        case["executable"] = False
    elif fault == "wrong_layer":
        case["layer"] = "harness"
    else:
        case["covered_surfaces"] = ["api:GET /api/health"]
    rule = next(rule for rule in matrix["surface_rules"] if rule["pattern"] == surface)
    rule["case_ids"] = [case["id"]]
    rule["required_layers"] = ["deterministic"]
    result = acceptance.classify_surfaces({"api": [surface]}, matrix, verified_case_ids=[case["id"]])
    assert result["coverage_gaps"] == [surface]


def test_default_selection_cannot_hide_a_deleted_required_handler(settings, monkeypatch):
    monkeypatch.delitem(journeys.RUNNERS, "R10-J01-UPLOAD-SEARCH-RESTART")
    with pytest.raises(journeys.acceptance.AcceptanceError, match="executable_handler_missing"):
        journeys.run_deterministic_suite(settings)


def _declared_nodes(matrix):
    return tuple(sorted({node for case in matrix["cases"] for node in case["node_ids"]}))


@pytest.mark.parametrize(
    "fault",
    ["missing_handler", "swapped_handler", "uncollected_node", "unrelated_collected_node", "wrong_layer"],
)
def test_case_audit_rejects_broken_executable_bindings(monkeypatch, fault):
    matrix = acceptance.load_matrix()
    nodes = _declared_nodes(matrix)
    case = matrix["cases"][0]
    if fault == "missing_handler":
        monkeypatch.delitem(journeys.RUNNERS, case["id"])
    elif fault == "swapped_handler":
        monkeypatch.setitem(journeys.RUNNERS, case["id"], journeys.run_j02)
    elif fault == "uncollected_node":
        case["node_ids"] = ["tests/test_invented.py::test_absent"]
    elif fault == "unrelated_collected_node":
        case["node_ids"] = matrix["cases"][1]["node_ids"]
    else:
        case["layer"] = "isolated-live"
    report = acceptance.audit_case_bindings(matrix, nodes)
    assert report["valid"] is False
    assert case["id"] not in report["verified_case_ids"]


def test_case_audit_checks_actual_dispatch_with_exact_collected_nodes():
    matrix = acceptance.load_matrix()
    report = acceptance.audit_case_bindings(matrix, _declared_nodes(matrix))
    assert report["valid"] is True, report
    assert set(report["verified_case_ids"]) == {case["id"] for case in matrix["cases"] if case["executable"]}


def test_coverage_requires_verified_bindings_in_every_requested_layer():
    matrix = acceptance.load_matrix()
    surface = "api:POST /api/files"
    no_proof = acceptance.classify_surfaces({"api": [surface]}, matrix)
    assert no_proof["coverage_gaps"] == [surface]
    bindings = acceptance.audit_case_bindings(matrix, _declared_nodes(matrix))
    verified = bindings["verified_case_ids"]
    proved = acceptance.classify_surfaces({"api": [surface]}, matrix, verified_case_ids=verified)
    assert proved["coverage_gaps"] == []
    rule = next(rule for rule in matrix["surface_rules"] if rule["pattern"] == surface)
    rule["required_layers"].append("isolated-live")
    missing_live = acceptance.classify_surfaces({"api": [surface]}, matrix, verified_case_ids=verified)
    assert missing_live["coverage_gaps"] == [surface]


@pytest.mark.parametrize("nodeids", [None, ()])
def test_case_audit_rejects_zero_collection(nodeids):
    report = acceptance.audit_case_bindings(acceptance.load_matrix(), nodeids)
    assert report["valid"] is False
    assert report["verified_case_ids"] == []


@pytest.mark.parametrize(
    "fault", ["scope_mismatch", "duplicate_rule", "unknown_case", "empty_required_layers"]
)
def test_matrix_rejects_ambiguous_or_inconsistent_coverage(tmp_path, fault):
    matrix = acceptance.load_matrix()
    if fault == "scope_mismatch":
        matrix["release_scope"]["required"].remove("CAP-TELEGRAM-LIVE")
    elif fault == "duplicate_rule":
        matrix["surface_rules"].append(matrix["surface_rules"][0])
    elif fault == "unknown_case":
        matrix["surface_rules"][0]["case_ids"] = ["R10-INVENTED"]
    else:
        matrix["surface_rules"][0]["required_layers"] = []
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(matrix))
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.load_matrix(path)


@pytest.mark.parametrize(
    "payload", [b'{"nodeids":[],"version":1}', b'{"nodeids":["not-a-test"],"version":1}', b"[]"]
)
def test_cli_audit_rejects_invalid_canonical_collection(tmp_path, capsys, payload):
    path = tmp_path / "collection.json"
    path.write_bytes(payload)
    assert acceptance.main(["--audit-only", "--collection", str(path)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "case_collection_invalid"


def test_isolated_cli_audits_collected_bindings_and_preserves_coverage_gaps(tmp_path):
    nodes = _declared_nodes(acceptance.load_matrix())
    path = tmp_path / "collection.json"
    path.write_text(json.dumps({"nodeids": nodes, "version": 1}, sort_keys=True, separators=(",", ":")))
    # This standalone CLI audits the exact source snapshot. The outer pytest
    # worker's installed-wheel claim is valid only with its own bootstrap.
    environment = dict(os.environ)
    environment.pop("FRIDAY_QUALITY_GATE_INSTALLED_SITE", None)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(acceptance.ROOT / acceptance.WRAPPER_RELATIVE),
            "--audit-only",
            "--collection",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=tmp_path,
        env=environment,
    )
    assert result.returncode == 2, result.stderr
    report = json.loads(result.stdout)
    assert report["case_bindings"]["valid"] is True
    assert report["valid"] is False
    assert any(code.startswith("surface_without_case:") for code in report["complaints"])


def test_collected_protocol_node_cannot_credit_required_live_execution():
    matrix = acceptance.load_matrix()
    case = next(case for case in matrix["cases"] if case["id"] == "R10-LIVE-DOC-WORD-FIRST-GEN")
    surface = "api:POST /api/chat"
    rule = next(rule for rule in matrix["surface_rules"] if rule["pattern"] == surface)
    rule["required_layers"] = ["isolated-live"]
    report = acceptance.classify_surfaces({"api": [surface]}, matrix, verified_case_ids=[case["id"]])
    assert report["coverage_gaps"] == [surface]


def _refresh_synthetic_children(gate_receipt, matrix, root):
    from tools import release_1_0_deterministic as deterministic

    context = gate_receipt["r10_deterministic"]["context"]
    rows = []
    for case in matrix["cases"]:
        if case["execution_driver"] != "journey":
            continue
        evidence = root / case["id"] / "evidence"
        evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = evidence / "case-receipt.json"
        request = {
            "schema": deterministic.SCHEMA,
            "case_id": case["id"],
            "settings": {"fixture": True},
            "identity": context["runtime"],
            "gate_context": context,
        }
        request_path = evidence / "request.json"
        request_path.write_bytes(deterministic._bytes(request))
        request_path.chmod(0o600)
        data = {
            "id": case["id"],
            "status": "PASS",
            "failure_codes": [],
            "attempt": 1,
            "process_schema": deterministic.SCHEMA,
            "cleanup_clear": True,
            "execution_observed": True,
            "go_emitted": False,
            "gate_context": context,
            "identity": context["runtime"],
            "evidence_dir": str(evidence),
            "request_sha256": deterministic._sha(request_path.read_bytes()),
            "settings_sha256": deterministic._sha(deterministic._bytes(request["settings"])),
            "duration_ns": 50,
            "timeout_s": case["timeout_s"],
        }
        path.write_bytes(deterministic._bytes(data))
        path.chmod(0o600)
        rows.append(
            deterministic._case_projection(
                path, deterministic._sha(path.read_bytes()), case["node_ids"][0], case, context
            )
        )
    gate_receipt["r10_deterministic"]["records"] = rows


def _refresh_synthetic_deadlines(receipt, matrix, policy, nodes):
    # Reader-policy fixtures only: never runtime/certification evidence.
    from tools import quality_gate_deadlines as deadline
    from tools import quality_gate_phase as phase

    selected = tuple(node for node in policy.classify(nodes) if node.tier != "nightly")
    plan = phase.gate_plan(selected, matrix, "synthetic-receipt-fixture")
    ledger = deadline.ParentDeadlineLedger(plan)
    commands = []
    durations = {row["nodeid"]: row["duration_ns"] for row in receipt["executed"]}
    at = 1
    for name in receipt["completed_steps"]:
        label = next((label for label in plan.phases if name == f"exact-release {label} tests"), None)
        start = at
        if label is not None:
            ledger.begin_phase(label, at)
            sequence = 0
            for node in plan.nodes:
                if node.phase != label:
                    continue
                for kind in ("start", "finish"):
                    sequence += 1
                    at += 1 if kind == "start" else durations[node.nodeid]
                    event = deadline.DeadlineEvent(
                        plan.run_id,
                        plan.sha256,
                        label,
                        "synthetic-worker",
                        sequence,
                        kind,
                        node.node_sha256,
                        at,
                    )
                    ledger.feed(deadline.encode_event(event), at)
            ledger.close_phase(label, at + 1, succeeded=True)
        commands.append(
            {
                "name": name,
                "start_ns": start,
                "finish_ns": at,
                "phase": label,
                "status": "passed",
                "cleanup": {
                    "schema": "friday.quality-gate-child-cleanup.v1",
                    "leader_returncode": 0,
                    "leader_reaped": True,
                    "reaped_descendants": 0,
                    "forced_leader": False,
                    "forced_descendants": False,
                    "kernel_echild": True,
                    "subreaper_restored": True,
                    "failure_codes": [],
                },
            }
        )
        at += 2
    receipt["active_deadlines"] = ledger.complete(at, succeeded=True)
    receipt["owned_commands"] = commands
    import copy

    auxiliary = []
    for label in ("candidate Git read", "private candidate clone", "exact-host prerequisite"):
        row = copy.deepcopy(commands[0])
        row.update(name=label, start_ns=0, finish_ns=0, phase=None)
        auxiliary.append(row)
    receipt["auxiliary_commands"] = auxiliary
    receipt["workload_metrics_before_evidence"]["wall_ns"] = max(1000, at)


@pytest.fixture
def gate_evidence(tmp_path, monkeypatch):
    """Synthetic canonical receipt publication; this fixture does not run a gate."""
    import os

    from tools import quality_gate as gate
    from tools import quality_gate_inventory as inventory

    matrix = acceptance.load_matrix()
    nodes = _declared_nodes(matrix)
    groups = {}
    for node in nodes:
        groups.setdefault(inventory.function_id(node), []).append(node)
    rules = tuple(
        inventory.FunctionRule(
            name,
            "acceptance.system-composition-and-observation",
            "change",
            "unit",
            300,
            1,
            len(exact),
            inventory.nodeids_sha256(tuple(exact)),
        )
        for name, exact in sorted(groups.items())
    )
    policy = inventory.GateInventory(rules)
    identity = {
        "base_sha": "0" * 40,
        "candidate_sha": "1" * 40,
        "candidate_tree": "2" * 40,
        "wheel_sha256": "3" * 64,
        "inventory_sha256": policy.digest,
    }
    receipt = {
        "schema": "friday.quality-gate-summary.v2",
        "result": "passed",
        "certification_eligible": True,
        **identity,
        "tier": "exact-release",
        "invariant_identity": "semantic-function+exact-parameter-set",
        "test_runtime_wheel_sha256": identity["wheel_sha256"],
        "comparison_wheel": {
            "epoch_commit": None,
            "expected_sha256": None,
            "observed_sha256": None,
            "build_profile": None,
        },
        "topology": {
            "requested_non_ui_workers": 20,
            "requested_ui_workers": 4,
            "effective_non_ui_workers": min(20, len(policy.modules)),
            "effective_ui_workers": 0,
        },
        "release_host_capacity": {
            "effective_cpus": 24,
            "initial_scratch_free_bytes": 32 << 30,
            "host_contour": {
                "os": {"id": "ubuntu", "version": "26.04", "architecture": "x86_64"},
                "userns_restriction": 1,
                "smoke": {
                    "user_namespace_distinct": True,
                    "network_namespace_distinct": True,
                    "profile_stack": "bwrap//&unpriv_bwrap (enforce)",
                },
                **{
                    key: {
                        "sha256": "4" * 64,
                        "size_bytes": 100,
                        "mode": "0644" if key == "apparmor_policy" else "0755",
                        "package": package,
                        "version": "synthetic-fixture",
                        "architecture": "amd64",
                    }
                    for key, package in {
                        "dpkg_query": "dpkg",
                        "dpkg": "dpkg",
                        "apparmor_parser": "apparmor",
                        "apparmor_policy": "apparmor",
                        "bubblewrap": "bubblewrap",
                    }.items()
                },
            },
        },
        "completed_steps": [
            command.name
            for command in gate._tier_static_commands(
                acceptance.ROOT,
                python=sys.executable,
                tier="exact-release",
                base_sha=identity["base_sha"],
                candidate_sha=identity["candidate_sha"],
                environment={},
            )
        ]
        + [
            "candidate wheel build",
            "candidate wheel verifier",
            "clean-install candidate wheel",
            "one authoritative candidate collection",
            "exact-release non-UI tests",
        ],
        "partition": gate._partition_evidence(policy.classify(nodes)),
        "executed": [{"nodeid": node, "duration_ns": 100} for node in nodes],
        "scratch_groups": [
            {
                "group": "non-UI",
                "node_count": len(nodes),
                "declared_budget_bytes": sum(rule.scratch_mb for rule in policy.rules) * 1024 * 1024,
                "baseline_bytes": 100,
                "peak_total_bytes": 200,
                "incremental_peak_bytes": 100,
                "enforced": False,
                "method": "sampled regular-file peak minus fixed baseline",
            }
        ],
        "workload_metrics_before_evidence": {
            "boundary": "after scratch cleanup, before summary composition",
            "wall_ns": 1000,
            "user_ns": 100,
            "sys_ns": 100,
            "max_rss_bytes": 1000,
            "peak_scratch_bytes": 200,
            "retry_count": 0,
        },
    }
    # Synthetic child attestations exercise reader policy only; no gate or
    # child execution is claimed by this fixture.
    from tools import release_1_0_deterministic as deterministic

    receipt["r10_deterministic"] = {
        "context": deterministic.make_gate_context(identity, acceptance.ROOT, acceptance.ROOT),
        "records": [],
    }
    _refresh_synthetic_children(receipt, matrix, tmp_path)
    _refresh_synthetic_deadlines(receipt, matrix, policy, nodes)
    path = tmp_path / "quality-gate-summary.json"
    monkeypatch.setattr(
        gate, "_exact_host_evidence", lambda: receipt["release_host_capacity"]["host_contour"]
    )
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        gate._write_tier_summary(fd, receipt)
    finally:
        os.close(fd)
    return matrix, nodes, policy, identity, receipt, path


def _gate_execution(value):
    import hashlib

    matrix, nodes, policy, identity, _receipt, path = value
    return acceptance.audit_gate_execution(
        matrix,
        nodes,
        receipt_path=path,
        receipt_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        expected_identity=identity,
        inventory=policy,
    )


def _ui_gate_evidence(gate_evidence, *, nonbrowser_nodes=()):
    """Publish synthetic canonical browser evidence, without executing a browser."""
    from dataclasses import replace

    from tools import quality_gate as gate
    from tools import quality_gate_inventory as inventory

    matrix, nodes, policy, identity, receipt, path = gate_evidence
    case = next(row for row in matrix["cases"] if row["id"] == "R10-UI-AUDIT")
    browser_functions = {
        inventory.function_id(node) for node in case["node_ids"] if node not in nonbrowser_nodes
    }
    policy = inventory.GateInventory(
        tuple(
            replace(rule, execution_kind="browser") if rule.function_id in browser_functions else rule
            for rule in policy.rules
        )
    )
    identity["inventory_sha256"] = policy.digest
    receipt["inventory_sha256"] = policy.digest
    receipt["r10_deterministic"]["context"]["release"]["inventory_sha256"] = policy.digest
    _refresh_synthetic_children(receipt, matrix, path.parent)
    classified = policy.classify(nodes)
    receipt["partition"] = gate._partition_evidence(classified)
    groups = (
        ("non-UI", tuple(node for node in classified if node.execution_kind != "browser")),
        ("UI", tuple(node for node in classified if node.execution_kind == "browser")),
    )
    receipt["topology"].update(
        effective_non_ui_workers=min(20, len({node.module_path for node in groups[0][1]})),
        effective_ui_workers=min(4, len({node.module_path for node in groups[1][1]})),
    )
    receipt["completed_steps"] = [
        name for name in receipt["completed_steps"] if name != "exact-release non-UI tests"
    ] + [f"exact-release {label} tests" for label, members in groups if members]
    receipt["scratch_groups"] = [
        {
            "group": label,
            "node_count": len(members),
            "declared_budget_bytes": sum(node.scratch_mb for node in members) * 1024 * 1024,
            "baseline_bytes": 100,
            "peak_total_bytes": 200,
            "incremental_peak_bytes": 100,
            "enforced": False,
            "method": "sampled regular-file peak minus fixed baseline",
        }
        for label, members in groups
        if members
    ]
    _refresh_synthetic_deadlines(receipt, matrix, policy, nodes)
    path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return matrix, nodes, policy, identity, receipt, path


@pytest.mark.parametrize("membership", ["browser", "mixed", "nonbrowser"])
def test_gate_receipt_ui_credit_requires_every_member_to_be_browser(gate_evidence, membership):
    case = next(row for row in gate_evidence[0]["cases"] if row["id"] == "R10-UI-AUDIT")
    nonbrowser = {
        "browser": (),
        "mixed": case["node_ids"][:1],
        "nonbrowser": case["node_ids"],
    }[membership]
    evidence = _ui_gate_evidence(gate_evidence, nonbrowser_nodes=nonbrowser)
    matrix, nodes, _policy, identity, _receipt, _path = evidence
    report = _gate_execution(evidence)
    expected = {row["id"]: "deterministic" for row in matrix["cases"] if row["layer"] == "deterministic"}
    if membership == "browser":
        expected["R10-UI-AUDIT"] = "user-ui"
        assert report["case_durations"]["R10-UI-AUDIT"] == {
            "basis": "sum_of_canonical_node_durations",
            "duration_ns": 300,
            "limit_ns": 300_000_000_000,
        }
    else:
        assert "R10-UI-AUDIT" not in report["case_durations"]
    assert report["status"] == "PASS" and report["case_layers"] == expected
    assert report["identity"] == identity and report["executed_nodes"] == len(nodes)
    assert report["go_emitted"] is False
    verified = acceptance.audit_case_bindings(matrix, nodes)["verified_case_ids"]
    surfaces = {"ui": ["ui:audit", "ui:backups"]}
    structural = acceptance.classify_surfaces(surfaces, matrix, verified_case_ids=verified)
    executed = acceptance.classify_surfaces(
        surfaces, matrix, verified_case_ids=verified, executed_case_layers=report["case_layers"]
    )
    assert structural["coverage_gaps"] == ["ui:audit", "ui:backups"]
    assert executed["coverage_gaps"] == (
        ["ui:backups"] if membership == "browser" else ["ui:audit", "ui:backups"]
    )


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("missing-node", "gate_receipt_execution_invalid"),
        ("duplicate-node", "gate_receipt_execution_invalid"),
        ("wrong-phase", "gate_receipt_active_deadlines_invalid"),
        ("over-budget", "gate_receipt_case_deadline_exceeded:R10-UI-AUDIT"),
        ("wrong-identity", "gate_receipt_identity_mismatch"),
        ("unclean-ui", "gate_receipt_active_deadlines_invalid"),
    ],
)
def test_gate_receipt_rejects_incomplete_or_unbound_ui_evidence(gate_evidence, fault, code):
    evidence = _ui_gate_evidence(gate_evidence)
    matrix, _nodes, _policy, _identity, receipt, path = evidence
    case = next(row for row in matrix["cases"] if row["id"] == "R10-UI-AUDIT")
    assert _gate_execution(evidence)["case_layers"][case["id"]] == "user-ui"
    target = case["node_ids"][0]
    if fault == "missing-node":
        receipt["executed"] = [row for row in receipt["executed"] if row["nodeid"] != target]
    elif fault == "duplicate-node":
        row = next(row for row in receipt["executed"] if row["nodeid"] == target)
        row["nodeid"] = case["node_ids"][1]
    elif fault == "wrong-phase":
        row = next(row for row in receipt["active_deadlines"]["attempts"] if row["nodeid"] == target)
        row["phase"] = "non-UI"
    elif fault == "over-budget":
        # Each node remains below300s; only the exact three-member UI case is over.
        for row in receipt["executed"]:
            if row["nodeid"] in case["node_ids"]:
                row["duration_ns"] = 100_000_000_000 + int(row["nodeid"] == target)
    elif fault == "wrong-identity":
        receipt["candidate_tree"] = "f" * 40
    else:
        row = next(row for row in receipt["owned_commands"] if row["phase"] == "UI")
        row["cleanup"]["kernel_echild"] = False
    path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    with pytest.raises(acceptance.AcceptanceError, match=f"^{code}$"):
        _gate_execution(evidence)


@pytest.mark.parametrize("late_drift", [False, True])
def test_cli_ui_receipt_preserves_denominator_and_unexecuted_layers(
    gate_evidence, monkeypatch, capsys, late_drift
):
    import hashlib

    from tools import quality_gate_inventory as inventory

    matrix, nodes, policy, identity, _receipt, path = _ui_gate_evidence(gate_evidence)
    collection = path.with_name("ui-collection.json")
    collection.write_text(json.dumps({"version": 1, "nodeids": nodes}, sort_keys=True, separators=(",", ":")))
    monkeypatch.setattr(acceptance, "load_matrix", lambda: matrix)
    monkeypatch.setattr(inventory, "load_inventory", lambda *args: policy)
    monkeypatch.setattr(acceptance, "discover_surfaces", lambda: {"ui": ["ui:audit", "ui:backups"]})
    checks = []

    def frozen_identity(*args):
        checks.append(True)
        if late_drift and len(checks) == 2:
            raise acceptance.AcceptanceError("gate_candidate_not_frozen")
        return identity

    monkeypatch.setattr(acceptance, "_frozen_gate_identity", frozen_identity)
    args = ["--audit-only", "--collection", str(collection)]
    assert acceptance.main(args) == 2
    before = json.loads(capsys.readouterr().out)
    required = {case["id"] for case in matrix["cases"] if case["release_required"]}
    assert {row["id"] for row in before["required_case_execution"]} == required
    assert all(row["status"] == "NOT_RUN" for row in before["required_case_execution"])
    assert before["surface_execution_gaps"] == ["ui:audit", "ui:backups"]
    args += [
        "--gate-receipt",
        str(path),
        "--gate-receipt-sha256",
        hashlib.sha256(path.read_bytes()).hexdigest(),
        "--candidate-sha",
        identity["candidate_sha"],
        "--base-sha",
        identity["base_sha"],
        "--wheel-sha256",
        identity["wheel_sha256"],
    ]
    assert acceptance.main(args) == 2
    after = json.loads(capsys.readouterr().out)
    assert after["go_emitted"] is False
    if late_drift:
        assert after["error"] == "gate_candidate_not_frozen" and after["valid"] is False
        return
    statuses = {row["id"]: row["status"] for row in after["required_case_execution"]}
    assert set(statuses) == required
    assert statuses["R10-UI-AUDIT"] == "PASS"
    assert all(
        statuses[case["id"]] == "NOT_RUN"
        for case in matrix["cases"]
        if case["release_required"] and case["layer"] != "deterministic" and case["id"] != "R10-UI-AUDIT"
    )
    assert after["surface_execution_gaps"] == ["ui:backups"]
    assert after["valid"] is False and after["execution_complete"] is False
    assert after["product_accepted_1_0"] is False


@pytest.mark.parametrize("excess_ns", [-1, 0, 1])
def test_gate_receipt_checks_the_sum_of_observed_case_node_durations(gate_evidence, excess_ns):
    matrix, _nodes, _policy, _identity, receipt, path = gate_evidence
    case = next(row for row in matrix["cases"] if row["id"] == "R10-ADMIN-FILES-PAGE")
    assert case["timeout_s"] == 300 and len(case["node_ids"]) == 3
    durations = dict.fromkeys(case["node_ids"], 100_000_000_000)
    durations[case["node_ids"][0]] += excess_ns
    for row in receipt["executed"]:
        if row["nodeid"] in durations:
            row["duration_ns"] = durations[row["nodeid"]]
    # Every node is below its canonical 300s limit. Only the composed case is
    # over budget; a per-node check cannot detect this regression.
    assert all(0 < value < 300_000_000_000 for value in durations.values())
    if excess_ns <= 0:
        _refresh_synthetic_deadlines(receipt, matrix, _policy, _nodes)
    path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    if excess_ns > 0:
        with pytest.raises(acceptance.AcceptanceError, match="gate_receipt_case_deadline_exceeded"):
            _gate_execution(gate_evidence)
    else:
        report = _gate_execution(gate_evidence)
        assert report["case_durations"][case["id"]] == {
            "basis": "sum_of_canonical_node_durations",
            "duration_ns": 300_000_000_000 + excess_ns,
            "limit_ns": 300_000_000_000,
        }
        assert report["case_layers"][case["id"]] == "deterministic"


@pytest.mark.parametrize("invalid", [None, True, 300.0, "300", 0, -1, 86_401])
def test_case_duration_limits_require_bounded_integer_seconds(tmp_path, gate_evidence, invalid):
    matrix = gate_evidence[0]
    case = next(row for row in matrix["cases"] if row["id"] == "R10-ADMIN-FILES-PAGE")
    if invalid is None:
        del case["timeout_s"]
    else:
        case["timeout_s"] = invalid
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(matrix))
    with pytest.raises(acceptance.AcceptanceError, match="matrix_case_timeout_invalid"):
        acceptance.load_matrix(path)
    # An in-memory caller cannot bypass the same limit validation.
    with pytest.raises(acceptance.AcceptanceError, match="matrix_case_timeout_invalid"):
        _gate_execution(gate_evidence)


@pytest.mark.parametrize("fault", ["absent", "missing-record", "other-candidate", "deleted-file"])
def test_gate_reader_requires_intact_child_execution_provenance(gate_evidence, fault):
    from pathlib import Path

    _matrix, _nodes, _policy, _identity, receipt, path = gate_evidence
    children = receipt["r10_deterministic"]
    if fault == "absent":
        del receipt["r10_deterministic"]
    elif fault == "missing-record":
        children["records"].pop()
    elif fault == "other-candidate":
        children["context"]["release"]["candidate_sha"] = "c" * 40
    else:
        Path(children["records"][0]["receipt_path"]).unlink()
    path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    with pytest.raises(acceptance.AcceptanceError, match="gate_receipt_"):
        _gate_execution(gate_evidence)


def test_bound_exact_gate_receipt_credits_only_observed_deterministic_cases(gate_evidence):
    matrix, nodes, _policy, identity, _receipt, _path = gate_evidence
    report = _gate_execution(gate_evidence)
    expected = {case["id"] for case in matrix["cases"] if case["layer"] == "deterministic"}
    assert report["status"] == "PASS" and set(report["case_layers"]) == expected
    assert report["identity"] == identity and report["executed_nodes"] == len(nodes)
    assert report["go_emitted"] is False
    assert all(not case_id.startswith("R10-LIVE-") for case_id in report["case_layers"])
    for rule in matrix["surface_rules"]:
        if rule["pattern"] == "api:POST /api/chat":
            rule["required_layers"] = ["isolated-live"]
    surfaces = {"api": ["api:POST /api/files", "api:POST /api/chat"]}
    bindings = acceptance.audit_case_bindings(matrix, nodes)
    before = acceptance.classify_surfaces(
        surfaces, matrix, verified_case_ids=bindings["verified_case_ids"], executed_case_layers={}
    )
    after = acceptance.classify_surfaces(
        surfaces,
        matrix,
        verified_case_ids=bindings["verified_case_ids"],
        executed_case_layers=report["case_layers"],
    )
    assert set(before["coverage_gaps"]) == set(surfaces["api"])
    assert after["coverage_kind"] == "executed" and after["coverage_gaps"] == ["api:POST /api/chat"]


@pytest.mark.parametrize(
    "fault",
    [
        "extra",
        "missing",
        "failed",
        "measurement",
        "change",
        "eligible_integer",
        "candidate_sha",
        "base_sha",
        "candidate_tree",
        "wheel_sha256",
        "inventory_sha256",
        "test_runtime_wheel_sha256",
        "retry",
        "retry_bool",
        "comparison",
        "empty_execution",
        "missing_execution",
        "duplicate_execution",
        "unknown_execution",
        "bool_duration",
        "negative_duration",
        "over_deadline",
        "partition",
        "bool_partition",
        "swapped_binding",
    ],
)
def test_gate_receipt_rejects_candidate_execution_and_policy_drift(gate_evidence, fault):
    matrix, _nodes, _policy, _identity, receipt, path = gate_evidence
    if fault == "extra":
        receipt["unvalidated_claim"] = "PASS"
    elif fault == "missing":
        del receipt["completed_steps"]
    elif fault == "failed":
        receipt["result"] = "failed"
    elif fault == "measurement":
        receipt["schema"] = "friday.quality-gate-measurement.v1"
    elif fault == "change":
        receipt["tier"] = "change"
    elif fault == "eligible_integer":
        receipt["certification_eligible"] = 1
    elif fault in {
        "candidate_sha",
        "base_sha",
        "candidate_tree",
        "wheel_sha256",
        "inventory_sha256",
        "test_runtime_wheel_sha256",
    }:
        receipt[fault] = "f" * len(receipt[fault])
    elif fault in {"retry", "retry_bool"}:
        receipt["workload_metrics_before_evidence"]["retry_count"] = 1 if fault == "retry" else False
    elif fault == "comparison":
        receipt["comparison_wheel"]["epoch_commit"] = "f" * 40
    elif fault == "empty_execution":
        receipt["executed"] = []
    elif fault == "missing_execution":
        receipt["executed"].pop()
    elif fault == "duplicate_execution":
        receipt["executed"][-1] = receipt["executed"][0]
    elif fault == "unknown_execution":
        receipt["executed"][0]["nodeid"] = "tests/test_invented.py::test_invented"
    elif fault in {"bool_duration", "negative_duration", "over_deadline"}:
        receipt["executed"][0]["duration_ns"] = {
            "bool_duration": True,
            "negative_duration": -1,
            "over_deadline": 301_000_000_000,
        }[fault]
    elif fault == "partition":
        receipt["partition"][0]["tier"] = "nightly"
    elif fault == "bool_partition":
        row = next(row for row in receipt["partition"] if row["scratch_mb"] == 1)
        row["scratch_mb"] = True
    else:
        matrix["cases"][0]["node_ids"] = matrix["cases"][1]["node_ids"]
    path.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    with pytest.raises(acceptance.AcceptanceError, match="gate_receipt_"):
        _gate_execution(gate_evidence)


@pytest.mark.parametrize(
    "fault",
    ["digest", "mode", "symlink", "hardlink", "fifo", "oversized", "duplicate_json", "noncanonical", "nan"],
)
def test_gate_receipt_file_cannot_bypass_private_bounded_identity(gate_evidence, fault):
    import hashlib
    import os
    import time

    matrix, nodes, policy, identity, _receipt, path = gate_evidence
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if fault == "digest":
        path.write_bytes(raw.replace(b'"duration_ns":100', b'"duration_ns":101'))
    elif fault == "mode":
        path.chmod(0o644)
    elif fault in {"symlink", "hardlink", "fifo"}:
        other = path.with_name("original.json")
        path.rename(other)
        if fault == "symlink":
            path.symlink_to(other)
        elif fault == "hardlink":
            os.link(other, path)
        else:
            os.mkfifo(path, 0o600)
    elif fault == "oversized":
        with path.open("wb") as file:
            file.truncate((64 << 20) + 1)
    else:
        raw = {
            "duplicate_json": raw.replace(b'"result":"passed"', b'"result":"failed","result":"passed"'),
            "noncanonical": raw + b"\n",
            "nan": raw.replace(b'"duration_ns":100', b'"duration_ns":NaN'),
        }[fault]
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
    started = time.monotonic()
    with pytest.raises(acceptance.AcceptanceError, match="gate_receipt_"):
        acceptance.audit_gate_execution(
            matrix,
            nodes,
            receipt_path=path,
            receipt_sha256=digest,
            expected_identity=identity,
            inventory=policy,
        )
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("late_drift", [False, True])
def test_cli_receipt_audit_keeps_unexecuted_layers_and_required_denominator(
    gate_evidence, monkeypatch, capsys, late_drift
):
    import hashlib

    from tools import quality_gate_inventory as inventory

    matrix, nodes, policy, identity, _receipt, path = gate_evidence
    collection = path.with_name("collection.json")
    collection.write_text(json.dumps({"version": 1, "nodeids": nodes}, sort_keys=True, separators=(",", ":")))
    monkeypatch.setattr(acceptance, "load_matrix", lambda: matrix)
    checks = []

    def frozen_identity(*args):
        checks.append(True)
        if late_drift and len(checks) == 2:
            raise acceptance.AcceptanceError("gate_candidate_not_frozen")
        return identity

    monkeypatch.setattr(acceptance, "_frozen_gate_identity", frozen_identity)
    monkeypatch.setattr(inventory, "load_inventory", lambda *args: policy)
    monkeypatch.setattr(
        acceptance, "discover_surfaces", lambda: {"api": ["api:POST /api/files", "api:POST /api/chat"]}
    )
    args = ["--audit-only", "--collection", str(collection)]
    assert acceptance.main(args) == 2
    no_receipt = json.loads(capsys.readouterr().out)
    assert no_receipt["gate_execution"]["status"] == "NOT_RUN"
    assert all(row["status"] == "NOT_RUN" for row in no_receipt["required_case_execution"])
    args += [
        "--gate-receipt",
        str(path),
        "--gate-receipt-sha256",
        hashlib.sha256(path.read_bytes()).hexdigest(),
        "--candidate-sha",
        identity["candidate_sha"],
        "--base-sha",
        identity["base_sha"],
        "--wheel-sha256",
        identity["wheel_sha256"],
    ]
    assert acceptance.main(args) == 2
    result = json.loads(capsys.readouterr().out)
    if late_drift:
        assert result["error"] == "gate_candidate_not_frozen"
        assert result["valid"] is False and result["go_emitted"] is False
        return
    assert result["gate_execution"]["status"] == "PASS" and result["execution_complete"] is False
    assert result["go_emitted"] is False and result["product_accepted_1_0"] is False
    assert len(result["required_case_execution"]) == sum(case["release_required"] for case in matrix["cases"])
    assert all(
        row["status"] == "NOT_RUN"
        for row in result["required_case_execution"]
        if row["required_layer"] != "deterministic"
    )


def test_gate_identity_rejects_a_dirty_or_different_candidate(tmp_path, monkeypatch):
    from tools import quality_gate as gate
    from tools import quality_gate_inventory as inventory

    candidate = "a" * 40
    policy = inventory.load_inventory()
    monkeypatch.setattr(
        gate,
        "_git_output",
        lambda root, *args: candidate if args[0] == "rev-parse" else " M tools/release_1_0_acceptance.py",
    )
    with pytest.raises(acceptance.AcceptanceError, match="gate_candidate_not_frozen"):
        acceptance._frozen_gate_identity(candidate, "d" * 40, "b" * 64, policy)
    monkeypatch.setattr(gate, "_git_output", lambda *args: "c" * 40)
    with pytest.raises(acceptance.AcceptanceError, match="gate_candidate_not_frozen"):
        acceptance._frozen_gate_identity(candidate, "d" * 40, "b" * 64, policy)


def _frozen_candidate(tmp_path, monkeypatch):
    from tools import quality_gate as gate
    from tools import quality_gate_inventory as inventory

    root = tmp_path / "source"
    root.mkdir()
    paths = (
        acceptance.WRAPPER_RELATIVE,
        "tools/release_1_0_capability_matrix.json",
        "tools/quality_gate_inventory.tsv",
        "tools/release_1_0_live_journeys.py",
        "tools/release_1_0_live_cases.py",
        "tools/release_1_0_native.py",
        "tools/release_1_0_deterministic.py",
        "tools/quality_gate.py",
        "tools/quality_gate_inventory.py",
    )
    for relative in paths:
        path = root / relative
        path.parent.mkdir(exist_ok=True)
        path.write_bytes((acceptance.ROOT / relative).read_bytes())
    # Keep this real Git authority fixture small; no pytest/product execution.
    module = "tests/test_synthetic.py"
    node = module + "::test_bound"
    (root / "tests").mkdir()
    (root / module).write_text("def test_bound():\n    pass\n")
    policy = inventory.GateInventory(
        (
            inventory.FunctionRule(
                node,
                "acceptance.system-composition-and-observation",
                "change",
                "unit",
                300,
                1,
                1,
                inventory.nodeids_sha256((node,)),
            ),
        )
    )
    (root / "tools/quality_gate_inventory.tsv").write_bytes(inventory.canonical_inventory_bytes(policy))

    def git(*args):
        return subprocess.check_output(
            [
                "/usr/bin/git",
                "-C",
                str(root),
                "-c",
                "user.name=Synthetic test",
                "-c",
                "user.email=synthetic@invalid",
                *args,
            ],
            text=True,
        ).strip()

    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "synthetic base")
    base = git("rev-parse", "HEAD")
    (root / "marker").write_text("synthetic candidate")
    git("add", ".")
    git("commit", "-qm", "synthetic candidate")
    candidate = git("rev-parse", "HEAD")
    monkeypatch.setattr(acceptance, "ROOT", root)
    monkeypatch.setattr(gate, "ROOT", root)
    monkeypatch.setattr(gate, "__file__", str(root / "tools/quality_gate.py"))
    return root, git, base, candidate, policy


def test_gate_identity_checks_actual_git_bytes_even_when_status_hides_a_change(tmp_path, monkeypatch):
    root, git, base, candidate, policy = _frozen_candidate(tmp_path, monkeypatch)
    observed = acceptance._frozen_gate_identity(candidate, base, "b" * 64, policy)
    assert observed["candidate_tree"] == git("rev-parse", "HEAD^{tree}")
    target = acceptance.WRAPPER_RELATIVE
    git("update-index", "--assume-unchanged", target)
    with (root / target).open("a") as file:
        file.write("\n# hidden post-freeze source mutation\n")
    assert git("status", "--porcelain=v1") == ""
    with pytest.raises(acceptance.AcceptanceError, match="gate_candidate_not_frozen"):
        acceptance._frozen_gate_identity(candidate, base, "b" * 64, policy)


@pytest.mark.parametrize("fault", ["assume", "skip", "same_stat", "hardlink", "executable", "ignored"])
def test_gate_identity_rejects_hidden_changes_outside_registration_files(tmp_path, monkeypatch, fault):
    import os

    root, git, base, candidate, policy = _frozen_candidate(tmp_path, monkeypatch)
    path = root / "tests/test_synthetic.py"
    if fault == "assume":
        git("update-index", "--assume-unchanged", "tests/test_synthetic.py")
        path.write_text("def test_bound():\n    fail\n")
    elif fault == "skip":
        git("update-index", "--skip-worktree", "tests/test_synthetic.py")
    elif fault == "same_stat":
        git("config", "core.trustctime", "false")
        git("config", "core.checkStat", "minimal")
        # Avoid Git's racy-clean fallback without sleeping: refresh the index
        # against an old mtime, then retain that cached size and mtime on damage.
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        git("update-index", "--refresh")
        assert git("status", "--porcelain=v1") == ""
        before = path.stat()
        path.write_bytes(path.read_bytes().replace(b"pass", b"fail"))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif fault == "hardlink":
        os.link(path, tmp_path / "external-link")
    elif fault == "executable":
        git("config", "core.filemode", "false")
        path.chmod(0o755)
    else:
        (root / ".git/info/exclude").write_text("injected.py\n")
        (root / "injected.py").write_text("# ignored import authority\n")
    assert git("status", "--porcelain=v1") == ""
    with pytest.raises(acceptance.AcceptanceError, match="gate_candidate_not_frozen"):
        acceptance._frozen_gate_identity(candidate, base, "b" * 64, policy)


@pytest.mark.parametrize(
    ("location", "value"),
    [
        ("topology.requested_non_ui_workers", 20.0),
        ("topology.effective_non_ui_workers", 21),
        ("topology.effective_ui_workers", 4),
        ("topology.extra", 1),
        ("completed_steps", []),
        ("completed_steps", ["all tests passed"]),
        ("workload_metrics_before_evidence.boundary", "before cleanup"),
        ("workload_metrics_before_evidence.wall_ns", False),
        ("workload_metrics_before_evidence.sys_ns", -1),
        ("workload_metrics_before_evidence.max_rss_bytes", "1000"),
        ("workload_metrics_before_evidence.peak_scratch_bytes", 199),
        ("scratch_groups", []),
        ("scratch_groups.0.node_count", True),
        ("scratch_groups.0.declared_budget_bytes", 0),
        ("scratch_groups.0.incremental_peak_bytes", 0),
        ("scratch_groups.0.enforced", True),
        ("scratch_groups.0.method", "invented"),
        ("release_host_capacity", {}),
        ("release_host_capacity.effective_cpus", 23),
        ("release_host_capacity.initial_scratch_free_bytes", 0),
        ("release_host_capacity.host_contour.os.version", "24.04"),
        ("release_host_capacity.host_contour.userns_restriction", True),
        ("release_host_capacity.host_contour.smoke.user_namespace_distinct", 1),
        ("release_host_capacity.host_contour.smoke.profile_stack", "unconfined"),
        ("release_host_capacity.host_contour.bubblewrap.package", "invented"),
        ("release_host_capacity.host_contour.bubblewrap.sha256", "not-a-digest"),
        ("release_host_capacity.host_contour.bubblewrap.size_bytes", 0),
        ("release_host_capacity.host_contour.bubblewrap.mode", "0777"),
        ("release_host_capacity.host_contour.bubblewrap.version", ""),
        ("release_host_capacity.host_contour.bubblewrap.architecture", []),
    ],
)
def test_gate_receipt_rejects_unobserved_or_malformed_policy_evidence(gate_evidence, location, value):
    receipt, path = gate_evidence[-2:]
    fields = location.split(".")
    target = receipt
    for field in fields[:-1]:
        target = target[int(field)] if isinstance(target, list) else target[field]
    target[fields[-1]] = value
    path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    with pytest.raises(acceptance.AcceptanceError, match="gate_receipt_policy_invalid"):
        _gate_execution(gate_evidence)


@pytest.mark.parametrize("include_nightly", [False, True])
def test_gate_policy_uses_selected_browser_modules_and_excludes_nightly(gate_evidence, include_nightly):
    from tools import quality_gate as gate
    from tools import quality_gate_inventory as inventory

    matrix, nodes, policy, identity, receipt, path = gate_evidence
    extra_nodes = ["tests/test_synthetic_browser.py::test_browser"]
    if include_nightly:
        extra_nodes.append("tests/test_synthetic_nightly.py::test_observation")
    rules = list(policy.rules)
    for node in extra_nodes:
        nightly = "nightly" in node
        rules.append(
            inventory.FunctionRule(
                node,
                "acceptance.system-composition-and-observation",
                "nightly" if nightly else "exact-release",
                "unit" if nightly else "browser",
                300,
                1,
                1,
                inventory.nodeids_sha256((node,)),
            )
        )
    nodes = tuple(sorted((*nodes, *extra_nodes)))
    policy = inventory.GateInventory(tuple(sorted(rules, key=lambda rule: rule.function_id)))
    identity["inventory_sha256"] = policy.digest
    receipt["inventory_sha256"] = policy.digest
    receipt["r10_deterministic"]["context"]["release"]["inventory_sha256"] = policy.digest
    _refresh_synthetic_children(receipt, matrix, path.parent)
    receipt["partition"] = gate._partition_evidence(policy.classify(nodes))
    receipt["executed"].append({"nodeid": extra_nodes[0], "duration_ns": 100})
    receipt["topology"]["effective_ui_workers"] = 1
    receipt["completed_steps"].append("exact-release UI tests")
    receipt["scratch_groups"].append(
        {
            "group": "UI",
            "node_count": 1,
            "declared_budget_bytes": 1024 * 1024,
            "baseline_bytes": 300,
            "peak_total_bytes": 400,
            "incremental_peak_bytes": 100,
            "enforced": False,
            "method": "sampled regular-file peak minus fixed baseline",
        }
    )
    receipt["workload_metrics_before_evidence"]["peak_scratch_bytes"] = 500
    _refresh_synthetic_deadlines(receipt, matrix, policy, nodes)
    path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    report = _gate_execution((matrix, nodes, policy, identity, receipt, path))
    assert report["status"] == "PASS"
    assert report["executed_nodes"] == len(nodes) - int(include_nightly)
    assert set(report["case_layers"].values()) == {"deterministic"}


@pytest.mark.parametrize("mode", [["--audit-only"], ["--plan", "final"], ["--preflight"]])
def test_partial_gate_identity_arguments_are_never_silently_ignored(tmp_path, mode):
    with pytest.raises(SystemExit) as caught:
        acceptance.main([*mode, "--gate-receipt", str(tmp_path / "private.json")])
    assert caught.value.code == 2


@pytest.mark.parametrize(
    "fault",
    [
        "legacy",
        "missing",
        "duplicate-index",
        "stream",
        "case-total",
        "retry",
        "unknown-node",
        "cleanup-echild",
        "cleanup-forced",
        "cleanup-bool",
        "command-name",
        "junit-interval",
    ],
)
def test_gate_receipt_requires_replayable_first_attempts_and_observed_owned_cleanup(gate_evidence, fault):
    _matrix, _nodes, _policy, _identity, receipt, path = gate_evidence
    active = receipt["active_deadlines"]
    if fault == "legacy":
        receipt["schema"] = "friday.quality-gate-summary.v1"
    elif fault == "missing":
        del receipt["active_deadlines"]
    elif fault == "duplicate-index":
        active["attempts"][0]["finish_event_index"] = active["attempts"][0]["start_event_index"]
    elif fault == "stream":
        active["event_stream_sha256"] = "0" * 64
    elif fault == "case-total":
        active["cases"][0]["used_ns"] += 1
    elif fault == "retry":
        active["retry_count"] = 1
    elif fault == "unknown-node":
        active["attempts"][0]["node_sha256"] = "0" * 64
    elif fault == "cleanup-echild":
        receipt["owned_commands"][-1]["cleanup"]["kernel_echild"] = False
    elif fault == "cleanup-forced":
        receipt["owned_commands"][-1]["cleanup"]["forced_descendants"] = True
    elif fault == "cleanup-bool":
        receipt["owned_commands"][-1]["cleanup"]["leader_returncode"] = False
    elif fault == "command-name":
        receipt["owned_commands"][-1]["name"] = "foreign command"
    elif fault == "junit-interval":
        receipt["executed"][0]["duration_ns"] += 2_000_000
    path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    with pytest.raises(
        acceptance.AcceptanceError,
        match="gate_receipt_(active_deadlines_invalid|not_exact_acceptance|shape_invalid)",
    ):
        _gate_execution(gate_evidence)

    if fault == "junit-interval":
        receipt["executed"][0]["duration_ns"] -= 2_000_000
        receipt["workload_metrics_before_evidence"]["wall_ns"] = 1
        path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
        with pytest.raises(acceptance.AcceptanceError, match="gate_receipt_active_deadlines_invalid"):
            _gate_execution(gate_evidence)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "empty",
        "clone-missing",
        "clone-duplicate",
        "foreign",
        "phase",
        "failed",
        "echild",
        "forced",
        "overlap",
    ],
)
def test_gate_receipt_requires_clean_nonoverlapping_early_and_late_commands(gate_evidence, fault):
    _matrix, _nodes, _policy, _identity, receipt, path = gate_evidence
    auxiliary = receipt["auxiliary_commands"]
    if fault == "missing":
        del receipt["auxiliary_commands"]
    elif fault == "empty":
        auxiliary.clear()
    elif fault == "clone-missing":
        auxiliary.pop(1)
    elif fault == "clone-duplicate":
        auxiliary.insert(1, dict(auxiliary[1]))
    elif fault == "foreign":
        auxiliary[0]["name"] = "unowned fallback"
    elif fault == "phase":
        auxiliary[0]["phase"] = "non-UI"
    elif fault == "failed":
        auxiliary[0]["status"] = "failed"
    elif fault == "echild":
        auxiliary[0]["cleanup"]["kernel_echild"] = False
    elif fault == "forced":
        auxiliary[0]["cleanup"]["forced_descendants"] = True
    elif fault == "overlap":
        measured = next(row for row in receipt["owned_commands"] if row["phase"] is not None)
        auxiliary[-1].update(start_ns=measured["start_ns"], finish_ns=measured["finish_ns"] + 1)
    path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    with pytest.raises(
        acceptance.AcceptanceError, match="gate_receipt_(active_deadlines_invalid|shape_invalid)"
    ):
        _gate_execution(gate_evidence)


@pytest.fixture
def native_cli_transport(native_receipt_transport, tmp_path, monkeypatch):
    """Retain real context/receipt readers; replace only local candidate authority."""
    import hashlib

    bundle = native_receipt_transport
    matrix = bundle["matrix"]
    context = {
        "schema": "friday.r10-native-context.v1",
        "base_sha": "d" * 40,
        "identity": dict(bundle["identity"]),
        "run_id": bundle["arguments"]["expected_run_id"],
        "case_ids": [bundle["case_id"]],
        "secondary_enabled": False,
        "secondary_mode": "disabled",
    }
    context_path = tmp_path / "independent-context.json"
    collection = tmp_path / "native-collection.json"
    collection.write_text(
        json.dumps({"version": 1, "nodeids": _declared_nodes(matrix)}, sort_keys=True, separators=(",", ":"))
    )
    monkeypatch.setattr(acceptance, "load_matrix", lambda: matrix)
    monkeypatch.setattr(acceptance, "_frozen_native_identity", lambda value, policy: dict(value["identity"]))
    monkeypatch.setattr(
        acceptance,
        "discover_surfaces",
        lambda: {"api": ["api:GET /api/files/{raw_id}", "api:POST /api/chat"]},
    )
    monkeypatch.setattr(acceptance, "audit_sealed_batteries", lambda: {"valid": True, "complaints": []})

    def publish_context(raw=None):
        context_path.write_bytes(
            raw if raw is not None else json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
        )
        context_path.chmod(0o600)
        return hashlib.sha256(context_path.read_bytes()).hexdigest()

    def arguments():
        return [
            "--audit-only",
            "--collection",
            str(collection),
            "--native-receipt",
            str(bundle["arguments"]["receipt_path"]),
            "--native-receipt-sha256",
            bundle["arguments"]["receipt_sha256"],
            "--native-context",
            str(context_path),
            "--native-context-sha256",
            hashlib.sha256(context_path.read_bytes()).hexdigest(),
        ]

    publish_context()
    return {
        **bundle,
        "context": context,
        "context_path": context_path,
        "collection": collection,
        "publish_context": publish_context,
        "cli_arguments": arguments,
    }


@pytest.mark.parametrize("status", ["PASS", "FAIL", "NOT_RUN", "root-suppressed-PASS"])
def test_cli_native_receipt_preserves_status_denominator_and_gaps(native_cli_transport, capsys, status):
    bundle = native_cli_transport
    matrix, case_id = bundle["matrix"], bundle["case_id"]
    required = {
        case["id"]: {"id": case["id"], "required_layer": case["layer"], "status": "NOT_RUN"}
        for case in matrix["cases"]
        if case["release_required"]
    }
    assert acceptance.main(bundle["cli_arguments"]()[:3]) == 2
    before = json.loads(capsys.readouterr().out)
    assert {row["id"]: row for row in before["required_case_execution"]} == required
    assert before["native_execution"] == {
        "status": "NOT_RUN",
        "case_layers": {},
        "case_statuses": {},
        "root_failure": None,
        "go_emitted": False,
    }
    assert before["surface_execution_gaps"] == ["api:GET /api/files/{raw_id}", "api:POST /api/chat"]
    if status in {"FAIL", "NOT_RUN"}:
        bundle["final"].update(
            status=status, failure_codes=["native_worker_response_invalid"], root_class="harness"
        )
        bundle["summary"]["status"] = "FAIL"
        bundle["entry"]["worker_response"] = bundle["save"]("case-001-worker-response.json", b"")
    elif status == "root-suppressed-PASS":
        bundle["summary"].update(
            status="FAIL",
            root_failure={
                "id": "native-run-root",
                "code": "native_controller_interrupted",
                "root_class": "environment",
            },
        )
    bundle["publish"]()
    assert acceptance.main(bundle["cli_arguments"]()) == 2
    report = json.loads(capsys.readouterr().out)
    required[case_id]["status"] = "NOT_RUN" if status == "root-suppressed-PASS" else status
    assert {row["id"]: row for row in report["required_case_execution"]} == required
    assert report["additional_required"] == before["additional_required"] == len(required)
    assert report["surface_coverage_gaps"] == before["surface_coverage_gaps"]
    assert report["surface_execution_gaps"] == before["surface_execution_gaps"]
    assert report["native_execution"]["case_layers"] == (
        {case_id: "isolated-live"} if status == "PASS" else {}
    )
    assert report["native_execution"]["case_statuses"][case_id] == (
        "PASS" if status == "root-suppressed-PASS" else status
    )
    assert report["native_execution"]["root_failure"] == bundle["summary"]["root_failure"]
    assert report["native_execution"]["results"] == bundle["summary"]["results"]
    assert report["gate_execution"] == before["gate_execution"]
    assert report["execution_complete"] is False
    assert report["product_accepted_1_0"] is False and report["go_emitted"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "unknown-field",
        "missing-identity",
        "duplicate-key",
        "integer-boolean",
        "empty-selection",
        "duplicate-selection",
        "wrong-mode",
        "noncanonical",
        "digest",
        "inside-run",
        "unverified-binding",
    ],
)
def test_cli_native_rejects_invalid_context_and_unverified_selection(native_cli_transport, capsys, fault):
    bundle = native_cli_transport
    context = bundle["context"]
    if fault == "unknown-field":
        context["unexpected"] = "retained"
    elif fault == "missing-identity":
        del context["identity"]["installed_site_sha256"]
    elif fault == "integer-boolean":
        context["secondary_enabled"] = 0
    elif fault == "empty-selection":
        context["case_ids"] = []
    elif fault == "duplicate-selection":
        context["case_ids"] *= 2
    elif fault == "wrong-mode":
        context["secondary_mode"] = "assist"
    bundle["publish_context"]()
    if fault == "duplicate-key":
        raw = (
            bundle["context_path"]
            .read_bytes()
            .replace(b'"secondary_enabled":false', b'"secondary_enabled":true,"secondary_enabled":false')
        )
        bundle["publish_context"](raw)
    elif fault == "noncanonical":
        bundle["publish_context"](bundle["context_path"].read_bytes() + b"\n")
    elif fault == "unverified-binding":
        selected = next(case for case in bundle["matrix"]["cases"] if case["id"] == bundle["case_id"])
        collection = json.loads(bundle["collection"].read_text())
        collection["nodeids"] = [node for node in collection["nodeids"] if node not in selected["node_ids"]]
        bundle["collection"].write_text(json.dumps(collection, sort_keys=True, separators=(",", ":")))
    args = bundle["cli_arguments"]()
    if fault == "digest":
        bundle["context_path"].write_bytes(bundle["context_path"].read_bytes() + b" ")
    elif fault == "inside-run":
        inside = bundle["root"] / "context.json"
        inside.write_bytes(bundle["context_path"].read_bytes())
        inside.chmod(0o600)
        args[args.index("--native-context") + 1] = str(inside)
    assert acceptance.main(args) == 2
    code = {
        "inside-run": "native_context_not_independent",
        "unverified-binding": "native_case_binding_invalid",
    }.get(fault, "native_context_invalid")
    assert json.loads(capsys.readouterr().out) == {
        "schema": acceptance.SCHEMA,
        "valid": False,
        "error": code,
        "go_emitted": False,
    }


@pytest.mark.parametrize("fault", ["context", "identity", "matrix"])
def test_cli_native_rejects_late_context_identity_and_matrix_drift(
    native_cli_transport, monkeypatch, capsys, fault
):
    import copy

    bundle = native_cli_transport
    checks = []

    def identity(context, policy):
        checks.append(True)
        if fault == "identity" and len(checks) == 2:
            raise acceptance.AcceptanceError("native_candidate_not_frozen")
        return dict(context["identity"])

    monkeypatch.setattr(acceptance, "_frozen_native_identity", identity)
    matrix_reads = []

    def matrix():
        matrix_reads.append(True)
        result = copy.deepcopy(bundle["matrix"])
        if fault == "matrix" and len(matrix_reads) >= 3:
            result["revision"] = "late-drift"
        return result

    monkeypatch.setattr(acceptance, "load_matrix", matrix)
    if fault == "context":

        def discover():
            bundle["context_path"].write_bytes(bundle["context_path"].read_bytes() + b" ")
            return {"api": ["api:POST /api/chat"]}

        monkeypatch.setattr(acceptance, "discover_surfaces", discover)
    assert acceptance.main(bundle["cli_arguments"]()) == 2
    assert len(checks) >= 1
    assert json.loads(capsys.readouterr().out) == {
        "schema": acceptance.SCHEMA,
        "valid": False,
        "error": "native_context_invalid" if fault == "context" else "native_candidate_not_frozen",
        "go_emitted": False,
    }


@pytest.mark.parametrize("mode", ["partial", "wrong-mode", "missing-collection"])
def test_cli_native_arguments_are_all_or_none(native_cli_transport, capsys, mode):
    args = native_cli_transport["cli_arguments"]()
    if mode == "partial":
        args = args[:-2]
    elif mode == "wrong-mode":
        args[0] = "--preflight"
    else:
        del args[1:3]
    with pytest.raises(SystemExit) as error:
        acceptance.main(args)
    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "native evidence requires --audit-only, --collection and all four native arguments" in output.err


@pytest.mark.parametrize("cross_candidate", [False, True])
def test_cli_merges_gate_and_native_only_for_same_candidate(
    native_cli_transport, monkeypatch, capsys, cross_candidate
):
    bundle = native_cli_transport
    gate_case = "R10-DIA-EMPTY-NO-ADMIN"
    gate_identity = {**bundle["identity"], "base_sha": bundle["context"]["base_sha"]}
    if cross_candidate:
        gate_identity["candidate_sha"] = "e" * 40
    monkeypatch.setattr(acceptance, "_frozen_gate_identity", lambda *args: dict(gate_identity))
    # Canonical gate parsing has separate tests; retain the real native reader here.
    gate_result = {
        "status": "PASS",
        "executed_nodes": 1,
        "case_layers": {gate_case: "deterministic"},
        "go_emitted": False,
    }
    monkeypatch.setattr(acceptance, "audit_gate_execution", lambda *args, **kwargs: dict(gate_result))
    args = bundle["cli_arguments"]() + [
        "--gate-receipt",
        str(bundle["root"].parent / "gate.json"),
        "--gate-receipt-sha256",
        "f" * 64,
        "--candidate-sha",
        gate_identity["candidate_sha"],
        "--base-sha",
        gate_identity["base_sha"],
        "--wheel-sha256",
        gate_identity["wheel_sha256"],
    ]
    assert acceptance.main(args) == 2
    report = json.loads(capsys.readouterr().out)
    if cross_candidate:
        assert report == {
            "schema": acceptance.SCHEMA,
            "valid": False,
            "error": "native_gate_identity_mismatch",
            "go_emitted": False,
        }
        return
    expected = {
        case["id"]: "PASS" if case["id"] in {gate_case, bundle["case_id"]} else "NOT_RUN"
        for case in bundle["matrix"]["cases"]
        if case["release_required"]
    }
    assert {row["id"]: row["status"] for row in report["required_case_execution"]} == expected
    assert report["gate_execution"] == gate_result
    assert report["native_execution"]["case_layers"] == {bundle["case_id"]: "isolated-live"}
    assert report["surface_execution_gaps"] == ["api:GET /api/files/{raw_id}"]
    assert report["execution_complete"] is False
    assert report["product_accepted_1_0"] is False and report["go_emitted"] is False


@pytest.mark.parametrize("fault", ["none", "tree", "source", "suite", "read-error"])
def test_frozen_native_identity_uses_native_source_and_suite_algorithms(monkeypatch, fault):
    from tools import release_1_0_native as native
    from tools import synthetic_live_battery as battery

    identity = {
        "candidate_sha": "1" * 40,
        "candidate_tree": "2" * 40,
        "candidate_source_sha256": "3" * 64,
        "wheel_sha256": "4" * 64,
        "installed_site_sha256": "5" * 64,
        "suite_sha256": "6" * 64,
        "model_environment_sha256": "7" * 64,
    }
    context = {"identity": identity, "base_sha": "d" * 40}
    policy = object()
    calls = []

    def gate(candidate, base, wheel, inventory):
        assert (candidate, base, wheel) == ("1" * 40, "d" * 40, "4" * 64)
        assert inventory is policy
        calls.append("gate")
        return {"candidate_tree": "e" * 40 if fault == "tree" else "2" * 40}

    paths = ("friday/config.py", "tools/release_1_0_native.py")

    def source_paths(**kwargs):
        assert kwargs == {
            "root": acceptance.ROOT,
            "instrument_path": acceptance.ROOT / "tools/release_1_0_native.py",
            "manifest_paths": [acceptance.ROOT / "tools/release_1_0_capability_matrix.json"],
        }
        calls.append("paths")
        return paths

    def source_digest(**kwargs):
        assert kwargs == {"root": acceptance.ROOT, "relative_paths": paths}
        calls.append("source")
        if fault == "read-error":
            raise OSError("synthetic missing tracked byte")
        return "e" * 64 if fault == "source" else "3" * 64

    def suite(root):
        assert root == acceptance.ROOT
        calls.append("suite")
        return "e" * 64 if fault == "suite" else "6" * 64

    monkeypatch.setattr(acceptance, "_frozen_gate_identity", gate)
    monkeypatch.setattr(battery, "_candidate_source_paths", source_paths)
    monkeypatch.setattr(battery, "_candidate_source_digest", source_digest)
    monkeypatch.setattr(native, "_suite_digest", suite)
    if fault == "none":
        result = acceptance._frozen_native_identity(context, policy)
        assert result == identity and result is not identity
    else:
        code = "native_candidate_not_frozen" if fault == "read-error" else "native_context_candidate_mismatch"
        with pytest.raises(acceptance.AcceptanceError, match=f"^{code}$"):
            acceptance._frozen_native_identity(context, policy)
    assert calls == (
        ["gate"]
        if fault == "tree"
        else ["gate", "paths", "source"]
        if fault == "read-error"
        else ["gate", "paths", "source", "suite"]
    )
