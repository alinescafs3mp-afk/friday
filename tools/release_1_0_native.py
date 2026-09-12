"""Native isolated R10 execution, composed from the existing gate boundaries.

This module never emits release GO. Selected secondary configuration travels
through the same isolated boundary, with separate inference and health counters.
Successful transport alone does not certify positive secondary workload use.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import socket
import stat
import sys
import sysconfig
import tempfile
import time
import tomllib
import traceback
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "friday.r10-native.v1"
PROTOCOL = "friday.r10-native-worker.v1"
PROBE_PROTOCOL = "friday.r10-native-runtime-probe.v1"
RUNTIME_BINDING_SCHEMA = "friday.r10-native-runtime-binding.v1"
SUITE_PATHS = (
    "tools/release_1_0_native.py",
    "tools/release_1_0_native_assets.py",
    "tools/release_1_0_live_cases.py",
    "tools/release_1_0_live_journeys.py",
    "tools/release_1_0_acceptance.py",
    "tools/release_1_0_capability_matrix.json",
    "tools/document_contour_live_battery.py",
    "tools/synthetic_live_battery.py",
    "tools/synthetic_live_b09_evidence.py",
    "tools/synthetic_live_b09_lab.py",
    "tools/synthetic_live_acceptance.py",
    "tools/quality_gate.py",
)


def live_handlers() -> dict[str, Any]:
    from tools.release_1_0_live_cases import WORD_VARIANTS, run_word_case

    return {row[0]: run_word_case for row in WORD_VARIANTS}


class NativeError(RuntimeError):
    """Closed code only; private exception details never reach public summaries."""


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, (NativeError, _assets().NativeAssetError)) and re.fullmatch(
        r"native_[a-z0-9_]{1,88}", str(exc)
    ):
        return str(exc)
    return "native_setup_failed"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _worker_environment_digest(environment: Mapping[str, str]) -> str:
    if not isinstance(environment, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in environment.items()
    ):
        raise NativeError("native_worker_environment_invalid")
    return _sha(
        _canonical(
            {
                "schema": "friday.r10-native-worker-environment.v1",
                "environment": dict(environment),
            }
        )
    )


def _runtime_binding(
    identity: Mapping[str, str],
    *,
    effective_runtime_sha256: str,
    worker_environment_sha256: str,
    probe_receipt_sha256: str,
) -> str:
    keys = (
        "case_id",
        "run_id",
        "candidate_sha",
        "candidate_tree",
        "candidate_source_sha256",
        "wheel_sha256",
        "installed_site_sha256",
        "suite_sha256",
        "model_environment_sha256",
    )
    if any(key not in identity for key in keys):
        raise NativeError("native_runtime_binding_identity_invalid")
    return _sha(
        _canonical(
            {
                "schema": RUNTIME_BINDING_SCHEMA,
                "identity": {key: identity[key] for key in keys},
                "effective_runtime_sha256": effective_runtime_sha256,
                "worker_environment_sha256": worker_environment_sha256,
                "probe_receipt_sha256": probe_receipt_sha256,
                **({"secondary_ca": identity["secondary_ca"]} if "secondary_ca" in identity else {}),
            }
        )
    )


def _dependencies():
    from tools import document_contour_live_battery as lifecycle
    from tools import quality_gate as gate
    from tools import synthetic_live_acceptance as acceptance
    from tools import synthetic_live_battery as battery

    return lifecycle, gate, acceptance, battery


def _suite_digest(source: Path) -> str:
    return _sha(_canonical({path: _sha((source / path).read_bytes()) for path in SUITE_PATHS}))


def _assets():
    from tools import release_1_0_native_assets

    return release_1_0_native_assets


def _secondary_enabled(environment: Mapping[str, str], *, battery: Any) -> bool:
    # Match the installed product's _bool_env, including nonstandard true values.
    return battery._environment_setting(
        environment, "FRIDAY_SECONDARY_LLM_ENABLED", "0"
    ).strip().lower() not in {"", "0", "false", "no", "off"}


def _secondary_mode(environment: Mapping[str, str], *, battery: Any) -> str:
    selected = (
        battery._environment_setting(environment, "FRIDAY_SECONDARY_LLM_MODE", "disabled").strip().casefold()
    )
    return selected if selected in {"shadow", "assist"} else "disabled"


def _native_endpoints(environment: Mapping[str, str], *, battery: Any) -> dict[str, str]:
    endpoints = battery._configured_model_endpoint_urls(environment)
    enabled = _secondary_enabled(environment, battery=battery)
    if enabled:
        endpoints["secondary"] = (
            battery._environment_setting(environment, "FRIDAY_SECONDARY_LLM_BASE_URL", "").strip().rstrip("/")
        )
    return battery._validated_relay_endpoints(endpoints, include_secondary=enabled)[0]


def _require_secondary_runtime(settings: Any, *, battery: Any) -> None:
    enabled = _secondary_enabled(os.environ, battery=battery)
    if settings.secondary_llm_enabled is not enabled:
        raise NativeError("native_secondary_mode_mismatch")
    if enabled and (
        settings.secondary_llm_mode != _secondary_mode(os.environ, battery=battery)
        or settings.secondary_llm_configured is not True
        or settings.secondary_llm_base_url != _native_endpoints(os.environ, battery=battery)["secondary"]
    ):
        raise NativeError("native_secondary_runtime_incomplete")


def _effective_runtime_hash(settings: Any, *, candidate_source_sha256: str, battery: Any) -> str:
    base = battery._runtime_hash(settings, candidate_source_sha256=candidate_source_sha256)
    if not settings.secondary_llm_enabled:
        return base
    fields = (
        "enabled",
        "mode",
        "base_url",
        "model",
        "api_key",
        "ca_file",
        "connect_timeout_sec",
        "read_timeout_sec",
        "call_budget_sec",
        "admission_timeout_sec",
        "health_interval_sec",
        "cooldown_sec",
        "max_context_tokens",
        "max_concurrency",
        "profile",
        "workloads",
        "allow_private_text",
        "document_map_mode",
    )
    secondary = {name: getattr(settings, "secondary_llm_" + name) for name in fields}
    secondary["admission_policy"] = {
        name: getattr(settings, name)
        for name in (
            "semantic_supervisor_mode",
            "semantic_supervisor_tasks",
            "semantic_supervisor_max_steps",
            "semantic_supervisor_max_review_rounds",
            "semantic_supervisor_timeout_sec",
            "semantic_supervisor_effect_mode",
        )
    }
    secondary["configured"] = settings.secondary_llm_configured
    from friday.secondary_brain.profiles import get_secondary_runtime_admission

    admission = get_secondary_runtime_admission(
        settings.secondary_llm_profile, mode=settings.secondary_llm_mode
    )
    secondary["profile_identity"] = (
        None
        if admission is None
        else {
            "admission": admission.kind.value,
            "profile_id": admission.profile.profile_id,
            "manifest_sha256": admission.profile.manifest_sha256,
            "ca_pin_sha256": admission.profile.gateway_ca_certificate_sha256,
            "max_output_tokens": admission.profile.max_output_tokens,
        }
    )
    ca_file = settings.secondary_llm_ca_file
    secondary["ca_sha256"] = _sha(_assets().capture_ca(Path(ca_file)).content) if ca_file else None
    # Only the resulting digest is emitted: credentials and certificate paths
    # remain part of the private, effective runtime identity.
    return _sha(
        _canonical({"schema": "friday.r10-native-secondary-runtime.v1", "base": base, "secondary": secondary})
    )


def _selected_ca(environment: Mapping[str, str], *, battery: Any):
    if not _secondary_enabled(environment, battery=battery):
        return None
    value = battery._environment_setting(environment, "FRIDAY_SECONDARY_LLM_CA_FILE", "").strip()
    return _assets().capture_ca(Path(value)) if value else None


def _verify_worker_ca(request: Mapping[str, Any], evidence: Path, settings: Any) -> None:
    descriptor = request.get("secondary_ca")
    if descriptor is None:
        if settings.secondary_llm_enabled and settings.secondary_llm_ca_file:
            raise NativeError("native_secondary_ca_missing")
        return
    path = _assets().verify_staged(descriptor, evidence.parent)
    if settings.secondary_llm_enabled is not True or settings.secondary_llm_ca_file != str(path):
        raise NativeError("native_secondary_ca_binding_mismatch")


def _model_environment(env_file: Path) -> dict[str, str]:
    _, _, _, battery = _dependencies()
    values = battery._read_private_live_env_file(env_file)
    # Preserve the selected file exactly. Ambient model settings cannot fill
    # omissions, override it or change which configuration this receipt covers.
    _native_endpoints(values, battery=battery)
    return values


def _sandbox_command(
    *,
    snapshot: Path,
    site: Path,
    case_root: Path,
    relays: Path,
    request_path: Path,
    request_sha: str,
    probe: bool = False,
    secondary_ca: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    _, _, _, battery = _dependencies()
    if not battery.BWRAP_PATH.is_file() or not os.access(battery.BWRAP_PATH, os.X_OK):
        raise NativeError("native_filesystem_sandbox_unavailable")
    runtime = Path(sys.prefix).resolve()
    mounts = () if runtime.is_relative_to("/usr") else ("--ro-bind", str(runtime), str(runtime))
    # Add dependency directories explicitly: Python must not execute .pth or
    # sitecustomize before the first authority check. Bind the harness namespace
    # to the snapshot so an installed regular "tools" package cannot shadow it.
    dependency_sites = sorted(
        {str(Path(sysconfig.get_path(key)).resolve()) for key in ("purelib", "platlib")}
    )
    if any(
        not Path(path).is_dir()
        or not (Path(path).is_relative_to(runtime) or Path(path).is_relative_to("/usr"))
        for path in dependency_sites
    ):
        raise NativeError("native_dependency_site_outside_runtime")
    asset_mounts = ()
    if secondary_ca is not None:
        ca_path = _assets().verify_staged(secondary_ca, case_root / "evidence")
        asset_mounts = ("--ro-bind", str(ca_path), str(ca_path))
    entrypoint = "runtime_probe_main" if probe else "worker_main"
    bootstrap = (
        "import sys,types,importlib.machinery;"
        "sys.path[:0]=['/candidate-site'];"
        f"sys.path.extend({dependency_sites!r});"
        "t=types.ModuleType('tools');t.__path__=['/workspace/tools'];t.__package__='tools';"
        "t.__spec__=importlib.machinery.ModuleSpec('tools',loader=None,is_package=True);"
        "t.__spec__.submodule_search_locations=t.__path__;sys.modules['tools']=t;"
        "from tools import synthetic_live_battery as b;"
        "b._reject_preloaded_product_modules();"
        "from tools import release_1_0_native as n;"
        f"raise SystemExit(n.{entrypoint}({str(request_path)!r},{request_sha!r}))"
    )
    return (
        str(battery.BWRAP_PATH),
        "--die-with-parent",
        "--unshare-pid",
        "--unshare-net",
        "--tmpfs",
        "/",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/etc",
        "/etc",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/dev/shm",
        "--chmod",
        "0700",
        "/dev/shm",
        "--tmpfs",
        "/tmp",
        "--chmod",
        "0700",
        "/tmp",
        "--tmpfs",
        "/run",
        "--chmod",
        "0700",
        "/run",
        *mounts,
        "--ro-bind",
        str(site),
        "/candidate-site",
        "--ro-bind",
        str(snapshot),
        "/workspace",
        "--bind",
        str(case_root),
        str(case_root),
        *asset_mounts,
        "--ro-bind",
        str(relays),
        "/run/friday-relays",
        "--chdir",
        str(case_root / "home"),
        "--",
        sys.executable,
        "-I",
        "-S",
        "-B",
        "-c",
        bootstrap,
    )


def _validate_request(value: Any) -> dict[str, Any]:
    keys = {
        "protocol",
        "case_id",
        "run_id",
        "candidate_sha",
        "candidate_tree",
        "candidate_source_sha256",
        "candidate_files",
        "wheel_sha256",
        "installed_site_sha256",
        "suite_sha256",
        "model_environment_sha256",
        "expected_effective_runtime_sha256",
        "expected_worker_environment_sha256",
        "runtime_probe_receipt_sha256",
        "expected_runtime_binding_sha256",
        "fixture_sha256",
    }
    if isinstance(value, dict) and "secondary_ca" in value:
        keys.add("secondary_ca")
        try:
            _assets().validate_descriptor(value["secondary_ca"])
        except _assets().NativeAssetError:
            raise NativeError("native_worker_request_invalid") from None
    if not isinstance(value, dict) or set(value) != keys or value.get("protocol") != PROTOCOL:
        raise NativeError("native_worker_request_invalid")
    for key in (
        "candidate_source_sha256",
        "wheel_sha256",
        "installed_site_sha256",
        "suite_sha256",
        "model_environment_sha256",
        "expected_effective_runtime_sha256",
        "expected_worker_environment_sha256",
        "runtime_probe_receipt_sha256",
        "expected_runtime_binding_sha256",
        "fixture_sha256",
    ):
        if not isinstance(value[key], str) or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None:
            raise NativeError("native_worker_request_identity_invalid")
    for key in ("candidate_sha", "candidate_tree"):
        if not isinstance(value[key], str) or re.fullmatch(r"[0-9a-f]{40}", value[key]) is None:
            raise NativeError("native_worker_request_identity_invalid")
    from tools.release_1_0_live_cases import word_fixture

    if not isinstance(value["case_id"], str) or not isinstance(value["run_id"], str):
        raise NativeError("native_worker_request_identity_invalid")
    fixture = word_fixture(value["case_id"], value["run_id"])
    if fixture.sha256 != value["fixture_sha256"]:
        raise NativeError("native_fixture_identity_mismatch")
    paths = value["candidate_files"]
    if (
        not isinstance(paths, list)
        or not paths
        or not all(isinstance(path, str) for path in paths)
        or paths != sorted(set(paths))
    ):
        raise NativeError("native_candidate_inventory_invalid")
    if (
        _runtime_binding(
            value,
            effective_runtime_sha256=value["expected_effective_runtime_sha256"],
            worker_environment_sha256=value["expected_worker_environment_sha256"],
            probe_receipt_sha256=value["runtime_probe_receipt_sha256"],
        )
        != value["expected_runtime_binding_sha256"]
    ):
        raise NativeError("native_runtime_binding_identity_invalid")
    return value


def _validate_probe_request(value: Any) -> dict[str, Any]:
    keys = {
        "protocol",
        "probe_nonce",
        "case_id",
        "run_id",
        "candidate_sha",
        "candidate_tree",
        "candidate_source_sha256",
        "candidate_files",
        "wheel_sha256",
        "installed_site_sha256",
        "suite_sha256",
        "model_environment_sha256",
        "worker_environment_sha256",
    }
    if isinstance(value, dict) and "secondary_ca" in value:
        keys.add("secondary_ca")
        try:
            _assets().validate_descriptor(value["secondary_ca"])
        except _assets().NativeAssetError:
            raise NativeError("native_runtime_probe_request_invalid") from None
    if not isinstance(value, dict) or set(value) != keys or value.get("protocol") != PROBE_PROTOCOL:
        raise NativeError("native_runtime_probe_request_invalid")
    for key in (
        "candidate_source_sha256",
        "wheel_sha256",
        "installed_site_sha256",
        "suite_sha256",
        "model_environment_sha256",
        "worker_environment_sha256",
    ):
        if not isinstance(value[key], str) or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None:
            raise NativeError("native_runtime_probe_identity_invalid")
    for key in ("candidate_sha", "candidate_tree"):
        if not isinstance(value[key], str) or re.fullmatch(r"[0-9a-f]{40}", value[key]) is None:
            raise NativeError("native_runtime_probe_identity_invalid")
    if (
        not isinstance(value["case_id"], str)
        or not isinstance(value["run_id"], str)
        or not isinstance(value["probe_nonce"], str)
        or re.fullmatch(r"[0-9a-f]{32}", value["probe_nonce"]) is None
    ):
        raise NativeError("native_runtime_probe_identity_invalid")
    paths = value["candidate_files"]
    if (
        not isinstance(paths, list)
        or not paths
        or not all(isinstance(path, str) for path in paths)
        or paths != sorted(set(paths))
    ):
        raise NativeError("native_candidate_inventory_invalid")
    return value


def _verify_worker_candidate(request: Mapping[str, Any], *, gate: Any, battery: Any) -> tuple[Path, Path]:
    if Path("/workspace") != ROOT:
        raise NativeError("native_worker_workspace_invalid")
    home = Path(os.environ["FRIDAY_HOME"]).resolve()
    evidence = Path(os.environ["FRIDAY_LIVE_BATTERY_EVIDENCE"]).resolve()
    if (
        battery._candidate_source_digest(root=ROOT, relative_paths=request["candidate_files"])
        != request["candidate_source_sha256"]
    ):
        raise NativeError("native_source_changed_before_case")
    if _suite_digest(ROOT) != request["suite_sha256"]:
        raise NativeError("native_suite_changed_before_case")
    if gate._projection_digest(Path("/candidate-site")) != request["installed_site_sha256"]:
        raise NativeError("native_installed_site_changed")
    if not battery._home_has_only_process_scratch(home):
        raise NativeError("native_case_home_not_fresh")
    return home, evidence


def _observed_runtime_identity(settings: Any, request: Mapping[str, Any], *, battery: Any) -> dict[str, str]:
    effective = _effective_runtime_hash(
        settings, candidate_source_sha256=request["candidate_source_sha256"], battery=battery
    )
    environment = _worker_environment_digest(os.environ)
    return {
        "effective_runtime_sha256": effective,
        "worker_environment_sha256": environment,
        "runtime_binding_sha256": _runtime_binding(
            request,
            effective_runtime_sha256=effective,
            worker_environment_sha256=environment,
            probe_receipt_sha256=request["runtime_probe_receipt_sha256"],
        ),
    }


def _expected_runtime_identity(request: Mapping[str, Any]) -> dict[str, str]:
    return {
        "effective_runtime_sha256": request["expected_effective_runtime_sha256"],
        "worker_environment_sha256": request["expected_worker_environment_sha256"],
        "runtime_binding_sha256": request["expected_runtime_binding_sha256"],
    }


def _invalid_runtime_identity(request: Mapping[str, Any]) -> dict[str, str]:
    effective = "0" * 64
    environment = _worker_environment_digest(os.environ)
    return {
        "effective_runtime_sha256": effective,
        "worker_environment_sha256": environment,
        "runtime_binding_sha256": _runtime_binding(
            request,
            effective_runtime_sha256=effective,
            worker_environment_sha256=environment,
            probe_receipt_sha256=request["runtime_probe_receipt_sha256"],
        ),
    }


def _worker_task_ids() -> frozenset[int]:
    try:
        names = os.listdir("/proc/self/task")
        task_ids = frozenset(int(name) for name in names)
    except (OSError, TypeError, ValueError):
        raise NativeError("native_worker_thread_census_unavailable") from None
    if not task_ids or any(task_id <= 0 for task_id in task_ids):
        raise NativeError("native_worker_thread_census_invalid")
    return task_ids


def _require_worker_task_ids(expected: frozenset[int]) -> None:
    if _worker_task_ids() != expected:
        raise NativeError("native_unowned_worker_thread")


def _enter_worker_containment(battery: Any) -> dict[str, Any]:
    root_task_ids = _worker_task_ids()
    enabled = _secondary_enabled(os.environ, battery=battery)
    endpoints = _native_endpoints(os.environ, battery=battery)
    endpoint_settings = SimpleNamespace(
        llm_base_url=endpoints["model"],
        embeddings_base_url=endpoints["embedding"],
        rerank_base_url=endpoints["reranker"],
    )
    if enabled:
        endpoint_settings.secondary_llm_base_url = endpoints["secondary"]
        relay_owner = battery._UnixRelayLoopbackBridge.from_settings(
            endpoint_settings, include_secondary=True
        )
    else:
        relay_owner = battery._UnixRelayLoopbackBridge.from_settings(endpoint_settings)
    relay = relay_owner.__enter__()
    http_probe = None
    network_owner = None
    try:
        http_probe = (
            battery.LocalEndpointHttpProbe(endpoint_settings, include_secondary=True)
            if enabled
            else battery.LocalEndpointHttpProbe(endpoint_settings)
        )
        http_probe.install()
        network_owner = (
            battery.LocalEndpointNetworkGuard(tuple(endpoints.values()), relay_routes=relay.routes)
            if enabled
            else battery.LocalEndpointNetworkGuard.from_settings(endpoint_settings, relay_routes=relay.routes)
        )
        network = network_owner.__enter__()
        phase = SimpleNamespace(live=False)
        require_address = network._require_address

        def phase_require_address(sock: Any, address: Any) -> Any:
            if not phase.live:
                return network._deny()
            return require_address(sock, address)

        network._require_address = phase_require_address
    except BaseException:
        if http_probe is not None:
            http_probe.restore()
        relay_owner.__exit__(None, None, None)
        raise
    return {
        "relay_owner": relay_owner,
        "relay": relay,
        "http_probe": http_probe,
        "network_owner": network_owner,
        "network": network,
        "phase": phase,
        "root_task_ids": root_task_ids,
    }


def _leave_worker_containment(containment: Mapping[str, Any], *, restore_guards: bool) -> bool:
    cleanup_clear = True
    cleanup_deadline = time.monotonic() + 2.0
    try:
        relay = containment["relay"]
        try:
            with relay._lock:
                # __exit__ clears this registry. Retain the socket objects so
                # clearing the list cannot substitute for actually closing FDs.
                original_listeners = tuple(relay._listeners)
        finally:
            containment["relay_owner"].__exit__(None, None, None)
        with relay._lock:
            threads = tuple(relay._threads)
            connections = tuple(relay._connections)
            stopped = relay._stop.is_set()
            listeners = tuple(relay._listeners)
        if (
            not stopped
            or listeners
            or any(thread.is_alive() for thread in threads)
            or any(listener.fileno() != -1 for listener in original_listeners)
            or any(connection.fileno() != -1 for connection in connections)
        ):
            cleanup_clear = False
        else:
            expected = containment["root_task_ids"]
            joined_relay_ids = frozenset(
                thread.native_id for thread in threads if thread.native_id is not None
            )
            observed = _worker_task_ids()
            # Python join may precede removal of its native TID from /proc.
            # Settle only already-joined relay IDs, using the remainder of the
            # same aggregate shutdown budget. Unknown or missing IDs fail now;
            # final acceptance still requires exact kernel equality.
            while (
                observed != expected
                and expected.issubset(observed)
                and (observed - expected).issubset(joined_relay_ids)
            ):
                remaining = cleanup_deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.005, remaining))
                observed = _worker_task_ids()
            if observed != expected:
                cleanup_clear = False
    except BaseException:
        cleanup_clear = False
    if restore_guards:
        try:
            containment["http_probe"].restore()
        except BaseException:
            cleanup_clear = False
        try:
            containment["network_owner"].__exit__(None, None, None)
        except BaseException:
            cleanup_clear = False
    return cleanup_clear


def _runtime_probe(
    request: Mapping[str, Any],
    *,
    dependencies: tuple[Any, Any, Any, Any],
    network: Any,
    task_ids: frozenset[int],
) -> dict[str, Any]:
    _, gate, _, battery = dependencies
    home, evidence = _verify_worker_candidate(request, gate=gate, battery=battery)
    expected_environment = request["worker_environment_sha256"]
    if _worker_environment_digest(os.environ) != expected_environment:
        raise NativeError("native_runtime_probe_environment_mismatch")

    battery._assert_worker_product_authority("/candidate-site")
    gate._require_installed_wheel_imports(Path("/candidate-site"))
    from friday.config import load_settings

    settings = load_settings()
    battery._assert_worker_paths(settings, home, evidence)
    battery._assert_live_model_runtime(settings)
    _require_secondary_runtime(settings, battery=battery)
    _verify_worker_ca(request, evidence, settings)
    effective_runtime = _effective_runtime_hash(
        settings, candidate_source_sha256=request["candidate_source_sha256"], battery=battery
    )
    if _worker_environment_digest(os.environ) != expected_environment:
        raise NativeError("native_runtime_probe_environment_changed")
    _require_worker_task_ids(task_ids)
    if network.denied_attempts:
        raise NativeError("native_runtime_probe_network_attempted")
    if not battery._home_has_only_process_scratch(home):
        raise NativeError("native_runtime_probe_wrote_home")
    battery._assert_worker_product_authority("/candidate-site")
    gate._require_installed_wheel_imports(Path("/candidate-site"))
    return {
        "schema": PROBE_PROTOCOL,
        "status": "PASS",
        "identity": dict(request),
        "effective_runtime_sha256": effective_runtime,
        "worker_environment_sha256": expected_environment,
    }


def _secondary_observation(app: Any, *, expected_mode: str) -> dict[str, Any]:
    from friday.secondary_brain import SecondaryBrainScheduler

    scheduler = getattr(app.state, "secondary_brain", None)
    if not isinstance(scheduler, SecondaryBrainScheduler):
        raise NativeError("native_secondary_scheduler_missing")
    public = scheduler.public_status()
    observed = {key: public[key] for key in ("mode", "state", "configured", "available")}
    if not _secondary_observation_valid(observed) or observed["mode"] != expected_mode:
        raise NativeError("native_secondary_scheduler_unavailable")
    return observed


def _secondary_observation_valid(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == {"mode", "state", "configured", "available"}
        and type(value["mode"]) is str
        and type(value["state"]) is str
        and value["mode"] in {"shadow", "assist"}
        and value["state"] in {"probing", "healthy", "degraded", "cooldown"}
        and value["configured"] is True
        and type(value["available"]) is bool
    )


def _worker_contained(
    request: Mapping[str, Any],
    *,
    dependencies: tuple[Any, Any, Any, Any],
    containment: Mapping[str, Any],
    task_ids: frozenset[int],
) -> dict[str, Any]:
    _, gate, _, battery = dependencies
    from tools.release_1_0_live_cases import SignedSession, word_fixture

    home, evidence = _verify_worker_candidate(request, gate=gate, battery=battery)
    if _worker_environment_digest(os.environ) != request["expected_worker_environment_sha256"]:
        raise NativeError("native_runtime_environment_mismatch")
    battery._assert_worker_product_authority("/candidate-site")
    gate._require_installed_wheel_imports(Path("/candidate-site"))
    from friday.config import ensure_runtime_dirs, load_settings

    settings = load_settings()
    battery._assert_worker_paths(settings, home, evidence)
    battery._assert_live_model_runtime(settings)
    _require_secondary_runtime(settings, battery=battery)
    _verify_worker_ca(request, evidence, settings)
    runtime_before = _observed_runtime_identity(settings, request, battery=battery)
    if runtime_before != _expected_runtime_identity(request):
        raise NativeError("native_runtime_identity_mismatch")
    ensure_runtime_dirs(settings)
    _require_worker_task_ids(task_ids)

    http_probe = containment["http_probe"]
    network = containment["network"]
    if any(http_probe.counts.values()) or network.denied_attempts:
        raise NativeError("native_cold_runtime_network_attempted")

    fixture = word_fixture(request["case_id"], request["run_id"])
    from fastapi.testclient import TestClient

    from friday.server import create_app

    _require_worker_task_ids(task_ids)
    if any(http_probe.counts.values()) or network.denied_attempts:
        raise NativeError("native_cold_runtime_network_attempted")
    containment["phase"].live = True
    secondary_runtime = {}
    try:
        app = create_app(settings)
        with TestClient(app) as client:
            if settings.secondary_llm_enabled:
                secondary_runtime["startup"] = _secondary_observation(
                    app, expected_mode=settings.secondary_llm_mode
                )
            session = SignedSession(
                client,
                bridge_secret=settings.telegram_bridge_secret,
                chat_id=int(os.environ["FRIDAY_LIVE_BATTERY_MAIN_CHAT"]),
            )
            result = live_handlers()[request["case_id"]](session, app.state.storage, fixture)
            if settings.secondary_llm_enabled:
                secondary_runtime["after_case"] = _secondary_observation(
                    app, expected_mode=settings.secondary_llm_mode
                )
            battery._secure_write_json(
                evidence, {"request_identity": dict(request), "http": session.evidence}
            )
            for digest, payload in session.artifacts.items():
                battery._secure_write_bytes(evidence.parent / f"artifact-{digest}.bin", payload)
    finally:
        containment["phase"].live = False

    try:
        runtime_live_after = _observed_runtime_identity(settings, request, battery=battery)
        battery._assert_worker_paths(settings, home, evidence)
        battery._assert_live_model_runtime(settings)
        _require_secondary_runtime(settings, battery=battery)
        _verify_worker_ca(request, evidence, settings)
    except Exception:
        runtime_live_after = _invalid_runtime_identity(request)
        result["failure_codes"].append("native_runtime_identity_changed")

    runtime_reloaded_after: dict[str, str] | None
    try:
        settings_reloaded = load_settings()
        battery._assert_worker_paths(settings_reloaded, home, evidence)
        battery._assert_live_model_runtime(settings_reloaded)
        _require_secondary_runtime(settings_reloaded, battery=battery)
        _verify_worker_ca(request, evidence, settings_reloaded)
        runtime_reloaded_after = _observed_runtime_identity(settings_reloaded, request, battery=battery)
    except Exception:
        runtime_reloaded_after = None
        result["failure_codes"].extend(
            ["native_runtime_identity_changed", "native_runtime_identity_reload_failed"]
        )

    expected_runtime = _expected_runtime_identity(request)
    if runtime_live_after != expected_runtime or runtime_reloaded_after != expected_runtime:
        result["failure_codes"].append("native_runtime_identity_changed")
    try:
        _require_worker_task_ids(task_ids)
    except NativeError:
        result["failure_codes"].append("native_unowned_worker_thread")

    # These counters cover import, configuration, construction, startup, case,
    # shutdown and the two post-run identity observations under one guard.
    counts = dict(http_probe.counts)
    if counts["model"] < 1:
        result["failure_codes"].append("native_primary_http_not_observed")
    if counts["other"] or network.denied_attempts:
        result["failure_codes"].append("native_network_boundary_violation")
    battery._assert_worker_product_authority("/candidate-site")
    gate._require_installed_wheel_imports(Path("/candidate-site"))
    result["failure_codes"] = sorted(set(result["failure_codes"]))
    result["status"] = "FAIL" if result["failure_codes"] else "PASS"
    result.update(
        schema=PROTOCOL,
        layer="isolated-live",
        identity=dict(request),
        runtime_sha256=runtime_live_after["effective_runtime_sha256"],
        runtime_identity_before=runtime_before,
        runtime_identity_live_after=runtime_live_after,
        runtime_identity_reloaded_after=runtime_reloaded_after,
        python_version=sys.version,
        model_http_counts=counts,
        secondary_enabled=settings.secondary_llm_enabled,
        **({"secondary_runtime": secondary_runtime} if settings.secondary_llm_enabled else {}),
        root_class=(
            "harness"
            if any(code.startswith("native_") for code in result["failure_codes"])
            else "product"
            if result["failure_codes"]
            else None
        ),
    )
    return result


def _worker(request: Mapping[str, Any]) -> dict[str, Any]:
    from tools import synthetic_live_battery as battery

    battery._install_no_exec_seccomp()
    containment = _enter_worker_containment(battery)
    task_ids = _worker_task_ids()
    result: dict[str, Any] | None = None
    try:
        dependencies = _dependencies()
        _require_worker_task_ids(task_ids)
        result = _worker_contained(
            request,
            dependencies=dependencies,
            containment=containment,
            task_ids=task_ids,
        )
        return result
    finally:
        cleanup_clear = _leave_worker_containment(containment, restore_guards=True)
        if result is not None and not cleanup_clear:
            result.update(
                status="FAIL",
                failure_codes=sorted(
                    set(result.get("failure_codes", [])) | {"native_worker_relay_cleanup_uncertain"}
                ),
                root_class="environment",
            )


def _read_sealed_worker_request(request_name: str, request_sha: str, *, expected_name: str) -> Any:
    path = Path(request_name)
    expected_parent = Path(os.environ["FRIDAY_LIVE_BATTERY_EVIDENCE"]).parent
    if path.parent != expected_parent or path.name != expected_name:
        raise NativeError("native_worker_request_path_invalid")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or not 0 < before.st_size <= 1 << 20
        ):
            raise NativeError("native_worker_request_metadata_invalid")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read((1 << 20) + 1)
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise NativeError("native_worker_request_changed")
    finally:
        os.close(fd)
    if not raw or len(raw) > 1 << 20 or _sha(raw) != request_sha:
        raise NativeError("native_worker_request_digest_invalid")
    return json.loads(raw)


def _sealed_file_stamp(path: Path, expected: bytes) -> tuple[int, ...] | None:
    """Return stable metadata only when one private regular file has exact bounded bytes."""

    if not expected or len(expected) > 1 << 20:
        return None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        )
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_size != len(expected)
        ):
            return None
        chunks: list[bytes] = []
        remaining = len(expected) + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        path_after = path.lstat()
    except OSError:
        return None
    finally:
        os.close(descriptor)
    stamp = (
        before.st_dev,
        before.st_ino,
        before.st_uid,
        stat.S_IMODE(before.st_mode),
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_stamp = (
        after.st_dev,
        after.st_ino,
        after.st_uid,
        stat.S_IMODE(after.st_mode),
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    path_stamp = (
        path_after.st_dev,
        path_after.st_ino,
        path_after.st_uid,
        stat.S_IMODE(path_after.st_mode),
        path_after.st_nlink,
        path_after.st_size,
        path_after.st_mtime_ns,
        path_after.st_ctime_ns,
    )
    if b"".join(chunks) != expected or after_stamp != stamp or path_stamp != stamp:
        return None
    return stamp


def _host_relays_cleanup_clear(
    owner: Any,
    directory: Path,
    relays: Sequence[Any],
) -> bool:
    try:
        if owner.directory is not None or owner._temporary is not None or owner._relays or directory.exists():
            return False
        for relay in relays:
            with relay._lock:
                threads = tuple(relay._threads)
                connections = tuple(relay._connections)
                listener = relay._listener
                stopped = relay._stop.is_set()
            if (
                not stopped
                or listener is not None
                or relay.socket_path.exists()
                or any(thread.is_alive() for thread in threads)
                or any(connection.fileno() != -1 for connection in connections)
            ):
                return False
    except (AttributeError, OSError, RuntimeError, TypeError):
        return False
    return True


def _install_bounded_host_relay_connect(relays: Sequence[Any], *, deadline: float) -> None:
    for relay in relays:
        targets = tuple(relay._targets)

        def bounded_connect(*, owned_relay: Any = relay, owned_targets: tuple[Any, ...] = targets):
            last_error: OSError | None = None
            for family, sockaddr in owned_targets:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("native_host_relay_connect_deadline")
                upstream = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
                owned_relay._track(upstream)
                try:
                    upstream.settimeout(min(1.0, remaining))
                    upstream.connect(sockaddr)
                    if owned_relay._stop.is_set():
                        raise OSError("native_host_relay_stopping")
                    upstream.settimeout(None)
                    return upstream
                except OSError as exc:
                    last_error = exc
                    upstream.close()
                    owned_relay._untrack(upstream)
            if last_error is not None:
                raise last_error
            raise OSError("native_host_relay_endpoint_unresolved")

        relay._connect_upstream = bounded_connect


def runtime_probe_main(request_name: str, request_sha: str) -> int:
    from tools import synthetic_live_battery as battery

    sink = battery._BoundedTextSink(battery.MAX_WORKER_LOG_BYTES)
    request = None
    try:
        battery._install_no_exec_seccomp()
        network_owner = battery.LocalEndpointNetworkGuard(())
        network = network_owner.__enter__()
        task_ids = _worker_task_ids()
        dependencies = _dependencies()
        _require_worker_task_ids(task_ids)
        lifecycle = dependencies[0]
        lifecycle._unblock_worker_control_signals()
        request = _validate_probe_request(
            _read_sealed_worker_request(request_name, request_sha, expected_name="runtime-probe-request.json")
        )
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            report = _runtime_probe(
                request,
                dependencies=dependencies,
                network=network,
                task_ids=task_ids,
            )
    except Exception as exc:
        # Retain setup diagnostics only in the bounded private probe log.
        traceback.print_exc(file=sink)
        code = _failure_code(exc) if isinstance(exc, NativeError) else "native_runtime_probe_exception"
        report = {
            "schema": PROBE_PROTOCOL,
            "status": "FAIL",
            "failure_codes": [code],
            "error_type": type(exc).__name__,
        }
        if request is not None:
            report["identity"] = request
    if sink.getvalue():
        log = Path(os.environ["FRIDAY_LIVE_BATTERY_EVIDENCE"]).parent / "runtime-probe.log"
        battery._secure_write_bytes(log, sink.getvalue())
    if sink.truncated:
        report = {
            "schema": PROBE_PROTOCOL,
            "status": "FAIL",
            "failure_codes": ["native_runtime_probe_log_oversized"],
        }
        if request is not None:
            report["identity"] = request
    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


def worker_main(request_name: str, request_sha: str) -> int:
    from tools import synthetic_live_battery as battery

    # A bounded sink owns application stdout/stderr before any runtime starts.
    sink = battery._BoundedTextSink(battery.MAX_WORKER_LOG_BYTES)
    report: dict[str, Any]
    request = None
    containment = None
    try:
        request = _validate_request(
            _read_sealed_worker_request(request_name, request_sha, expected_name="worker-request.json")
        )
        if _worker_environment_digest(os.environ) != request["expected_worker_environment_sha256"]:
            raise NativeError("native_runtime_environment_mismatch")
        battery._install_no_exec_seccomp()
        containment = _enter_worker_containment(battery)
        task_ids = _worker_task_ids()
        dependencies = _dependencies()
        _require_worker_task_ids(task_ids)
        dependencies[0]._unblock_worker_control_signals()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            report = _worker_contained(
                request,
                dependencies=dependencies,
                containment=containment,
                task_ids=task_ids,
            )
    except Exception as exc:
        # Distinguish an unexecuted worker/setup failure from a case's observed
        # product verdict, without promoting one root to many product defects.
        code = _failure_code(exc) if isinstance(exc, NativeError) else "native_worker_exception"
        report = {
            "schema": PROTOCOL,
            "status": "NOT_RUN",
            "root_class": "harness",
            "failure_codes": [code],
            "error_type": type(exc).__name__,
        }
        if request is not None:
            report.update(
                identity=request, id=request["case_id"], requested_layer="isolated-live", layer="not-started"
            )
    finally:
        if containment is not None and not _leave_worker_containment(containment, restore_guards=False):
            report.update(
                status=("FAIL" if "runtime_identity_before" in report else "NOT_RUN"),
                root_class="environment",
                failure_codes=sorted(
                    set(report.get("failure_codes", [])) | {"native_worker_relay_cleanup_uncertain"}
                ),
            )
    if sink.getvalue():
        log = Path(os.environ["FRIDAY_LIVE_BATTERY_EVIDENCE"]).parent / "runtime.log"
        battery._secure_write_bytes(log, sink.getvalue())
    if sink.truncated:
        report.update(status="FAIL", root_class="harness", failure_codes=["native_runtime_log_oversized"])
    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


def _runtime_probe_result_valid(result: Any, request: Mapping[str, Any]) -> bool:
    return bool(
        isinstance(result, dict)
        and set(result)
        == {
            "schema",
            "status",
            "identity",
            "effective_runtime_sha256",
            "worker_environment_sha256",
        }
        and result.get("schema") == PROBE_PROTOCOL
        and result.get("status") == "PASS"
        and result.get("identity") == request
        and isinstance(result.get("effective_runtime_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", result["effective_runtime_sha256"])
        and result.get("worker_environment_sha256") == request["worker_environment_sha256"]
    )


def _runtime_identity_valid(value: Any, request: Mapping[str, Any]) -> bool:
    if not (
        isinstance(value, dict)
        and set(value)
        == {
            "effective_runtime_sha256",
            "worker_environment_sha256",
            "runtime_binding_sha256",
        }
        and all(
            isinstance(value.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", value[key]) is not None
            for key in value
        )
    ):
        return False
    return value["runtime_binding_sha256"] == _runtime_binding(
        request,
        effective_runtime_sha256=value["effective_runtime_sha256"],
        worker_environment_sha256=value["worker_environment_sha256"],
        probe_receipt_sha256=request["runtime_probe_receipt_sha256"],
    )


def _worker_result_valid(
    result: Any,
    request: Mapping[str, Any],
    *,
    expected_secondary: bool = False,
    expected_secondary_mode: str | None = None,
) -> bool:
    if type(expected_secondary) is not bool:
        return False
    if not isinstance(result, dict) or result.get("schema") != PROTOCOL or result.get("identity") != request:
        return False
    if result.get("id") != request["case_id"] or result.get("status") not in {"PASS", "FAIL", "NOT_RUN"}:
        return False
    codes = result.get("failure_codes")
    if not isinstance(codes, list) or any(
        not isinstance(code, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code) is None for code in codes
    ):
        return False
    if codes != sorted(set(codes)):
        return False
    if result["status"] == "NOT_RUN":
        return bool(
            set(result)
            == {
                "schema",
                "status",
                "root_class",
                "failure_codes",
                "error_type",
                "identity",
                "id",
                "requested_layer",
                "layer",
            }
            and codes
            and result.get("requested_layer") == "isolated-live"
            and result.get("layer") == "not-started"
            and result.get("root_class") in {"harness", "environment"}
            and isinstance(result.get("error_type"), str)
        )
    expected_keys = {
        "id",
        "status",
        "failure_codes",
        "attempt",
        "chat_submissions",
        "duration_ms",
        "observed_safe",
        "schema",
        "layer",
        "identity",
        "runtime_sha256",
        "runtime_identity_before",
        "runtime_identity_live_after",
        "runtime_identity_reloaded_after",
        "python_version",
        "model_http_counts",
        "secondary_enabled",
        "root_class",
    }
    if expected_secondary:
        if expected_secondary_mode not in {"shadow", "assist"}:
            return False
        expected_keys.add("secondary_runtime")
        observations = result.get("secondary_runtime")
        if (
            not isinstance(observations, dict)
            or set(observations) != {"startup", "after_case"}
            or any(
                not _secondary_observation_valid(v) or v["mode"] != expected_secondary_mode
                for v in observations.values()
            )
        ):
            return False
    if set(result) != expected_keys:
        return False
    if (
        result.get("layer") != "isolated-live"
        or type(result.get("attempt")) is not int
        or result["attempt"] != 1
        or type(result.get("duration_ms")) is not int
        or result["duration_ms"] < 0
        or not isinstance(result.get("python_version"), str)
        or result.get("root_class") not in {None, "harness", "product", "environment"}
    ):
        return False
    before = result.get("runtime_identity_before")
    live_after = result.get("runtime_identity_live_after")
    reloaded_after = result.get("runtime_identity_reloaded_after")
    expected = _expected_runtime_identity(request)
    if (
        not _runtime_identity_valid(before, request)
        or not _runtime_identity_valid(live_after, request)
        or (reloaded_after is not None and not _runtime_identity_valid(reloaded_after, request))
        or before != expected
        or result.get("runtime_sha256") != live_after["effective_runtime_sha256"]
    ):
        return False
    runtime_changed = live_after != expected or reloaded_after != expected
    if ("native_runtime_identity_changed" in codes) is not runtime_changed:
        return False
    reload_failed = reloaded_after is None
    if ("native_runtime_identity_reload_failed" in codes) is not reload_failed:
        return False
    if runtime_changed and result["status"] != "FAIL":
        return False
    observed = result.get("observed_safe")
    counts = result.get("model_http_counts")
    artifact_present = bool(
        isinstance(observed, dict)
        and set(observed) == {"fixture_sha256", "artifact_sha256", "artifact_size_bytes"}
        and isinstance(observed.get("artifact_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", observed["artifact_sha256"])
        and type(observed.get("artifact_size_bytes")) is int
        and observed["artifact_size_bytes"] > 0
    )
    if (
        type(result.get("chat_submissions")) is not int
        or result["chat_submissions"] not in {0, 1}
        or not isinstance(observed, dict)
        or set(observed)
        not in (
            {"fixture_sha256"},
            {"fixture_sha256", "artifact_sha256", "artifact_size_bytes"},
        )
        or (
            set(observed) == {"fixture_sha256", "artifact_sha256", "artifact_size_bytes"}
            and not artifact_present
        )
        or observed.get("fixture_sha256") != request["fixture_sha256"]
        or not isinstance(counts, dict)
        or set(counts)
        != (
            {"model", "embedding", "reranker", "other"}
            | ({"secondary", "secondary_health"} if expected_secondary else set())
        )
        or any(type(counts[key]) is not int or counts[key] < 0 for key in counts)
        or result.get("python_version") != sys.version
        or result.get("secondary_enabled") is not expected_secondary
    ):
        return False
    if result["status"] == "FAIL":
        return bool(codes) and result.get("root_class") in {"harness", "product", "environment"}
    return bool(
        not codes
        and result.get("chat_submissions") == 1
        and artifact_present
        and counts["model"] > 0
        and counts["other"] == 0
        and result.get("secondary_enabled") is expected_secondary
        and result.get("root_class") is None
        and live_after == expected
        and reloaded_after == expected
    )


def _run_case(
    *,
    spec: Mapping[str, Any],
    index: int,
    run_id: str,
    source: Path,
    snapshot: Any,
    site: Path,
    run_dir: Path,
    model_env: Mapping[str, str],
    identity: Mapping[str, str],
    signals: Any,
    captured_ca: Any = None,
) -> dict[str, Any]:
    lifecycle, gate, _, battery = _dependencies()
    from tools.release_1_0_live_cases import MAX_ARTIFACT_BYTES, word_fixture

    case_dir = run_dir / f"case-{index:03d}"
    case_dir.mkdir(mode=0o700)
    home = case_dir / "home"
    home.mkdir(mode=0o700)
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir(mode=0o700)
    evidence = evidence_dir / "observed.json"
    context = battery.PassContext(
        "R10",
        spec["id"],
        index,
        0,
        battery.FIXED_CLOCK,
        battery.FIXED_TIMEZONE,
        identity["suite_sha256"],
        home,
        evidence,
    )
    environment = battery._worker_environment(model_env, context)
    environment["FRIDAY_QUALITY_GATE_INSTALLED_SITE"] = "/candidate-site"
    environment["FRIDAY_LIVE_BATTERY_PRODUCT_ROOT"] = "/candidate-site"
    environment["FRIDAY_TELEGRAM_OWNER_CHAT_IDS"] = environment["FRIDAY_LIVE_BATTERY_MAIN_CHAT"]
    # Keep execve and CPython's locale-coercion view identical.  The complete
    # mapping below, including secrets, is hashed but never serialized publicly.
    environment["LANG"] = "C.UTF-8"
    environment["LC_ALL"] = "C.UTF-8"
    environment["PWD"] = str(home)
    enabled = _secondary_enabled(model_env, battery=battery)
    if captured_ca is None:
        captured_ca = _selected_ca(model_env, battery=battery)
    secondary_ca = None
    if captured_ca is not None:
        selected_ca_path = battery._environment_setting(model_env, "FRIDAY_SECONDARY_LLM_CA_FILE", "").strip()
        if (
            not enabled
            or not selected_ca_path
            or captured_ca.source_path != Path(os.path.abspath(selected_ca_path))
        ):
            raise NativeError("native_secondary_ca_binding_mismatch")
        _assets().check_source(captured_ca)
        secondary_ca = _assets().stage_ca(captured_ca, evidence_dir)
        environment["FRIDAY_SECONDARY_LLM_CA_FILE"] = secondary_ca["staged_path"]
        environment.pop("JERICHO_SECONDARY_LLM_CA_FILE", None)
    for key in battery._PROCESS_SCRATCH_PATHS:
        Path(environment[key]).mkdir(parents=True, mode=0o700)
    fixture = word_fixture(spec["id"], run_id)
    environment_sha = _worker_environment_digest(environment)
    probe_request = {
        "protocol": PROBE_PROTOCOL,
        "probe_nonce": uuid.uuid4().hex,
        "case_id": spec["id"],
        "run_id": run_id,
        **identity,
        "candidate_files": list(snapshot.relative_paths),
        "worker_environment_sha256": environment_sha,
        **({"secondary_ca": secondary_ca} if secondary_ca is not None else {}),
    }
    probe_path = evidence_dir / "runtime-probe-request.json"
    probe_raw = _canonical(probe_request)
    battery._secure_write_bytes(probe_path, probe_raw)
    probe_stamp = _sealed_file_stamp(probe_path, probe_raw)
    if probe_stamp is None:
        raise NativeError("native_runtime_probe_request_seal_failed")
    empty_relays = case_dir / "runtime-probe-relays"
    empty_relays.mkdir(mode=0o700)
    started = datetime.now(UTC).isoformat()
    clock = time.monotonic()
    deadline = clock + spec["timeout_s"]
    result: dict[str, Any]
    cleanup_clear = False
    request: dict[str, Any] = probe_request
    relay_owner: Any | None = None
    relay_directory: Path | None = None
    owned_relays: tuple[Any, ...] = ()
    relay_cleanup_clear = True

    def remaining_timeout() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NativeError("native_case_deadline_exhausted")
        return remaining

    def finish(value: dict[str, Any]) -> dict[str, Any]:
        if secondary_ca is not None and cleanup_clear:
            try:
                _assets().check_source(captured_ca)
                _assets().verify_staged(secondary_ca, evidence_dir)
            except _assets().NativeAssetError:
                value.update(
                    status="FAIL",
                    failure_codes=sorted(
                        set(value.get("failure_codes", []) + ["native_secondary_ca_changed"])
                    ),
                    root_class="harness",
                )
        value.update(
            id=spec["id"],
            attempt=1,
            started_at=started,
            ended_at=datetime.now(UTC).isoformat(),
            duration_ms=round((time.monotonic() - clock) * 1000),
            process_cleanup_clear=cleanup_clear,
            go_emitted=False,
        )
        receipt_path = (
            evidence_dir / "case-receipt.json"
            if cleanup_clear
            else run_dir / f"case-{index:03d}-receipt.json"
        )
        battery._secure_write_json(receipt_path, value)
        return value

    try:
        if _sha(_canonical(dict(model_env))) != identity["model_environment_sha256"]:
            cleanup_clear = True
            return finish(
                {
                    "identity": probe_request,
                    "status": "NOT_RUN",
                    "failure_codes": ["native_model_environment_changed"],
                    "root_class": "harness",
                }
            )
        with lifecycle._private_worker_log(evidence_dir / "runtime-probe-process.log") as probe_log:
            probe_command = _sandbox_command(
                snapshot=snapshot.root,
                site=site,
                case_root=case_dir,
                relays=empty_relays,
                request_path=probe_path,
                request_sha=_sha(probe_raw),
                probe=True,
                **({"secondary_ca": secondary_ca} if secondary_ca is not None else {}),
            )
            probe_outcome = lifecycle._run_worker_process(
                probe_command,
                environment=environment,
                private_log=probe_log,
                controller_signal_handlers=signals,
                timeout_sec=remaining_timeout(),
                stdout_limit_bytes=1 << 20,
            )
        cleanup_clear = probe_outcome.cleanup_clear
        battery._secure_write_bytes(
            run_dir / f"case-{index:03d}-runtime-probe-response.json", probe_outcome.stdout
        )
        probe_failures = list(probe_outcome.cleanup_failure_codes)
        if probe_outcome.returncode:
            probe_failures.append("native_runtime_probe_exit_nonzero")
        try:
            probe_result = json.loads(probe_outcome.stdout)
        except (ValueError, UnicodeError):
            probe_result = None
        if not _runtime_probe_result_valid(probe_result, probe_request):
            probe_failures.append("native_runtime_probe_response_invalid")
        if cleanup_clear and _sealed_file_stamp(probe_path, probe_raw) != probe_stamp:
            probe_failures.append("native_runtime_probe_request_changed")
        if (
            _sha(_canonical(dict(model_env))) != identity["model_environment_sha256"]
            or _worker_environment_digest(environment) != environment_sha
        ):
            probe_failures.append("native_model_environment_changed")
        if (
            battery._candidate_source_digest(root=source, relative_paths=snapshot.relative_paths)
            != identity["candidate_source_sha256"]
        ):
            probe_failures.append("native_candidate_changed")
        if gate._projection_digest(site) != identity["installed_site_sha256"]:
            probe_failures.append("native_installed_site_changed")
        if not battery._home_has_only_process_scratch(home):
            probe_failures.append("native_runtime_probe_wrote_home")
        if probe_failures or not cleanup_clear:
            if not cleanup_clear:
                probe_failures.append("native_runtime_probe_cleanup_uncertain")
            return finish(
                {
                    "identity": probe_request,
                    "status": "NOT_RUN" if cleanup_clear else "FAIL",
                    "failure_codes": sorted(set(probe_failures)),
                    "root_class": "harness" if cleanup_clear else "environment",
                }
            )

        assert isinstance(probe_result, dict)
        effective_runtime_sha = probe_result["effective_runtime_sha256"]
        probe_receipt_sha = _sha(probe_outcome.stdout)
        if secondary_ca is not None:
            _assets().check_source(captured_ca)
            _assets().verify_staged(secondary_ca, evidence_dir)
        expected_binding_sha = _runtime_binding(
            probe_request,
            effective_runtime_sha256=effective_runtime_sha,
            worker_environment_sha256=environment_sha,
            probe_receipt_sha256=probe_receipt_sha,
        )
        request = {
            "protocol": PROTOCOL,
            "case_id": spec["id"],
            "run_id": run_id,
            **identity,
            "candidate_files": list(snapshot.relative_paths),
            "expected_effective_runtime_sha256": effective_runtime_sha,
            "expected_worker_environment_sha256": environment_sha,
            "runtime_probe_receipt_sha256": probe_receipt_sha,
            "expected_runtime_binding_sha256": expected_binding_sha,
            "fixture_sha256": fixture.sha256,
            **({"secondary_ca": secondary_ca} if secondary_ca is not None else {}),
        }
        request_path = evidence_dir / "worker-request.json"
        raw = _canonical(request)
        battery._secure_write_bytes(request_path, raw)
        request_stamp = _sealed_file_stamp(request_path, raw)
        if request_stamp is None:
            raise NativeError("native_worker_request_seal_failed")
        endpoints = _native_endpoints(environment, battery=battery)
        timeout = remaining_timeout()
        cleanup_clear = False
        relay_cleanup_clear = False
        relay_owner = (
            battery._HostEndpointRelays(endpoints, include_secondary=True)
            if enabled
            else battery._HostEndpointRelays(endpoints)
        )
        try:
            with (
                relay_owner as relays,
                lifecycle._private_worker_log(evidence_dir / "process.log") as log,
            ):
                if relays.directory is None:
                    raise NativeError("native_relay_unavailable")
                relay_directory = relays.directory
                owned_relays = tuple(relays._relays)
                _install_bounded_host_relay_connect(owned_relays, deadline=deadline)
                command = _sandbox_command(
                    snapshot=snapshot.root,
                    site=site,
                    case_root=case_dir,
                    relays=relays.directory,
                    request_path=request_path,
                    request_sha=_sha(raw),
                    **({"secondary_ca": secondary_ca} if secondary_ca is not None else {}),
                )
                outcome = lifecycle._run_worker_process(
                    command,
                    environment=environment,
                    private_log=log,
                    controller_signal_handlers=signals,
                    timeout_sec=min(timeout, remaining_timeout()),
                    stdout_limit_bytes=1 << 20,
                )
        finally:
            relay_cleanup_clear = bool(
                relay_directory is not None
                and _host_relays_cleanup_clear(relay_owner, relay_directory, owned_relays)
            )
        cleanup_clear = outcome.cleanup_clear and relay_cleanup_clear
        battery._secure_write_bytes(run_dir / f"case-{index:03d}-worker-response.json", outcome.stdout)
        try:
            result = json.loads(outcome.stdout)
        except (ValueError, UnicodeError):
            result = {
                "status": "NOT_RUN",
                "failure_codes": ["native_worker_response_invalid"],
                "root_class": "harness",
            }
        if not isinstance(result, dict):
            result = {
                "status": "NOT_RUN",
                "failure_codes": ["native_worker_response_invalid"],
                "root_class": "harness",
            }
        if not _worker_result_valid(
            result,
            request,
            expected_secondary=enabled,
            expected_secondary_mode=_secondary_mode(model_env, battery=battery),
        ):
            result = {
                "identity": request,
                "id": spec["id"],
                "status": "NOT_RUN",
                "failure_codes": ["native_worker_identity_missing"],
                "root_class": "harness",
            }
        if cleanup_clear and result["status"] == "PASS":
            # A hash in worker JSON is not a retained artifact. Verify the
            # actual source and output bytes after the process has stopped.
            observed = result["observed_safe"]
            for digest, size in (
                (fixture.sha256, len(fixture.content)),
                (observed["artifact_sha256"], observed["artifact_size_bytes"]),
            ):
                try:
                    actual = gate._bounded_file_identity(
                        evidence_dir / f"artifact-{digest}.bin",
                        maximum=MAX_ARTIFACT_BYTES,
                        executable=False,
                        owner_uid=os.getuid(),
                        single_link=True,
                    )
                    valid_artifact = (
                        actual["sha256"] == digest
                        and actual["size_bytes"] == size
                        and actual["mode"] == "0600"
                    )
                except (OSError, RuntimeError):
                    valid_artifact = False
                if not valid_artifact:
                    result.update(
                        status="FAIL",
                        failure_codes=["native_retained_artifact_invalid"],
                        root_class="harness",
                    )
                    break
        process_failures = list(outcome.cleanup_failure_codes)
        if outcome.returncode:
            process_failures.append("native_worker_exit_nonzero")
        if not cleanup_clear:
            process_failures.append("native_worker_cleanup_uncertain")
        if not relay_cleanup_clear:
            process_failures.append("native_host_relay_cleanup_uncertain")
        if process_failures or not cleanup_clear:
            result.update(
                status="FAIL", failure_codes=sorted(set(process_failures)), root_class="environment"
            )
        if (
            battery._candidate_source_digest(root=source, relative_paths=snapshot.relative_paths)
            != identity["candidate_source_sha256"]
        ):
            result.update(status="FAIL", failure_codes=["native_candidate_changed"], root_class="harness")
        if cleanup_clear and _sealed_file_stamp(request_path, raw) != request_stamp:
            result.update(status="FAIL", failure_codes=["native_request_changed"], root_class="harness")
        if gate._projection_digest(site) != identity["installed_site_sha256"]:
            result.update(
                status="FAIL", failure_codes=["native_installed_site_changed"], root_class="harness"
            )
        if (
            _sha(_canonical(dict(model_env))) != identity["model_environment_sha256"]
            or _worker_environment_digest(environment) != environment_sha
        ):
            result.update(
                status="FAIL",
                failure_codes=["native_model_environment_changed"],
                root_class="harness",
            )
        return finish(result)
    except lifecycle.ControllerSignal as exc:
        cleanup_clear = exc.worker_cleanup_clear is True and relay_cleanup_clear
        relay_failures = [] if relay_cleanup_clear else ["native_host_relay_cleanup_uncertain"]
        battery._secure_write_json(
            evidence_dir / "case-receipt.json"
            if cleanup_clear
            else run_dir / f"case-{index:03d}-receipt.json",
            {
                "id": spec["id"],
                "identity": request,
                "status": "NOT_RUN",
                "attempt": 1,
                "failure_codes": [
                    "native_controller_interrupted",
                    *exc.worker_cleanup_failure_codes,
                    *relay_failures,
                ],
                "root_class": "environment",
                "process_cleanup_clear": cleanup_clear,
                "duration_ms": round((time.monotonic() - clock) * 1000),
                "go_emitted": False,
            },
        )
        raise
    finally:
        # Evidence is retained. Remove the newly-created private runtime only
        # after the process boundary has positively established cleanup.
        if cleanup_clear:
            shutil.rmtree(home)


def _retained_evidence_index(
    run_dir: Path,
    selected: Sequence[str],
    results: Sequence[Mapping[str, Any]],
    *,
    retained_wheel: Path | None = None,
) -> dict[str, Any]:
    """Bind retained bytes, preserving raw worker and final controller receipts.

    This index is transport evidence. Its hashes do not validate a worker's
    oracle or authorize execution credit; the acceptance reader must do that.
    """
    from tools import quality_gate as gate
    from tools.release_1_0_live_cases import MAX_ARTIFACT_BYTES

    def reference(path: Path, *, maximum: int = 1 << 20, allow_empty: bool = False) -> dict[str, Any] | None:
        try:
            before = path.lstat()
        except FileNotFoundError:
            return None
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise NativeError("native_evidence_file_invalid")
        if allow_empty and before.st_size == 0:
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
            ):
                raise NativeError("native_evidence_file_invalid")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                if (
                    gate._stat_identity(os.fstat(fd)) != gate._stat_identity(before)
                    or os.read(fd, 1) != b""
                    or gate._stat_identity(os.fstat(fd)) != gate._stat_identity(before)
                    or gate._stat_identity(path.lstat()) != gate._stat_identity(before)
                ):
                    raise NativeError("native_evidence_file_invalid")
            finally:
                os.close(fd)
            return {"path": path.relative_to(run_dir).as_posix(), "sha256": _sha(b""), "size_bytes": 0}
        measured = gate._bounded_file_identity(
            path, maximum=maximum, executable=False, owner_uid=os.getuid(), single_link=True
        )
        if measured["mode"] != "0600":
            raise NativeError("native_evidence_file_invalid")
        return {
            "path": path.relative_to(run_dir).as_posix(),
            "sha256": measured["sha256"],
            "size_bytes": measured["size_bytes"],
        }

    if len(selected) != len(set(selected)) or len(results) != len(selected):
        raise NativeError("native_evidence_selection_invalid")
    by_id = {row["id"]: row for row in results}
    if len(by_id) != len(results) or set(by_id) != set(selected):
        raise NativeError("native_evidence_selection_invalid")
    identity = reference(run_dir / "frozen-identity.json")
    wheel = reference(retained_wheel, maximum=64 << 20) if retained_wheel is not None else None
    cases = []
    for index, case_id in enumerate(selected, 1):
        result = by_id[case_id]
        evidence = run_dir / f"case-{index:03d}" / "evidence"
        clean = result.get("process_cleanup_clear") is True
        receipt = reference(
            evidence / "case-receipt.json" if clean else run_dir / f"case-{index:03d}-receipt.json"
        )
        # Do not traverse an uncertain worker's evidence directory. The root
        # receipt and explicit missing references retain the failed boundary.
        files = {
            "receipt": receipt,
            "probe_request": reference(evidence / "runtime-probe-request.json") if clean else None,
            "probe_response": reference(
                run_dir / f"case-{index:03d}-runtime-probe-response.json",
                allow_empty=result["status"] != "PASS",
            )
            if clean
            else None,
            "worker_request": reference(evidence / "worker-request.json") if clean else None,
            "worker_response": reference(
                run_dir / f"case-{index:03d}-worker-response.json", allow_empty=result["status"] != "PASS"
            )
            if clean
            else None,
        }
        artifacts = []
        observed = result.get("observed_safe", {})
        if clean and isinstance(observed, dict):
            for key in ("fixture_sha256", "artifact_sha256"):
                digest = observed.get(key)
                if digest is None:
                    continue
                if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                    raise NativeError("native_evidence_artifact_invalid")
                item = reference(evidence / f"artifact-{digest}.bin", maximum=MAX_ARTIFACT_BYTES)
                if item is None or item["sha256"] != digest:
                    raise NativeError("native_evidence_artifact_invalid")
                if key == "artifact_sha256" and item["size_bytes"] != observed.get("artifact_size_bytes"):
                    raise NativeError("native_evidence_artifact_invalid")
                artifacts.append({"kind": key, **item})
        if result["status"] == "PASS" and (
            not clean
            or identity is None
            or wheel is None
            or any(value is None for value in files.values())
            or len(artifacts) != 2
        ):
            raise NativeError("native_evidence_pass_incomplete")
        cases.append({"id": case_id, "index": index, **files, "artifacts": artifacts})
    return {
        "schema": "friday.r10-native-evidence.v1",
        "selected_cases": list(selected),
        "identity": identity,
        "wheel": wheel,
        "cases": cases,
    }


def _native_context_parent(path: Path) -> int:
    """Open an existing private canonical parent without modifying its mode."""
    fd = None
    try:
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or path.parent.resolve(strict=True) / path.name != path
        ):
            raise NativeError("native_context_path_invalid")
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise NativeError("native_context_path_invalid")
        return fd
    except BaseException as exc:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if isinstance(exc, (OSError, RuntimeError)):
            raise NativeError("native_context_path_invalid") from exc
        raise


def _validate_native_context_destination(path: Path, run_dir: Path) -> None:
    if path.is_relative_to(ROOT.resolve()) or path.is_relative_to(run_dir.resolve()):
        raise NativeError("native_context_path_invalid")
    fd = _native_context_parent(path)
    try:
        for name in (path.name, path.name + ".sha256"):
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise NativeError("native_context_destination_exists")
    finally:
        os.close(fd)


def _check_native_context(path: Path, retained: tuple[tuple[bytes, tuple[int, int]], ...]) -> None:
    fd = _native_context_parent(path)
    try:
        for name, (expected, inode) in zip((path.name, path.name + ".sha256"), retained, strict=True):
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
            try:
                before = os.fstat(child)
                raw = bytearray()
                while len(raw) <= len(expected):
                    block = os.read(child, len(expected) + 1 - len(raw))
                    if not block:
                        break
                    raw.extend(block)
                after = os.fstat(child)
                linked = os.stat(name, dir_fd=fd, follow_symlinks=False)

                def stable(info):
                    return (
                        info.st_dev,
                        info.st_ino,
                        info.st_mode,
                        info.st_uid,
                        info.st_nlink,
                        info.st_size,
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                    )

                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_uid != os.getuid()
                    or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or (before.st_dev, before.st_ino) != inode
                    or stable(before) != stable(after)
                    or stable(after) != stable(linked)
                    or bytes(raw) != expected
                ):
                    raise NativeError("native_context_changed")
            finally:
                os.close(child)
    except (OSError, ValueError) as exc:
        raise NativeError("native_context_changed") from exc
    finally:
        os.close(fd)


def _publish_native_context(
    path: Path, context: Mapping[str, Any]
) -> tuple[tuple[bytes, tuple[int, int]], ...]:
    """Reserve both private outputs exclusively; never remove an unowned entry."""
    raw = _canonical(context)
    payloads = (raw, (_sha(raw) + "\n").encode())
    parent = _native_context_parent(path)
    descriptors = []
    owned = []
    retained = []
    try:
        for name in (path.name, path.name + ".sha256"):
            child = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
            descriptors.append(child)
            info = os.fstat(child)
            owned.append((name, child, (info.st_dev, info.st_ino)))
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise NativeError("native_context_publish_failed")
        for (_, child, inode), payload in zip(owned, payloads, strict=True):
            remaining = memoryview(payload)
            while remaining:
                written = os.write(child, remaining)
                if written <= 0:
                    raise NativeError("native_context_publish_failed")
                remaining = remaining[written:]
            os.fsync(child)
            retained.append((payload, inode))
        os.fsync(parent)
        _check_native_context(path, tuple(retained))
        return tuple(retained)
    except BaseException as exc:
        for name, _, inode in owned:
            try:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == inode:
                    os.unlink(name, dir_fd=parent)
            except OSError:
                pass
        if isinstance(exc, (OSError, NativeError)):
            raise NativeError("native_context_publish_failed") from exc
        raise
    finally:
        pending_error = sys.exc_info()[0] is not None
        close_error = None
        for descriptor in [*descriptors, parent]:
            try:
                os.close(descriptor)
            except OSError as exc:
                close_error = close_error or exc
        if close_error is not None and not pending_error:
            raise NativeError("native_context_publish_failed") from close_error


@dataclass(frozen=True, repr=False)
class _CertifiedWheel:
    """Exact-release wheel and the independently retained receipt that binds it."""

    path: Path
    sha256: str
    receipt_path: Path
    receipt_sha256: str
    version: str
    content: bytes


def _read_private_certified_file(
    path: Path,
    expected_sha256: str,
    *,
    maximum: int,
    run_dir: Path,
    invalid_code: str,
    digest_code: str,
) -> bytes:
    """Read one stable external 0600 artifact without changing its metadata."""
    from tools import quality_gate as gate

    descriptor = None
    try:
        lexical = Path(os.path.abspath(path))
        root = ROOT.resolve()
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or lexical != path
            or path.resolve(strict=True) != path
            or path.is_relative_to(root)
            or path.is_relative_to(run_dir)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        ):
            raise NativeError(invalid_code)
        parent = path.parent.lstat()
        before = path.lstat()
        if (
            path.parent.resolve(strict=True) != path.parent
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 0 < before.st_size <= maximum
        ):
            raise NativeError(invalid_code)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        opened = os.fstat(descriptor)
        if gate._stat_identity(opened) != gate._stat_identity(before):
            raise NativeError(invalid_code)
        raw = bytearray()
        while len(raw) <= maximum:
            block = os.read(descriptor, min(1 << 20, maximum + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        if (
            len(raw) != opened.st_size
            or gate._stat_identity(os.fstat(descriptor)) != gate._stat_identity(opened)
            or gate._stat_identity(path.lstat()) != gate._stat_identity(opened)
        ):
            raise NativeError(invalid_code)
    except NativeError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise NativeError(invalid_code) from exc
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
    payload = bytes(raw)
    if _sha(payload) != expected_sha256:
        raise NativeError(digest_code)
    return payload


def _canonical_json_object(raw: bytes, *, code: str) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate key")
        return value

    try:
        value = json.loads(raw, object_pairs_hook=unique_pairs)
        if not isinstance(value, dict) or raw != _canonical(value):
            raise ValueError("noncanonical JSON")
        return value
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise NativeError(code) from exc


def _wheel_version(raw: bytes, *, source: Path) -> str:
    try:
        project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        expected = project["version"]
        if (
            not isinstance(expected, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+!-]{0,79}", expected) is None
        ):
            raise ValueError("candidate version")
        with zipfile.ZipFile(io.BytesIO(raw)) as wheel:
            infos = wheel.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ValueError("duplicate wheel member")
            total = 0
            for info in infos:
                member = PurePosixPath(info.filename)
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                if (
                    not info.filename
                    or "\\" in info.filename
                    or "\x00" in info.filename
                    or member.is_absolute()
                    or any(part in {"", ".", ".."} for part in member.parts)
                    or info.flag_bits & 0x1
                    or stat.S_ISLNK(unix_mode)
                    or info.file_size > 64 << 20
                ):
                    raise ValueError("unsafe wheel member")
                total += info.file_size
            if total > 256 << 20:
                raise ValueError("oversized wheel")
            metadata = [
                info for info in infos if re.fullmatch(r"friday-[^/]+\.dist-info/METADATA", info.filename)
            ]
            if len(metadata) != 1 or metadata[0].file_size > 1 << 20:
                raise ValueError("wheel distribution identity")
            fields = {}
            for line in wheel.read(metadata[0]).decode("utf-8").splitlines():
                key, separator, value = line.partition(":")
                if separator and key in {"Name", "Version"} and key not in fields:
                    fields[key] = value.strip()
        if fields != {"Name": "friday", "Version": expected}:
            raise NativeError("native_certified_wheel_version_mismatch")
        normalized = re.sub(r"[^A-Za-z0-9.]+", "_", expected)
        if metadata[0].filename != f"friday-{normalized}.dist-info/METADATA":
            raise NativeError("native_certified_wheel_version_mismatch")
    except NativeError:
        raise
    except (KeyError, OSError, TypeError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
        raise NativeError("native_certified_wheel_invalid") from exc
    return expected


def _validate_certified_wheel(
    *,
    path: Path,
    sha256: str,
    receipt_path: Path,
    receipt_sha256: str,
    candidate_sha: str,
    base_sha: str | None,
    run_dir: Path,
) -> _CertifiedWheel:
    """Bind exact wheel bytes to one canonical, independently pinned gate receipt."""
    from tools import quality_gate as gate

    wheel = _read_private_certified_file(
        path,
        sha256,
        maximum=64 << 20,
        run_dir=run_dir,
        invalid_code="native_certified_wheel_invalid",
        digest_code="native_certified_wheel_digest_mismatch",
    )
    receipt_raw = _read_private_certified_file(
        receipt_path,
        receipt_sha256,
        maximum=64 << 20,
        run_dir=run_dir,
        invalid_code="native_certification_receipt_invalid",
        digest_code="native_certification_receipt_digest_mismatch",
    )
    receipt = _canonical_json_object(receipt_raw, code="native_certification_receipt_invalid")
    expected_tree = gate._git_output(ROOT, "rev-parse", "HEAD^{tree}")
    receipt_fields = {
        "schema",
        "result",
        "certification_eligible",
        "candidate_sha",
        "candidate_tree",
        "base_sha",
        "tier",
        "inventory_sha256",
        "invariant_identity",
        "wheel_sha256",
        "test_runtime_wheel_sha256",
        "comparison_wheel",
        "topology",
        "release_host_capacity",
        "completed_steps",
        "partition",
        "executed",
        "scratch_groups",
        "workload_metrics_before_evidence",
        "active_deadlines",
        "owned_commands",
        "auxiliary_commands",
    }
    comparison = {
        "epoch_commit": None,
        "expected_sha256": None,
        "observed_sha256": None,
        "build_profile": None,
    }
    if (
        set(receipt) - {"r10_deterministic"} != receipt_fields
        or receipt.get("schema") != "friday.quality-gate-summary.v2"
        or receipt.get("result") != "passed"
        or receipt.get("certification_eligible") is not True
        or receipt.get("tier") != "exact-release"
        or receipt.get("candidate_sha") != candidate_sha
        or receipt.get("candidate_tree") != expected_tree
        or re.fullmatch(r"[0-9a-f]{40}", str(receipt.get("base_sha") or "")) is None
        or receipt.get("base_sha") == candidate_sha
        or receipt.get("wheel_sha256") != sha256
        or receipt.get("test_runtime_wheel_sha256") != sha256
        or receipt.get("comparison_wheel") != comparison
        or receipt.get("invariant_identity") != "semantic-function+exact-parameter-set"
        or re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("inventory_sha256") or "")) is None
        or not isinstance(receipt.get("partition"), list)
        or not isinstance(receipt.get("executed"), list)
        or not isinstance(receipt.get("workload_metrics_before_evidence"), dict)
        or type(receipt["workload_metrics_before_evidence"].get("retry_count")) is not int
        or receipt["workload_metrics_before_evidence"]["retry_count"] != 0
        or (base_sha is not None and receipt.get("base_sha") != base_sha)
    ):
        raise NativeError("native_certified_wheel_source_mismatch")
    completed = receipt.get("completed_steps")
    required = (
        "candidate wheel build",
        "candidate wheel verifier",
        "clean-install candidate wheel",
        "one authoritative candidate collection",
    )
    if (
        not isinstance(completed, list)
        or not all(isinstance(item, str) for item in completed)
        or any(item not in completed for item in required)
        or [completed.index(item) for item in required] != sorted(completed.index(item) for item in required)
    ):
        raise NativeError("native_certification_receipt_invalid")
    version = _wheel_version(wheel, source=ROOT)
    normalized = re.sub(r"[^A-Za-z0-9.]+", "_", version)
    if path.name != f"friday-{normalized}-py3-none-any.whl":
        raise NativeError("native_certified_wheel_version_mismatch")
    return _CertifiedWheel(path, sha256, receipt_path, receipt_sha256, version, wheel)


def _retained_wheel_sha256(path: Path) -> str:
    """Measure a private retained wheel without repairing or changing its mode."""
    from tools import quality_gate as gate

    try:
        measured = gate._bounded_file_identity(
            path,
            maximum=64 << 20,
            executable=False,
            owner_uid=os.getuid(),
            single_link=True,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise NativeError("native_retained_wheel_identity_mismatch") from exc
    if measured["mode"] != "0600":
        raise NativeError("native_retained_wheel_identity_mismatch")
    return str(measured["sha256"])


def _install_certified_wheel(
    wheel: _CertifiedWheel,
    retained_wheel: Path,
    *,
    source: Path,
    scratch: Path,
    gate: Any,
) -> Path:
    """Verify and privately install exact-release bytes; never rebuild them."""
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    verify = gate.GateCommand(
        "certified wheel verifier",
        (
            sys.executable,
            str(source / "deploy" / "host-control" / "verify_wheel.py"),
            str(retained_wheel),
            wheel.sha256,
        ),
        environment,
        cwd=source,
        timeout_s=300,
    )
    if gate.run_command(verify) != 0:
        raise NativeError("native_certified_wheel_verification_failed")
    site = scratch / "certified-runtime" / "site-packages"
    site.mkdir(parents=True, mode=0o700)
    install = gate.GateCommand(
        "clean-install certified wheel",
        (
            sys.executable,
            "-I",
            "-m",
            "pip",
            "--isolated",
            "install",
            "--no-deps",
            "--no-compile",
            "--target",
            str(site),
            str(retained_wheel),
        ),
        environment,
        cwd=scratch,
        timeout_s=600,
    )
    if gate.run_command(install) != 0:
        raise NativeError("native_certified_wheel_install_failed")
    if _retained_wheel_sha256(retained_wheel) != wheel.sha256:
        raise NativeError("native_retained_wheel_identity_mismatch")
    return site


@dataclass(frozen=True, repr=False)
class _NativeRunInputs:
    """Validated in-process inputs; never a serialized bypass of preflight."""

    candidate_sha: str
    run_dir: Path
    run_id: str
    base_sha: str | None
    context_path: Path | None
    selected: list[str]
    matrix: dict[str, Any]
    specs: dict[str, Any]
    model_env: dict[str, str]
    captured_ca: Any
    certified_wheel: _CertifiedWheel | None


def _prepare_native_run(
    *,
    env_file: Path,
    candidate_sha: str,
    run_dir: Path,
    case_ids: Sequence[str] | None = None,
    run_id: str | None = None,
    base_sha: str | None = None,
    context_path: Path | None = None,
    certified_wheel_path: Path | None = None,
    certified_wheel_sha256: str | None = None,
    certification_receipt_path: Path | None = None,
    certification_receipt_sha256: str | None = None,
) -> _NativeRunInputs:
    lifecycle, gate, acceptance, battery = _dependencies()
    from tools.release_1_0_acceptance import load_matrix
    from tools.release_1_0_live_cases import WORD_VARIANTS

    explicit = (run_id, base_sha, context_path)
    if any(value is not None for value in explicit) and not all(value is not None for value in explicit):
        raise NativeError("native_context_inputs_invalid")
    if context_path is not None:
        if (
            not isinstance(run_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", run_id) is None
            or not isinstance(base_sha, str)
            or re.fullmatch(r"[0-9a-f]{40}", base_sha) is None
            or base_sha == candidate_sha
            or not isinstance(context_path, Path)
        ):
            raise NativeError("native_context_inputs_invalid")
        _validate_native_context_destination(context_path, run_dir)
    if not isinstance(candidate_sha, str) or re.fullmatch(r"[0-9a-f]{40}", candidate_sha) is None:
        raise NativeError("native_candidate_identity_invalid")
    if gate._git_output(ROOT, "rev-parse", "HEAD") != candidate_sha or gate._git_output(
        ROOT, "status", "--porcelain=v1"
    ):
        raise NativeError("native_candidate_not_frozen")
    if context_path is not None:
        try:
            ancestor = gate._git_output(ROOT, "merge-base", base_sha, candidate_sha)
        except (OSError, RuntimeError, ValueError) as exc:
            raise NativeError("native_context_base_invalid") from exc
        if ancestor != base_sha:
            raise NativeError("native_context_base_invalid")
    certified_values = (
        certified_wheel_path,
        certified_wheel_sha256,
        certification_receipt_path,
        certification_receipt_sha256,
    )
    if any(value is not None for value in certified_values) and not all(
        value is not None for value in certified_values
    ):
        raise NativeError("native_certified_wheel_inputs_invalid")
    certified_wheel = None
    if certified_wheel_path is not None:
        if (
            not isinstance(certified_wheel_sha256, str)
            or not isinstance(certification_receipt_path, Path)
            or not isinstance(certification_receipt_sha256, str)
        ):
            raise NativeError("native_certified_wheel_inputs_invalid")
        certified_wheel = _validate_certified_wheel(
            path=certified_wheel_path,
            sha256=certified_wheel_sha256,
            receipt_path=certification_receipt_path,
            receipt_sha256=certification_receipt_sha256,
            candidate_sha=candidate_sha,
            base_sha=base_sha,
            run_dir=run_dir,
        )
    if case_ids is not None and (
        not isinstance(case_ids, Sequence)
        or isinstance(case_ids, (str, bytes))
        or not all(isinstance(cid, str) for cid in case_ids)
    ):
        raise NativeError("native_case_selection_invalid")
    selected = list(case_ids) if case_ids is not None else [row[0] for row in WORD_VARIANTS]
    if (
        not selected
        or len(selected) != len(set(selected))
        or not set(selected).issubset({row[0] for row in WORD_VARIANTS})
    ):
        raise NativeError("native_case_selection_invalid")
    matrix = load_matrix()
    specs = {case["id"]: case for case in matrix["cases"]}
    if any(
        type(specs[cid].get("timeout_s")) is not int
        or not 0 < specs[cid]["timeout_s"] <= lifecycle.WORKER_TIMEOUT_SEC
        for cid in selected
    ):
        raise NativeError("native_case_timeout_invalid")
    model_env = _model_environment(env_file)
    if (
        context_path is not None
        and _secondary_enabled(model_env, battery=battery)
        and _secondary_mode(model_env, battery=battery) not in {"shadow", "assist"}
    ):
        raise NativeError("native_context_secondary_invalid")
    captured_ca = _selected_ca(model_env, battery=battery)
    acceptance._assert_configured_model_environment(model_env)
    battery._assert_ignored_or_external(run_dir)
    if not run_dir.is_absolute() or run_dir.exists():
        raise NativeError("native_evidence_directory_not_new")
    return _NativeRunInputs(
        candidate_sha=candidate_sha,
        run_dir=run_dir,
        run_id=uuid.uuid4().hex if run_id is None else run_id,
        base_sha=base_sha,
        context_path=context_path,
        selected=selected,
        matrix=matrix,
        specs=specs,
        model_env=model_env,
        captured_ca=captured_ca,
        certified_wheel=certified_wheel,
    )


def _run_native_locked(inputs: _NativeRunInputs) -> dict[str, Any]:
    """Execute under the caller's host lock, retained through descendant cleanup.

    Only a reviewed owning parent may dispatch this private body in its child.
    Ordinary in-process and CLI callers use run_native, which acquires the lock.
    """
    lifecycle, gate, acceptance, battery = _dependencies()
    candidate_sha, run_dir, run_id = inputs.candidate_sha, inputs.run_dir, inputs.run_id
    base_sha, context_path = inputs.base_sha, inputs.context_path
    selected, matrix, specs = inputs.selected, inputs.matrix, inputs.specs
    model_env, captured_ca = inputs.model_env, inputs.captured_ca
    certified_wheel = inputs.certified_wheel
    results = []
    retained_wheel = None
    root_failure = None
    active_case = None
    owned_run_dir = False
    signals = lifecycle._install_controller_signal_handlers()
    try:
        lifecycle._activate_controller_signal_handlers(signals)
        run_dir.mkdir(mode=0o700)
        owned_run_dir = True
        battery._preflight_private_filesystem(run_dir)
        with tempfile.TemporaryDirectory(prefix="native-build-", dir=run_dir) as raw_scratch:
            scratch = Path(raw_scratch)
            with gate._candidate_projection(candidate_sha, scratch, origin=ROOT) as source:
                if certified_wheel is None:
                    build_env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
                    wheel, wheel_sha, _, site, _ = gate._build_reusable_wheel(
                        source,
                        scratch,
                        candidate_sha=candidate_sha,
                        python=sys.executable,
                        environment=build_env,
                        runner=gate.run_command,
                    )
                    retained_wheel = run_dir / wheel.name
                    battery._secure_write_bytes(retained_wheel, wheel.read_bytes())
                else:
                    certified_wheel = _validate_certified_wheel(
                        path=certified_wheel.path,
                        sha256=certified_wheel.sha256,
                        receipt_path=certified_wheel.receipt_path,
                        receipt_sha256=certified_wheel.receipt_sha256,
                        candidate_sha=candidate_sha,
                        base_sha=base_sha,
                        run_dir=run_dir,
                    )
                    wheel_sha = certified_wheel.sha256
                    retained_wheel = run_dir / certified_wheel.path.name
                    battery._secure_write_bytes(retained_wheel, certified_wheel.content)
                    site = _install_certified_wheel(
                        certified_wheel,
                        retained_wheel,
                        source=source,
                        scratch=scratch,
                        gate=gate,
                    )
                if _retained_wheel_sha256(retained_wheel) != wheel_sha:
                    raise NativeError("native_retained_wheel_identity_mismatch")
                battery._validated_quality_gate_installed_site(
                    {"FRIDAY_QUALITY_GATE_INSTALLED_SITE": str(site)}
                )
                snapshot = battery._CandidateSourceSnapshot(
                    source_root=source,
                    relative_paths=battery._candidate_source_paths(
                        root=source,
                        instrument_path=source / "tools/release_1_0_native.py",
                        manifest_paths=[source / "tools/release_1_0_capability_matrix.json"],
                    ),
                )
                try:
                    identity = {
                        "candidate_sha": candidate_sha,
                        "candidate_tree": gate._git_output(source, "rev-parse", "HEAD^{tree}"),
                        "candidate_source_sha256": snapshot.sha256,
                        "wheel_sha256": wheel_sha,
                        "installed_site_sha256": gate._projection_digest(site),
                        "suite_sha256": _suite_digest(source),
                        "model_environment_sha256": _sha(_canonical(model_env)),
                    }
                    battery._secure_write_json(run_dir / "frozen-identity.json", identity)
                    retained_context = None
                    if context_path is not None:
                        retained_context = _publish_native_context(
                            context_path,
                            {
                                "schema": "friday.r10-native-context.v1",
                                "base_sha": base_sha,
                                "identity": identity,
                                "run_id": run_id,
                                "case_ids": selected,
                                "secondary_enabled": _secondary_enabled(model_env, battery=battery),
                                "secondary_mode": _secondary_mode(model_env, battery=battery)
                                if _secondary_enabled(model_env, battery=battery)
                                else "disabled",
                            },
                        )
                    readiness = acceptance._model_readiness_barrier(model_env)
                    if not readiness.dispatch_clear:
                        raise NativeError("native_model_readiness_failed")
                    for index, case_id in enumerate(selected, 1):
                        if context_path is not None:
                            _check_native_context(context_path, retained_context)
                        active_case = case_id
                        result = _run_case(
                            spec=specs[case_id],
                            index=index,
                            run_id=run_id,
                            source=source,
                            snapshot=snapshot,
                            site=site,
                            run_dir=run_dir,
                            model_env=model_env,
                            identity=identity,
                            signals=signals,
                            captured_ca=captured_ca,
                        )
                        results.append(result)
                        active_case = None
                        if not result["process_cleanup_clear"]:
                            root_failure = {
                                "id": "native-run-root",
                                "code": "native_prior_cleanup_uncertain",
                                "root_class": "environment",
                            }
                            break
                    # Recheck retained evidence after dispatch, when mutation or
                    # deletion can no longer hide behind the initial copy check.
                    try:
                        retained_matches = _retained_wheel_sha256(retained_wheel) == wheel_sha
                    except (OSError, RuntimeError):
                        retained_matches = False
                    if not retained_matches:
                        raise NativeError("native_retained_wheel_identity_mismatch")
                finally:
                    snapshot.close()
    except (Exception, lifecycle.ControllerSignal) as exc:
        if not owned_run_dir:
            # No directory ownership: do not write into a conflicting run.
            raise
        interrupted = isinstance(exc, lifecycle.ControllerSignal)
        root_failure = {
            "id": "native-run-root",
            "code": "native_controller_interrupted" if interrupted else _failure_code(exc),
            "root_class": "environment" if interrupted else "harness",
            "error_type": type(exc).__name__,
        }
        if interrupted:
            root_failure["signal_number"] = exc.signal_number
        battery._secure_write_json(run_dir / "root-failure.json", root_failure)
        battery._secure_write_bytes(
            run_dir / "failure-private.log",
            "".join(traceback.format_exception(exc, limit=12)).encode("utf-8", "replace")[:65536],
        )
    finally:
        lifecycle._finalize_controller_signal_handlers(signals, lambda: None)
    completed = {row["id"] for row in results}
    for case_id in selected:
        if case_id not in completed:
            results.append(
                {
                    "id": case_id,
                    "status": "NOT_RUN",
                    "failure_codes": ["native_run_root_prevented_completion"],
                    "root_ref": "native-run-root",
                    "attempt": 1 if case_id == active_case else 0,
                    "root_class": root_failure["root_class"] if root_failure else "harness",
                }
            )
    report = {
        "schema": SCHEMA,
        "run_id": run_id,
        "planned": len(selected),
        "results": [
            {
                key: value
                for key, value in row.items()
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
            for row in results
        ],
        "status": "PASS" if not root_failure and all(row["status"] == "PASS" for row in results) else "FAIL",
        "root_failure": root_failure,
        "go_emitted": False,
        "required_denominator": sum(case["release_required"] for case in matrix["cases"]),
        "required_results": [
            {
                "id": case["id"],
                "required_layer": case["layer"],
                "status": next((row["status"] for row in results if row["id"] == case["id"]), "NOT_RUN"),
                "selected": case["id"] in selected,
            }
            for case in matrix["cases"]
            if case["release_required"]
        ],
        "scope": "selected isolated-live cases only; not complete release acceptance",
    }
    try:
        report["evidence"] = _retained_evidence_index(
            run_dir, selected, results, retained_wheel=retained_wheel
        )
    except (NativeError, OSError, RuntimeError, ValueError) as exc:
        report["evidence"] = {"schema": "friday.r10-native-evidence.v1", "invalid": True}
        report["status"] = "FAIL"
        if report["root_failure"] is None:
            report["root_failure"] = {
                "id": "native-run-root",
                "code": _failure_code(exc),
                "root_class": "harness",
            }
    battery._secure_write_json(run_dir / "summary.json", report)
    return report


def run_native(
    *,
    env_file: Path,
    candidate_sha: str,
    run_dir: Path,
    case_ids: Sequence[str] | None = None,
    run_id: str | None = None,
    base_sha: str | None = None,
    context_path: Path | None = None,
    certified_wheel_path: Path | None = None,
    certified_wheel_sha256: str | None = None,
    certification_receipt_path: Path | None = None,
    certification_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate first, then serialize execution and all retained publication."""
    inputs = _prepare_native_run(
        env_file=env_file,
        candidate_sha=candidate_sha,
        run_dir=run_dir,
        case_ids=case_ids,
        run_id=run_id,
        base_sha=base_sha,
        context_path=context_path,
        certified_wheel_path=certified_wheel_path,
        certified_wheel_sha256=certified_wheel_sha256,
        certification_receipt_path=certification_receipt_path,
        certification_receipt_sha256=certification_receipt_sha256,
    )
    _, _, acceptance, _ = _dependencies()
    with acceptance._ExclusiveAcceptanceRun(acceptance._acceptance_lock_path()):
        return _run_native_locked(inputs)
