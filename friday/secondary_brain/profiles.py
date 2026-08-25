"""Code-owned identities for secondary runtimes admitted to Friday."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PROFILE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,79}")
_PROFILE_SCHEMA = "friday.secondary-runtime-profile.v7"
_EXPECTED_HARDWARE_RUNTIME_RECEIPT_SHA256 = "0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f"
_EXPECTED_SOURCE_MODEL_MANIFEST_SHA256 = "438df0a0b2f6b4164c2fd9d9ed309925abbc94ed8deb056b692d2ccad7887fd9"
_EXPECTED_RUNTIME_IMAGE = (
    "lmsysorg/sglang@sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405"
)
_EXPECTED_RUNTIME_IMAGE_CONFIG_DIGEST = (
    "sha256:f7adc6c05df9ff711b82ad291cf1db6eaf30590c4d929833d632abfef3895efc"
)
_EXPECTED_RUNTIME_IMAGE_OCI_MANIFEST_DIGEST = (
    "sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405"
)
_EXPECTED_RUNTIME_SOURCE_REVISION = "29481685462732237d80d86076d6563e1f658102"
_EXPECTED_MODEL_PATH = "/source/snapshot"
_CONTEXT_LADDER = frozenset({4096, 8192, 12288, 16384, 24576, 32768, 40960, 49152, 65536})
_CHUNKED_PREFILL_GRID = frozenset({256, 512, 1024, 1536, 2048})
_PROFILE_KEYS = frozenset(
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
        "sglang_sampler_compat_patch_sha256",
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
        "deterministic_inference_enabled",
        "context_tokens",
        "max_total_tokens",
        "mem_fraction_static",
        "max_running_requests",
        "max_output_tokens",
        "chunked_prefill_size",
        "page_size",
        "radix_cache_enabled",
        "overlap_schedule_enabled",
        "hybrid_swa_memory_enabled",
        "swa_full_tokens_ratio",
        "cuda_graph_backend_decode",
        "cuda_graph_backend_prefill",
        "cuda_graph_max_bs_decode",
        "cuda_graph_bs_decode",
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
    "sglang_sampler_compat_patch_sha256",
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
    "deterministic_inference_enabled",
    "context_tokens",
    "max_total_tokens",
    "mem_fraction_static",
    "max_running_requests",
    "max_output_tokens",
    "chunked_prefill_size",
    "page_size",
    "radix_cache_enabled",
    "overlap_schedule_enabled",
    "hybrid_swa_memory_enabled",
    "swa_full_tokens_ratio",
    "cuda_graph_backend_decode",
    "cuda_graph_backend_prefill",
    "cuda_graph_max_bs_decode",
    "cuda_graph_bs_decode",
    "no_cpu_offload",
)
_REQUIRED_HASH_KEYS = (
    "hardware_runtime_receipt_sha256",
    "source_model_manifest_sha256",
    "sglang_compat_patch_sha256",
    "sglang_sampler_compat_patch_sha256",
    "runtime_manifest_sha256",
)
_EVIDENCE_HASH_KEYS = (
    "quality_evidence_sha256",
    "capacity_evidence_sha256",
    "soak_evidence_sha256",
    "failure_evidence_sha256",
)
_ZERO_SHA256 = "0" * 64
_WORKLOAD_POLICY_SCHEMA = "friday.secondary-workload-policy.v1"
_WORKLOAD_POLICY_KEYS = frozenset(
    {
        "schema",
        "status",
        "policy_id",
        "runtime_profile_id",
        "runtime_profile_manifest_sha256",
        "allowed_global_modes",
        "document_map_modes",
        "additional_workloads",
        "modality",
        "effect_class",
        "private_text_required",
        "max_context_tokens",
        "max_output_tokens",
        "max_concurrency",
        "primary_fallback_required",
        "primary_final_synthesis_required",
        "secondary_publication_allowed",
        "secondary_tools_allowed",
        "secondary_effects_allowed",
        "gateway_manifest_change_required",
        "windows_container_restart_required",
    }
)


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite profile value")


@dataclass(frozen=True, slots=True)
class SecondaryRuntimeProfile:
    """One immutable, live-certified advisory runtime binding."""

    profile_id: str
    endpoint_base_url: str
    served_model_alias: str
    manifest_sha256: str
    engine_binding_sha256: str
    hardware_runtime_receipt_sha256: str
    gateway_ca_certificate_sha256: str
    max_context_tokens: int
    max_total_tokens: int
    max_concurrency: int
    max_output_tokens: int
    chunked_prefill_size: int
    mem_fraction_static: str
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
    deterministic_inference_enabled: bool
    page_size: int
    radix_cache_enabled: bool
    overlap_schedule_enabled: bool
    hybrid_swa_memory_enabled: bool
    swa_full_tokens_ratio: str
    cuda_graph_backend_decode: str
    cuda_graph_backend_prefill: str
    cuda_graph_max_bs_decode: int
    cuda_graph_bs_decode: tuple[int, ...]
    no_cpu_offload: bool
    allowed_modes: frozenset[str]
    allowed_workloads: frozenset[str]
    model_repository: str
    model_revision: str
    source_model_manifest_sha256: str
    model_path: str
    runtime_image: str
    runtime_image_config_digest: str
    runtime_image_oci_manifest_digest: str
    runtime_source_revision: str
    sglang_compat_patch_sha256: str
    sglang_sampler_compat_patch_sha256: str
    runtime_manifest_sha256: str

    def accepts_manifest(self, raw: bytes) -> bool:
        """Match one exact accepted manifest, never a candidate lookalike."""

        return self._accepts_exact_manifest(raw, expected_status="accepted")

    def accepts_provisional_candidate_manifest(self, raw: bytes) -> bool:
        """Match one exact unelevated candidate for shadow-only observation."""

        return self._accepts_exact_manifest(raw, expected_status="candidate")

    def _accepts_exact_manifest(self, raw: bytes, *, expected_status: str) -> bool:
        if expected_status not in {"accepted", "candidate"}:
            return False

        if not 1 <= len(raw) <= 65_536 or hashlib.sha256(raw).hexdigest() != self.manifest_sha256:
            return False
        try:
            value = json.loads(
                raw.decode("utf-8", errors="strict"),
                parse_constant=_reject_constant,
            )
        except (UnicodeError, ValueError, TypeError):
            return False
        if (
            not isinstance(value, dict)
            or set(value) != _PROFILE_KEYS
            or raw != _canonical_json(value)
            or value.get("schema") != _PROFILE_SCHEMA
            or value.get("status") != expected_status
        ):
            return False
        hashes = [value.get(key) for key in _REQUIRED_HASH_KEYS]
        if any(
            not isinstance(item, str) or _SHA256_RE.fullmatch(item) is None or item == _ZERO_SHA256
            for item in hashes
        ):
            return False
        evidence_hashes = [value.get(key) for key in _EVIDENCE_HASH_KEYS]
        if expected_status == "accepted":
            if any(
                not isinstance(item, str) or _SHA256_RE.fullmatch(item) is None or item == _ZERO_SHA256
                for item in evidence_hashes
            ):
                return False
        elif evidence_hashes != [_ZERO_SHA256] * len(_EVIDENCE_HASH_KEYS):
            return False
        try:
            endpoint = urlsplit(self.endpoint_base_url)
        except ValueError:
            return False
        local_http = endpoint.scheme == "http" and endpoint.hostname in {
            "127.0.0.1",
            "::1",
            "localhost",
        }
        manifest_ca_sha256 = value.get("gateway_ca_certificate_sha256")
        if not (
            (local_http and manifest_ca_sha256 == "")
            or (
                endpoint.scheme == "https"
                and isinstance(manifest_ca_sha256, str)
                and _SHA256_RE.fullmatch(manifest_ca_sha256) is not None
                and manifest_ca_sha256 != "0" * 64
            )
        ):
            return False
        try:
            engine_projection = {key: value[key] for key in _ENGINE_KEYS}
        except KeyError:
            return False
        engine_sha256 = hashlib.sha256(_canonical_json(engine_projection)).hexdigest()
        modes = value.get("allowed_modes")
        workloads = value.get("allowed_workloads")
        exact_integer_fields = (
            "context_tokens",
            "max_total_tokens",
            "max_running_requests",
            "max_output_tokens",
            "chunked_prefill_size",
            "page_size",
            "cuda_graph_max_bs_decode",
        )
        return not (
            any(type(value.get(key)) is not int for key in exact_integer_fields)
            or value.get("context_tokens") not in _CONTEXT_LADDER
            or value.get("engine_binding_sha256") != engine_sha256
            or engine_sha256 != self.engine_binding_sha256
            or value.get("profile_id") != self.profile_id
            or self.profile_id != f"gptoss20b-{engine_sha256}"
            or value.get("served_model_alias") != self.served_model_alias
            or value.get("endpoint_base_url") != self.endpoint_base_url
            or value.get("hardware_runtime_receipt_sha256") != self.hardware_runtime_receipt_sha256
            or value.get("source_model_repository") != self.model_repository
            or value.get("source_model_revision") != self.model_revision
            or value.get("source_model_manifest_sha256") != self.source_model_manifest_sha256
            or value.get("gateway_ca_certificate_sha256") != self.gateway_ca_certificate_sha256
            or value.get("runtime_image") != self.runtime_image
            or value.get("runtime_image_config_digest") != self.runtime_image_config_digest
            or value.get("runtime_image_oci_manifest_digest") != self.runtime_image_oci_manifest_digest
            or value.get("runtime_source_revision") != self.runtime_source_revision
            or value.get("sglang_compat_patch_sha256") != self.sglang_compat_patch_sha256
            or value.get("sglang_sampler_compat_patch_sha256") != self.sglang_sampler_compat_patch_sha256
            or value.get("runtime_manifest_sha256") != self.runtime_manifest_sha256
            or value.get("model_path") != self.model_path
            or value.get("quantization") != self.quantization
            or value.get("dtype") != self.dtype
            or value.get("kv_cache_dtype") != self.kv_cache_dtype
            or value.get("kv_cache_scale_policy") != self.kv_cache_scale_policy
            or value.get("attention_backend") != self.attention_backend
            or value.get("prefill_attention_backend") != self.prefill_attention_backend
            or value.get("decode_attention_backend") != self.decode_attention_backend
            or value.get("sampling_backend") != self.sampling_backend
            or value.get("moe_runner_backend") != self.moe_runner_backend
            or value.get("mxfp4_moe_precision") != self.mxfp4_moe_precision
            or value.get("mm_feature_transport") != self.mm_feature_transport
            or value.get("deterministic_inference_enabled") is not self.deterministic_inference_enabled
            or value.get("context_tokens") != self.max_context_tokens
            or value.get("max_total_tokens") != self.max_total_tokens
            or value.get("mem_fraction_static") != self.mem_fraction_static
            or value.get("max_running_requests") != self.max_concurrency
            or value.get("max_output_tokens") != self.max_output_tokens
            or value.get("chunked_prefill_size") != self.chunked_prefill_size
            or value.get("page_size") != self.page_size
            or value.get("radix_cache_enabled") is not self.radix_cache_enabled
            or value.get("overlap_schedule_enabled") is not self.overlap_schedule_enabled
            or value.get("hybrid_swa_memory_enabled") is not self.hybrid_swa_memory_enabled
            or value.get("swa_full_tokens_ratio") != self.swa_full_tokens_ratio
            or value.get("cuda_graph_backend_decode") != self.cuda_graph_backend_decode
            or value.get("cuda_graph_backend_prefill") != self.cuda_graph_backend_prefill
            or value.get("cuda_graph_max_bs_decode") != self.cuda_graph_max_bs_decode
            or value.get("cuda_graph_bs_decode") != list(self.cuda_graph_bs_decode)
            or value.get("no_cpu_offload") is not self.no_cpu_offload
            or not isinstance(modes, list)
            or modes != sorted(self.allowed_modes)
            or not isinstance(workloads, list)
            or workloads != sorted(self.allowed_workloads)
        )

    @property
    def is_well_formed(self) -> bool:
        try:
            endpoint = urlsplit(self.endpoint_base_url)
            hostname = endpoint.hostname
        except ValueError:
            hostname = None
            endpoint = urlsplit("")
        local_http = endpoint.scheme == "http" and hostname in {"127.0.0.1", "::1", "localhost"}
        hashes = (
            self.manifest_sha256,
            self.engine_binding_sha256,
            self.hardware_runtime_receipt_sha256,
            self.source_model_manifest_sha256,
            self.sglang_compat_patch_sha256,
            self.sglang_sampler_compat_patch_sha256,
            self.runtime_manifest_sha256,
        )
        ca_is_bound = bool(
            (local_http and not self.gateway_ca_certificate_sha256)
            or (endpoint.scheme == "https" and _SHA256_RE.fullmatch(self.gateway_ca_certificate_sha256))
        )
        return bool(
            _PROFILE_ID_RE.fullmatch(self.profile_id)
            and self.profile_id == f"gptoss20b-{self.engine_binding_sha256}"
            and (endpoint.scheme == "https" or local_http)
            and hostname
            and not endpoint.username
            and not endpoint.password
            and endpoint.path.rstrip("/") == "/v1"
            and not endpoint.query
            and not endpoint.fragment
            and self.served_model_alias == f"friday-secondary-{self.profile_id}"
            and all(_SHA256_RE.fullmatch(value) for value in hashes)
            and all(value != _ZERO_SHA256 for value in hashes)
            and ca_is_bound
            and type(self.max_context_tokens) is int
            and self.max_context_tokens in _CONTEXT_LADDER
            and type(self.max_total_tokens) is int
            and self.max_total_tokens == self.max_context_tokens
            and type(self.max_concurrency) is int
            and self.max_concurrency == 1
            and type(self.max_output_tokens) is int
            and 64 <= self.max_output_tokens <= 4096
            and self.max_output_tokens < self.max_context_tokens
            and type(self.chunked_prefill_size) is int
            and self.chunked_prefill_size in _CHUNKED_PREFILL_GRID
            and self.mem_fraction_static in {"0.86", "0.88", "0.90", "0.92", "0.94", "0.95", "0.96", "0.97"}
            and self.quantization == "mxfp4"
            and self.dtype == "bfloat16"
            and self.kv_cache_dtype in {"bf16", "fp8_e4m3"}
            and self.kv_cache_scale_policy
            == ("not_applicable" if self.kv_cache_dtype == "bf16" else "implicit_unit")
            and self.attention_backend == "triton"
            and self.prefill_attention_backend == "triton"
            and self.decode_attention_backend == "triton"
            and self.sampling_backend == "pytorch"
            and self.moe_runner_backend == "flashinfer_mxfp4"
            and self.mxfp4_moe_precision == "default"
            and self.mm_feature_transport == "cpu"
            and self.deterministic_inference_enabled is False
            and type(self.page_size) is int
            and self.page_size in {1, 16}
            and type(self.radix_cache_enabled) is bool
            and type(self.overlap_schedule_enabled) is bool
            and self.hybrid_swa_memory_enabled is True
            and self.swa_full_tokens_ratio in {"0.25", "0.50", "0.80", "1.00"}
            and self.cuda_graph_backend_decode in {"disabled", "full"}
            and self.cuda_graph_backend_prefill == "disabled"
            and type(self.cuda_graph_max_bs_decode) is int
            and (
                (
                    self.cuda_graph_backend_decode == "disabled"
                    and self.cuda_graph_max_bs_decode == 0
                    and self.cuda_graph_bs_decode == ()
                )
                or (
                    self.cuda_graph_backend_decode == "full"
                    and self.cuda_graph_max_bs_decode == 1
                    and self.cuda_graph_bs_decode == (1,)
                )
            )
            and self.no_cpu_offload is True
            and self.allowed_modes
            and self.allowed_modes <= {"shadow", "assist"}
            and self.allowed_workloads
            and self.allowed_workloads
            <= {
                "classify",
                "extract",
                "query_rewrite",
                "summarize",
                "document_map",
                "critique",
                "verify",
                "plan_candidate",
            }
            and self.model_repository == "openai/gpt-oss-20b"
            and self.model_revision == "6cee5e81ee83917806bbde320786a8fb61efebee"
            and self.hardware_runtime_receipt_sha256 == _EXPECTED_HARDWARE_RUNTIME_RECEIPT_SHA256
            and self.source_model_manifest_sha256 == _EXPECTED_SOURCE_MODEL_MANIFEST_SHA256
            and self.model_path == _EXPECTED_MODEL_PATH
            and self.runtime_image == _EXPECTED_RUNTIME_IMAGE
            and self.runtime_image_config_digest == _EXPECTED_RUNTIME_IMAGE_CONFIG_DIGEST
            and self.runtime_image_oci_manifest_digest == _EXPECTED_RUNTIME_IMAGE_OCI_MANIFEST_DIGEST
            and self.runtime_source_revision == _EXPECTED_RUNTIME_SOURCE_REVISION
        )


@dataclass(frozen=True, slots=True)
class SecondaryWorkloadPolicy:
    """Code-owned product policy layered over one unchanged runtime identity."""

    policy_id: str
    manifest_sha256: str
    runtime_profile_id: str
    runtime_profile_manifest_sha256: str
    allowed_global_modes: frozenset[str]
    document_map_modes: frozenset[str]
    additional_workloads: frozenset[str]
    max_context_tokens: int
    max_output_tokens: int
    max_concurrency: int

    def accepts_manifest(self, raw: bytes) -> bool:
        if not 1 <= len(raw) <= 16_384 or hashlib.sha256(raw).hexdigest() != self.manifest_sha256:
            return False
        try:
            value = json.loads(
                raw.decode("utf-8", errors="strict"),
                parse_constant=_reject_constant,
            )
        except (UnicodeError, ValueError, TypeError):
            return False
        return bool(
            isinstance(value, dict)
            and set(value) == _WORKLOAD_POLICY_KEYS
            and raw == _canonical_json(value)
            and value.get("schema") == _WORKLOAD_POLICY_SCHEMA
            and value.get("status") == "implementation_ready"
            and value.get("policy_id") == self.policy_id
            and value.get("runtime_profile_id") == self.runtime_profile_id
            and value.get("runtime_profile_manifest_sha256") == self.runtime_profile_manifest_sha256
            and value.get("allowed_global_modes") == sorted(self.allowed_global_modes)
            and value.get("document_map_modes") == sorted(self.document_map_modes)
            and value.get("additional_workloads") == sorted(self.additional_workloads)
            and value.get("modality") == "text"
            and value.get("effect_class") == "read_only"
            and value.get("private_text_required") is True
            and value.get("max_context_tokens") == self.max_context_tokens
            and value.get("max_output_tokens") == self.max_output_tokens
            and value.get("max_concurrency") == self.max_concurrency
            and value.get("primary_fallback_required") is True
            and value.get("primary_final_synthesis_required") is True
            and value.get("secondary_publication_allowed") is False
            and value.get("secondary_tools_allowed") is False
            and value.get("secondary_effects_allowed") is False
            and value.get("gateway_manifest_change_required") is False
            and value.get("windows_container_restart_required") is False
        )

    @property
    def is_well_formed(self) -> bool:
        return bool(
            self.policy_id == "gptoss20b-document-map-v1"
            and _SHA256_RE.fullmatch(self.manifest_sha256)
            and _PROFILE_ID_RE.fullmatch(self.runtime_profile_id)
            and _SHA256_RE.fullmatch(self.runtime_profile_manifest_sha256)
            and self.allowed_global_modes == frozenset({"assist"})
            and self.document_map_modes == frozenset({"assist", "shadow"})
            and self.additional_workloads == frozenset({"document_map"})
            and self.max_context_tokens == 4096
            and self.max_output_tokens == 512
            and self.max_concurrency == 1
        )


# Exact finalist accepted by the complete quality/capacity/soak/failure chain.
# Product policy remains separate: the initial release keeps public discarded
# shadow/extract, while private shadow and assist require distinct activations.
_ACCEPTED_GPT_OSS_20B_FINALIST = SecondaryRuntimeProfile(
    profile_id=("gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f"),
    endpoint_base_url="https://192.168.1.35:8443/v1",
    served_model_alias=(
        "friday-secondary-gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f"
    ),
    manifest_sha256="93ea5698b8b6a9bf8a7dc697ffe37d7353055aa16555188991747bba73d059e3",
    engine_binding_sha256=("2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f"),
    hardware_runtime_receipt_sha256=("0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f"),
    gateway_ca_certificate_sha256=("392756a74fd9100635c42f4fbf7e5a5f1822d18ea898ebb7848b9fdd0bddc1fe"),
    max_context_tokens=4096,
    max_total_tokens=4096,
    max_concurrency=1,
    max_output_tokens=512,
    chunked_prefill_size=256,
    mem_fraction_static="0.96",
    quantization="mxfp4",
    dtype="bfloat16",
    kv_cache_dtype="bf16",
    kv_cache_scale_policy="not_applicable",
    attention_backend="triton",
    prefill_attention_backend="triton",
    decode_attention_backend="triton",
    sampling_backend="pytorch",
    moe_runner_backend="flashinfer_mxfp4",
    mxfp4_moe_precision="default",
    mm_feature_transport="cpu",
    deterministic_inference_enabled=False,
    page_size=1,
    radix_cache_enabled=True,
    overlap_schedule_enabled=True,
    hybrid_swa_memory_enabled=True,
    swa_full_tokens_ratio="0.80",
    cuda_graph_backend_decode="full",
    cuda_graph_backend_prefill="disabled",
    cuda_graph_max_bs_decode=1,
    cuda_graph_bs_decode=(1,),
    no_cpu_offload=True,
    allowed_modes=frozenset({"assist", "shadow"}),
    allowed_workloads=frozenset({"extract"}),
    model_repository="openai/gpt-oss-20b",
    model_revision="6cee5e81ee83917806bbde320786a8fb61efebee",
    source_model_manifest_sha256=("438df0a0b2f6b4164c2fd9d9ed309925abbc94ed8deb056b692d2ccad7887fd9"),
    model_path="/source/snapshot",
    runtime_image=("lmsysorg/sglang@sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405"),
    runtime_image_config_digest=("sha256:f7adc6c05df9ff711b82ad291cf1db6eaf30590c4d929833d632abfef3895efc"),
    runtime_image_oci_manifest_digest=(
        "sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405"
    ),
    runtime_source_revision="29481685462732237d80d86076d6563e1f658102",
    sglang_compat_patch_sha256=("4ec4bbf76c047bf93d782525250ef79f8c2dae925d0035b95d97a41285052ffb"),
    sglang_sampler_compat_patch_sha256=("5ddc5343c1ac368052046bc467d0d8fbd7fe3288b6ea8f88beb89cd4c8962d2e"),
    runtime_manifest_sha256=("15be7b3bdaa3cd76ace1bcc93ca461598a9583d920f4f3e55924db2f6b643428"),
)

# This policy deliberately does not alter the gateway-served runtime manifest.
# The engine, Windows bundle, profile ID and accepted manifest digest remain
# exactly the already-certified finalist.  It only admits one additional
# product workload through an explicit release-operator transition; the model
# still receives bounded text and can neither publish nor execute anything.
_DOCUMENT_MAP_WORKLOAD_POLICY = SecondaryWorkloadPolicy(
    policy_id="gptoss20b-document-map-v1",
    manifest_sha256="c881eefe53d5b02baee3feb133605838021fabe642578b163bdd46e6bd8a2fc2",
    runtime_profile_id=_ACCEPTED_GPT_OSS_20B_FINALIST.profile_id,
    runtime_profile_manifest_sha256=_ACCEPTED_GPT_OSS_20B_FINALIST.manifest_sha256,
    allowed_global_modes=frozenset({"assist"}),
    document_map_modes=frozenset({"assist", "shadow"}),
    additional_workloads=frozenset({"document_map"}),
    max_context_tokens=4096,
    max_output_tokens=512,
    max_concurrency=1,
)

SECONDARY_WORKLOAD_POLICIES: Mapping[str, SecondaryWorkloadPolicy] = MappingProxyType(
    {_DOCUMENT_MAP_WORKLOAD_POLICY.policy_id: _DOCUMENT_MAP_WORKLOAD_POLICY}
)


# Filled only from a completed live battery and an immutable profile manifest.
ACCEPTED_SECONDARY_RUNTIME_PROFILES: Mapping[str, SecondaryRuntimeProfile] = MappingProxyType(
    {_ACCEPTED_GPT_OSS_20B_FINALIST.profile_id: _ACCEPTED_GPT_OSS_20B_FINALIST}
)

# Accepted and provisional registries are disjoint by construction.  A new
# candidate must earn its own exact code-owned entry before public shadow.
PROVISIONAL_SHADOW_SECONDARY_RUNTIME_PROFILES: Mapping[str, SecondaryRuntimeProfile] = MappingProxyType({})


class SecondaryProfileAdmission(StrEnum):
    ACCEPTED = "accepted"
    PROVISIONAL_SHADOW = "provisional_shadow"


@dataclass(frozen=True, slots=True)
class SecondaryRuntimeAdmission:
    """One code-owned profile plus the only manifest status it may serve."""

    profile: SecondaryRuntimeProfile
    kind: SecondaryProfileAdmission

    @property
    def is_provisional_shadow(self) -> bool:
        return self.kind is SecondaryProfileAdmission.PROVISIONAL_SHADOW

    def accepts_manifest(self, raw: bytes) -> bool:
        if self.kind is SecondaryProfileAdmission.ACCEPTED:
            return self.profile.accepts_manifest(raw)
        return self.profile.accepts_provisional_candidate_manifest(raw)


def get_secondary_runtime_profile(profile_id: str) -> SecondaryRuntimeProfile | None:
    profile = ACCEPTED_SECONDARY_RUNTIME_PROFILES.get(profile_id)
    if profile is None or not profile.is_well_formed:
        return None
    return profile


def get_secondary_workload_policy(
    profile: SecondaryRuntimeProfile,
    *,
    global_mode: str,
) -> SecondaryWorkloadPolicy | None:
    """Resolve the exact product overlay without changing endpoint identity."""

    policy = SECONDARY_WORKLOAD_POLICIES.get("gptoss20b-document-map-v1")
    if (
        policy is None
        or not policy.is_well_formed
        or policy.runtime_profile_id != profile.profile_id
        or policy.runtime_profile_manifest_sha256 != profile.manifest_sha256
        or global_mode not in policy.allowed_global_modes
        or policy.max_context_tokens != profile.max_context_tokens
        or policy.max_output_tokens != profile.max_output_tokens
        or policy.max_concurrency != profile.max_concurrency
    ):
        return None
    return policy


def secondary_effective_workloads(
    profile: SecondaryRuntimeProfile,
    *,
    global_mode: str,
) -> frozenset[str]:
    """Return runtime-certified workloads plus one exact product overlay."""

    policy = get_secondary_workload_policy(profile, global_mode=global_mode)
    if policy is None:
        return profile.allowed_workloads
    return profile.allowed_workloads | policy.additional_workloads


def get_secondary_runtime_admission(
    profile_id: str,
    *,
    mode: str,
) -> SecondaryRuntimeAdmission | None:
    """Resolve accepted profiles normally and candidates only for exact shadow mode."""

    if profile_id in ACCEPTED_SECONDARY_RUNTIME_PROFILES and profile_id in (
        PROVISIONAL_SHADOW_SECONDARY_RUNTIME_PROFILES
    ):
        return None
    accepted = get_secondary_runtime_profile(profile_id)
    if accepted is not None:
        if mode not in accepted.allowed_modes:
            return None
        return SecondaryRuntimeAdmission(accepted, SecondaryProfileAdmission.ACCEPTED)
    if mode != "shadow":
        return None
    provisional = PROVISIONAL_SHADOW_SECONDARY_RUNTIME_PROFILES.get(profile_id)
    if (
        provisional is None
        or not provisional.is_well_formed
        or "shadow" not in provisional.allowed_modes
        or provisional.allowed_workloads != frozenset({"extract"})
    ):
        return None
    return SecondaryRuntimeAdmission(
        provisional,
        SecondaryProfileAdmission.PROVISIONAL_SHADOW,
    )
