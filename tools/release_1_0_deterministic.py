"""Bounded child execution for R10 deterministic journeys, using the existing lifecycle."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import sysconfig
import time
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "friday.r10-deterministic-process.v1"
MAX_REQUEST = 1 << 20
GATE_CONTEXT_ENV = "FRIDAY_R10_GATE_CONTEXT"
GATE_SCHEMA = "friday.r10-gate-journeys.v1"
SUITE_PATHS = (
    "tools/release_1_0_deterministic.py",
    "tools/release_1_0_acceptance.py",
    "tools/release_1_0_live_journeys.py",
    "tools/release_1_0_capability_matrix.json",
    "tools/release_1_0_native.py",
    "tools/quality_gate.py",
    "tools/quality_gate_inventory.py",
    "tools/quality_gate_deadlines.py",
    "tools/quality_gate_deadline_plugin.py",
    "tools/quality_gate_phase.py",
    "tools/quality_gate_process.py",
    "tools/document_contour_live_battery.py",
    "tools/synthetic_live_battery.py",
    "tools/synthetic_live_b09_evidence.py",
    "tools/synthetic_live_b09_lab.py",
)


def _bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _settings_types() -> dict[str, type]:
    from friday.config import FridaySettings, RuntimeProfile, SglangExtraArgs, VllmExtraArgs

    return {cls.__name__: cls for cls in (FridaySettings, RuntimeProfile, SglangExtraArgs, VllmExtraArgs)}


def _encode(value: Any, depth: int = 0) -> Any:
    if depth > 16:
        raise ValueError("deterministic_settings_depth")
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, Path):
        return {"path": str(value)}
    if type(value) in (list, tuple):
        return {type(value).__name__: [_encode(item, depth + 1) for item in value]}
    if type(value) in _settings_types().values():
        return {
            "dataclass": type(value).__name__,
            "fields": {
                field.name: _encode(getattr(value, field.name), depth + 1)
                for field in dataclasses.fields(value)
            },
        }
    raise ValueError("deterministic_settings_type")


def _decode(value: Any, depth: int = 0) -> Any:
    if depth > 16:
        raise ValueError("deterministic_settings_depth")
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if not isinstance(value, dict):
        raise ValueError("deterministic_settings_shape")
    if set(value) == {"path"} and isinstance(value["path"], str):
        return Path(value["path"])
    for key, cls in (("list", list), ("tuple", tuple)):
        if set(value) == {key} and isinstance(value[key], list):
            return cls(_decode(item, depth + 1) for item in value[key])
    if set(value) == {"dataclass", "fields"} and isinstance(value["dataclass"], str):
        cls = _settings_types().get(value["dataclass"])
        fields = value["fields"]
        if (
            cls is not None
            and isinstance(fields, dict)
            and set(fields) == {field.name for field in dataclasses.fields(cls)}
        ):
            return cls(**{key: _decode(item, depth + 1) for key, item in fields.items()})
    raise ValueError("deterministic_settings_shape")


def _identity(product_root: Path, source_root: Path = ROOT) -> dict[str, Any]:
    from tools import quality_gate as gate

    return {
        "schema": SCHEMA,
        "python": sys.version,
        "product_sha256": {
            name: gate._projection_digest(product_root / name) for name in gate._WHEEL_NAMESPACES
        },
        "suite_sha256": {name: _sha((source_root / name).read_bytes()) for name in SUITE_PATHS},
        # The canonical gate receipt supplies candidate/tree/wheel identity.
        # A standalone diagnostic must not invent that release provenance.
        "requires_canonical_gate_receipt": True,
    }


def _hex(value: Any, length: int = 64) -> bool:
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


def validate_gate_context(value: Any) -> None:
    from tools import quality_gate as gate

    keys = {
        "base_sha": 40,
        "candidate_sha": 40,
        "candidate_tree": 40,
        "wheel_sha256": 64,
        "inventory_sha256": 64,
    }
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "release", "nonce", "runtime"}
        or value["schema"] != GATE_SCHEMA
        or not _hex(value["nonce"])
        or not isinstance(value["release"], dict)
        or set(value["release"]) != set(keys)
        or any(not _hex(value["release"][key], size) for key, size in keys.items())
        or value["release"]["base_sha"] == value["release"]["candidate_sha"]
    ):
        raise ValueError("deterministic_gate_context_invalid")
    runtime = value["runtime"]
    if (
        not isinstance(runtime, dict)
        or set(runtime)
        != {"schema", "python", "product_sha256", "suite_sha256", "requires_canonical_gate_receipt"}
        or runtime["schema"] != SCHEMA
        or runtime["requires_canonical_gate_receipt"] is not True
        or not isinstance(runtime["python"], str)
        or not 0 < len(runtime["python"]) <= 512
    ):
        raise ValueError("deterministic_gate_context_invalid")
    for key, names in (("product_sha256", gate._WHEEL_NAMESPACES), ("suite_sha256", SUITE_PATHS)):
        if (
            not isinstance(runtime[key], dict)
            or set(runtime[key]) != set(names)
            or not all(_hex(digest) for digest in runtime[key].values())
        ):
            raise ValueError("deterministic_gate_context_invalid")


def make_gate_context(release: dict[str, str], product_root: Path, source_root: Path) -> dict[str, Any]:
    value = {
        "schema": GATE_SCHEMA,
        "release": dict(release),
        "nonce": os.urandom(32).hex(),
        "runtime": _identity(product_root, source_root),
    }
    validate_gate_context(value)
    return value


def read_gate_context(environment: dict[str, str]) -> dict[str, Any] | None:
    from tools import quality_gate as gate

    raw = environment.get(GATE_CONTEXT_ENV)
    if raw is None:
        return None
    if not 0 < len(raw) <= 16384:
        raise ValueError("deterministic_gate_context_invalid")
    value = json.loads(raw, object_pairs_hook=gate._strict_json_object)
    validate_gate_context(value)
    return value


def _journey_bindings(matrix: dict[str, Any], nodes: Any) -> dict[str, dict[str, Any]]:
    selected = set(nodes)
    bindings = {}
    for case in matrix["cases"]:
        if case["execution_driver"] != "journey" or not case["executable"]:
            continue
        if len(case["node_ids"]) != 1 or case["layer"] != "deterministic":
            raise ValueError("deterministic_gate_binding_invalid")
        node = case["node_ids"][0]
        if node in selected:
            if node in bindings:
                raise ValueError("deterministic_gate_binding_invalid")
            bindings[node] = case
    return bindings


def _case_projection(
    path: Path, digest: str, node: str, case: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    from tools import release_1_0_acceptance as acceptance

    if path.name != "case-receipt.json" or path.parent.name != "evidence":
        raise ValueError("deterministic_gate_receipt_path")
    receipt = acceptance._read_bound_gate_receipt(path, digest, max_bytes=MAX_REQUEST)
    if (
        receipt.get("status") != "PASS"
        or receipt.get("failure_codes") != []
        or type(receipt.get("attempt")) is not int
        or receipt["attempt"] != 1
        or receipt.get("id") != case["id"]
        or receipt.get("process_schema") != SCHEMA
        or receipt.get("cleanup_clear") is not True
        or receipt.get("execution_observed") is not True
        or receipt.get("go_emitted") is not False
        or _bytes(receipt.get("gate_context")) != _bytes(context)
        or _bytes(receipt.get("identity")) != _bytes(context["runtime"])
        or receipt.get("evidence_dir") != str(path.parent)
        or not _hex(receipt.get("request_sha256"))
        or not _hex(receipt.get("settings_sha256"))
        or type(receipt.get("timeout_s")) is not int
        or receipt["timeout_s"] != case["timeout_s"]
        or type(receipt.get("duration_ns")) is not int
        or not 0 <= receipt["duration_ns"] <= case["timeout_s"] * 1_000_000_000
    ):
        raise ValueError("deterministic_gate_receipt_invalid")
    request = acceptance._read_bound_gate_receipt(
        path.parent / "request.json",
        receipt["request_sha256"],
        max_bytes=MAX_REQUEST,
    )
    if (
        set(request) != {"schema", "case_id", "settings", "identity", "gate_context"}
        or request["schema"] != SCHEMA
        or request["case_id"] != case["id"]
        or _bytes(request["identity"]) != _bytes(context["runtime"])
        or _bytes(request["gate_context"]) != _bytes(context)
        or _sha(_bytes(request["settings"])) != receipt["settings_sha256"]
    ):
        raise ValueError("deterministic_gate_request_invalid")
    return {
        "nodeid": node,
        "case_id": case["id"],
        "receipt_path": str(path),
        "receipt_sha256": digest,
        "request_sha256": receipt["request_sha256"],
        "settings_sha256": receipt["settings_sha256"],
        "duration_ns": receipt["duration_ns"],
        "timeout_s": receipt["timeout_s"],
    }


def collect_gate_journeys(
    report_path: Path, nodes: Any, context: dict[str, Any], matrix: dict[str, Any]
) -> list[dict[str, Any]]:
    """Close private child evidence while the authoritative JUnit still exists."""
    from tools import quality_gate as gate

    validate_gate_context(context)
    summary = gate.junit_summary(report_path)
    if summary.failures or summary.errors or summary.skipped or set(summary.nodeids) != set(nodes):
        raise ValueError("deterministic_gate_junit_invalid")
    bindings = _journey_bindings(matrix, nodes)
    records = []
    seen = set()
    for testcase in ET.parse(report_path).iter("testcase"):
        pairs = [
            (prop.attrib.get("name"), prop.attrib.get("value"))
            for prop in testcase.findall("properties/property")
        ]
        node = next(value for name, value in pairs if name == gate._NODEID_PROPERTY)
        special = [(name, value) for name, value in pairs if str(name).startswith("r10_")]
        if node not in bindings:
            if special:
                raise ValueError("deterministic_gate_unbound_properties")
            continue
        if len(special) != 2 or {key for key, _ in special} != {
            "r10_case_receipt",
            "r10_case_receipt_sha256",
        }:
            raise ValueError("deterministic_gate_receipt_properties")
        properties = dict(special)
        name = properties["r10_case_receipt"]
        digest = properties["r10_case_receipt_sha256"]
        if not isinstance(name, str) or not isinstance(digest, str):
            raise ValueError("deterministic_gate_receipt_properties")
        records.append(_case_projection(Path(name), digest, node, bindings[node], context))
        seen.add(node)
    if seen != set(bindings):
        raise ValueError("deterministic_gate_receipt_missing")
    return records


def validate_gate_journeys(
    value: Any, release: dict[str, str], matrix: dict[str, Any], durations: dict[str, int]
) -> dict[str, dict[str, Any]]:
    """Read the canonical writer's bound, observed child projections, never run them."""
    if not isinstance(value, dict) or set(value) != {"context", "records"}:
        raise ValueError("deterministic_gate_evidence_invalid")
    validate_gate_context(value["context"])
    if value["context"]["release"] != release or not isinstance(value["records"], list):
        raise ValueError("deterministic_gate_evidence_invalid")
    bindings = _journey_bindings(matrix, durations)
    seen = {}
    digests = set()
    keys = {
        "nodeid",
        "case_id",
        "receipt_sha256",
        "receipt_path",
        "request_sha256",
        "settings_sha256",
        "duration_ns",
        "timeout_s",
    }
    for row in value["records"]:
        if not isinstance(row, dict) or set(row) != keys or not isinstance(row["nodeid"], str):
            raise ValueError("deterministic_gate_evidence_invalid")
        node = row["nodeid"]
        case = bindings.get(node)
        if (
            case is None
            or node in seen
            or row["case_id"] != case["id"]
            or any(not _hex(row[key]) for key in ("receipt_sha256", "request_sha256", "settings_sha256"))
            or row["receipt_sha256"] in digests
            or type(row["timeout_s"]) is not int
            or row["timeout_s"] != case["timeout_s"]
            or type(row["duration_ns"]) is not int
            or not 0 <= row["duration_ns"] <= min(durations[node], case["timeout_s"] * 1_000_000_000)
        ):
            raise ValueError("deterministic_gate_evidence_invalid")
        if (
            not isinstance(row["receipt_path"], str)
            or _case_projection(
                Path(row["receipt_path"]), row["receipt_sha256"], node, case, value["context"]
            )
            != row
        ):
            raise ValueError("deterministic_gate_evidence_invalid")
        seen[node] = row
        digests.add(row["receipt_sha256"])
    if set(seen) != set(bindings):
        raise ValueError("deterministic_gate_evidence_missing")
    return {row["case_id"]: row for row in seen.values()}


