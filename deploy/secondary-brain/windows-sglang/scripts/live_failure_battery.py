#!/usr/bin/env python3
"""Observe controlled live endpoint loss and recovery without claiming a power-off test."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import selectors
import socket
import ssl
import stat
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from endpoint_common import (
    EndpointError,
    build_tls_context,
    configure_expected_model,
    evidence_identity,
    load_api_key,
    normalize_base_url,
    runtime_process_epoch,
    verify_remote_profile_epoch,
)
from failure_battery import SUITE_FILES

SCHEMA = "friday.secondary-live-failure-battery.v1"
PHYSICAL_STATE_SCHEMA = "friday.secondary-physical-failure-state.v1"
PHYSICAL_OBSERVATION_SCHEMA = "friday.secondary-physical-failure-observation.v1"
EVIDENCE_SCOPE = "controlled_gateway_outage_and_runtime_restart"
ENDPOINT = "https://192.168.1.35:8443/v1"
PRIMARY_HEALTH_ENDPOINT = "http://127.0.0.1:8000/api/health"
SSH_HOST_ALIAS = "friday-secondary-brain"
REMOTE_BUNDLE_PATH = r"C:\ProgramData\FridaySecondary\bundle"
REPO_ROOT = Path(__file__).resolve().parents[4]
_SSH_OPTIONS = (
    "-T",
    "-o",
    "BatchMode=yes",
    "-o",
    "IdentitiesOnly=yes",
    "-o",
    "PreferredAuthentications=publickey",
    "-o",
    "PasswordAuthentication=no",
    "-o",
    "KbdInteractiveAuthentication=no",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=5",
    "-o",
    "ServerAliveCountMax=3",
)
CONTROL_ACTIONS = frozenset({"stop_gateway", "start_gateway", "restart_runtime", "recover_all"})
_CONTROL_COMMANDS = {
    "stop_gateway": "stop gateway",
    "start_gateway": "start gateway",
    "restart_runtime": "restart --timeout 60 sglang",
    "recover_all": "start sglang gateway",
}
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class LiveFailureBatteryError(RuntimeError):
    """One content-free live failure observation rejection."""


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _sha256(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _write_new(path: Path, value: dict[str, Any]) -> str:
    raw = _canonical(value)
    parent = path.absolute().parent
    if not parent.is_dir() or path.exists() or path.is_symlink():
        raise LiveFailureBatteryError("live failure output path is not new")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise LiveFailureBatteryError("live failure evidence could not be created") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return _sha256(raw)


def _powershell(action: str) -> str:
    compose_action = _CONTROL_COMMANDS.get(action)
    if compose_action is None:
        raise LiveFailureBatteryError("control action is outside the closed command set")
    return (
        "$ErrorActionPreference='Stop';"
        f"$bundle=[IO.Path]::GetFullPath('{REMOTE_BUNDLE_PATH}');"
        f"if($bundle -cne '{REMOTE_BUNDLE_PATH}'){{exit 41}};"
        "if(-not (Test-Path -LiteralPath $bundle -PathType Container)){exit 42};"
        "$envFile=Join-Path $bundle '.env';$composeFile=Join-Path $bundle 'compose.yml';"
        "if(-not (Test-Path -LiteralPath $envFile -PathType Leaf)){exit 43};"
        "if(-not (Test-Path -LiteralPath $composeFile -PathType Leaf)){exit 44};"
        "Set-Location -LiteralPath $bundle;"
        f"& docker.exe compose --env-file $envFile --file $composeFile {compose_action} "
        "1>$null 2>$null;"
        "if($LASTEXITCODE -ne 0){exit 45}"
    )


def _run_control(action: str) -> None:
    encoded = base64.b64encode(_powershell(action).encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            [
                "ssh",
                *_SSH_OPTIONS,
                SSH_HOST_ALIAS,
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiveFailureBatteryError("fixed key-only SSH control failed") from exc
    if result.returncode != 0:
        raise LiveFailureBatteryError("fixed key-only SSH control was rejected")


def _run_observation(script: str) -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [
                "ssh",
                *_SSH_OPTIONS,
                SSH_HOST_ALIAS,
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            raise LiveFailureBatteryError("fixed SSH observation has no bounded output channel")
        raw = bytearray()
        deadline = time.monotonic() + 30.0
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait(timeout=10)
                    raise LiveFailureBatteryError("fixed SSH observation exceeded its time bound")
                for key, _mask in selector.select(timeout=min(remaining, 1.0)):
                    chunk = os.read(key.fd, 129)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    raw.extend(chunk)
                    if len(raw) > 128:
                        process.kill()
                        process.wait(timeout=10)
                        raise LiveFailureBatteryError("fixed SSH observation exceeded its output bound")
                if process.poll() is not None and not selector.select(timeout=0):
                    selector.unregister(process.stdout)
        returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except LiveFailureBatteryError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        if process is not None:
            process.kill()
        raise LiveFailureBatteryError("fixed key-only SSH observation failed") from exc
    if returncode != 0:
        raise LiveFailureBatteryError("fixed key-only SSH observation was rejected")
    try:
        value = bytes(raw).decode("ascii", errors="strict").strip()
    except UnicodeError:
        raise LiveFailureBatteryError("fixed SSH observation encoding is invalid") from None
    if not value or len(value) > 96 or any(character not in "0123456789" for character in value):
        raise LiveFailureBatteryError("fixed SSH observation value is invalid")
    return value


def _laptop_boot_epoch_sha256() -> str:
    epoch = _run_observation(
        "$ErrorActionPreference='Stop';"
        "$v=(Get-CimInstance -ClassName Win32_OperatingSystem).LastBootUpTime."
        "ToUniversalTime().Ticks;"
        "if($v -le 0){exit 51};[Console]::Out.Write([string]$v)"
    )
    return _sha256(epoch)


def _source_identity() -> tuple[str, str]:
    runner = Path(__file__).resolve()
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
        relative = str(runner.relative_to(REPO_ROOT))
        observed_paths = (
            *SUITE_FILES,
            relative,
            str((runner.parent / "failure_battery.py").relative_to(REPO_ROOT)),
            str((runner.parent / "runtime_profile_operator.py").relative_to(REPO_ROOT)),
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all", "--", *observed_paths],
            cwd=REPO_ROOT,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise LiveFailureBatteryError("physical observation source identity is unavailable") from exc
    source_head = head.stdout.strip()
    if (
        head.returncode != 0
        or _COMMIT.fullmatch(source_head) is None
        or dirty.returncode != 0
        or bool(dirty.stdout)
    ):
        raise LiveFailureBatteryError("physical observation runner is not committed and clean")
    return source_head, _sha256(runner.read_bytes())


def _primary_process_epoch_sha256(pid: int) -> str:
    if isinstance(pid, bool) or not 2 <= pid <= 4_194_304:
        raise LiveFailureBatteryError("primary PID is outside the closed range")
    path = Path("/proc") / str(pid) / "stat"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LiveFailureBatteryError("primary process epoch is unavailable") from exc
    if not 1 <= len(raw) <= 8192 or b"\x00" in raw:
        raise LiveFailureBatteryError("primary process epoch is invalid")
    try:
        text = raw.decode("ascii", errors="strict")
    except UnicodeError:
        raise LiveFailureBatteryError("primary process epoch encoding is invalid") from None
    closing = text.rfind(")")
    fields = text[closing + 2 :].split() if closing > 0 else []
    if len(fields) < 20 or not fields[19].isdigit():
        raise LiveFailureBatteryError("primary process epoch is invalid")
    try:
        os.kill(pid, 0)
    except OSError as exc:
        raise LiveFailureBatteryError("primary process is not alive") from exc
    _require_primary_health_socket_owner(pid)
    return _sha256(f"{pid}:{fields[19]}")


def _friday_backend_main_pid() -> int:
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                "--property=MainPID",
                "--value",
                "friday-backend.service",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiveFailureBatteryError("Friday backend service identity is unavailable") from exc
    value = result.stdout.strip()
    if result.returncode != 0 or not value.isdigit():
        raise LiveFailureBatteryError("Friday backend service identity is unavailable")
    pid = int(value)
    if not 2 <= pid <= 4_194_304:
        raise LiveFailureBatteryError("Friday backend service is not running")
    return pid


def _require_primary_health_socket_owner(pid: int) -> None:
    listening_inodes: set[str] = set()
    try:
        for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
            for line in table.read_text(encoding="ascii", errors="strict").splitlines()[1:]:
                fields = line.split()
                if len(fields) < 10 or fields[3] != "0A":
                    continue
                _address, separator, port_hex = fields[1].rpartition(":")
                if separator and int(port_hex, 16) == 8000 and fields[9].isdigit():
                    listening_inodes.add(fields[9])
        descriptors = list((Path("/proc") / str(pid) / "fd").iterdir())
        if len(descriptors) > 4096:
            raise LiveFailureBatteryError("primary process descriptor set is unbounded")
        owned_inodes = {
            match.group(1)
            for descriptor in descriptors
            if (match := re.fullmatch(r"socket:\[([0-9]+)\]", os.readlink(descriptor))) is not None
        }
    except LiveFailureBatteryError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise LiveFailureBatteryError("primary health socket ownership is unavailable") from exc
    if not listening_inodes.intersection(owned_inodes):
        raise LiveFailureBatteryError("primary PID does not own the Friday health listener")


def _primary_health(timeout_sec: float) -> str:
    request = urllib.request.Request(
        PRIMARY_HEALTH_ENDPOINT,
        headers={"Accept": "application/json", "User-Agent": "friday-secondary-physical-witness/1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=min(timeout_sec, 10.0)) as response:  # noqa: S310
            if int(response.status) != 200:
                raise LiveFailureBatteryError("primary health endpoint did not return HTTP 200")
            raw = response.read(65_537)
    except LiveFailureBatteryError:
        raise
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise LiveFailureBatteryError("primary health endpoint is unavailable") from exc
    if len(raw) > 65_536:
        raise LiveFailureBatteryError("primary health response exceeded the bound")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LiveFailureBatteryError("primary health response is invalid") from exc
    version = value.get("version") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("status") != "ok"
        or not isinstance(version, str)
        or not 1 <= len(version) <= 80
    ):
        raise LiveFailureBatteryError("primary health response is not ready")
    return version


def _read_state(path: Path) -> tuple[dict[str, Any], bytes]:
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or not 1 <= metadata.st_size <= 65_536
        ):
            raise LiveFailureBatteryError("physical observation state is not a bounded regular file")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_size,
            metadata.st_mtime_ns,
        ):
            raise LiveFailureBatteryError("physical observation state identity changed")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(65_537)
            after = os.fstat(stream.fileno())
        if len(raw) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise LiveFailureBatteryError("physical observation state identity changed")
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except LiveFailureBatteryError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LiveFailureBatteryError("physical observation state is invalid") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict) or raw != _canonical(value):
        raise LiveFailureBatteryError("physical observation state is not canonical")
    return value, raw


def _tls_handshake_available(ca_file: Path, timeout_sec: float) -> bool:
    normalized = normalize_base_url(ENDPOINT)
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or parsed.hostname != "192.168.1.35" or parsed.port != 8443:
        raise LiveFailureBatteryError("live endpoint differs from the fixed laptop authority")
    context = build_tls_context(normalized, ca_file)
    if context is None:
        raise LiveFailureBatteryError("live endpoint did not build a TLS context")
    try:
        with (
            socket.create_connection((parsed.hostname, parsed.port), timeout=min(timeout_sec, 10.0)) as raw,
            context.wrap_socket(raw, server_hostname=parsed.hostname),
        ):
            return True
    except (OSError, ssl.SSLError, TimeoutError):
        return False


def _ready_epoch(api_key: str, ca_file: Path, timeout_sec: float) -> str:
    verify_remote_profile_epoch(
        ENDPOINT,
        api_key=api_key,
        timeout_sec=timeout_sec,
        ca_file=ca_file,
    )
    return runtime_process_epoch(
        ENDPOINT,
        api_key=api_key,
        timeout_sec=timeout_sec,
        ca_file=ca_file,
    )


def _wait_for_tls_loss(ca_file: Path, *, timeout_sec: float) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not _tls_handshake_available(ca_file, min(timeout_sec, 10.0)):
            return
        time.sleep(0.25)
    raise LiveFailureBatteryError("controlled gateway stop did not remove the TLS endpoint")


def _wait_for_application_loss(api_key: str, ca_file: Path, *, timeout_sec: float) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            _ready_epoch(api_key, ca_file, min(timeout_sec, 10.0))
        except EndpointError:
            return
        time.sleep(0.25)
    raise LiveFailureBatteryError("runtime restart did not expose an application outage")


def _wait_ready(api_key: str, ca_file: Path, *, timeout_sec: float) -> str:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            return _ready_epoch(api_key, ca_file, min(timeout_sec, 30.0))
        except EndpointError:
            time.sleep(1.0)
    raise LiveFailureBatteryError("exact candidate endpoint did not recover before the deadline")


def _recover_or_reject(
    api_key: str,
    ca_file: Path,
    recovery_timeout_sec: float,
    original: BaseException | None = None,
) -> None:
    try:
        _run_control("recover_all")
        _wait_ready(api_key, ca_file, timeout_sec=recovery_timeout_sec)
    except (LiveFailureBatteryError, EndpointError) as recovery_error:
        if original is not None:
            raise LiveFailureBatteryError(
                "live battery failed and exact-candidate recovery was rejected"
            ) from original
        raise LiveFailureBatteryError("exact-candidate recovery was rejected") from recovery_error


def run_battery(
    *,
    candidate: Path,
    api_key_file: Path,
    ca_file: Path,
    output: Path,
    timeout_sec: float = 15.0,
    recovery_timeout_sec: float = 600.0,
) -> dict[str, Any]:
    if not 1.0 <= timeout_sec <= 60.0 or not 60.0 <= recovery_timeout_sec <= 1800.0:
        raise LiveFailureBatteryError("live battery timeout is outside the closed range")
    source_head, runner_sha256 = _source_identity()
    try:
        configure_expected_model(candidate, ca_file)
        api_key = load_api_key(api_key_file)
        if not _tls_handshake_available(ca_file, timeout_sec):
            raise LiveFailureBatteryError("fixed laptop TLS endpoint is unavailable before the battery")
        epoch_before = _ready_epoch(api_key, ca_file, timeout_sec)
    except EndpointError as exc:
        raise LiveFailureBatteryError("candidate-bound live endpoint is not ready") from exc

    gateway_stopped = False
    try:
        _run_control("stop_gateway")
        gateway_stopped = True
        _wait_for_tls_loss(ca_file, timeout_sec=timeout_sec)
        _run_control("start_gateway")
        gateway_stopped = False
        gateway_recovery_epoch = _wait_ready(
            api_key,
            ca_file,
            timeout_sec=recovery_timeout_sec,
        )
        if gateway_recovery_epoch != epoch_before:
            raise LiveFailureBatteryError("gateway-only outage unexpectedly changed the runtime epoch")
    except (LiveFailureBatteryError, EndpointError) as exc:
        gateway_stopped = False
        _recover_or_reject(api_key, ca_file, recovery_timeout_sec, exc)
        raise LiveFailureBatteryError("controlled gateway outage journey failed") from exc
    finally:
        if gateway_stopped:
            _recover_or_reject(api_key, ca_file, recovery_timeout_sec)

    try:
        _run_control("restart_runtime")
        _wait_for_application_loss(api_key, ca_file, timeout_sec=timeout_sec)
        epoch_after = _wait_ready(api_key, ca_file, timeout_sec=recovery_timeout_sec)
        if epoch_after == epoch_before:
            raise LiveFailureBatteryError("runtime restart did not change the process epoch")
    except (LiveFailureBatteryError, EndpointError) as exc:
        _recover_or_reject(api_key, ca_file, recovery_timeout_sec, exc)
        raise LiveFailureBatteryError("controlled runtime restart journey failed") from exc

    evidence = {
        "schema": SCHEMA,
        "status": "passed",
        "evidence_scope": EVIDENCE_SCOPE,
        **evidence_identity(),
        "source_head": source_head,
        "endpoint_base_url": ENDPOINT,
        "runner_sha256": runner_sha256,
        "control_surface": {
            "ssh_host_alias": SSH_HOST_ALIAS,
            "authentication": "key_only_batch",
            "remote_bundle_path": REMOTE_BUNDLE_PATH,
            "command_set": sorted(CONTROL_ACTIONS),
            "ssh_output_retained": False,
        },
        "controlled_gateway_stop_observed": True,
        "tls_endpoint_loss_observed": True,
        "exact_candidate_gateway_recovery_observed": True,
        "gateway_recovery_preserved_runtime_epoch": True,
        "controlled_runtime_restart_observed": True,
        "runtime_application_outage_observed": True,
        "exact_candidate_runtime_recovery_observed": True,
        "runtime_epoch_before_sha256": _sha256(epoch_before),
        "runtime_epoch_after_sha256": _sha256(epoch_after),
        "runtime_epoch_changed": True,
        "physical_laptop_power_loss_observed": False,
        "friday_primary_process_continuity_observed": False,
        "primary_fallback_exactly_once_observed": False,
        "mid_turn_primary_fallback_observed": False,
        "raw_content_retained": False,
        "credentials_retained": False,
    }
    output_sha256 = _write_new(output, evidence)
    return {
        "status": "controlled_live_failure_passed",
        "physical_laptop_power_loss_observed": False,
        "primary_process_continuity_observed": False,
        "output_sha256": output_sha256,
    }


PHYSICAL_BEGIN_KEYS = frozenset(
    {
        "schema",
        "status",
        "candidate_profile_id",
        "candidate_profile_sha256",
        "served_model_alias",
        "gateway_ca_certificate_sha256",
        "observer_source_head",
        "observer_runner_sha256",
        "primary_pid",
        "primary_process_epoch_before_sha256",
        "primary_version",
        "laptop_boot_epoch_before_sha256",
        "raw_content_retained",
        "credentials_retained",
    }
)
PHYSICAL_OFF_KEYS = PHYSICAL_BEGIN_KEYS | frozenset(
    {
        "physical_begin_state_sha256",
        "physical_tls_endpoint_unavailable_observed",
        "primary_process_epoch_while_off_sha256",
        "physical_laptop_power_loss_operator_observed",
        "ordinary_primary_fallback_exactly_once_operator_observed",
        "mid_turn_primary_fallback_exactly_once_operator_observed",
        "effect_replay_operator_observed",
        "v12_readiness_changed_operator_observed",
    }
)


def begin_physical_observation(
    *,
    candidate: Path,
    api_key_file: Path,
    ca_file: Path,
    primary_pid: int,
    output: Path,
    timeout_sec: float = 15.0,
) -> dict[str, Any]:
    if not 1.0 <= timeout_sec <= 60.0:
        raise LiveFailureBatteryError("physical observation timeout is outside the closed range")
    try:
        configure_expected_model(candidate, ca_file)
        api_key = load_api_key(api_key_file)
        _ready_epoch(api_key, ca_file, timeout_sec)
    except EndpointError as exc:
        raise LiveFailureBatteryError("exact candidate is not ready before physical observation") from exc
    if primary_pid != _friday_backend_main_pid():
        raise LiveFailureBatteryError("primary PID is not friday-backend.service MainPID")
    source_head, runner_sha256 = _source_identity()
    primary_epoch = _primary_process_epoch_sha256(primary_pid)
    primary_version = _primary_health(timeout_sec)
    laptop_boot = _laptop_boot_epoch_sha256()
    state = {
        "schema": PHYSICAL_STATE_SCHEMA,
        "status": "awaiting_physical_power_loss",
        **evidence_identity(),
        "observer_source_head": source_head,
        "observer_runner_sha256": runner_sha256,
        "primary_pid": primary_pid,
        "primary_process_epoch_before_sha256": primary_epoch,
        "primary_version": primary_version,
        "laptop_boot_epoch_before_sha256": laptop_boot,
        "raw_content_retained": False,
        "credentials_retained": False,
    }
    if set(state) != PHYSICAL_BEGIN_KEYS:
        raise LiveFailureBatteryError("physical observation begin state is incomplete")
    output_sha256 = _write_new(output, state)
    return {
        "status": "awaiting_physical_power_loss",
        "next_step": "physically_power_off_laptop_then_record_off_state",
        "output_sha256": output_sha256,
    }


def record_physical_power_loss(
    *,
    candidate: Path,
    ca_file: Path,
    state_path: Path,
    output: Path,
    physical_power_loss_observed: bool,
    ordinary_fallback_observed: bool,
    mid_turn_fallback_observed: bool,
    no_effect_replay_observed: bool,
    v12_readiness_unchanged_observed: bool,
    timeout_sec: float = 15.0,
) -> dict[str, Any]:
    if not 1.0 <= timeout_sec <= 60.0:
        raise LiveFailureBatteryError("physical observation timeout is outside the closed range")
    configure_expected_model(candidate, ca_file)
    state, state_raw = _read_state(state_path)
    source_head, runner_sha256 = _source_identity()
    if (
        set(state) != PHYSICAL_BEGIN_KEYS
        or state.get("schema") != PHYSICAL_STATE_SCHEMA
        or state.get("status") != "awaiting_physical_power_loss"
        or any(state.get(key) != value for key, value in evidence_identity().items())
        or state.get("observer_source_head") != source_head
        or state.get("observer_runner_sha256") != runner_sha256
        or state.get("raw_content_retained") is not False
        or state.get("credentials_retained") is not False
    ):
        raise LiveFailureBatteryError("physical observation begin state is invalid")
    confirmations = (
        physical_power_loss_observed,
        ordinary_fallback_observed,
        mid_turn_fallback_observed,
        no_effect_replay_observed,
        v12_readiness_unchanged_observed,
    )
    if any(value is not True for value in confirmations):
        raise LiveFailureBatteryError("every manual physical-off observation must be explicit")
    if _tls_handshake_available(ca_file, timeout_sec):
        raise LiveFailureBatteryError("laptop TLS endpoint is still reachable during physical-off stage")
    primary_pid = state.get("primary_pid")
    if type(primary_pid) is not int:
        raise LiveFailureBatteryError("physical observation primary PID is invalid")
    if primary_pid != _friday_backend_main_pid():
        raise LiveFailureBatteryError("Friday backend service changed during laptop power loss")
    primary_epoch = _primary_process_epoch_sha256(primary_pid)
    if primary_epoch != state.get("primary_process_epoch_before_sha256"):
        raise LiveFailureBatteryError("Friday primary process changed during laptop power loss")
    if _primary_health(timeout_sec) != state.get("primary_version"):
        raise LiveFailureBatteryError("Friday primary release changed during laptop power loss")
    off_state = {
        **state,
        "status": "physical_power_loss_observed_awaiting_recovery",
        "physical_begin_state_sha256": _sha256(state_raw),
        "physical_tls_endpoint_unavailable_observed": True,
        "primary_process_epoch_while_off_sha256": primary_epoch,
        "physical_laptop_power_loss_operator_observed": True,
        "ordinary_primary_fallback_exactly_once_operator_observed": True,
        "mid_turn_primary_fallback_exactly_once_operator_observed": True,
        "effect_replay_operator_observed": False,
        "v12_readiness_changed_operator_observed": False,
    }
    if set(off_state) != PHYSICAL_OFF_KEYS:
        raise LiveFailureBatteryError("physical observation off state is incomplete")
    output_sha256 = _write_new(output, off_state)
    return {
        "status": "awaiting_exact_candidate_recovery",
        "next_step": "power_on_laptop_then_finish_physical_observation",
        "output_sha256": output_sha256,
    }


def finish_physical_observation(
    *,
    candidate: Path,
    api_key_file: Path,
    ca_file: Path,
    state_path: Path,
    output: Path,
    readmitted_without_primary_restart_observed: bool,
    timeout_sec: float = 15.0,
) -> dict[str, Any]:
    if not 1.0 <= timeout_sec <= 60.0:
        raise LiveFailureBatteryError("physical observation timeout is outside the closed range")
    try:
        configure_expected_model(candidate, ca_file)
        api_key = load_api_key(api_key_file)
        _ready_epoch(api_key, ca_file, timeout_sec)
    except EndpointError as exc:
        raise LiveFailureBatteryError("exact candidate did not recover after physical power loss") from exc
    state, state_raw = _read_state(state_path)
    source_head, runner_sha256 = _source_identity()
    if (
        set(state) != PHYSICAL_OFF_KEYS
        or state.get("schema") != PHYSICAL_STATE_SCHEMA
        or state.get("status") != "physical_power_loss_observed_awaiting_recovery"
        or any(state.get(key) != value for key, value in evidence_identity().items())
        or state.get("observer_source_head") != source_head
        or state.get("observer_runner_sha256") != runner_sha256
        or readmitted_without_primary_restart_observed is not True
    ):
        raise LiveFailureBatteryError("physical observation recovery state is invalid or unconfirmed")
    primary_pid = state.get("primary_pid")
    if type(primary_pid) is not int:
        raise LiveFailureBatteryError("physical observation primary PID is invalid")
    if primary_pid != _friday_backend_main_pid():
        raise LiveFailureBatteryError("Friday backend service changed during physical observation")
    primary_after = _primary_process_epoch_sha256(primary_pid)
    if primary_after != state.get("primary_process_epoch_before_sha256") or primary_after != state.get(
        "primary_process_epoch_while_off_sha256"
    ):
        raise LiveFailureBatteryError("Friday primary process did not remain continuous")
    if _primary_health(timeout_sec) != state.get("primary_version"):
        raise LiveFailureBatteryError("Friday primary release changed during physical observation")
    laptop_after = _laptop_boot_epoch_sha256()
    if laptop_after == state.get("laptop_boot_epoch_before_sha256"):
        raise LiveFailureBatteryError("laptop boot epoch did not change after physical power loss")
    evidence = {
        "schema": PHYSICAL_OBSERVATION_SCHEMA,
        "status": "observed",
        **evidence_identity(),
        "observation_scope": "physical_power_loss_with_existing_primary_process",
        "observation_method": "code_owned_manual_state_machine",
        "observation_state_sha256": _sha256(state_raw),
        "observer_source_head": source_head,
        "observer_runner_sha256": runner_sha256,
        "laptop_boot_epoch_before_sha256": state["laptop_boot_epoch_before_sha256"],
        "laptop_boot_epoch_after_sha256": laptop_after,
        "friday_primary_process_epoch_before_sha256": state["primary_process_epoch_before_sha256"],
        "friday_primary_process_epoch_after_sha256": primary_after,
        "physical_laptop_power_loss_observed": True,
        "friday_primary_process_continuity_observed": True,
        "ordinary_primary_fallback_exactly_once_operator_observed": True,
        "mid_turn_primary_fallback_exactly_once_operator_observed": True,
        "readmitted_without_primary_restart_operator_observed": True,
        "effect_replay_operator_observed": False,
        "v12_readiness_changed_operator_observed": False,
        "raw_content_retained": False,
        "credentials_retained": False,
    }
    output_sha256 = _write_new(output, evidence)
    return {"status": "physical_failure_observed", "output_sha256": output_sha256}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    controlled = commands.add_parser("controlled")
    controlled.add_argument("--candidate", required=True, type=Path)
    controlled.add_argument("--api-key-file", required=True, type=Path)
    controlled.add_argument("--ca-file", required=True, type=Path)
    controlled.add_argument("--timeout-sec", default=15.0, type=float)
    controlled.add_argument("--recovery-timeout-sec", default=600.0, type=float)
    controlled.add_argument("--output", required=True, type=Path)

    begin = commands.add_parser("physical-begin")
    begin.add_argument("--candidate", required=True, type=Path)
    begin.add_argument("--api-key-file", required=True, type=Path)
    begin.add_argument("--ca-file", required=True, type=Path)
    begin.add_argument("--primary-pid", required=True, type=int)
    begin.add_argument("--timeout-sec", default=15.0, type=float)
    begin.add_argument("--output", required=True, type=Path)

    off = commands.add_parser("physical-off")
    off.add_argument("--candidate", required=True, type=Path)
    off.add_argument("--ca-file", required=True, type=Path)
    off.add_argument("--state", required=True, type=Path)
    off.add_argument("--timeout-sec", default=15.0, type=float)
    off.add_argument("--physical-power-loss-observed", required=True, action="store_true")
    off.add_argument(
        "--ordinary-primary-fallback-exactly-once-operator-observed",
        required=True,
        action="store_true",
    )
    off.add_argument(
        "--mid-turn-primary-fallback-exactly-once-operator-observed",
        required=True,
        action="store_true",
    )
    off.add_argument("--no-effect-replay-operator-observed", required=True, action="store_true")
    off.add_argument("--v12-readiness-unchanged-operator-observed", required=True, action="store_true")
    off.add_argument("--output", required=True, type=Path)

    finish = commands.add_parser("physical-finish")
    finish.add_argument("--candidate", required=True, type=Path)
    finish.add_argument("--api-key-file", required=True, type=Path)
    finish.add_argument("--ca-file", required=True, type=Path)
    finish.add_argument("--state", required=True, type=Path)
    finish.add_argument("--timeout-sec", default=15.0, type=float)
    finish.add_argument(
        "--readmitted-without-primary-restart-operator-observed",
        required=True,
        action="store_true",
    )
    finish.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "controlled":
            result = run_battery(
                candidate=args.candidate,
                api_key_file=args.api_key_file,
                ca_file=args.ca_file,
                output=args.output,
                timeout_sec=args.timeout_sec,
                recovery_timeout_sec=args.recovery_timeout_sec,
            )
        elif args.command == "physical-begin":
            result = begin_physical_observation(
                candidate=args.candidate,
                api_key_file=args.api_key_file,
                ca_file=args.ca_file,
                primary_pid=args.primary_pid,
                output=args.output,
                timeout_sec=args.timeout_sec,
            )
        elif args.command == "physical-off":
            result = record_physical_power_loss(
                candidate=args.candidate,
                ca_file=args.ca_file,
                state_path=args.state,
                output=args.output,
                physical_power_loss_observed=args.physical_power_loss_observed,
                ordinary_fallback_observed=(args.ordinary_primary_fallback_exactly_once_operator_observed),
                mid_turn_fallback_observed=(args.mid_turn_primary_fallback_exactly_once_operator_observed),
                no_effect_replay_observed=args.no_effect_replay_operator_observed,
                v12_readiness_unchanged_observed=(args.v12_readiness_unchanged_operator_observed),
                timeout_sec=args.timeout_sec,
            )
        else:
            result = finish_physical_observation(
                candidate=args.candidate,
                api_key_file=args.api_key_file,
                ca_file=args.ca_file,
                state_path=args.state,
                output=args.output,
                readmitted_without_primary_restart_observed=(
                    args.readmitted_without_primary_restart_operator_observed
                ),
                timeout_sec=args.timeout_sec,
            )
    except LiveFailureBatteryError as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
