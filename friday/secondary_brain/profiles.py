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
_PROFILE_SCHEMA = "friday.secondary-runtime-profile.v2"
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
_CHUNKED_PREFILL_GRID = frozenset({512, 1024, 1536, 2048})
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
    "runtime_manifest_sha256",
)
_EVIDENCE_HASH_KEYS = (
    "quality_evidence_sha256",
    "capacity_evidence_sha256",
    "soak_evidence_sha256",
    "failure_evidence_sha256",
)
_ZERO_SHA256 = "0" * 64


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
                not isinstance(item, str)
                or _SHA256_RE.fullmatch(item) is None
                or item == _ZERO_SHA256
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
            and self.mem_fraction_static
            in {"0.86", "0.88", "0.90", "0.92", "0.94", "0.95", "0.96", "0.97"}
            and self.quantization == "mxfp4"
            and self.dtype == "bfloat16"
            and self.kv_cache_dtype in {"bf16", "fp8_e4m3"}
            and self.kv_cache_scale_policy
            == ("not_applicable" if self.kv_cache_dtype == "bf16" else "implicit_unit")
            and self.attention_backend == "triton"
            and self.prefill_attention_backend == "triton"
            and self.decode_attention_backend in {"triton", "trtllm_mha"}
            and self.sampling_backend in {"pytorch", "flashinfer"}
            and self.moe_runner_backend == "flashinfer_mxfp4"
            and self.mxfp4_moe_precision == "default"
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


# Filled only from a completed live battery and an immutable profile manifest.
# An empty registry deliberately makes every private-LAN endpoint fail closed.
ACCEPTED_SECONDARY_RUNTIME_PROFILES: Mapping[str, SecondaryRuntimeProfile] = MappingProxyType({})

# Filled only with one exact matrix finalist after quality/capacity/soak screening.
# This registry never grants assist authority and remains empty until that point.
PROVISIONAL_SHADOW_SECONDARY_RUNTIME_PROFILES: Mapping[
    str, SecondaryRuntimeProfile
] = MappingProxyType({})


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