def _read_request(path: Path, expected_sha: str) -> dict[str, Any]:
    from tools import release_1_0_native as native

    value = native._read_sealed_worker_request(str(path), expected_sha, expected_name="request.json")
    if not isinstance(value, dict) or _sha(_bytes(value)) != expected_sha:
        raise ValueError("deterministic_request_shape")
    return value


def _command(request: Path, digest: str, product_root: Path) -> tuple[str, ...]:
    sites = sorted({sysconfig.get_path(key) for key in ("purelib", "platlib")})
    bootstrap = (
        "import sys,types,importlib.machinery;"
        f"sys.path[:0]=[{str(product_root)!r}];sys.path.extend({sites!r});"
        "t=types.ModuleType('tools');"
        f"t.__path__=[{str(ROOT / 'tools')!r}];t.__package__='tools';"
        "t.__spec__=importlib.machinery.ModuleSpec('tools',loader=None,is_package=True);"
        "t.__spec__.submodule_search_locations=t.__path__;sys.modules['tools']=t;"
        "from tools.release_1_0_deterministic import worker_main;"
        f"raise SystemExit(worker_main({str(request)!r},{digest!r},{str(product_root)!r}))"
    )
    return (sys.executable, "-I", "-S", "-B", "-c", bootstrap)


def worker_main(request_name: str, request_sha: str, product_name: str) -> int:
    from tools import synthetic_live_battery as battery

    sink = battery._BoundedTextSink(battery.MAX_WORKER_LOG_BYTES)
    result: dict[str, Any] = {"status": "FAIL", "failure_codes": ["deterministic_worker_exception"]}
    request_path = Path(request_name)
    try:
        battery._install_no_exec_seccomp()
        guard = battery.LocalEndpointNetworkGuard(())
        guard.__enter__()  # Retain the guard through interpreter exit.
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            request = _read_request(request_path, request_sha)
            if (
                set(request) != {"schema", "case_id", "settings", "identity", "gate_context"}
                or request["schema"] != SCHEMA
            ):
                raise ValueError("deterministic_request_shape")
            from tools import document_contour_live_battery as lifecycle
            from tools import quality_gate as gate
            from tools import release_1_0_live_journeys as journeys

            lifecycle._unblock_worker_control_signals()
            product_root = Path(product_name)
            battery._assert_worker_product_authority(product_root)
            gate._require_installed_wheel_imports(product_root)
            if _identity(product_root) != request["identity"]:
                raise ValueError("deterministic_source_changed")
            if request["gate_context"] is not None:
                validate_gate_context(request["gate_context"])
                if request["gate_context"]["runtime"] != request["identity"]:
                    raise ValueError("deterministic_gate_runtime_mismatch")
            settings = _decode(request["settings"])
            if type(settings) is not _settings_types()["FridaySettings"]:
                raise ValueError("deterministic_settings_shape")
            if any(
                getattr(settings, name)
                for name in ("llm_enabled", "embeddings_enabled", "secondary_llm_enabled", "workers_enabled")
            ):
                raise ValueError("deterministic_models_or_workers_enabled")
            if settings.home != request_path.parent.parent / "home":
                raise ValueError("deterministic_home_identity")
            from friday.config import ensure_runtime_dirs

            ensure_runtime_dirs(settings)
            os.chdir(settings.home)
            result = journeys.RUNNERS[request["case_id"]](settings)
            if result.get("id") != request["case_id"]:
                raise ValueError("deterministic_case_identity")
            if guard.denied_attempts or _encode(settings) != request["settings"]:
                raise ValueError("deterministic_runtime_changed")
            if _identity(product_root) != request["identity"]:
                raise ValueError("deterministic_source_changed")
            gate._require_installed_wheel_imports(product_root)
            result.update(
                process_schema=SCHEMA,
                request_sha256=request_sha,
                identity=request["identity"],
                gate_context=request["gate_context"],
            )
    except Exception as exc:
        traceback.print_exc(file=sink)
        result = {
            "status": "FAIL",
            "failure_codes": ["deterministic_worker_exception"],
            "error_type": type(exc).__name__,
        }
    if sink.getvalue():
        battery._secure_write_bytes(request_path.parent / "runtime.log", sink.getvalue())
    if sink.truncated:
        result = {"status": "FAIL", "failure_codes": ["deterministic_log_oversized"]}
    sys.stdout.write(_bytes(result).decode())
    return 0


