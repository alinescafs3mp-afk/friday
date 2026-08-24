"""Closed host/runtime receipt and live GPU verification before SGLang import."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "friday.secondary-hardware-runtime.v1"
MAX_RECEIPT_BYTES = 16 * 1024
EXPECTED_ACCEPTED_RECEIPT_SHA256 = "0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f"
NVIDIA_SMI = "/usr/bin/nvidia-smi"
EXPECTED_GPU_UUID = "GPU-d7ef849e-55f5-f33c-2812-9dc32b644b07"
EXPECTED_GPU_NAME = "NVIDIA GeForce RTX 5080 Laptop GPU"
EXPECTED_GPU_MEMORY_TOTAL_MIB = 16_303
EXPECTED_GPU_COMPUTE_CAPABILITY = "12.0"
EXPECTED_GPU_DRIVER_VERSION = "610.88"
NVIDIA_SMI_COMMAND = (
    NVIDIA_SMI,
    "--id=0",
    "--query-gpu=uuid,name,memory.total,compute_cap,driver_version",
    "--format=csv,noheader,nounits",
)

EXPECTED_DOCKER = {
    "client_api_version": "1.55",
    "client_version": "29.7.2",
    "compose_version": "5.4.0",
    "desktop_file_version": "4.87.0.236836",
    "desktop_product_version": "4.87.0.236836",
    "server_api_version": "1.55",
    "server_architecture": "x86_64",
    "server_os": "linux",
    "server_version": "29.7.2",
}
EXPECTED_GPU = {
    "compute_capability": EXPECTED_GPU_COMPUTE_CAPABILITY,
    "driver_version": EXPECTED_GPU_DRIVER_VERSION,
    "memory_total_mib": EXPECTED_GPU_MEMORY_TOTAL_MIB,
    "name": EXPECTED_GPU_NAME,
    "uuid": EXPECTED_GPU_UUID,
}
EXPECTED_WINDOWS = {
    "build": "26200",
    "caption": "Майкрософт Windows 11 Pro",
    "version": "10.0.26200",
}
EXPECTED_WSL = {
    "direct3d_version": "1.611.1-81528511",
    "dxcore_version": "10.0.26100.1-240331-1435.ge-release",
    "kernel_version": "6.6.114.1-1",
    "msrdc_version": "1.2.6676",
    "version": "2.7.3.0",
    "windows_component_version": "10.0.26200.9168",
    "wslg_version": "1.0.73",
}
_TOP_LEVEL_KEYS = {"docker", "gpu", "schema", "status", "windows", "wsl"}


class HardwareRuntimeContractError(RuntimeError):
    """A bounded, content-free startup rejection."""


@dataclass(frozen=True, slots=True)
class HardwareRuntimeReceipt:
    receipt_sha256: str
    gpu_uuid: str
    driver_version: str
    memory_total_mib: int
    compute_capability: str


def canonical_receipt_json(value: Any) -> bytes:
    """Return the sole accepted canonical UTF-8 representation."""

    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _reject_constant(_value: str) -> None:
    raise HardwareRuntimeContractError("hardware receipt contains a non-finite number")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise HardwareRuntimeContractError("hardware receipt contains a duplicate key")
        value[key] = item
    return value


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _read_exact_receipt(path: Path, expected_sha256: str) -> dict[str, Any]:
    if not _is_sha256(expected_sha256) or expected_sha256 != EXPECTED_ACCEPTED_RECEIPT_SHA256:
        raise HardwareRuntimeContractError("hardware receipt expectation is invalid")
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= MAX_RECEIPT_BYTES:
            raise HardwareRuntimeContractError("hardware receipt is absent or unsafe")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_dev != metadata.st_dev
            or before.st_ino != metadata.st_ino
            or before.st_size != metadata.st_size
            or before.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise HardwareRuntimeContractError("hardware receipt changed before verification")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(MAX_RECEIPT_BYTES + 1)
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
            raise HardwareRuntimeContractError("hardware receipt changed during verification")
    except HardwareRuntimeContractError:
        raise
    except OSError as exc:
        raise HardwareRuntimeContractError("hardware receipt is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise HardwareRuntimeContractError("hardware receipt hash differs from the profile")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise HardwareRuntimeContractError("hardware receipt encoding is invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            parse_constant=_reject_constant,
            object_pairs_hook=_strict_object,
        )
    except HardwareRuntimeContractError:
        raise
    except Exception:
        raise HardwareRuntimeContractError("hardware receipt is not strict UTF-8 JSON") from None
    if not isinstance(value, dict) or raw != canonical_receipt_json(value):
        raise HardwareRuntimeContractError("hardware receipt is not canonical")
    return value


def _validate_receipt(value: dict[str, Any]) -> None:
    if set(value) != _TOP_LEVEL_KEYS or value.get("schema") != SCHEMA:
        raise HardwareRuntimeContractError("hardware receipt schema is invalid")
    if value.get("status") != "accepted":
        raise HardwareRuntimeContractError("hardware receipt is not accepted")
    if value.get("docker") != EXPECTED_DOCKER:
        raise HardwareRuntimeContractError("Docker runtime differs from the closed receipt")
    if value.get("gpu") != EXPECTED_GPU:
        raise HardwareRuntimeContractError("GPU identity differs from the closed receipt")
    if value.get("windows") != EXPECTED_WINDOWS:
        raise HardwareRuntimeContractError("Windows runtime differs from the closed receipt")
    if value.get("wsl") != EXPECTED_WSL:
        raise HardwareRuntimeContractError("WSL runtime differs from the closed receipt")


def _safe_subprocess_environment() -> dict[str, str]:
    environment = {"LANG": "C", "LC_ALL": "C"}
    # The pinned CUDA image may need its image-owned library search path and
    # NVIDIA projection. Neither value supplies an expected identity: every
    # resulting field is compared to the profile-bound receipt.
    for name in ("LD_LIBRARY_PATH", "NVIDIA_DRIVER_CAPABILITIES", "NVIDIA_VISIBLE_DEVICES"):
        value = os.environ.get(name)
        if value and len(value) <= 4096 and "\0" not in value:
            environment[name] = value
    return environment


def observe_live_gpu() -> dict[str, Any]:
    """Return one bounded exact in-container GPU projection."""

    try:
        result = subprocess.run(
            NVIDIA_SMI_COMMAND,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=_safe_subprocess_environment(),
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HardwareRuntimeContractError("bounded nvidia-smi probe failed") from exc
    if (
        result.returncode != 0
        or len(result.stdout) > 1024
        or len(result.stderr) > 1024
        or result.stderr.strip()
    ):
        raise HardwareRuntimeContractError("bounded nvidia-smi probe was not clean")
    try:
        text = result.stdout.decode("ascii", errors="strict").strip("\r\n")
    except UnicodeError as exc:
        raise HardwareRuntimeContractError("nvidia-smi projection encoding is invalid") from exc
    if "\n" in text or "\r" in text:
        raise HardwareRuntimeContractError("nvidia-smi returned more than one GPU row")
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 5 or not parts[2].isdigit():
        raise HardwareRuntimeContractError("nvidia-smi projection shape is invalid")
    return {
        "compute_capability": parts[3],
        "driver_version": parts[4],
        "memory_total_mib": int(parts[2]),
        "name": parts[1],
        "uuid": parts[0],
    }


def verify_live_hardware_runtime(
    accepted_receipt_path: Path,
    expected_receipt_sha256: str,
) -> HardwareRuntimeReceipt:
    """Verify the host receipt and live GPU before importing the model runtime."""

    value = _read_exact_receipt(accepted_receipt_path, expected_receipt_sha256)
    _validate_receipt(value)
    if observe_live_gpu() != EXPECTED_GPU:
        raise HardwareRuntimeContractError("live GPU differs from the profile-bound receipt")
    return HardwareRuntimeReceipt(
        receipt_sha256=expected_receipt_sha256,
        gpu_uuid=EXPECTED_GPU_UUID,
        driver_version=EXPECTED_GPU_DRIVER_VERSION,
        memory_total_mib=EXPECTED_GPU_MEMORY_TOTAL_MIB,
        compute_capability=EXPECTED_GPU_COMPUTE_CAPABILITY,
    )
