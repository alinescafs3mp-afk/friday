#!/usr/bin/env python3
"""Build and promote one immutable secondary runtime profile from exact evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

from endpoint_common import EXPECTED_HARDWARE_RUNTIME_RECEIPT_SHA256
from failure_battery import JOURNEY_TESTS, SUITE_FILES, journey_contract_sha256

_BUNDLE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BUNDLE_ROOT / "runtime"))
from source_model_manifest import (  # type: ignore[import-not-found]  # noqa: E402
    SOURCE_FILE_COUNT,
    SOURCE_MANIFEST_RAW_SHA256,
    SOURCE_REPOSITORY,
    SOURCE_REVISION,
    SOURCE_TOTAL_BYTES,
    SourceModelManifestError,
    verify_source_model_manifest,
)

PROFILE_SCHEMA = "friday.secondary-runtime-profile.v2"
CAPACITY_SCHEMA = "friday.secondary-capacity-evidence.v1"
FAILURE_SCHEMA = "friday.secondary-failure-battery.v1"
RUNTIME_IMAGE = "lmsysorg/sglang@sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405"
RUNTIME_IMAGE_LOCAL_ID = "sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405"
RUNTIME_IMAGE_CONFIG_DIGEST = "sha256:f7adc6c05df9ff711b82ad291cf1db6eaf30590c4d929833d632abfef3895efc"
RUNTIME_IMAGE_OCI_MANIFEST_DIGEST = "sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405"
RUNTIME_SOURCE_REVISION = "29481685462732237d80d86076d6563e1f658102"
RUNTIME_VERSIONS = {
    "sglang_version": "0.5.17",
    "cuda_runtime_version": "13.0",
    "pytorch_version": "2.11.0+cu130",
    "flashinfer_version": "0.6.15.post1",
    "sgl_kernel_version": "0.4.5",
}
MODEL_PATH = "/source/snapshot"
ENDPOINT = "https://192.168.1.35:8443/v1"
GATEWAY_IMAGE = (
    "nginxinc/nginx-unprivileged@sha256:d61d7ef52430df468e74ed6ee6e914429b80e20ba988e3176278a73165f876cf"
)
GATEWAY_IMAGE_CONFIG_DIGEST = "sha256:89dc7d054bddca245db3d5a779e363007d0e75b1161cfe2f283ebeaf0ed90d50"
GATEWAY_IMAGE_LOCAL_ID = "sha256:8d764dd92e0b48d0ca94887dc0fe1df6dffc5200b25b2efcc2deb7ffb61d714c"
CONTEXT_LADDER = frozenset({4096, 8192, 12288, 16384, 24576, 32768})
MEMORY_GRID = frozenset({"0.86", "0.88", "0.90", "0.92", "0.94", "0.96", "0.97"})
MODES = frozenset({"shadow", "assist"})
WORKLOADS = frozenset(
    {
        "classify",
        "extract",
        "query_rewrite",
        "summarize",
        "document_map",
        "critique",
        "verify",
        "plan_candidate",
    }
)
FAILURE_JOURNEYS = frozenset(JOURNEY_TESTS)
QUALITY_CASES = frozenset(
    {
        "exact_model_alias",
        "ordinary_ru",
        "ordinary_en",
        "strict_json_ru",
        "strict_json_en",
        "reasoning_low",
        "reasoning_medium",
        "reasoning_high",
        "no_tool",
        "multi_turn",
        "long_system",
        "unicode_file_numbers",
        "stop_sequence",
        "max_token_truncation",
        "arithmetic",
        "extraction_and_date",
        "ru_summary_faithfulness",
        "contradiction",
        "citation_preservation",
        "wrong_language_guard",
        "tool_call_shape",
        "tool_result_continuation",
        "stream_cancellation",
        "client_disconnect_recovery",
        "reject_empty",
        "reject_nan",
        "reject_degeneration",
        "reject_harmony",
    }
)
PROFILE_KEYS = frozenset(
    {
        "schema",
        "status",
        "profile_id",
        "engine_binding_sha256",
        "hardware_runtime_receipt_sha256",
        "endpoint_base_url",
        "served_model_alias",
        "source_model_repository",
        "source_model_revision",
        "source_model_manifest_sha256",
        "gateway_ca_certificate_sha256",
        "runtime_image",
        "runtime_image_config_digest",
        "runtime_image_oci_manifest_digest",
        "runtime_source_revision",
        "runtime_manifest_sha256",
        "model_path",
        "quantization",
        "dtype",
        "kv_cache_dtype",
        "attention_backend",
        "moe_runner_backend",
        "mxfp4_moe_precision",
        "context_tokens",
        "max_total_tokens",
        "mem_fraction_static",
        "max_running_requests",
        "max_output_tokens",
        "chunked_prefill_size",
        "cuda_graph_backend_decode",
        "cuda_graph_backend_prefill",
        "allowed_modes",
        "allowed_workloads",
        "no_cpu_offload",
        "quality_evidence_sha256",
        "capacity_evidence_sha256",
        "soak_evidence_sha256",
        "failure_evidence_sha256",
    }
)
ENGINE_KEYS = (
    "source_model_repository",
    "source_model_revision",
    "hardware_runtime_receipt_sha256",
    "source_model_manifest_sha256",
    "runtime_image",
    "runtime_image_config_digest",
    "runtime_image_oci_manifest_digest",
    "runtime_source_revision",
    "runtime_manifest_sha256",
    "model_path",
    "quantization",
    "dtype",
    "kv_cache_dtype",
    "attention_backend",
    "moe_runner_backend",
    "mxfp4_moe_precision",
    "context_tokens",
    "max_total_tokens",
    "mem_fraction_static",
    "max_running_requests",
    "max_output_tokens",
    "chunked_prefill_size",
    "cuda_graph_backend_decode",
    "cuda_graph_backend_prefill",
    "no_cpu_offload",
)
RUNTIME_KEYS = frozenset(
    {
        "schema",
        "status",
        "image_ref",
        "image_id",
        "image_config_digest",
        "image_oci_manifest_digest",
        "gateway_image_ref",
        "gateway_image_id",
        "gateway_expected_version",
        "gateway_expected_user",
        "gateway_expected_platform",
        "gateway_expected_platform_manifest_digest",
        "gateway_expected_config_digest",
        "sglang_version",
        "sglang_git_revision",
        "cuda_runtime_version",
        "pytorch_version",
        "flashinfer_version",
        "sgl_kernel_version",
        "nvidia_driver_version",
        "gpu_name",
        "gpu_vram_mib",
        "gpu_compute_capability",
        "served_model_alias_policy",
        "published_endpoint",
        "plain_sglang_lan_published",
        "note",
    }
)
ZERO_SHA256 = "0" * 64
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,79}\Z")
_PROCESS_EPOCH = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


class ProfileOperatorError(RuntimeError):
    """One content-free operator rejection."""


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _reject_constant(_value: str) -> None:
    raise ProfileOperatorError("JSON contains a non-finite number")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProfileOperatorError("JSON contains a duplicate key")
        value[key] = item
    return value


def _read_regular(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= maximum_bytes:
            raise ProfileOperatorError(f"{label} is not a bounded regular file")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_dev != metadata.st_dev
            or before.st_ino != metadata.st_ino
            or before.st_size != metadata.st_size
            or before.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise ProfileOperatorError(f"{label} changed before verification")
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
            raise ProfileOperatorError(f"{label} changed during verification")
        return raw
    except ProfileOperatorError:
        raise
    except OSError as exc:
        raise ProfileOperatorError(f"{label} is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _strict_json(path: Path, *, label: str, maximum_bytes: int = 8 << 20) -> tuple[dict[str, Any], bytes]:
    raw = _read_regular(path, maximum_bytes=maximum_bytes, label=label)
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ProfileOperatorError(f"{label} encoding is invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            parse_constant=_reject_constant,
            object_pairs_hook=_strict_object,
        )
    except ProfileOperatorError:
        raise
    except Exception:
        raise ProfileOperatorError(f"{label} is not strict UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise ProfileOperatorError(f"{label} is not a JSON object")
    return value, raw


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_new(path: Path, raw: bytes, *, label: str) -> None:
    parent = path.absolute().parent
    if not parent.is_dir() or path.exists() or path.is_symlink():
        raise ProfileOperatorError(f"{label} output path is not new")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        view = memoryview(raw)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("short write")
            written += count
        os.fsync(descriptor)
    except OSError as exc:
        raise ProfileOperatorError(f"{label} output could not be created") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _closed_csv(raw: str, allowed: frozenset[str], *, label: str) -> list[str]:
    values = [item.strip().casefold() for item in raw.split(",") if item.strip()]
    if not values or values != sorted(set(values)) or not set(values) <= allowed:
        raise ProfileOperatorError(f"{label} is outside the closed vocabulary")
    return values


def _accepted_manifest(
    path: Path,
    *,
    label: str,
    schema: str,
    maximum_bytes: int = 8 << 20,
) -> tuple[dict[str, Any], bytes]:
    value, raw = _strict_json(path, label=label, maximum_bytes=maximum_bytes)
    if value.get("schema") != schema or value.get("status") != "accepted":
        raise ProfileOperatorError(f"{label} is not accepted")
    return value, raw


def _exact_int(value: Any, *, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProfileOperatorError(f"{label} is outside the closed range")
    return value


def _exact_number(value: Any, *, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileOperatorError(f"{label} is not numeric")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ProfileOperatorError(f"{label} is outside the closed range")
    return result


def _engine_sha256(profile: dict[str, Any]) -> str:
    try:
        projection = {key: profile[key] for key in ENGINE_KEYS}
    except KeyError:
        raise ProfileOperatorError("profile engine projection is incomplete") from None
    return _sha256(canonical_json(projection))


def _validate_runtime_manifest(value: dict[str, Any], hardware: dict[str, Any]) -> None:
    gpu = hardware.get("gpu")
    version_keys = (
        "sglang_version",
        "cuda_runtime_version",
        "pytorch_version",
        "flashinfer_version",
        "sgl_kernel_version",
    )
    if (
        set(value) != RUNTIME_KEYS
        or not isinstance(gpu, dict)
        or value.get("image_ref") != RUNTIME_IMAGE
        or value.get("image_id") != RUNTIME_IMAGE_LOCAL_ID
        or value.get("image_config_digest") != RUNTIME_IMAGE_CONFIG_DIGEST
        or value.get("image_oci_manifest_digest") != RUNTIME_IMAGE_OCI_MANIFEST_DIGEST
        or value.get("gateway_image_ref") != GATEWAY_IMAGE
        or value.get("gateway_image_id") != GATEWAY_IMAGE_LOCAL_ID
        or value.get("gateway_expected_version") != "1.31.3"
        or value.get("gateway_expected_user") != "101"
        or value.get("gateway_expected_platform") != "linux/amd64"
        or value.get("gateway_expected_platform_manifest_digest")
        != "sha256:8d764dd92e0b48d0ca94887dc0fe1df6dffc5200b25b2efcc2deb7ffb61d714c"
        or value.get("gateway_expected_config_digest")
        != "sha256:89dc7d054bddca245db3d5a779e363007d0e75b1161cfe2f283ebeaf0ed90d50"
        or any(
            not isinstance(value.get(key), str) or _VERSION.fullmatch(value[key]) is None
            for key in version_keys
        )
        or any(str(value.get(key, "")).startswith("REPLACE_") for key in version_keys)
        or any(value.get(key) != expected for key, expected in RUNTIME_VERSIONS.items())
        or value.get("sglang_git_revision") != RUNTIME_SOURCE_REVISION
        or value.get("nvidia_driver_version") != gpu.get("driver_version")
        or value.get("gpu_name") != gpu.get("name")
        or value.get("gpu_vram_mib") != gpu.get("memory_total_mib")
        or type(value.get("gpu_vram_mib")) is not int
        or value.get("gpu_compute_capability") != gpu.get("compute_capability")
        or value.get("served_model_alias_policy") != "friday-secondary-{profile_id}"
        or value.get("published_endpoint") != ENDPOINT
        or value.get("plain_sglang_lan_published") is not False
        or value.get("note") != "No runtime identity is accepted until measured on 192.168.1.35."
    ):
        raise ProfileOperatorError("runtime manifest identity is invalid")


def _verify_source_manifest(path: Path) -> str:
    try:
        receipt = verify_source_model_manifest(path, SOURCE_MANIFEST_RAW_SHA256)
    except SourceModelManifestError as exc:
        raise ProfileOperatorError("source model identity is invalid") from exc
    if (
        receipt.manifest_sha256 != SOURCE_MANIFEST_RAW_SHA256
        or receipt.source_revision != SOURCE_REVISION
        or receipt.file_count != SOURCE_FILE_COUNT
        or receipt.total_bytes != SOURCE_TOTAL_BYTES
    ):
        raise ProfileOperatorError("source model identity is invalid")
    return receipt.manifest_sha256


def _validate_bound_chain(
    *,
    hardware_receipt: Path,
    source_model_manifest: Path,
    runtime_manifest: Path,
    ca_certificate: Path,
) -> dict[str, str]:
    hardware, hardware_raw = _accepted_manifest(
        hardware_receipt,
        label="hardware receipt",
        schema="friday.secondary-hardware-runtime.v1",
    )
    if _sha256(hardware_raw) != EXPECTED_HARDWARE_RUNTIME_RECEIPT_SHA256:
        raise ProfileOperatorError("hardware receipt differs from the code-owned identity")
    source_manifest_sha256 = _verify_source_manifest(source_model_manifest)
    runtime, runtime_raw = _accepted_manifest(
        runtime_manifest,
        label="runtime manifest",
        schema="friday.secondary-sglang-runtime.v1",
    )
    _validate_runtime_manifest(runtime, hardware)
    ca_raw = _read_regular(ca_certificate, maximum_bytes=65_536, label="gateway CA")
    try:
        ca_pem = ca_raw.decode("ascii", errors="strict")
    except UnicodeError:
        raise ProfileOperatorError("gateway CA encoding is invalid") from None
    if "-----BEGIN CERTIFICATE-----" not in ca_pem or "-----END CERTIFICATE-----" not in ca_pem:
        raise ProfileOperatorError("gateway CA is not a PEM certificate")
    return {
        "hardware_runtime_receipt_sha256": _sha256(hardware_raw),
        "source_model_manifest_sha256": source_manifest_sha256,
        "runtime_manifest_sha256": _sha256(runtime_raw),
        "runtime_source_revision": str(runtime["sglang_git_revision"]),
        "gateway_ca_certificate_sha256": _sha256(ca_raw),
    }


def build_candidate(args: argparse.Namespace) -> dict[str, Any]:
    chain = _validate_bound_chain(
        hardware_receipt=args.hardware_receipt,
        source_model_manifest=args.source_model_manifest,
        runtime_manifest=args.runtime_manifest,
        ca_certificate=args.ca_certificate,
    )

    context = _exact_int(args.context_tokens, minimum=4096, maximum=32768, label="context")
    if context not in CONTEXT_LADDER:
        raise ProfileOperatorError("context is outside the certified ladder")
    output = _exact_int(args.max_output_tokens, minimum=64, maximum=4096, label="output")
    if output >= context:
        raise ProfileOperatorError("output budget is not below context")
    chunk = _exact_int(args.chunked_prefill_size, minimum=512, maximum=2048, label="chunk")
    memory = f"{args.mem_fraction_static:.2f}"
    if memory not in MEMORY_GRID:
        raise ProfileOperatorError("memory fraction is outside the closed grid")
    modes = _closed_csv(args.allowed_modes, MODES, label="modes")
    workloads = _closed_csv(args.allowed_workloads, WORKLOADS, label="workloads")
    profile: dict[str, Any] = {
        "schema": PROFILE_SCHEMA,
        "status": "candidate",
        "profile_id": "pending",
        "engine_binding_sha256": ZERO_SHA256,
        "hardware_runtime_receipt_sha256": chain["hardware_runtime_receipt_sha256"],
        "endpoint_base_url": ENDPOINT,
        "served_model_alias": "pending",
        "source_model_repository": SOURCE_REPOSITORY,
        "source_model_revision": SOURCE_REVISION,
        "source_model_manifest_sha256": chain["source_model_manifest_sha256"],
        "gateway_ca_certificate_sha256": chain["gateway_ca_certificate_sha256"],
        "runtime_image": RUNTIME_IMAGE,
        "runtime_image_config_digest": RUNTIME_IMAGE_CONFIG_DIGEST,
        "runtime_image_oci_manifest_digest": RUNTIME_IMAGE_OCI_MANIFEST_DIGEST,
        "runtime_source_revision": chain["runtime_source_revision"],
        "runtime_manifest_sha256": chain["runtime_manifest_sha256"],
        "model_path": MODEL_PATH,
        "quantization": "mxfp4",
        "dtype": "bfloat16",
        "kv_cache_dtype": "bf16",
        "attention_backend": "triton",
        "moe_runner_backend": "flashinfer_mxfp4",
        "mxfp4_moe_precision": "default",
        "context_tokens": context,
        "max_total_tokens": context,
        "mem_fraction_static": memory,
        "max_running_requests": 1,
        "max_output_tokens": output,
        "chunked_prefill_size": chunk,
        "cuda_graph_backend_decode": "disabled",
        "cuda_graph_backend_prefill": "disabled",
        "allowed_modes": modes,
        "allowed_workloads": workloads,
        "no_cpu_offload": True,
        "quality_evidence_sha256": ZERO_SHA256,
        "capacity_evidence_sha256": ZERO_SHA256,
        "soak_evidence_sha256": ZERO_SHA256,
        "failure_evidence_sha256": ZERO_SHA256,
    }
    binding = _engine_sha256(profile)
    profile["engine_binding_sha256"] = binding
    profile["profile_id"] = f"gptoss20b-{binding}"
    profile["served_model_alias"] = f"friday-secondary-{profile['profile_id']}"
    if set(profile) != PROFILE_KEYS:
        raise ProfileOperatorError("candidate profile field set is invalid")
    _write_new(args.profile_id_output, str(profile["profile_id"]).encode("ascii"), label="profile id")
    _write_new(args.output, canonical_json(profile), label="candidate profile")
    return {
        "schema": "friday.secondary-profile-operation.v1",
        "status": "candidate_created",
        "profile_id": profile["profile_id"],
        "profile_sha256": _sha256(canonical_json(profile)),
    }


def _evidence_matches(value: dict[str, Any], candidate: dict[str, Any], candidate_sha256: str) -> bool:
    return bool(
        value.get("candidate_profile_id") == candidate.get("profile_id")
        and value.get("candidate_profile_sha256") == candidate_sha256
        and value.get("served_model_alias") == candidate.get("served_model_alias")
        and value.get("gateway_ca_certificate_sha256") == candidate.get("gateway_ca_certificate_sha256")
    )


def _validate_quality_cases(cases: Any) -> None:
    expected_keys = {
        "case",
        "status",
        "latency_sec",
        "prompt_tokens",
        "completion_tokens",
        "output_sha256",
    }
    if not isinstance(cases, list) or len(cases) != len(QUALITY_CASES):
        raise ProfileOperatorError("quality evidence case set is incomplete")
    names: list[str] = []
    for row in cases:
        if not isinstance(row, dict) or set(row) != expected_keys:
            raise ProfileOperatorError("quality evidence row shape is invalid")
        name = row.get("case")
        digest = row.get("output_sha256")
        if (
            not isinstance(name, str)
            or row.get("status") != "passed"
            or not isinstance(digest, str)
            or (digest != "" if name == "stream_cancellation" else _SHA256.fullmatch(digest) is None)
        ):
            raise ProfileOperatorError("quality evidence row is invalid")
        _exact_number(row.get("latency_sec"), minimum=0.0, maximum=3_600.0, label="quality latency")
        _exact_int(row.get("prompt_tokens"), minimum=0, maximum=10_000_000, label="quality prompt")
        _exact_int(
            row.get("completion_tokens"),
            minimum=0,
            maximum=10_000_000,
            label="quality completion",
        )
        names.append(name)
    if len(set(names)) != len(names) or set(names) != QUALITY_CASES:
        raise ProfileOperatorError("quality evidence case set is invalid")


def accept_capacity(args: argparse.Namespace) -> dict[str, Any]:
    candidate, candidate_raw = _strict_json(args.candidate, label="candidate profile")
    _validate_candidate(candidate, candidate_raw)
    candidate_sha256 = _sha256(candidate_raw)
    initial, initial_raw = _strict_json(args.initial_trial, label="initial capacity trial")
    cold, cold_raw = _strict_json(args.cold_restart_trial, label="cold restart capacity trial")
    soak, soak_raw = _strict_json(args.soak, label="soak evidence", maximum_bytes=64 << 20)
    context = candidate["context_tokens"]
    memory = float(candidate["mem_fraction_static"])
    trial_epochs: list[str] = []
    for label, trial in (("initial", initial), ("cold restart", cold)):
        rows = trial.get("trials")
        repeats = _exact_int(
            trial.get("repeats_per_candidate"), minimum=3, maximum=10, label=f"{label} repeats"
        )
        trial_memory = _exact_number(
            trial.get("mem_fraction_static"),
            minimum=min(float(item) for item in MEMORY_GRID),
            maximum=max(float(item) for item in MEMORY_GRID),
            label=f"{label} memory fraction",
        )
        runtime_epoch = trial.get("runtime_process_start_time_seconds")
        if (
            trial.get("schema") != "friday.secondary-context-capacity-trial.v1"
            or trial.get("status") != "measured_not_yet_certified"
            or not _evidence_matches(trial, candidate, candidate_sha256)
            or trial.get("candidates") != [context]
            or trial.get("largest_passing_trial_tokens") != context
            or trial_memory != memory
            or not isinstance(runtime_epoch, str)
            or not 1 <= len(runtime_epoch) <= 40
            or _PROCESS_EPOCH.fullmatch(runtime_epoch) is None
            or runtime_epoch.startswith("0")
            or not isinstance(rows, list)
            or len(rows) != repeats
            or any(
                not isinstance(row, dict)
                or row.get("context_target_tokens") != context
                or row.get("prompt_near_limit") is not True
                or row.get("generated_envelope_met") is not True
                or row.get("headroom_met") is not True
                for row in rows
            )
        ):
            raise ProfileOperatorError(f"{label} capacity trial did not pass")
        trial_epochs.append(runtime_epoch)
    if _sha256(initial_raw) == _sha256(cold_raw) or trial_epochs[0] == trial_epochs[1]:
        raise ProfileOperatorError("cold restart capacity trial did not change runtime epoch")
    completed_requests = _exact_int(
        soak.get("completed_requests"), minimum=0, maximum=1_000_000, label="soak requests"
    )
    elapsed_sec = _exact_number(soak.get("elapsed_sec"), minimum=0.0, maximum=86_400.0, label="soak duration")
    failures = _exact_int(soak.get("failures"), minimum=0, maximum=1_000_000, label="soak failures")
    if (
        soak.get("schema") != "friday.secondary-sglang-soak.v1"
        or soak.get("status") != "passed"
        or not _evidence_matches(soak, candidate, candidate_sha256)
        or completed_requests < 100
        or elapsed_sec < 1800
        or failures != 0
        or soak.get("raw_content_retained") is not False
        or soak.get("api_key_retained") is not False
    ):
        raise ProfileOperatorError("soak does not certify the selected capacity")
    accepted = {
        "schema": CAPACITY_SCHEMA,
        "status": "accepted",
        "candidate_profile_id": candidate["profile_id"],
        "candidate_profile_sha256": candidate_sha256,
        "served_model_alias": candidate["served_model_alias"],
        "gateway_ca_certificate_sha256": candidate["gateway_ca_certificate_sha256"],
        "context_tokens": context,
        "mem_fraction_static": candidate["mem_fraction_static"],
        "initial_trial_sha256": _sha256(initial_raw),
        "cold_restart_trial_sha256": _sha256(cold_raw),
        "soak_sha256": _sha256(soak_raw),
        "raw_content_retained": False,
    }
    raw = canonical_json(accepted)
    _write_new(args.output, raw, label="capacity evidence")
    return {
        "schema": "friday.secondary-profile-operation.v1",
        "status": "capacity_accepted",
        "capacity_evidence_sha256": _sha256(raw),
    }


def _validate_candidate(value: dict[str, Any], raw: bytes) -> None:
    context = _exact_int(value.get("context_tokens"), minimum=4096, maximum=32768, label="context")
    total = _exact_int(value.get("max_total_tokens"), minimum=4096, maximum=32768, label="token pool")
    concurrency = _exact_int(value.get("max_running_requests"), minimum=1, maximum=1, label="concurrency")
    output = _exact_int(value.get("max_output_tokens"), minimum=64, maximum=4096, label="output")
    chunk = _exact_int(value.get("chunked_prefill_size"), minimum=512, maximum=2048, label="chunk")
    modes = value.get("allowed_modes")
    workloads = value.get("allowed_workloads")
    hashes = (
        value.get("source_model_manifest_sha256"),
        value.get("gateway_ca_certificate_sha256"),
        value.get("hardware_runtime_receipt_sha256"),
        value.get("runtime_manifest_sha256"),
    )
    if (
        set(value) != PROFILE_KEYS
        or raw != canonical_json(value)
        or value.get("schema") != PROFILE_SCHEMA
        or value.get("status") != "candidate"
        or value.get("engine_binding_sha256") != _engine_sha256(value)
        or value.get("profile_id") != f"gptoss20b-{value.get('engine_binding_sha256')}"
        or value.get("served_model_alias") != f"friday-secondary-{value.get('profile_id')}"
        or value.get("hardware_runtime_receipt_sha256") != EXPECTED_HARDWARE_RUNTIME_RECEIPT_SHA256
        or value.get("endpoint_base_url") != ENDPOINT
        or value.get("source_model_repository") != SOURCE_REPOSITORY
        or value.get("source_model_revision") != SOURCE_REVISION
        or value.get("source_model_manifest_sha256") != SOURCE_MANIFEST_RAW_SHA256
        or any(not isinstance(item, str) or _SHA256.fullmatch(item) is None for item in hashes)
        or value.get("runtime_image") != RUNTIME_IMAGE
        or value.get("runtime_image_config_digest") != RUNTIME_IMAGE_CONFIG_DIGEST
        or value.get("runtime_image_oci_manifest_digest") != RUNTIME_IMAGE_OCI_MANIFEST_DIGEST
        or value.get("runtime_source_revision") != RUNTIME_SOURCE_REVISION
        or value.get("model_path") != MODEL_PATH
        or value.get("quantization") != "mxfp4"
        or value.get("dtype") != "bfloat16"
        or value.get("kv_cache_dtype") != "bf16"
        or value.get("attention_backend") != "triton"
        or value.get("moe_runner_backend") != "flashinfer_mxfp4"
        or value.get("mxfp4_moe_precision") != "default"
        or context not in CONTEXT_LADDER
        or total != context
        or concurrency != 1
        or output >= context
        or chunk not in {512, 1024, 1536, 2048}
        or value.get("cuda_graph_backend_decode") != "disabled"
        or value.get("cuda_graph_backend_prefill") != "disabled"
        or value.get("mem_fraction_static") not in MEMORY_GRID
        or not isinstance(modes, list)
        or any(not isinstance(item, str) for item in modes)
        or modes != sorted(set(modes))
        or not set(modes) <= MODES
        or not modes
        or not isinstance(workloads, list)
        or any(not isinstance(item, str) for item in workloads)
        or workloads != sorted(set(workloads))
        or not set(workloads) <= WORKLOADS
        or not workloads
        or value.get("no_cpu_offload") is not True
        or any(
            value.get(key) != ZERO_SHA256
            for key in (
                "quality_evidence_sha256",
                "capacity_evidence_sha256",
                "soak_evidence_sha256",
                "failure_evidence_sha256",
            )
        )
    ):
        raise ProfileOperatorError("candidate profile is invalid")


def accept_profile(args: argparse.Namespace) -> dict[str, Any]:
    candidate, candidate_raw = _strict_json(args.candidate, label="candidate profile")
    _validate_candidate(candidate, candidate_raw)
    chain = _validate_bound_chain(
        hardware_receipt=args.hardware_receipt,
        source_model_manifest=args.source_model_manifest,
        runtime_manifest=args.runtime_manifest,
        ca_certificate=args.ca_certificate,
    )
    if any(candidate.get(key) != value for key, value in chain.items()):
        raise ProfileOperatorError("candidate profile no longer matches the verified source/runtime chain")
    candidate_sha256 = _sha256(candidate_raw)
    evidence_specs = (
        ("quality", args.quality, "friday.secondary-quality-battery.v1"),
        ("capacity", args.capacity, CAPACITY_SCHEMA),
        ("soak", args.soak, "friday.secondary-sglang-soak.v1"),
        ("failure", args.failure, FAILURE_SCHEMA),
    )
    evidence_hashes: dict[str, str] = {}
    for name, path, schema in evidence_specs:
        value, raw = _strict_json(path, label=f"{name} evidence", maximum_bytes=64 << 20)
        if (
            value.get("schema") != schema
            or value.get("status") not in {"passed", "accepted"}
            or not _evidence_matches(value, candidate, candidate_sha256)
        ):
            raise ProfileOperatorError(f"{name} evidence is not accepted for this candidate")
        if name == "quality":
            cases = value.get("cases")
            _validate_quality_cases(cases)
            if value.get("raw_content_retained") is not False or value.get("api_key_retained") is not False:
                raise ProfileOperatorError("quality evidence is incomplete")
        elif name == "capacity":
            if (
                value.get("context_tokens") != candidate.get("context_tokens")
                or value.get("mem_fraction_static") != candidate.get("mem_fraction_static")
                or any(
                    _SHA256.fullmatch(str(value.get(key, ""))) is None
                    for key in (
                        "initial_trial_sha256",
                        "cold_restart_trial_sha256",
                        "soak_sha256",
                    )
                )
            ):
                raise ProfileOperatorError("capacity evidence is incomplete")
        elif name == "soak":
            completed_requests = _exact_int(
                value.get("completed_requests"),
                minimum=0,
                maximum=1_000_000,
                label="soak requests",
            )
            elapsed_sec = _exact_number(
                value.get("elapsed_sec"), minimum=0.0, maximum=86_400.0, label="soak duration"
            )
            failures = _exact_int(value.get("failures"), minimum=0, maximum=1_000_000, label="soak failures")
            if (
                completed_requests < 100
                or elapsed_sec < 1800
                or failures != 0
                or value.get("raw_content_retained") is not False
                or value.get("api_key_retained") is not False
            ):
                raise ProfileOperatorError("soak evidence is incomplete")
        else:
            journeys = value.get("journeys")
            suite_hashes = value.get("suite_file_sha256")
            runner_raw = _read_regular(
                _BUNDLE_ROOT / "scripts" / "failure_battery.py",
                maximum_bytes=1 << 20,
                label="failure battery runner",
            )
            if (
                not isinstance(journeys, dict)
                or set(journeys) != FAILURE_JOURNEYS
                or any(
                    row
                    != {
                        "status": "passed",
                        "assertion_test": JOURNEY_TESTS[journey],
                    }
                    for journey, row in journeys.items()
                )
                or value.get("runner_sha256") != _sha256(runner_raw)
                or value.get("journey_contract_sha256") != journey_contract_sha256()
                or not isinstance(value.get("source_head"), str)
                or _REVISION.fullmatch(value["source_head"]) is None
                or not isinstance(suite_hashes, dict)
                or set(suite_hashes) != set(SUITE_FILES)
                or any(
                    not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
                    for digest in suite_hashes.values()
                )
                or type(value.get("test_count")) is not int
                or value["test_count"] < len(FAILURE_JOURNEYS)
                or value.get("primary_fallback_exactly_once") is not True
                or value.get("effect_replay_observed") is not False
                or value.get("v12_readiness_changed") is not False
                or value.get("primary_only_flag_verified") is not True
                or value.get("raw_content_retained") is not False
                or value.get("credentials_retained") is not False
            ):
                raise ProfileOperatorError("failure evidence is incomplete")
        evidence_hashes[name] = _sha256(raw)
    accepted = dict(candidate)
    accepted.update(
        {
            "status": "accepted",
            "quality_evidence_sha256": evidence_hashes["quality"],
            "capacity_evidence_sha256": evidence_hashes["capacity"],
            "soak_evidence_sha256": evidence_hashes["soak"],
            "failure_evidence_sha256": evidence_hashes["failure"],
        }
    )
    if _engine_sha256(accepted) != candidate["engine_binding_sha256"]:
        raise ProfileOperatorError("evidence promotion changed the engine binding")
    raw = canonical_json(accepted)
    _write_new(args.output, raw, label="accepted profile")
    return {
        "schema": "friday.secondary-profile-operation.v1",
        "status": "profile_accepted",
        "profile_id": accepted["profile_id"],
        "profile_sha256": _sha256(raw),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    candidate = commands.add_parser("candidate")
    candidate.add_argument("--hardware-receipt", required=True, type=Path)
    candidate.add_argument("--source-model-manifest", required=True, type=Path)
    candidate.add_argument("--runtime-manifest", required=True, type=Path)
    candidate.add_argument("--ca-certificate", required=True, type=Path)
    candidate.add_argument("--context-tokens", required=True, type=int)
    candidate.add_argument("--max-output-tokens", default=2048, type=int)
    candidate.add_argument("--mem-fraction-static", required=True, type=float)
    candidate.add_argument("--chunked-prefill-size", default=1024, type=int)
    candidate.add_argument("--allowed-modes", default="assist,shadow")
    candidate.add_argument("--allowed-workloads", default="extract")
    candidate.add_argument("--profile-id-output", required=True, type=Path)
    candidate.add_argument("--output", required=True, type=Path)

    capacity = commands.add_parser("accept-capacity")
    capacity.add_argument("--candidate", required=True, type=Path)
    capacity.add_argument("--initial-trial", required=True, type=Path)
    capacity.add_argument("--cold-restart-trial", required=True, type=Path)
    capacity.add_argument("--soak", required=True, type=Path)
    capacity.add_argument("--output", required=True, type=Path)

    accept = commands.add_parser("accept-profile")
    accept.add_argument("--candidate", required=True, type=Path)
    accept.add_argument("--hardware-receipt", required=True, type=Path)
    accept.add_argument("--source-model-manifest", required=True, type=Path)
    accept.add_argument("--runtime-manifest", required=True, type=Path)
    accept.add_argument("--ca-certificate", required=True, type=Path)
    accept.add_argument("--quality", required=True, type=Path)
    accept.add_argument("--capacity", required=True, type=Path)
    accept.add_argument("--soak", required=True, type=Path)
    accept.add_argument("--failure", required=True, type=Path)
    accept.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "candidate":
            result = build_candidate(args)
        elif args.command == "accept-capacity":
            result = accept_capacity(args)
        else:
            result = accept_profile(args)
    except ProfileOperatorError as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
