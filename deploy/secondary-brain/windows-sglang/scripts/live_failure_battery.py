#!/usr/bin/env python3
"""Observe controlled live endpoint loss and recovery without claiming a power-off test."""

from __future__ import annotations

import argparse
import base64
import contextlib
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
    configured_profile_context_tokens,
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
PRODUCT_BEGIN_SCHEMA = "friday.secondary-product-failure-begin.v1"
PRODUCT_OFF_SCHEMA = "friday.secondary-product-failure-off.v1"
PRODUCT_OBSERVATION_SCHEMA = "friday.secondary-product-failure-observation.v1"
EVIDENCE_SCOPE = "controlled_gateway_outage_and_runtime_restart"
ENDPOINT = "https://192.168.1.35:8443/v1"
PRIMARY_BASE_URL = "https://127.0.0.1:8000"
PRIMARY_HEALTH_ENDPOINT = f"{PRIMARY_BASE_URL}/api/health"
PRIMARY_DIAGNOSTICS_ENDPOINT = f"{PRIMARY_BASE_URL}/api/admin/diagnostics"
PRODUCT_WORKLOAD = "extract"
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
_MAX_COUNTER = (1 << 63) - 1
_SECONDARY_FAILURES = frozenset(
    {
        "disabled",
        "misconfigured",
        "mode_disallowed",
        "workload_disallowed",
        "private_text_disallowed",
        "secret_material_denied",
        "unsupported_modality",
        "effect_denied",
        "context_exceeded",
        "admission_busy",
        "cooldown",
        "deadline",
        "connect_failed",
        "timeout",
        "http_transient",
        "http_rejected",
        "auth_rejected",
        "wrong_profile",
        "wrong_model",
        "malformed_response",
        "tool_call_rejected",
        "reasoning_leak",
        "degeneration",
        "cancelled",
    }
)
_PHYSICAL_OUTAGE_FAILURES = frozenset(
    {
        "admission_busy",
        "cooldown",
        "connect_failed",
        "timeout",
        "http_transient",
        "http_rejected",
    }
)


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


def _preflight_new_output_path(path: Path) -> None:
    parent = path.absolute().parent
    try:
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise LiveFailureBatteryError("live failure output parent is unavailable") from exc
    if not stat.S_ISDIR(parent_metadata.st_mode) or parent.is_symlink():
        raise LiveFailureBatteryError("live failure output parent is not a regular directory")
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LiveFailureBatteryError("live failure output identity is unavailable") from exc
    raise LiveFailureBatteryError("live failure output path is not new")


def _write_new_pair(
    main_path: Path,
    main_value: dict[str, Any],
    product_path: Path,
    product_value: dict[str, Any],
) -> tuple[str, str]:
    if main_path.absolute() == product_path.absolute():
        raise LiveFailureBatteryError("physical and product outputs must be distinct")
    # Check both names and parents before reserving either name. Reserving the
    # product path first additionally guarantees that a pre-existing product
    # receipt can never leave a newly created main receipt behind.
    _preflight_new_output_path(main_path)
    _preflight_new_output_path(product_path)
    raw_by_path = {
        main_path: _canonical(main_value),
        product_path: _canonical(product_value),
    }
    descriptors: dict[Path, int] = {}
    identities: dict[Path, tuple[int, int]] = {}
    try:
        for path in (product_path, main_path):
            descriptor = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                stat.S_IRUSR | stat.S_IWUSR,
            )
            metadata = os.fstat(descriptor)
            identities[path] = (metadata.st_dev, metadata.st_ino)
            expected_owner = os.geteuid() if hasattr(os, "geteuid") else metadata.st_uid
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != expected_owner
                or metadata.st_size != 0
            ):
                os.close(descriptor)
                raise LiveFailureBatteryError("reserved live failure output is invalid")
            descriptors[path] = descriptor
        for path in (main_path, product_path):
            descriptor = descriptors.pop(path)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw_by_path[path])
                stream.flush()
                os.fsync(stream.fileno())
    except (LiveFailureBatteryError, OSError) as exc:
        for descriptor in descriptors.values():
            with contextlib.suppress(OSError):
                os.close(descriptor)
        for path, identity in identities.items():
            try:
                metadata = path.lstat()
                if (metadata.st_dev, metadata.st_ino) == identity:
                    path.unlink()
            except OSError:
                pass
        if isinstance(exc, LiveFailureBatteryError):
            raise
        raise LiveFailureBatteryError("paired live failure evidence could not be created") from exc
    return _sha256(raw_by_path[main_path]), _sha256(raw_by_path[product_path])


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


class _NoPrimaryRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        _request: urllib.request.Request,
        _file_pointer: Any,
        _code: int,
        _message: str,
        _headers: Any,
        _new_url: str,
    ) -> None:
        return None


