"""Strict shared launch contract for the detachable secondary runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "friday.secondary-runtime-profile.v4"
EXPECTED_MODEL_PATH = "/source/snapshot"
EXPECTED_SOURCE_REVISION = "6cee5e81ee83917806bbde320786a8fb61efebee"
EXPECTED_SOURCE_MANIFEST_SHA256 = "438df0a0b2f6b4164c2fd9d9ed309925abbc94ed8deb056b692d2ccad7887fd9"
EXPECTED_HARDWARE_RUNTIME_RECEIPT_SHA256 = "0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f"
EXPECTED_RUNTIME_IMAGE = (
    "lmsysorg/sglang@sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405"
)
EXPECTED_RUNTIME_IMAGE_CONFIG_DIGEST = (
    "sha256:f7adc6c05df9ff711b82ad291cf1db6eaf30590c4d929833d632abfef3895efc"
)
EXPECTED_RUNTIME_IMAGE_OCI_MANIFEST_DIGEST = (
    "sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405"
)
EXPECTED_RUNTIME_SOURCE_REVISION = "29481685462732237d80d86076d6563e1f658102"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_REVISION_RE = re.compile(r"[0-9a-f]{40}")
_PROFILE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,79}")
_MEMORY_FRACTIONS = frozenset(
    {"0.86", "0.88", "0.90", "0.92", "0.94", "0.95", "0.96", "0.97"}
)
_CONTEXT_LADDER = frozenset(
    {4096, 8192, 12288, 16384, 24576, 32768, 40960, 49152, 65536}
)
_CHUNKED_PREFILL_GRID = frozenset({512, 1024, 1536, 2048})
_MODES = frozenset({"shadow", "assist"})
_WORKLOADS = frozenset(
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
_KEYS = frozenset(
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
        "sglang_compat_patch_sha256",
        "runtime_manifest_sha256",
        "model_path",
        "quantization",
        "dtype",
        "kv_cache_dtype",
        "kv_cache_scale_policy",
        "attention_backend",
        "prefill_attention_backend",
        "decode_attention_backend",
        "sampling_backend",
        "moe_runner_backend",
        "mxfp4_moe_precision",
        "mm_feature_transport",
        "page_size",
        "radix_cache_enabled",
        "overlap_schedule_enabled",
        "hybrid_swa_memory_enabled",
        "swa_full_tokens_ratio",
        "context_tokens",
        "max_total_tokens",
        "mem_fraction_static",
        "max_running_requests",
        "max_output_tokens",
        "chunked_prefill_size",
        "cuda_graph_backend_decode",
        "cuda_graph_max_bs_decode",
        "cuda_graph_bs_decode",
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
_ENGINE_KEYS = (
    "source_model_repository",
    "source_model_revision",
    "hardware_runtime_receipt_sha256",
    "source_model_manifest_sha256",
    "runtime_image",
    "runtime_image_config_digest",
    "runtime_image_oci_manifest_digest",
    "runtime_source_revision",
    "sglang_compat_patch_sha256",
    "runtime_manifest_sha256",
    "model_path",
    "quantization",
    "dtype",
    "kv_cache_dtype",
    "kv_cache_scale_policy",
    "attention_backend",
    "prefill_attention_backend",
    "decode_attention_backend",
    "sampling_backend",
    "moe_runner_backend",
    "mxfp4_moe_precision",
    "mm_feature_transport",
    "page_size",
    "radix_cache_enabled",
    "overlap_schedule_enabled",
    "hybrid_swa_memory_enabled",
    "swa_full_tokens_ratio",
    "context_tokens",
    "max_total_tokens",
    "mem_fraction_static",
    "max_running_requests",
    "max_output_tokens",
    "chunked_prefill_size",
    "cuda_graph_backend_decode",
    "cuda_graph_max_bs_decode",
    "cuda_graph_bs_decode",
    "cuda_graph_backend_prefill",
    "no_cpu_offload",
)


class ProfileContractError(RuntimeError):
    """Content-free rejection safe for startup logs."""


def _read_bounded_regular(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= maximum_bytes:
            raise ProfileContractError(f"{label} size or file type is invalid")
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
            raise ProfileContractError(f"{label} changed before verification")
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
            raise ProfileContractError(f"{label} changed during verification")
        return raw
    except ProfileContractError:
        raise
    except OSError as exc:
        raise ProfileContractError(f"{label} is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _strict_json(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > 65_536 or raw.startswith(b"\xef\xbb\xbf"):
        raise ProfileContractError("profile manifest size or encoding is invalid")

    def reject_constant(_value: str) -> None:
        raise ProfileContractError("profile manifest contains a non-finite number")

    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), parse_constant=reject_constant)
    except ProfileContractError:
        raise
    except Exception:
        raise ProfileContractError("profile manifest is not strict UTF-8 JSON") from None
    if not isinstance(value, dict) or set(value) != _KEYS or raw != _canonical_json(value):
        raise ProfileContractError("profile manifest is not canonical or has an unknown field")
    return value


def _exact_int(value: Any, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProfileContractError("profile manifest integer is outside the closed range")
    return int(value)


def engine_binding_sha256(value: dict[str, Any]) -> str:
    """Hash the immutable engine projection, excluding policy and evidence."""

    try:
        projection = {key: value[key] for key in _ENGINE_KEYS}
    except KeyError:
        raise ProfileContractError("profile engine projection is incomplete") from None
    return hashlib.sha256(_canonical_json(projection)).hexdigest()


def _closed_list(value: Any, allowed: frozenset[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        raise ProfileContractError("profile manifest list is invalid")
    normalized = tuple(value)
    if normalized != tuple(sorted(set(normalized))) or not set(normalized) <= allowed:
        raise ProfileContractError("profile manifest list is outside the closed vocabulary")
    return normalized


@dataclass(frozen=True, slots=True)
class LaunchProfile:
    profile_id: str
    manifest_sha256: str
    status: str
    runtime_image: str
    runtime_image_config_digest: str
    runtime_image_oci_manifest_digest: str
    sglang_compat_patch_sha256: str
    source_model_manifest_sha256: str
    hardware_runtime_receipt_sha256: str
    model_path: str
    served_model_alias: str
    quantization: str
    dtype: str
    kv_cache_dtype: str
    kv_cache_scale_policy: str
    attention_backend: str
    prefill_attention_backend: str
    decode_attention_backend: str
    sampling_backend: str
    moe_runner_backend: str
    mxfp4_moe_precision: str
    mm_feature_transport: str
    page_size: int
    radix_cache_enabled: bool
    overlap_schedule_enabled: bool
    hybrid_swa_memory_enabled: bool
    swa_full_tokens_ratio: str
    context_tokens: int
    max_total_tokens: int
    mem_fraction_static: str
    max_running_requests: int
    max_output_tokens: int
    chunked_prefill_size: int
    cuda_graph_backend_decode: str
    cuda_graph_max_bs_decode: int
    cuda_graph_bs_decode: tuple[int, ...]
    cuda_graph_backend_prefill: str

    def server_arguments(self, api_key: str) -> list[str]:
        arguments = [
            "--model-path",
            self.model_path,
            "--served-model-name",
            self.served_model_alias,
            "--quantization",
            self.quantization,
            "--dtype",
            self.dtype,
            "--host",
            "0.0.0.0",
            "--port",
            "30000",
            "--api-key",
            api_key,
            "--reasoning-parser",
            "gpt-oss",
            "--tool-call-parser",
            "gpt-oss",
            "--attention-backend",
            self.attention_backend,
            "--prefill-attention-backend",
            self.prefill_attention_backend,
            "--decode-attention-backend",
            self.decode_attention_backend,
            "--sampling-backend",
            self.sampling_backend,
            "--moe-runner-backend",
            self.moe_runner_backend,
            "--flashinfer-mxfp4-moe-precision",
            self.mxfp4_moe_precision,
            "--mm-feature-transport",
            self.mm_feature_transport,
            "--kv-cache-dtype",
            self.kv_cache_dtype,
            "--page-size",
            str(self.page_size),
            "--swa-full-tokens-ratio",
            self.swa_full_tokens_ratio,
            "--chunked-prefill-size",
            str(self.chunked_prefill_size),
            "--max-running-requests",
            str(self.max_running_requests),
            "--cuda-graph-backend-decode",
            self.cuda_graph_backend_decode,
            "--cuda-graph-backend-prefill",
            self.cuda_graph_backend_prefill,
        ]
        if self.cuda_graph_backend_decode == "full":
            arguments.extend(
                [
                    "--cuda-graph-max-bs-decode",
                    str(self.cuda_graph_max_bs_decode),
                    "--cuda-graph-bs-decode",
                    *(str(size) for size in self.cuda_graph_bs_decode),
                ]
            )
        if not self.radix_cache_enabled:
            arguments.append("--disable-radix-cache")
        if not self.overlap_schedule_enabled:
            arguments.append("--disable-overlap-schedule")
        arguments.extend(
            [
                "--context-length",
                str(self.context_tokens),
                "--max-total-tokens",
                str(self.max_total_tokens),
                "--mem-fraction-static",
                self.mem_fraction_static,
                "--enable-metrics",
                "--enable-cache-report",
            ]
        )
        return arguments


def load_launch_profile(
    manifest_path: Path,
    profile_id_path: Path,
    *,
    actual_runtime_image: str,
) -> LaunchProfile:
    raw = _read_bounded_regular(manifest_path, maximum_bytes=65_536, label="profile manifest")
    value = _strict_json(raw)
    profile_id_bytes = _read_bounded_regular(profile_id_path, maximum_bytes=80, label="profile id file")
    try:
        profile_id = profile_id_bytes.decode("ascii", errors="strict")
    except UnicodeError:
        raise ProfileContractError("profile id file is invalid") from None
    if not _PROFILE_ID_RE.fullmatch(profile_id) or value["profile_id"] != profile_id:
        raise ProfileContractError("profile id does not match the manifest")
    if value["schema"] != SCHEMA or value["status"] not in {"candidate", "accepted"}:
        raise ProfileContractError("profile schema or status is invalid")
    if value["endpoint_base_url"] != "https://192.168.1.35:8443/v1":
        raise ProfileContractError("profile endpoint is invalid")
    binding_sha256 = engine_binding_sha256(value)
    expected_profile_id = f"gptoss20b-{binding_sha256}"
    if value["engine_binding_sha256"] != binding_sha256 or profile_id != expected_profile_id:
        raise ProfileContractError("profile engine binding is invalid")
    expected_alias = f"friday-secondary-{profile_id}"
    if value["served_model_alias"] != expected_alias or value["model_path"] != EXPECTED_MODEL_PATH:
        raise ProfileContractError("profile model projection is invalid")
    if (
        value["source_model_repository"] != "openai/gpt-oss-20b"
        or value["source_model_revision"] != EXPECTED_SOURCE_REVISION
    ):
        raise ProfileContractError("profile source identity is invalid")
    for key in (
        "source_model_manifest_sha256",
        "gateway_ca_certificate_sha256",
        "hardware_runtime_receipt_sha256",
        "runtime_manifest_sha256",
        "quality_evidence_sha256",
        "capacity_evidence_sha256",
        "soak_evidence_sha256",
        "failure_evidence_sha256",
    ):
        if not isinstance(value[key], str) or not _SHA256_RE.fullmatch(value[key]):
            raise ProfileContractError("profile evidence identity is invalid")
        if value["status"] == "accepted" and value[key] == "0" * 64:
            raise ProfileContractError("accepted profile has missing evidence")
    if value["hardware_runtime_receipt_sha256"] != EXPECTED_HARDWARE_RUNTIME_RECEIPT_SHA256:
        raise ProfileContractError("profile hardware/runtime receipt identity is invalid")
    if value["source_model_manifest_sha256"] != EXPECTED_SOURCE_MANIFEST_SHA256:
        raise ProfileContractError("profile source manifest identity is invalid")
    runtime_image = value["runtime_image"]
    runtime_config_digest = value["runtime_image_config_digest"]
    runtime_oci_digest = value["runtime_image_oci_manifest_digest"]
    runtime_revision = value["runtime_source_revision"]
    compat_patch_sha256 = value["sglang_compat_patch_sha256"]
    if (
        not isinstance(runtime_image, str)
        or runtime_image != EXPECTED_RUNTIME_IMAGE
        or not isinstance(runtime_config_digest, str)
        or _IMAGE_ID_RE.fullmatch(runtime_config_digest) is None
        or runtime_config_digest != EXPECTED_RUNTIME_IMAGE_CONFIG_DIGEST
        or not isinstance(runtime_oci_digest, str)
        or _IMAGE_ID_RE.fullmatch(runtime_oci_digest) is None
        or runtime_oci_digest != EXPECTED_RUNTIME_IMAGE_OCI_MANIFEST_DIGEST
        or not isinstance(runtime_revision, str)
        or not _REVISION_RE.fullmatch(runtime_revision)
        or runtime_revision != EXPECTED_RUNTIME_SOURCE_REVISION
        or not isinstance(compat_patch_sha256, str)
        or _SHA256_RE.fullmatch(compat_patch_sha256) is None
        or compat_patch_sha256 == "0" * 64
    ):
        raise ProfileContractError("profile runtime identity is invalid")
    if runtime_image != actual_runtime_image:
        raise ProfileContractError("running image does not match the profile")
    if (
        value["quantization"] != "mxfp4"
        or value["dtype"] != "bfloat16"
        or not isinstance(value["kv_cache_dtype"], str)
        or value["kv_cache_dtype"] not in {"bf16", "fp8_e4m3"}
        or not isinstance(value["kv_cache_scale_policy"], str)
        or value["kv_cache_scale_policy"]
        != {"bf16": "not_applicable", "fp8_e4m3": "implicit_unit"}.get(value["kv_cache_dtype"])
    ):
        raise ProfileContractError("profile quantization is invalid")
    if (
        value["attention_backend"] != "triton"
        or value["prefill_attention_backend"] != "triton"
        or not isinstance(value["decode_attention_backend"], str)
        or value["decode_attention_backend"] not in {"triton", "trtllm_mha"}
        or not isinstance(value["sampling_backend"], str)
        or value["sampling_backend"] not in {"pytorch", "flashinfer"}
        or value["moe_runner_backend"] != "flashinfer_mxfp4"
        or value["mxfp4_moe_precision"] != "default"
        or value["mm_feature_transport"] != "cpu"
    ):
        raise ProfileContractError("profile kernel selection is invalid")
    page_size = _exact_int(value["page_size"], minimum=1, maximum=16)
    if page_size not in {1, 16}:
        raise ProfileContractError("profile page size is invalid")
    if not isinstance(value["radix_cache_enabled"], bool) or not isinstance(
        value["overlap_schedule_enabled"], bool
    ):
        raise ProfileContractError("profile scheduler selection is invalid")
    if value["hybrid_swa_memory_enabled"] is not True:
        raise ProfileContractError("profile hybrid SWA memory selection is invalid")
    if not isinstance(value["swa_full_tokens_ratio"], str) or value[
        "swa_full_tokens_ratio"
    ] not in {"0.25", "0.50", "0.80", "1.00"}:
        raise ProfileContractError("profile SWA token ratio is invalid")
    context_tokens = _exact_int(value["context_tokens"], minimum=4096, maximum=65536)
    if context_tokens not in _CONTEXT_LADDER:
        raise ProfileContractError("profile context is outside the certified ladder")
    max_total_tokens = _exact_int(value["max_total_tokens"], minimum=4096, maximum=65536)
    if max_total_tokens != context_tokens:
        raise ProfileContractError("profile token pool must equal the single-request context")
    mem_fraction = value["mem_fraction_static"]
    if not isinstance(mem_fraction, str) or mem_fraction not in _MEMORY_FRACTIONS:
        raise ProfileContractError("profile memory fraction is invalid")
    max_running_requests = _exact_int(value["max_running_requests"], minimum=1, maximum=1)
    max_output_tokens = _exact_int(value["max_output_tokens"], minimum=64, maximum=4096)
    if max_output_tokens >= context_tokens:
        raise ProfileContractError("profile output budget is invalid")
    chunked_prefill_size = _exact_int(value["chunked_prefill_size"], minimum=512, maximum=2048)
    if chunked_prefill_size not in _CHUNKED_PREFILL_GRID:
        raise ProfileContractError("profile chunked prefill size is invalid")
    cuda_graph_backend_decode = value["cuda_graph_backend_decode"]
    if not isinstance(cuda_graph_backend_decode, str) or cuda_graph_backend_decode not in {
        "disabled",
        "full",
    }:
        raise ProfileContractError("profile CUDA graph selection is invalid")
    cuda_graph_max_bs_decode = _exact_int(
        value["cuda_graph_max_bs_decode"], minimum=0, maximum=1
    )
    cuda_graph_bs_value = value["cuda_graph_bs_decode"]
    if not isinstance(cuda_graph_bs_value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in cuda_graph_bs_value
    ):
        raise ProfileContractError("profile CUDA graph batch sizes are invalid")
    cuda_graph_bs_decode = tuple(cuda_graph_bs_value)
    expected_graph_shape = (0, ()) if cuda_graph_backend_decode == "disabled" else (1, (1,))
    if (
        (cuda_graph_max_bs_decode, cuda_graph_bs_decode) != expected_graph_shape
        or value["cuda_graph_backend_prefill"] != "disabled"
    ):
        raise ProfileContractError("profile CUDA graph selection is invalid")
    _closed_list(value["allowed_modes"], _MODES)
    _closed_list(value["allowed_workloads"], _WORKLOADS)
    if value["no_cpu_offload"] is not True:
        raise ProfileContractError("profile permits CPU offload")
    return LaunchProfile(
        profile_id=profile_id,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        status=value["status"],
        runtime_image=runtime_image,
        runtime_image_config_digest=runtime_config_digest,
        runtime_image_oci_manifest_digest=runtime_oci_digest,
        sglang_compat_patch_sha256=compat_patch_sha256,
        source_model_manifest_sha256=value["source_model_manifest_sha256"],
        hardware_runtime_receipt_sha256=value["hardware_runtime_receipt_sha256"],
        model_path=value["model_path"],
        served_model_alias=value["served_model_alias"],
        quantization=value["quantization"],
        dtype=value["dtype"],
        kv_cache_dtype=value["kv_cache_dtype"],
        kv_cache_scale_policy=value["kv_cache_scale_policy"],
        attention_backend=value["attention_backend"],
        prefill_attention_backend=value["prefill_attention_backend"],
        decode_attention_backend=value["decode_attention_backend"],
        sampling_backend=value["sampling_backend"],
        moe_runner_backend=value["moe_runner_backend"],
        mxfp4_moe_precision=value["mxfp4_moe_precision"],
        mm_feature_transport=value["mm_feature_transport"],
        page_size=page_size,
        radix_cache_enabled=value["radix_cache_enabled"],
        overlap_schedule_enabled=value["overlap_schedule_enabled"],
        hybrid_swa_memory_enabled=value["hybrid_swa_memory_enabled"],
        swa_full_tokens_ratio=value["swa_full_tokens_ratio"],
        context_tokens=context_tokens,
        max_total_tokens=max_total_tokens,
        mem_fraction_static=mem_fraction,
        max_running_requests=max_running_requests,
        max_output_tokens=max_output_tokens,
        chunked_prefill_size=chunked_prefill_size,
        cuda_graph_backend_decode=cuda_graph_backend_decode,
        cuda_graph_max_bs_decode=cuda_graph_max_bs_decode,
        cuda_graph_bs_decode=cuda_graph_bs_decode,
        cuda_graph_backend_prefill=value["cuda_graph_backend_prefill"],
    )