def _runtime_mismatch_digest(
    case_id: str,
    context: dict[str, Any],
    identity: dict[str, Any],
    *,
    absent_site: bool,
    product_is_root: bool,
) -> dict[str, Any]:
    """Retain only hashes and fixed identity keys, bound to the failed gate case."""

    def digests(runtime: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": _sha(_bytes(runtime["schema"])),
            "python": _sha(runtime["python"].encode()),
            "requires_canonical_gate_receipt": _sha(_bytes(runtime["requires_canonical_gate_receipt"])),
            "product_sha256": dict(runtime["product_sha256"]),
            "suite_sha256": dict(runtime["suite_sha256"]),
        }

    claimed, observed = digests(context["runtime"]), digests(identity)
    return {
        "schema": "friday.r10-runtime-mismatch-digest.v1",
        "case_id": case_id,
        "nonce": context["nonce"],
        "absent_site": absent_site,
        "product_is_root": product_is_root,
        "claimed_sha256": claimed,
        "observed_sha256": observed,
        "field_equal": {
            name: {key: value == observed[name][key] for key, value in expected.items()}
            if isinstance(expected, dict)
            else expected == observed[name]
            for name, expected in claimed.items()
        },
    }


def run_case(
    case_id: str, settings: Any, timeout_s: int, *, gate_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    started = time.monotonic_ns()
    from tools import document_contour_live_battery as lifecycle
    from tools import quality_gate as gate
    from tools import release_1_0_native as native
    from tools import synthetic_live_battery as battery

    if type(timeout_s) is not int or not 0 < timeout_s <= lifecycle.WORKER_TIMEOUT_SEC:
        raise ValueError("deterministic_timeout_invalid")
    case_root = Path(settings.home).parent
    root_stat = case_root.stat()
    if (
        not case_root.is_absolute()
        or case_root.resolve() != case_root
        or not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != os.getuid()
        or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        raise ValueError("deterministic_root_not_private")
    evidence = case_root / "evidence"
    evidence.mkdir(mode=0o700)
    installed_site = gate._validated_installed_site(os.environ)
    product_root = installed_site or ROOT
    identity = _identity(product_root)
    if gate_context is not None:
        validate_gate_context(gate_context)
        if product_root == ROOT or gate_context["runtime"] != identity:
            # Evidence already has a private, retained case owner. A failed
            # diagnostic write must not replace or bypass the original fence.
            with contextlib.suppress(OSError, battery.BatteryContractError):
                battery._secure_write_bytes(
                    evidence / "runtime-mismatch-digest.json",
                    _bytes(
                        _runtime_mismatch_digest(
                            case_id,
                            gate_context,
                            identity,
                            absent_site=installed_site is None,
                            product_is_root=product_root == ROOT,
                        )
                    ),
                )
            raise ValueError("deterministic_gate_runtime_mismatch")
    request = {
        "schema": SCHEMA,
        "case_id": case_id,
        "settings": _encode(settings),
        "identity": identity,
        "gate_context": gate_context,
    }
    raw = _bytes(request)
    if len(raw) > MAX_REQUEST:
        raise ValueError("deterministic_request_oversized")
    path = evidence / "request.json"
    battery._secure_write_bytes(path, raw)
    digest = _sha(raw)
    request_stamp = native._sealed_file_stamp(path, raw)
    if request_stamp is None:
        raise ValueError("deterministic_request_identity")
    settings.home.mkdir(mode=0o700)
    # Start from the existing closed test environment; never inherit model or
    # production HOME settings merely because the controller was launched there.
    cleanup_clear = False
    execution_observed = False
    result: dict[str, Any] = {"status": "FAIL", "failure_codes": ["deterministic_controller_exception"]}
    interrupted: BaseException | None = None
    environment_root = case_root / "environment"
    with gate._isolated_test_environment(prepare_schema_backups=False, source_root=ROOT) as template:
        environment = dict(template)
        template_root = Path(template["PYTHONPYCACHEPREFIX"]).parent
        shutil.copytree(template_root, environment_root)
        for key, value in environment.items():
            if value.startswith(str(template_root) + "/"):
                environment[key] = str(environment_root) + value[len(str(template_root)) :]
    environment.update(
        HOME=str(settings.home),
        FRIDAY_HOME=str(settings.home),
        JERICHO_HOME=str(settings.home),
        FRIDAY_QUALITY_GATE_INSTALLED_SITE=str(product_root) if product_root != ROOT else "",
        FRIDAY_LIVE_BATTERY_EVIDENCE=str(evidence / "observed.json"),
        FRIDAY_LLM_ENABLED="0",
        FRIDAY_EMBEDDINGS_ENABLED="0",
        FRIDAY_SECONDARY_LLM_ENABLED="0",
        FRIDAY_WORKERS_ENABLED="0",
    )
    signals = lifecycle._install_controller_signal_handlers()
    try:
        lifecycle._activate_controller_signal_handlers(signals)
        remaining = timeout_s - (time.monotonic_ns() - started) / 1_000_000_000
        if remaining <= 0:
            cleanup_clear = True
            result = {"status": "FAIL", "failure_codes": ["deterministic_setup_timeout"]}
        else:
            with lifecycle._private_worker_log(evidence / "process.log") as log:
                outcome = lifecycle._run_worker_process(
                    _command(path, digest, product_root),
                    environment=environment,
                    private_log=log,
                    timeout_sec=remaining,
                    stdout_limit_bytes=MAX_REQUEST,
                    controller_signal_handlers=signals,
                )
            cleanup_clear = outcome.cleanup_clear
            battery._secure_write_bytes(evidence / "response.json", outcome.stdout)
            try:
                result = json.loads(outcome.stdout)
            except (ValueError, UnicodeError):
                result = {}
            if not isinstance(result, dict) or (
                result.get("id") != case_id
                or result.get("process_schema") != SCHEMA
                or result.get("request_sha256") != digest
                or result.get("identity") != identity
                or result.get("gate_context") != gate_context
                or result.get("status") not in {"PASS", "FAIL"}
                or not isinstance(result.get("failure_codes"), list)
                or (result.get("status") == "PASS" and result["failure_codes"])
            ):
                result = {"status": "FAIL", "failure_codes": ["deterministic_response_invalid"]}
            execution_observed = result.get("process_schema") == SCHEMA
            faults = list(outcome.cleanup_failure_codes)
            if outcome.returncode:
                faults.append("deterministic_exit_nonzero")
            if not cleanup_clear:
                faults.append("deterministic_cleanup_uncertain")
            if faults:
                result = {"status": "FAIL", "failure_codes": sorted(set(faults))}
            if _identity(product_root) != identity or native._sealed_file_stamp(path, raw) != request_stamp:
                result = {"status": "FAIL", "failure_codes": ["deterministic_input_changed"]}
    except Exception as exc:
        result = {
            "status": "FAIL",
            "failure_codes": ["deterministic_controller_exception"],
            "error_type": type(exc).__name__,
        }
    except BaseException as exc:
        interrupted = exc
        if isinstance(exc, lifecycle.ControllerSignal):
            cleanup_clear = exc.worker_cleanup_clear is True
        result = {
            "status": "FAIL",
            "failure_codes": ["deterministic_controller_interrupted"],
            "error_type": type(exc).__name__,
        }
    finally:

        def finish() -> None:
            nonlocal cleanup_clear, result
            # All paths available to an uncertain worker stay private and intact.
            if cleanup_clear:
                try:
                    shutil.rmtree(settings.home)
                    shutil.rmtree(environment_root)
                except OSError:
                    cleanup_clear = False
                    result = {"status": "FAIL", "failure_codes": ["deterministic_path_cleanup_failed"]}
            duration_ns = time.monotonic_ns() - started
            if duration_ns > timeout_s * 1_000_000_000:
                result["status"] = "FAIL"
                result["failure_codes"] = sorted(
                    set(result.get("failure_codes", [])) | {"deterministic_case_deadline_exceeded"}
                )
            result.update(
                id=case_id,
                attempt=1,
                duration_ns=duration_ns,
                timeout_s=timeout_s,
                cleanup_clear=cleanup_clear,
                execution_observed=execution_observed,
                evidence_dir=str(evidence),
                settings_sha256=_sha(_bytes(request["settings"])),
                go_emitted=False,
            )
            battery._secure_write_bytes(evidence / "case-receipt.json", _bytes(result))

        lifecycle._finalize_controller_signal_handlers(signals, finish)
    if interrupted is not None:
        raise interrupted
    return result