def _primary_tls_context(ca_file: Path) -> tuple[ssl.SSLContext, str]:
    descriptor: int | None = None
    try:
        metadata = ca_file.lstat()
        if not stat.S_ISREG(metadata.st_mode) or ca_file.is_symlink() or not 1 <= metadata.st_size <= 65_536:
            raise LiveFailureBatteryError("primary CA is not a bounded regular file")
        descriptor = os.open(
            ca_file,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(65_537)
            after = os.fstat(stream.fileno())
        if (
            not 1 <= len(raw) <= 65_536
            or len(raw) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise LiveFailureBatteryError("primary CA identity changed while reading")
        pem = raw.decode("ascii", errors="strict")
        if "-----BEGIN CERTIFICATE-----" not in pem or "-----END CERTIFICATE-----" not in pem:
            raise LiveFailureBatteryError("primary CA is not a PEM certificate")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.load_verify_locations(cadata=pem)
        return context, _sha256(raw)
    except LiveFailureBatteryError:
        raise
    except (OSError, UnicodeError, ssl.SSLError) as exc:
        raise LiveFailureBatteryError("primary CA could not establish a trusted TLS context") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _load_primary_api_key(path: Path) -> str:
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        expected_owner = os.geteuid() if hasattr(os, "geteuid") else metadata.st_uid
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_uid != expected_owner
            or not 32 <= metadata.st_size <= 513
        ):
            raise LiveFailureBatteryError("primary diagnostics API key file is not private")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        expected_identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
        observed_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if not stat.S_ISREG(before.st_mode) or observed_identity != expected_identity:
            raise LiveFailureBatteryError("primary diagnostics API key identity changed")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(514)
            after = os.fstat(stream.fileno())
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if after_identity != observed_identity or len(raw) != before.st_size:
            raise LiveFailureBatteryError("primary diagnostics API key identity changed")
        body = raw[:-2] if raw.endswith(b"\r\n") else raw[:-1] if raw.endswith(b"\n") else raw
        if b"\x00" in raw or b"\r" in body or b"\n" in body:
            raise LiveFailureBatteryError("primary diagnostics API key must contain one line")
        key = body.decode("utf-8", errors="strict")
        if not 32 <= len(key) <= 512 or any(ord(character) < 33 or ord(character) > 126 for character in key):
            raise LiveFailureBatteryError("primary diagnostics API key is invalid")
        return key
    except LiveFailureBatteryError:
        raise
    except (OSError, UnicodeError) as exc:
        raise LiveFailureBatteryError("primary diagnostics API key is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _primary_json(
    endpoint: str,
    *,
    ca_file: Path,
    timeout_sec: float,
    api_key: str = "",
    maximum_bytes: int,
) -> tuple[dict[str, Any], str]:
    if endpoint not in {PRIMARY_HEALTH_ENDPOINT, PRIMARY_DIAGNOSTICS_ENDPOINT}:
        raise LiveFailureBatteryError("primary observation endpoint is outside the closed set")
    context, ca_sha256 = _primary_tls_context(ca_file)
    headers = {
        "Accept": "application/json",
        "User-Agent": "friday-secondary-physical-witness/2",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(endpoint, headers=headers, method="GET")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoPrimaryRedirects(),
        urllib.request.HTTPSHandler(context=context),
    )
    try:
        with opener.open(request, timeout=min(timeout_sec, 10.0)) as response:  # noqa: S310
            if int(response.status) != 200 or response.geturl() != endpoint:
                raise LiveFailureBatteryError("primary observation endpoint did not return direct HTTP 200")
            raw = response.read(maximum_bytes + 1)
    except LiveFailureBatteryError:
        raise
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise LiveFailureBatteryError("trusted primary observation endpoint is unavailable") from exc
    if len(raw) > maximum_bytes:
        raise LiveFailureBatteryError("primary observation response exceeded the bound")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LiveFailureBatteryError("primary observation response is invalid") from exc
    if not isinstance(value, dict):
        raise LiveFailureBatteryError("primary observation response is not an object")
    return value, ca_sha256


def _primary_health(timeout_sec: float, ca_file: Path) -> tuple[str, str]:
    value, ca_sha256 = _primary_json(
        PRIMARY_HEALTH_ENDPOINT,
        ca_file=ca_file,
        timeout_sec=timeout_sec,
        maximum_bytes=65_536,
    )
    version = value.get("version") if isinstance(value, dict) else None
    if value.get("status") != "ok" or not isinstance(version, str) or not 1 <= len(version) <= 80:
        raise LiveFailureBatteryError("primary health response is not ready")
    return version, ca_sha256


_PRODUCT_SNAPSHOT_KEYS = frozenset(
    {
        "schema",
        "role",
        "enabled",
        "configured",
        "mode",
        "state",
        "available",
        "profile_id",
        "profile_admission",
        "profile_manifest_match",
        "served_model_match",
        "context_cap_tokens",
        "selected_total",
        "success_total",
        "endpoint_request_total",
        "endpoint_success_total",
        "skipped_total",
        "primary_fallback_total",
        "probe_success_total",
        "probe_failure_total",
        "model_inventory_probe_success_total",
        "model_inventory_probe_failure_total",
        "fallback_reasons",
        "workload",
    }
)
_PRODUCT_WORKLOAD_KEYS = frozenset(
    {
        "name",
        "selected_total",
        "success_total",
        "skip_reasons",
        "fallback_reasons",
    }
)


def _counter(value: Any, *, label: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_COUNTER:
        raise LiveFailureBatteryError(f"primary secondary diagnostic {label} is invalid")
    return value


def _reason_counts(value: Any, *, label: str) -> dict[str, int]:
    if not isinstance(value, dict) or len(value) > len(_SECONDARY_FAILURES):
        raise LiveFailureBatteryError(f"primary secondary diagnostic {label} is invalid")
    result: dict[str, int] = {}
    for reason, count in value.items():
        if reason not in _SECONDARY_FAILURES:
            raise LiveFailureBatteryError(f"primary secondary diagnostic {label} is invalid")
        normalized = _counter(count, label=label)
        if normalized:
            result[str(reason)] = normalized
    return dict(sorted(result.items()))


def _product_snapshot(
    *,
    api_key_file: Path,
    ca_file: Path,
    timeout_sec: float,
) -> tuple[dict[str, Any], str]:
    api_key = _load_primary_api_key(api_key_file)
    report, ca_sha256 = _primary_json(
        PRIMARY_DIAGNOSTICS_ENDPOINT,
        ca_file=ca_file,
        timeout_sec=timeout_sec,
        api_key=api_key,
        maximum_bytes=1_048_576,
    )
    secondary = report.get("secondary")
    if not isinstance(secondary, dict):
        raise LiveFailureBatteryError("primary diagnostics have no secondary projection")
    workloads = secondary.get("workloads")
    workload = workloads.get(PRODUCT_WORKLOAD) if isinstance(workloads, dict) else None
    if not isinstance(workload, dict):
        raise LiveFailureBatteryError("primary diagnostics have no admitted product workload")
    snapshot = {
        "schema": secondary.get("schema"),
        "role": secondary.get("role"),
        "enabled": secondary.get("enabled"),
        "configured": secondary.get("configured"),
        "mode": secondary.get("mode"),
        "state": secondary.get("state"),
        "available": secondary.get("available"),
        "profile_id": secondary.get("profile"),
        "profile_admission": secondary.get("profile_admission"),
        "profile_manifest_match": secondary.get("profile_manifest_match"),
        "served_model_match": secondary.get("served_model_match"),
        "context_cap_tokens": _counter(
            secondary.get("context_cap_tokens"),
            label="context_cap_tokens",
        ),
        "selected_total": _counter(secondary.get("selected_total"), label="selected_total"),
        "success_total": _counter(secondary.get("success_total"), label="success_total"),
        "endpoint_request_total": _counter(
            secondary.get("endpoint_request_total"),
            label="endpoint_request_total",
        ),
        "endpoint_success_total": _counter(
            secondary.get("endpoint_success_total"),
            label="endpoint_success_total",
        ),
        "skipped_total": _counter(secondary.get("skipped_total"), label="skipped_total"),
        "primary_fallback_total": _counter(
            secondary.get("primary_fallback_total"),
            label="primary_fallback_total",
        ),
        "probe_success_total": _counter(
            secondary.get("probe_success_total"),
            label="probe_success_total",
        ),
        "probe_failure_total": _counter(
            secondary.get("probe_failure_total"),
            label="probe_failure_total",
        ),
        "model_inventory_probe_success_total": _counter(
            secondary.get("model_inventory_probe_success_total"),
            label="model_inventory_probe_success_total",
        ),
        "model_inventory_probe_failure_total": _counter(
            secondary.get("model_inventory_probe_failure_total"),
            label="model_inventory_probe_failure_total",
        ),
        "fallback_reasons": _reason_counts(
            secondary.get("fallback_reasons"),
            label="fallback_reasons",
        ),
        "workload": {
            "name": PRODUCT_WORKLOAD,
            "selected_total": _counter(
                workload.get("selected_total"),
                label="workload.selected_total",
            ),
            "success_total": _counter(
                workload.get("success_total"),
                label="workload.success_total",
            ),
            "skip_reasons": _reason_counts(
                workload.get("skip_reasons"),
                label="workload.skip_reasons",
            ),
            "fallback_reasons": _reason_counts(
                workload.get("fallback_reasons"),
                label="workload.fallback_reasons",
            ),
        },
    }
    if (
        set(snapshot) != _PRODUCT_SNAPSHOT_KEYS
        or set(snapshot["workload"]) != _PRODUCT_WORKLOAD_KEYS
        or snapshot["schema"] != "friday.optional-secondary-health.v1"
        or snapshot["role"] != "optional_advisory"
        or type(snapshot["enabled"]) is not bool
        or type(snapshot["configured"]) is not bool
        or snapshot["mode"] not in {"disabled", "shadow", "assist"}
        or snapshot["state"]
        not in {"disabled", "misconfigured", "probing", "healthy", "degraded", "cooldown"}
        or type(snapshot["available"]) is not bool
        or not isinstance(snapshot["profile_id"], str)
        or len(snapshot["profile_id"]) > 128
        or snapshot["profile_admission"] not in {"", "provisional_shadow", "accepted"}
        or type(snapshot["profile_manifest_match"]) is not bool
        or type(snapshot["served_model_match"]) is not bool
    ):
        raise LiveFailureBatteryError("primary secondary diagnostic projection is invalid")
    return snapshot, ca_sha256


def _require_product_identity(snapshot: dict[str, Any], *, available: bool) -> None:
    identity = evidence_identity()
    workload = snapshot.get("workload")
    counter_keys = (
        "context_cap_tokens",
        "selected_total",
        "success_total",
        "endpoint_request_total",
        "endpoint_success_total",
        "skipped_total",
        "primary_fallback_total",
        "probe_success_total",
        "probe_failure_total",
        "model_inventory_probe_success_total",
        "model_inventory_probe_failure_total",
    )
    if (
        set(snapshot) != _PRODUCT_SNAPSHOT_KEYS
        or snapshot.get("schema") != "friday.optional-secondary-health.v1"
        or snapshot.get("role") != "optional_advisory"
        or snapshot.get("enabled") is not True
        or snapshot.get("configured") is not True
        or snapshot.get("mode") != "assist"
        or snapshot.get("profile_id") != identity["candidate_profile_id"]
        or snapshot.get("profile_admission") != "accepted"
        or snapshot.get("profile_manifest_match") is not True
        or snapshot.get("served_model_match") is not True
        or snapshot.get("context_cap_tokens") != configured_profile_context_tokens()
        or snapshot.get("available") is not available
        or (available and snapshot.get("state") != "healthy")
        or (not available and snapshot.get("state") not in {"probing", "degraded", "cooldown"})
        or not isinstance(workload, dict)
        or set(workload) != _PRODUCT_WORKLOAD_KEYS
        or workload.get("name") != PRODUCT_WORKLOAD
    ):
        raise LiveFailureBatteryError("Friday is not bound to the exact accepted assist profile")
    for key in counter_keys:
        _counter(snapshot.get(key), label=key)
    assert isinstance(workload, dict)
    _counter(workload.get("selected_total"), label="workload.selected_total")
    _counter(workload.get("success_total"), label="workload.success_total")
    if (
        _reason_counts(snapshot.get("fallback_reasons"), label="fallback_reasons")
        != snapshot.get("fallback_reasons")
        or _reason_counts(workload.get("skip_reasons"), label="workload.skip_reasons")
        != workload.get("skip_reasons")
        or _reason_counts(workload.get("fallback_reasons"), label="workload.fallback_reasons")
        != workload.get("fallback_reasons")
    ):
        raise LiveFailureBatteryError("Friday product counters are not canonical")


def _counter_delta(after: int, before: int, *, label: str) -> int:
    if after < before:
        raise LiveFailureBatteryError(f"primary secondary diagnostic {label} moved backwards")
    return after - before


def _reason_deltas(
    after: dict[str, int],
    before: dict[str, int],
    *,
    label: str,
) -> dict[str, int]:
    result: dict[str, int] = {}
    for reason in sorted(set(after) | set(before)):
        delta = _counter_delta(after.get(reason, 0), before.get(reason, 0), label=label)
        if delta:
            result[reason] = delta
    return result


def _physical_off_product_deltas(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    before_workload = before["workload"]
    after_workload = after["workload"]
    selected_delta = _counter_delta(
        after_workload["selected_total"],
        before_workload["selected_total"],
        label="workload.selected_total",
    )
    success_delta = _counter_delta(
        after_workload["success_total"],
        before_workload["success_total"],
        label="workload.success_total",
    )
    fallback_delta = _counter_delta(
        after["primary_fallback_total"],
        before["primary_fallback_total"],
        label="primary_fallback_total",
    )
    workload_fallback_deltas = _reason_deltas(
        after_workload["fallback_reasons"],
        before_workload["fallback_reasons"],
        label="workload.fallback_reasons",
    )
    workload_skip_deltas = _reason_deltas(
        after_workload["skip_reasons"],
        before_workload["skip_reasons"],
        label="workload.skip_reasons",
    )
    if (
        selected_delta != 1
        or success_delta != 0
        or fallback_delta != 2
        or sum(workload_fallback_deltas.values()) != 2
        or workload_fallback_deltas != workload_skip_deltas
        or not workload_fallback_deltas
        or not set(workload_fallback_deltas) <= _PHYSICAL_OUTAGE_FAILURES
    ):
        raise LiveFailureBatteryError(
            "physical loss did not produce one mid-flight endpoint attempt and two exact product fallbacks"
        )
    return {
        "selected_during_outage_delta": selected_delta,
        "success_during_outage_delta": success_delta,
        "primary_fallback_during_outage_delta": fallback_delta,
        "fallback_reason_deltas": workload_fallback_deltas,
    }


def _readmission_product_deltas(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    before_workload = before["workload"]
    after_workload = after["workload"]
    selected_delta = _counter_delta(
        after_workload["selected_total"],
        before_workload["selected_total"],
        label="workload.selected_total",
    )
    success_delta = _counter_delta(
        after_workload["success_total"],
        before_workload["success_total"],
        label="workload.success_total",
    )
    fallback_delta = _counter_delta(
        after["primary_fallback_total"],
        before["primary_fallback_total"],
        label="primary_fallback_total",
    )
    probe_delta = _counter_delta(
        after["probe_success_total"],
        before["probe_success_total"],
        label="probe_success_total",
    )
    inventory_probe_delta = _counter_delta(
        after["model_inventory_probe_success_total"],
        before["model_inventory_probe_success_total"],
        label="model_inventory_probe_success_total",
    )
    fallback_reason_deltas = _reason_deltas(
        after_workload["fallback_reasons"],
        before_workload["fallback_reasons"],
        label="workload.fallback_reasons",
    )
    if (
        selected_delta != 1
        or success_delta != 1
        or fallback_delta != 0
        or probe_delta < 1
        or inventory_probe_delta < 1
        or fallback_reason_deltas
    ):
        raise LiveFailureBatteryError(
            "Friday did not readmit the exact secondary for one successful product request"
        )
    return {
        "selected_after_recovery_delta": selected_delta,
        "success_after_recovery_delta": success_delta,
        "primary_fallback_after_recovery_delta": fallback_delta,
        "probe_success_after_recovery_delta": probe_delta,
        "model_inventory_probe_success_after_recovery_delta": inventory_probe_delta,
    }


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
PRODUCT_BEGIN_KEYS = frozenset(
    {
        "schema",
        "status",
        "candidate_profile_id",
        "candidate_profile_sha256",
        "served_model_alias",
        "gateway_ca_certificate_sha256",
        "observer_source_head",
        "observer_runner_sha256",
        "physical_begin_state_sha256",
        "primary_pid",
        "primary_process_epoch_sha256",
        "primary_version",
        "primary_ca_certificate_sha256",
        "secondary_snapshot_before",
        "raw_content_retained",
        "credentials_retained",
    }
)
PRODUCT_OFF_KEYS = frozenset(
    {
        "schema",
        "status",
        "candidate_profile_id",
        "candidate_profile_sha256",
        "served_model_alias",
        "gateway_ca_certificate_sha256",
        "observer_source_head",
        "observer_runner_sha256",
        "physical_off_state_sha256",
        "product_begin_state_sha256",
        "primary_pid",
        "primary_process_epoch_sha256",
        "primary_version",
        "primary_ca_certificate_sha256",
        "secondary_snapshot_after_loss",
        "workload",
        "selected_during_outage_delta",
        "success_during_outage_delta",
        "primary_fallback_during_outage_delta",
        "fallback_reason_deltas",
        "raw_content_retained",
        "credentials_retained",
    }
)
PRODUCT_OBSERVATION_KEYS = frozenset(
    {
        "schema",
        "status",
        "candidate_profile_id",
        "candidate_profile_sha256",
        "served_model_alias",
        "gateway_ca_certificate_sha256",
        "observer_source_head",
        "observer_runner_sha256",
        "physical_observation_sha256",
        "product_off_state_sha256",
        "primary_pid",
        "primary_process_epoch_sha256",
        "primary_version",
        "primary_ca_certificate_sha256",
        "secondary_snapshot_after_readmission",
        "workload",
        "selected_after_recovery_delta",
        "success_after_recovery_delta",
        "primary_fallback_after_recovery_delta",
        "probe_success_after_recovery_delta",
        "model_inventory_probe_success_after_recovery_delta",
        "exact_profile_and_assist_mode_observed",
        "product_primary_fallback_counter_observed",
        "product_readmission_counter_observed",
        "raw_content_retained",
        "credentials_retained",
    }
)


def begin_physical_observation(
    *,
    candidate: Path,
    api_key_file: Path,
    ca_file: Path,
    primary_ca_file: Path,
    primary_pid: int,
    output: Path,
    primary_api_key_file: Path | None = None,
    product_output: Path | None = None,
    timeout_sec: float = 15.0,
) -> dict[str, Any]:
    if not 1.0 <= timeout_sec <= 60.0:
        raise LiveFailureBatteryError("physical observation timeout is outside the closed range")
    product_witness = primary_api_key_file is not None and product_output is not None
    if (primary_api_key_file is None) != (product_output is None):
        raise LiveFailureBatteryError("physical product witness inputs must be complete")
    if product_output is not None and output.absolute() == product_output.absolute():
        raise LiveFailureBatteryError("physical and product outputs must be distinct")
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
    primary_version, primary_ca_sha256 = _primary_health(timeout_sec, primary_ca_file)
    secondary_snapshot: dict[str, Any] | None = None
    if product_witness:
        assert primary_api_key_file is not None
        secondary_snapshot, diagnostics_ca_sha256 = _product_snapshot(
            api_key_file=primary_api_key_file,
            ca_file=primary_ca_file,
            timeout_sec=timeout_sec,
        )
        if diagnostics_ca_sha256 != primary_ca_sha256:
            raise LiveFailureBatteryError("primary observation CA identity changed between probes")
        _require_product_identity(secondary_snapshot, available=True)
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
    if not product_witness:
        output_sha256 = _write_new(output, state)
        return {
            "status": "awaiting_physical_power_loss",
            "next_step": "physically_power_off_laptop_then_record_off_state",
            "output_sha256": output_sha256,
        }
    assert secondary_snapshot is not None and product_output is not None
    output_sha256 = _sha256(_canonical(state))
    product_state = {
        "schema": PRODUCT_BEGIN_SCHEMA,
        "status": "awaiting_physical_product_failure",
        **evidence_identity(),
        "observer_source_head": source_head,
        "observer_runner_sha256": runner_sha256,
        "physical_begin_state_sha256": output_sha256,
        "primary_pid": primary_pid,
        "primary_process_epoch_sha256": primary_epoch,
        "primary_version": primary_version,
        "primary_ca_certificate_sha256": primary_ca_sha256,
        "secondary_snapshot_before": secondary_snapshot,
        "raw_content_retained": False,
        "credentials_retained": False,
    }
    if set(product_state) != PRODUCT_BEGIN_KEYS:
        raise LiveFailureBatteryError("physical product begin state is incomplete")
    observed_main_sha256, product_output_sha256 = _write_new_pair(
        output,
        state,
        product_output,
        product_state,
    )
    if observed_main_sha256 != output_sha256:
        raise LiveFailureBatteryError("physical begin output identity changed before creation")
    return {
        "status": "awaiting_physical_power_loss",
        "next_step": (
            "start one product request then power off laptop mid-flight; "
            "run one more product request while off; then record off state"
        ),
        "output_sha256": output_sha256,
        "product_output_sha256": product_output_sha256,
    }


def record_physical_power_loss(
    *,
    candidate: Path,
    ca_file: Path,
    primary_ca_file: Path,
    state_path: Path,
    output: Path,
    physical_power_loss_observed: bool,
    ordinary_fallback_observed: bool,
    mid_turn_fallback_observed: bool,
    no_effect_replay_observed: bool,
    v12_readiness_unchanged_observed: bool,
    primary_api_key_file: Path | None = None,
    product_state_path: Path | None = None,
    product_output: Path | None = None,
    timeout_sec: float = 15.0,
) -> dict[str, Any]:
    if not 1.0 <= timeout_sec <= 60.0:
        raise LiveFailureBatteryError("physical observation timeout is outside the closed range")
    product_inputs = (primary_api_key_file, product_state_path, product_output)
    product_witness = all(value is not None for value in product_inputs)
    if any(value is not None for value in product_inputs) and not product_witness:
        raise LiveFailureBatteryError("physical product witness inputs must be complete")
    if product_output is not None and output.absolute() == product_output.absolute():
        raise LiveFailureBatteryError("physical and product outputs must be distinct")
    configure_expected_model(candidate, ca_file)
    state, state_raw = _read_state(state_path)
    product_begin: dict[str, Any] | None = None
    product_begin_raw = b""
    if product_witness:
        assert product_state_path is not None
        product_begin, product_begin_raw = _read_state(product_state_path)
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
    begin_snapshot: dict[str, Any] | None = None
    if product_witness:
        assert product_begin is not None
        raw_begin_snapshot = product_begin.get("secondary_snapshot_before")
        if (
            set(product_begin) != PRODUCT_BEGIN_KEYS
            or product_begin.get("schema") != PRODUCT_BEGIN_SCHEMA
            or product_begin.get("status") != "awaiting_physical_product_failure"
            or any(product_begin.get(key) != value for key, value in evidence_identity().items())
            or product_begin.get("observer_source_head") != source_head
            or product_begin.get("observer_runner_sha256") != runner_sha256
            or product_begin.get("physical_begin_state_sha256") != _sha256(state_raw)
            or product_begin.get("primary_pid") != state.get("primary_pid")
            or product_begin.get("primary_process_epoch_sha256")
            != state.get("primary_process_epoch_before_sha256")
            or product_begin.get("primary_version") != state.get("primary_version")
            or not isinstance(product_begin.get("primary_ca_certificate_sha256"), str)
            or _SHA256.fullmatch(product_begin["primary_ca_certificate_sha256"]) is None
            or not isinstance(raw_begin_snapshot, dict)
            or product_begin.get("raw_content_retained") is not False
            or product_begin.get("credentials_retained") is not False
        ):
            raise LiveFailureBatteryError("physical product begin state is invalid")
        begin_snapshot = raw_begin_snapshot
        _require_product_identity(begin_snapshot, available=True)
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
    primary_version, primary_ca_sha256 = _primary_health(timeout_sec, primary_ca_file)
    if primary_version != state.get("primary_version"):
        raise LiveFailureBatteryError("Friday primary release changed during laptop power loss")
    secondary_after_loss: dict[str, Any] | None = None
    product_deltas: dict[str, Any] | None = None
    if product_witness:
        assert primary_api_key_file is not None and product_begin is not None
        if primary_ca_sha256 != product_begin.get("primary_ca_certificate_sha256"):
            raise LiveFailureBatteryError("primary observation CA identity changed between stages")
        secondary_after_loss, diagnostics_ca_sha256 = _product_snapshot(
            api_key_file=primary_api_key_file,
            ca_file=primary_ca_file,
            timeout_sec=timeout_sec,
        )
        if diagnostics_ca_sha256 != primary_ca_sha256:
            raise LiveFailureBatteryError("primary observation CA identity changed between probes")
        _require_product_identity(secondary_after_loss, available=False)
        assert begin_snapshot is not None
        product_deltas = _physical_off_product_deltas(begin_snapshot, secondary_after_loss)
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
    if not product_witness:
        output_sha256 = _write_new(output, off_state)
        return {
            "status": "awaiting_exact_candidate_recovery",
            "next_step": "power_on_laptop_then_finish_physical_observation",
            "output_sha256": output_sha256,
        }
    assert (
        product_begin is not None
        and secondary_after_loss is not None
        and product_deltas is not None
        and product_output is not None
    )
    output_sha256 = _sha256(_canonical(off_state))
    product_off_state = {
        "schema": PRODUCT_OFF_SCHEMA,
        "status": "product_fallback_observed_awaiting_readmission",
        **evidence_identity(),
        "observer_source_head": source_head,
        "observer_runner_sha256": runner_sha256,
        "physical_off_state_sha256": output_sha256,
        "product_begin_state_sha256": _sha256(product_begin_raw),
        "primary_pid": primary_pid,
        "primary_process_epoch_sha256": primary_epoch,
        "primary_version": primary_version,
        "primary_ca_certificate_sha256": primary_ca_sha256,
        "secondary_snapshot_after_loss": secondary_after_loss,
        "workload": PRODUCT_WORKLOAD,
        **product_deltas,
        "raw_content_retained": False,
        "credentials_retained": False,
    }
    if set(product_off_state) != PRODUCT_OFF_KEYS:
        raise LiveFailureBatteryError("physical product off state is incomplete")
    observed_main_sha256, product_output_sha256 = _write_new_pair(
        output,
        off_state,
        product_output,
        product_off_state,
    )
    if observed_main_sha256 != output_sha256:
        raise LiveFailureBatteryError("physical off output identity changed before creation")
    return {
        "status": "awaiting_exact_candidate_recovery",
        "next_step": (
            "power on laptop, wait out cooldown, run one eligible product request, "
            "then finish physical observation"
        ),
        "output_sha256": output_sha256,
        "product_output_sha256": product_output_sha256,
    }


def finish_physical_observation(
    *,
    candidate: Path,
    api_key_file: Path,
    ca_file: Path,
    primary_ca_file: Path,
    state_path: Path,
    output: Path,
    readmitted_without_primary_restart_observed: bool,
    primary_api_key_file: Path | None = None,
    product_state_path: Path | None = None,
    product_output: Path | None = None,
    timeout_sec: float = 15.0,
) -> dict[str, Any]:
    if not 1.0 <= timeout_sec <= 60.0:
        raise LiveFailureBatteryError("physical observation timeout is outside the closed range")
    product_inputs = (primary_api_key_file, product_state_path, product_output)
    product_witness = all(value is not None for value in product_inputs)
    if any(value is not None for value in product_inputs) and not product_witness:
        raise LiveFailureBatteryError("physical product witness inputs must be complete")
    if product_output is not None and output.absolute() == product_output.absolute():
        raise LiveFailureBatteryError("physical and product outputs must be distinct")
    try:
        configure_expected_model(candidate, ca_file)
        api_key = load_api_key(api_key_file)
        _ready_epoch(api_key, ca_file, timeout_sec)
    except EndpointError as exc:
        raise LiveFailureBatteryError("exact candidate did not recover after physical power loss") from exc
    state, state_raw = _read_state(state_path)
    product_off: dict[str, Any] | None = None
    product_off_raw = b""
    if product_witness:
        assert product_state_path is not None
        product_off, product_off_raw = _read_state(product_state_path)
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
    snapshot_after_loss: dict[str, Any] | None = None
    if product_witness:
        assert product_off is not None
        raw_snapshot_after_loss = product_off.get("secondary_snapshot_after_loss")
        fallback_reason_deltas = product_off.get("fallback_reason_deltas")
        canonical_fallback_deltas = _reason_counts(
            fallback_reason_deltas,
            label="fallback_reason_deltas",
        )
        if (
            set(product_off) != PRODUCT_OFF_KEYS
            or product_off.get("schema") != PRODUCT_OFF_SCHEMA
            or product_off.get("status") != "product_fallback_observed_awaiting_readmission"
            or any(product_off.get(key) != value for key, value in evidence_identity().items())
            or product_off.get("observer_source_head") != source_head
            or product_off.get("observer_runner_sha256") != runner_sha256
            or product_off.get("physical_off_state_sha256") != _sha256(state_raw)
            or not isinstance(product_off.get("product_begin_state_sha256"), str)
            or _SHA256.fullmatch(product_off["product_begin_state_sha256"]) is None
            or product_off.get("primary_pid") != state.get("primary_pid")
            or product_off.get("primary_process_epoch_sha256")
            != state.get("primary_process_epoch_before_sha256")
            or product_off.get("primary_version") != state.get("primary_version")
            or not isinstance(product_off.get("primary_ca_certificate_sha256"), str)
            or _SHA256.fullmatch(product_off["primary_ca_certificate_sha256"]) is None
            or not isinstance(raw_snapshot_after_loss, dict)
            or product_off.get("workload") != PRODUCT_WORKLOAD
            or product_off.get("selected_during_outage_delta") != 1
            or product_off.get("success_during_outage_delta") != 0
            or product_off.get("primary_fallback_during_outage_delta") != 2
            or canonical_fallback_deltas != fallback_reason_deltas
            or sum(canonical_fallback_deltas.values()) != 2
            or not set(canonical_fallback_deltas) <= _PHYSICAL_OUTAGE_FAILURES
            or product_off.get("raw_content_retained") is not False
            or product_off.get("credentials_retained") is not False
        ):
            raise LiveFailureBatteryError("physical product off state is invalid")
        snapshot_after_loss = raw_snapshot_after_loss
        _require_product_identity(snapshot_after_loss, available=False)
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
    primary_version, primary_ca_sha256 = _primary_health(timeout_sec, primary_ca_file)
    if primary_version != state.get("primary_version"):
        raise LiveFailureBatteryError("Friday primary release changed during physical observation")
    laptop_after = _laptop_boot_epoch_sha256()
    if laptop_after == state.get("laptop_boot_epoch_before_sha256"):
        raise LiveFailureBatteryError("laptop boot epoch did not change after physical power loss")
    secondary_after_readmission: dict[str, Any] | None = None
    readmission_deltas: dict[str, Any] | None = None
    if product_witness:
        assert primary_api_key_file is not None and product_off is not None
        if primary_ca_sha256 != product_off.get("primary_ca_certificate_sha256"):
            raise LiveFailureBatteryError("primary observation CA identity changed between stages")
        secondary_after_readmission, diagnostics_ca_sha256 = _product_snapshot(
            api_key_file=primary_api_key_file,
            ca_file=primary_ca_file,
            timeout_sec=timeout_sec,
        )
        if diagnostics_ca_sha256 != primary_ca_sha256:
            raise LiveFailureBatteryError("primary observation CA identity changed between probes")
        _require_product_identity(secondary_after_readmission, available=True)
        assert snapshot_after_loss is not None
        readmission_deltas = _readmission_product_deltas(
            snapshot_after_loss,
            secondary_after_readmission,
        )
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
    if not product_witness:
        output_sha256 = _write_new(output, evidence)
        return {"status": "physical_failure_observed", "output_sha256": output_sha256}
    assert (
        product_off is not None
        and secondary_after_readmission is not None
        and readmission_deltas is not None
        and product_output is not None
    )
    output_sha256 = _sha256(_canonical(evidence))
    product_evidence = {
        "schema": PRODUCT_OBSERVATION_SCHEMA,
        "status": "observed",
        **evidence_identity(),
        "observer_source_head": source_head,
        "observer_runner_sha256": runner_sha256,
        "physical_observation_sha256": output_sha256,
        "product_off_state_sha256": _sha256(product_off_raw),
        "primary_pid": primary_pid,
        "primary_process_epoch_sha256": primary_after,
        "primary_version": primary_version,
        "primary_ca_certificate_sha256": primary_ca_sha256,
        "secondary_snapshot_after_readmission": secondary_after_readmission,
        "workload": PRODUCT_WORKLOAD,
        **readmission_deltas,
        "exact_profile_and_assist_mode_observed": True,
        "product_primary_fallback_counter_observed": True,
        "product_readmission_counter_observed": True,
        "raw_content_retained": False,
        "credentials_retained": False,
    }
    if set(product_evidence) != PRODUCT_OBSERVATION_KEYS:
        raise LiveFailureBatteryError("physical product observation is incomplete")
    observed_main_sha256, product_output_sha256 = _write_new_pair(
        output,
        evidence,
        product_output,
        product_evidence,
    )
    if observed_main_sha256 != output_sha256:
        raise LiveFailureBatteryError("physical observation output identity changed before creation")
    return {
        "status": "physical_failure_observed",
        "output_sha256": output_sha256,
        "product_output_sha256": product_output_sha256,
    }


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
    begin.add_argument("--primary-api-key-file", type=Path)
    begin.add_argument("--primary-ca-file", required=True, type=Path)
    begin.add_argument("--primary-pid", required=True, type=int)
    begin.add_argument("--timeout-sec", default=15.0, type=float)
    begin.add_argument("--output", required=True, type=Path)
    begin.add_argument("--product-output", type=Path)

    off = commands.add_parser("physical-off")
    off.add_argument("--candidate", required=True, type=Path)
    off.add_argument("--ca-file", required=True, type=Path)
    off.add_argument("--primary-api-key-file", type=Path)
    off.add_argument("--primary-ca-file", required=True, type=Path)
    off.add_argument("--state", required=True, type=Path)
    off.add_argument("--product-state", type=Path)
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
    off.add_argument("--product-output", type=Path)

    finish = commands.add_parser("physical-finish")
    finish.add_argument("--candidate", required=True, type=Path)
    finish.add_argument("--api-key-file", required=True, type=Path)
    finish.add_argument("--ca-file", required=True, type=Path)
    finish.add_argument("--primary-api-key-file", type=Path)
    finish.add_argument("--primary-ca-file", required=True, type=Path)
    finish.add_argument("--state", required=True, type=Path)
    finish.add_argument("--product-state", type=Path)
    finish.add_argument("--timeout-sec", default=15.0, type=float)
    finish.add_argument(
        "--readmitted-without-primary-restart-operator-observed",
        required=True,
        action="store_true",
    )
    finish.add_argument("--output", required=True, type=Path)
    finish.add_argument("--product-output", type=Path)
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
                primary_api_key_file=args.primary_api_key_file,
                primary_ca_file=args.primary_ca_file,
                primary_pid=args.primary_pid,
                output=args.output,
                product_output=args.product_output,
                timeout_sec=args.timeout_sec,
            )
        elif args.command == "physical-off":
            result = record_physical_power_loss(
                candidate=args.candidate,
                ca_file=args.ca_file,
                primary_api_key_file=args.primary_api_key_file,
                primary_ca_file=args.primary_ca_file,
                state_path=args.state,
                product_state_path=args.product_state,
                output=args.output,
                product_output=args.product_output,
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
                primary_api_key_file=args.primary_api_key_file,
                primary_ca_file=args.primary_ca_file,
                state_path=args.state,
                product_state_path=args.product_state,
                output=args.output,
                product_output=args.product_output,
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
