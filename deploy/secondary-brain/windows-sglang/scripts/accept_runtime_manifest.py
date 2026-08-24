#!/usr/bin/env python3
"""Promote the exact secondary runtime template from closed live evidence."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

PROMOTION_SCHEMA = "friday.secondary-runtime-manifest-promotion.v1"
RUNTIME_SCHEMA = "friday.secondary-sglang-runtime.v1"
PREFLIGHT_SCHEMA = "friday.secondary-windows-preflight.v2"
HARDWARE_SCHEMA = "friday.secondary-hardware-runtime.v1"

MAX_TEMPLATE_BYTES = 16 * 1024
MAX_PREFLIGHT_BYTES = 64 * 1024
MAX_HARDWARE_BYTES = 16 * 1024

EXPECTED_TEMPLATE_SHA256 = "2914f7140d5055c0620aaee28e1d6e4725d164b3bf35d41819147d8e07bd9bf3"
EXPECTED_ACCEPTED_RUNTIME_SHA256 = "15be7b3bdaa3cd76ace1bcc93ca461598a9583d920f4f3e55924db2f6b643428"
EXPECTED_OBSERVED_HARDWARE_SHA256 = "7b850221e7e11ac0063971d7baaf627c96eae5441368f1907cc070106832b0f3"
EXPECTED_ACCEPTED_HARDWARE_SHA256 = "0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f"

SGLANG_IMAGE = (
    "lmsysorg/sglang@sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405"
)
SGLANG_IMAGE_ID = "sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405"
SGLANG_CONFIG_DIGEST = "sha256:f7adc6c05df9ff711b82ad291cf1db6eaf30590c4d929833d632abfef3895efc"
SGLANG_SOURCE_REVISION = "29481685462732237d80d86076d6563e1f658102"
GATEWAY_IMAGE = (
    "nginxinc/nginx-unprivileged@"
    "sha256:d61d7ef52430df468e74ed6ee6e914429b80e20ba988e3176278a73165f876cf"
)
GATEWAY_IMAGE_ID = "sha256:d61d7ef52430df468e74ed6ee6e914429b80e20ba988e3176278a73165f876cf"
GATEWAY_PLATFORM_DIGEST = (
    "sha256:8d764dd92e0b48d0ca94887dc0fe1df6dffc5200b25b2efcc2deb7ffb61d714c"
)
GATEWAY_CONFIG_DIGEST = "sha256:89dc7d054bddca245db3d5a779e363007d0e75b1161cfe2f283ebeaf0ed90d50"
PUBLISHED_ENDPOINT = "https://192.168.1.35:8443/v1"
OBSERVED_HARDWARE_PATH = (
    r"C:\ProgramData\FridaySecondary\bundle\evidence\hardware-runtime.observed.json"
)

EXPECTED_RUNTIME_VERSIONS = {
    "cuda_runtime_version": "13.0",
    "flashinfer_version": "0.6.15.post1",
    "pytorch_version": "2.11.0+cu130",
    "sgl_kernel_version": "0.4.5",
    "sglang_version": "0.5.17",
}
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
    "compute_capability": "12.0",
    "driver_version": "610.88",
    "memory_total_mib": 16_303,
    "name": "NVIDIA GeForce RTX 5080 Laptop GPU",
    "uuid": "GPU-d7ef849e-55f5-f33c-2812-9dc32b644b07",
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
EXPECTED_HARDWARE = {
    "docker": EXPECTED_DOCKER,
    "gpu": EXPECTED_GPU,
    "schema": HARDWARE_SCHEMA,
    "status": "accepted",
    "windows": EXPECTED_WINDOWS,
    "wsl": EXPECTED_WSL,
}
EXPECTED_RUNTIME_TEMPLATE = {
    "schema": RUNTIME_SCHEMA,
    "status": "template_not_accepted",
    "image_ref": SGLANG_IMAGE,
    "image_id": SGLANG_IMAGE_ID,
    "image_config_digest": SGLANG_CONFIG_DIGEST,
    "image_oci_manifest_digest": SGLANG_IMAGE_ID,
    "gateway_image_ref": GATEWAY_IMAGE,
    "gateway_image_id": GATEWAY_IMAGE_ID,
    "gateway_expected_version": "1.31.3",
    "gateway_expected_user": "101",
    "gateway_expected_platform": "linux/amd64",
    "gateway_expected_platform_manifest_digest": GATEWAY_PLATFORM_DIGEST,
    "gateway_expected_config_digest": GATEWAY_CONFIG_DIGEST,
    "sglang_version": "0.5.17",
    "sglang_git_revision": SGLANG_SOURCE_REVISION,
    "cuda_runtime_version": "13.0",
    "pytorch_version": "2.11.0+cu130",
    "flashinfer_version": "0.6.15.post1",
    "sgl_kernel_version": "0.4.5",
    "nvidia_driver_version": "610.88",
    "gpu_name": "NVIDIA GeForce RTX 5080 Laptop GPU",
    "gpu_vram_mib": 16_303,
    "gpu_compute_capability": "12.0",
    "served_model_alias_policy": "friday-secondary-{profile_id}",
    "published_endpoint": PUBLISHED_ENDPOINT,
    "plain_sglang_lan_published": False,
    "note": "No runtime identity is accepted until measured on 192.168.1.35.",
}
EXPECTED_GPU_CANARY = {
    "image_ref": SGLANG_IMAGE,
    "image_id": SGLANG_IMAGE_ID,
    "observation": {
        "compute_capability": [12, 0],
        "kernel_sum": 16_773_120.0,
        "memory_total_bytes": 17_094_475_776,
        "name": EXPECTED_GPU["name"],
    },
}
EXPECTED_SGLANG_HELP = {
    "image_ref": SGLANG_IMAGE,
    "image_id": SGLANG_IMAGE_ID,
    "image_config_digest": SGLANG_CONFIG_DIGEST,
    "image_oci_manifest_digest": SGLANG_IMAGE_ID,
    "compose_exact_selector_verified": True,
    "required_flag_count": 29,
    "required_flags_present": True,
    "required_flags_sha256": "15defb43aa2cef5f5df941822bbacd170c787513ef136cd6f951a6c0580d1cd9",
    "runtime_versions": EXPECTED_RUNTIME_VERSIONS,
}
EXPECTED_GATEWAY = {
    "image_ref": GATEWAY_IMAGE,
    "image_id": GATEWAY_IMAGE_ID,
    "platform": "linux/amd64",
    "user": "101",
    "nginx_version": "1.31.3",
    "runtime_probe": "verified",
    "compose_exact_selector_verified": True,
    "platform_manifest_digest": GATEWAY_PLATFORM_DIGEST,
    "config_digest": GATEWAY_CONFIG_DIGEST,
}

_PREFLIGHT_KEYS = {
    "schema",
    "status",
    "observed_at",
    "computer",
    "wsl",
    "docker",
    "host_gpu",
    "runtime_gpu",
    "gpu_container_canary",
    "sglang_help",
    "gateway_image",
    "hardware_runtime_receipt",
    "operator_checks_required",
    "credentials_retained",
}
_COMPUTER_KEYS = {
    "manufacturer",
    "model",
    "windows_caption",
    "windows_version",
    "windows_build",
    "expected_address",
    "expected_address_present",
}
_WSL_EVIDENCE_KEYS = {"components", "version_output_sha256", "status_output_sha256"}
_DOCKER_EVIDENCE_KEYS = {*EXPECTED_DOCKER, "desktop_autostart_observed"}
_HARDWARE_EVIDENCE_KEYS = {"status", "sha256", "output_path"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OBSERVED_AT = re.compile(
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r"T(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})\.[0-9]{7}Z\Z"
)


class RuntimeManifestPromotionError(RuntimeError):
    """A bounded, content-free promotion rejection."""


def canonical_json(value: Any) -> bytes:
    """Return the sole emitted UTF-8 JSON representation."""

    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_reparse(metadata: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and getattr(metadata, "st_file_attributes", 0) & flag)


def _read_regular(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or _is_reparse(metadata)
            or not 1 <= metadata.st_size <= maximum_bytes
        ):
            raise RuntimeManifestPromotionError(f"{label} is not a bounded regular file")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or _is_reparse(before)
            or before.st_dev != metadata.st_dev
            or before.st_ino != metadata.st_ino
            or before.st_size != metadata.st_size
            or before.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise RuntimeManifestPromotionError(f"{label} changed before verification")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(maximum_bytes + 1)
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
            raise RuntimeManifestPromotionError(f"{label} changed during verification")
        return raw
    except RuntimeManifestPromotionError:
        raise
    except OSError as exc:
        raise RuntimeManifestPromotionError(f"{label} is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _reject_constant(_value: str) -> None:
    raise RuntimeManifestPromotionError("JSON contains a non-finite number")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RuntimeManifestPromotionError("JSON contains a duplicate key")
        value[key] = item
    return value


def _read_json(path: Path, *, maximum_bytes: int, label: str) -> tuple[dict[str, Any], bytes]:
    raw = _read_regular(path, maximum_bytes=maximum_bytes, label=label)
    if raw.startswith(b"\xef\xbb\xbf"):
        raise RuntimeManifestPromotionError(f"{label} encoding is invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            parse_constant=_reject_constant,
            object_pairs_hook=_strict_object,
        )
    except RuntimeManifestPromotionError:
        raise
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeManifestPromotionError(f"{label} is not strict UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise RuntimeManifestPromotionError(f"{label} is not a JSON object")
    return value, raw


def _matches_exactly(value: Any, expected: Any) -> bool:
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(value) == set(expected) and all(
            _matches_exactly(value[key], expected[key]) for key in expected
        )
    if isinstance(expected, list):
        return len(value) == len(expected) and all(
            _matches_exactly(item, expected_item)
            for item, expected_item in zip(value, expected, strict=True)
        )
    return bool(value == expected)


def _require_keys(value: Any, keys: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise RuntimeManifestPromotionError(f"{label} shape is invalid")
    return value


def _require_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RuntimeManifestPromotionError(f"{label} identity is invalid")
    return value


def _require_bounded_text(value: Any, *, label: str, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or "\x00" in value
        or "\r" in value
        or "\n" in value
    ):
        raise RuntimeManifestPromotionError(f"{label} is invalid")
    return value


def _require_observed_at(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimeManifestPromotionError("automated preflight timestamp is invalid")
    match = _OBSERVED_AT.fullmatch(value)
    if match is None:
        raise RuntimeManifestPromotionError("automated preflight timestamp is invalid")
    try:
        datetime.strptime(
            f"{match.group('date')}T{match.group('time')}",
            "%Y-%m-%dT%H:%M:%S",
        )
    except ValueError:
        raise RuntimeManifestPromotionError("automated preflight timestamp is invalid") from None
    return value


def _validate_template(value: dict[str, Any], raw: bytes) -> None:
    if not _matches_exactly(value, EXPECTED_RUNTIME_TEMPLATE):
        raise RuntimeManifestPromotionError("runtime template identity is invalid")
    if _sha256(raw) != EXPECTED_TEMPLATE_SHA256:
        raise RuntimeManifestPromotionError("runtime template raw identity is invalid")


def _validate_hardware(value: dict[str, Any], raw: bytes) -> None:
    expected_raw = canonical_json(EXPECTED_HARDWARE)
    if _sha256(expected_raw) != EXPECTED_ACCEPTED_HARDWARE_SHA256:
        raise RuntimeManifestPromotionError("code-owned hardware identity is inconsistent")
    if not _matches_exactly(value, EXPECTED_HARDWARE) or raw != expected_raw:
        raise RuntimeManifestPromotionError("accepted hardware receipt identity is invalid")
    if _sha256(raw) != EXPECTED_ACCEPTED_HARDWARE_SHA256:
        raise RuntimeManifestPromotionError("accepted hardware receipt hash is invalid")


def _validate_preflight(
    value: dict[str, Any],
    *,
    hardware: dict[str, Any],
    template: dict[str, Any],
) -> None:
    _require_keys(value, _PREFLIGHT_KEYS, label="automated preflight")
    _require_observed_at(value.get("observed_at"))
    if (
        value.get("schema") != PREFLIGHT_SCHEMA
        or value.get("status") != "automated_preflight_checks_passed"
        or value.get("credentials_retained") is not False
        or value.get("operator_checks_required")
        != ["wsl_update_state", "docker_desktop_wsl2_setting", "ac_sleep_disabled"]
    ):
        raise RuntimeManifestPromotionError("automated preflight status is invalid")

    computer = _require_keys(value.get("computer"), _COMPUTER_KEYS, label="computer projection")
    _require_bounded_text(computer.get("manufacturer"), label="computer manufacturer")
    _require_bounded_text(computer.get("model"), label="computer model")
    if (
        computer.get("windows_caption") != hardware["windows"]["caption"]
        or computer.get("windows_version") != hardware["windows"]["version"]
        or computer.get("windows_build") != hardware["windows"]["build"]
        or computer.get("expected_address") != "192.168.1.35"
        or computer.get("expected_address_present") is not True
    ):
        raise RuntimeManifestPromotionError("computer projection identity is invalid")

    wsl = _require_keys(value.get("wsl"), _WSL_EVIDENCE_KEYS, label="WSL projection")
    if not _matches_exactly(wsl.get("components"), hardware["wsl"]):
        raise RuntimeManifestPromotionError("WSL projection identity is invalid")
    _require_sha256(wsl.get("version_output_sha256"), label="WSL version output")
    _require_sha256(wsl.get("status_output_sha256"), label="WSL status output")

    docker = _require_keys(value.get("docker"), _DOCKER_EVIDENCE_KEYS, label="Docker projection")
    if any(docker.get(key) != expected for key, expected in hardware["docker"].items()):
        raise RuntimeManifestPromotionError("Docker projection identity is invalid")
    if type(docker.get("desktop_autostart_observed")) is not bool:
        raise RuntimeManifestPromotionError("Docker autostart projection type is invalid")

    if not _matches_exactly(value.get("host_gpu"), hardware["gpu"]):
        raise RuntimeManifestPromotionError("host GPU projection identity is invalid")
    if not _matches_exactly(value.get("runtime_gpu"), hardware["gpu"]):
        raise RuntimeManifestPromotionError("runtime GPU projection identity is invalid")
    if not _matches_exactly(value.get("gpu_container_canary"), EXPECTED_GPU_CANARY):
        raise RuntimeManifestPromotionError("GPU canary identity is invalid")

    if not _matches_exactly(value.get("sglang_help"), EXPECTED_SGLANG_HELP):
        raise RuntimeManifestPromotionError("SGLang evidence identity is invalid")
    if not _matches_exactly(value.get("gateway_image"), EXPECTED_GATEWAY):
        raise RuntimeManifestPromotionError("gateway evidence identity is invalid")

    hardware_evidence = _require_keys(
        value.get("hardware_runtime_receipt"),
        _HARDWARE_EVIDENCE_KEYS,
        label="observed hardware receipt projection",
    )
    if (
        hardware_evidence.get("status") != "observed_unaccepted"
        or hardware_evidence.get("sha256") != EXPECTED_OBSERVED_HARDWARE_SHA256
        or hardware_evidence.get("output_path") != OBSERVED_HARDWARE_PATH
    ):
        raise RuntimeManifestPromotionError("observed hardware receipt identity is invalid")

    if (
        template["image_ref"] != value["sglang_help"]["image_ref"]
        or template["image_id"] != value["sglang_help"]["image_id"]
        or template["image_config_digest"] != value["sglang_help"]["image_config_digest"]
        or template["image_oci_manifest_digest"]
        != value["sglang_help"]["image_oci_manifest_digest"]
        or any(
            template[key] != value["sglang_help"]["runtime_versions"][key]
            for key in EXPECTED_RUNTIME_VERSIONS
        )
        or template["gateway_image_ref"] != value["gateway_image"]["image_ref"]
        or template["gateway_image_id"] != value["gateway_image"]["image_id"]
        or template["gateway_expected_platform_manifest_digest"]
        != value["gateway_image"]["platform_manifest_digest"]
        or template["gateway_expected_config_digest"] != value["gateway_image"]["config_digest"]
        or template["nvidia_driver_version"] != hardware["gpu"]["driver_version"]
        or template["gpu_name"] != hardware["gpu"]["name"]
        or template["gpu_vram_mib"] != hardware["gpu"]["memory_total_mib"]
        or template["gpu_compute_capability"] != hardware["gpu"]["compute_capability"]
    ):
        raise RuntimeManifestPromotionError("runtime evidence chain is inconsistent")


def _write_atomic_exclusive(path: Path, raw: bytes) -> None:
    target = path.absolute()
    parent = target.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise RuntimeManifestPromotionError("runtime manifest output parent is unavailable") from exc
    if not stat.S_ISDIR(parent_metadata.st_mode) or _is_reparse(parent_metadata):
        raise RuntimeManifestPromotionError("runtime manifest output parent is unsafe")
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RuntimeManifestPromotionError("runtime manifest output path is unavailable") from exc
    else:
        raise RuntimeManifestPromotionError("runtime manifest output path is not new")

    temporary = parent / f".{target.name}.tmp-{os.getpid()}-{secrets.token_hex(16)}"
    descriptor: int | None = None
    written_metadata: os.stat_result | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        view = memoryview(raw)
        offset = 0
        while offset < len(view):
            count = os.write(descriptor, view[offset:])
            if count <= 0:
                raise OSError("short write")
            offset += count
        os.fsync(descriptor)
        written_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(written_metadata.st_mode)
            or _is_reparse(written_metadata)
            or written_metadata.st_size != len(raw)
        ):
            raise OSError("written output identity changed")
        os.close(descriptor)
        descriptor = None
        temporary_metadata = temporary.lstat()
        if (
            not stat.S_ISREG(temporary_metadata.st_mode)
            or _is_reparse(temporary_metadata)
            or temporary_metadata.st_dev != written_metadata.st_dev
            or temporary_metadata.st_ino != written_metadata.st_ino
            or temporary_metadata.st_size != len(raw)
        ):
            raise OSError("temporary output identity changed")
        os.link(temporary, target)
        target_metadata = target.lstat()
        if (
            not stat.S_ISREG(target_metadata.st_mode)
            or _is_reparse(target_metadata)
            or target_metadata.st_dev != written_metadata.st_dev
            or target_metadata.st_ino != written_metadata.st_ino
            or target_metadata.st_size != len(raw)
        ):
            raise OSError("installed output identity changed")
        installed_raw = _read_regular(
            target,
            maximum_bytes=len(raw),
            label="installed runtime manifest",
        )
        if installed_raw != raw or _sha256(installed_raw) != _sha256(raw):
            raise OSError("installed output content changed")
    except OSError as exc:
        # Never unlink the target here: another writer can replace it between
        # the identity check and cleanup. A failed promotion leaves any target
        # in place for explicit operator inspection and never deletes peer data.
        raise RuntimeManifestPromotionError("runtime manifest output could not be created") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink()


def promote_runtime_manifest(
    template_path: Path,
    preflight_evidence_path: Path,
    hardware_receipt_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Validate the closed evidence chain and create one accepted manifest."""

    template, template_raw = _read_json(
        template_path,
        maximum_bytes=MAX_TEMPLATE_BYTES,
        label="runtime template",
    )
    _validate_template(template, template_raw)
    hardware, hardware_raw = _read_json(
        hardware_receipt_path,
        maximum_bytes=MAX_HARDWARE_BYTES,
        label="accepted hardware receipt",
    )
    _validate_hardware(hardware, hardware_raw)
    preflight, preflight_raw = _read_json(
        preflight_evidence_path,
        maximum_bytes=MAX_PREFLIGHT_BYTES,
        label="automated preflight evidence",
    )
    _validate_preflight(preflight, hardware=hardware, template=template)

    accepted = dict(template)
    accepted["status"] = "accepted"
    if any(
        not _matches_exactly(accepted[key], template[key])
        for key in template
        if key != "status"
    ):
        raise RuntimeManifestPromotionError("runtime promotion changed a non-status field")
    accepted_raw = canonical_json(accepted)
    if _sha256(accepted_raw) != EXPECTED_ACCEPTED_RUNTIME_SHA256:
        raise RuntimeManifestPromotionError("accepted runtime identity is inconsistent")
    _write_atomic_exclusive(output_path, accepted_raw)
    return {
        "schema": PROMOTION_SCHEMA,
        "status": "accepted_runtime_manifest_created",
        "template_sha256": _sha256(template_raw),
        "automated_preflight_sha256": _sha256(preflight_raw),
        "hardware_runtime_receipt_sha256": _sha256(hardware_raw),
        "runtime_manifest_sha256": _sha256(accepted_raw),
        "overwritten": False,
        "raw_content_retained": False,
        "credentials_retained": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--preflight-evidence", required=True, type=Path)
    parser.add_argument("--hardware-receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = promote_runtime_manifest(
            args.template,
            args.preflight_evidence,
            args.hardware_receipt,
            args.output,
        )
    except RuntimeManifestPromotionError as exc:
        result = {
            "schema": PROMOTION_SCHEMA,
            "status": "rejected",
            "reason": str(exc),
            "raw_content_retained": False,
            "credentials_retained": False,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
