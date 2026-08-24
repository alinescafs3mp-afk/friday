"""Code-owned identities for secondary runtimes admitted to Friday."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PROFILE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,79}")
_PROFILE_SCHEMA = "friday.secondary-runtime-profile.v1"
_EXPECTED_MODEL_PATH = "/models/gpt-oss-20b-nvfp4-modelopt/candidate"
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
        "converted_model_manifest_sha256",
        "conversion_manifest_sha256",
        "gateway_ca_certificate_sha256",
        "runtime_image",
        "runtime_source_revision",
        "runtime_manifest_sha256",
        "model_path",
        "quantization",
        "kv_cache_dtype",
        "attention_backend",
        "fp4_gemm_backend",
        "context_tokens",
        "max_total_tokens",
        "mem_fraction_static",
        "max_running_requests",
        "max_output_tokens",
        "chunked_prefill_size",
        "cuda_graph_max_bs",
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
    "converted_model_manifest_sha256",
    "conversion_manifest_sha256",
    "runtime_image",
    "runtime_source_revision",
    "runtime_manifest_sha256",
    "model_path",
    "quantization",
    "kv_cache_dtype",
    "attention_backend",
    "fp4_gemm_backend",
    "context_tokens",
    "max_total_tokens",
    "mem_fraction_static",
    "max_running_requests",
    "max_output_tokens",
    "chunked_prefill_size",
    "cuda_graph_max_bs",
    "no_cpu_offload",
)
_REQUIRED_HASH_KEYS = (
    "hardware_runtime_receipt_sha256",
    "converted_model_manifest_sha256",
    "conversion_manifest_sha256",
    "runtime_manifest_sha256",
    "quality_evidence_sha256",
    "capacity_evidence_sha256",
    "soak_evidence_sha256",
    "failure_evidence_sha256",
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
    gateway_ca_certificate_sha256: str
    max_context_tokens: int
    max_total_tokens: int
    max_concurrency: int
    max_output_tokens: int
    mem_fraction_static: str
    quantization: str
    kv_cache_dtype: str
    attention_backend: str
    fp4_gemm_backend: str
    allowed_modes: frozenset[str]
    allowed_workloads: frozenset[str]
    model_repository: str
    model_revision: str
    model_manifest_sha256: str
    runtime_image: str
    runtime_source_revision: str
    runtime_manifest_sha256: str

    def accepts_manifest(self, raw: bytes) -> bool:
        """Match one exact accepted manifest, never a candidate lookalike."""

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
            or value.get("status") != "accepted"
        ):
            return False
        hashes = [value.get(key) for key in _REQUIRED_HASH_KEYS]
        if any(
            not isinstance(item, str) or _SHA256_RE.fullmatch(item) is None or item == "0" * 64
            for item in hashes
        ):
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
        return not (
            value.get("engine_binding_sha256") != engine_sha256
            or engine_sha256 != self.engine_binding_sha256
            or value.get("profile_id") != self.profile_id
            or self.profile_id != f"gptoss20b-{engine_sha256}"
            or value.get("served_model_alias") != self.served_model_alias
            or value.get("endpoint_base_url") != self.endpoint_base_url
            or value.get("source_model_repository") != self.model_repository
            or value.get("source_model_revision") != self.model_revision
            or value.get("converted_model_manifest_sha256") != self.model_manifest_sha256
            or value.get("gateway_ca_certificate_sha256") != self.gateway_ca_certificate_sha256
            or value.get("runtime_image") != self.runtime_image
            or value.get("runtime_source_revision") != self.runtime_source_revision
            or value.get("runtime_manifest_sha256") != self.runtime_manifest_sha256
            or value.get("model_path") != _EXPECTED_MODEL_PATH
            or value.get("quantization") != self.quantization
            or value.get("kv_cache_dtype") != self.kv_cache_dtype
            or value.get("attention_backend") != self.attention_backend
            or value.get("fp4_gemm_backend") != self.fp4_gemm_backend
            or value.get("context_tokens") != self.max_context_tokens
            or value.get("max_total_tokens") != self.max_total_tokens
            or value.get("mem_fraction_static") != self.mem_fraction_static
            or value.get("max_running_requests") != self.max_concurrency
            or value.get("max_output_tokens") != self.max_output_tokens
            or value.get("cuda_graph_max_bs") != 1
            or not isinstance(value.get("chunked_prefill_size"), int)
            or isinstance(value.get("chunked_prefill_size"), bool)
            or not 512 <= value["chunked_prefill_size"] <= 2_048
            or value.get("no_cpu_offload") is not True
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
            self.model_manifest_sha256,
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
            and self.max_context_tokens > 0
            and self.max_total_tokens == self.max_context_tokens
            and self.max_concurrency == 1
            and 1 <= self.max_output_tokens <= self.max_context_tokens
            and self.mem_fraction_static in {"0.86", "0.88", "0.90", "0.92", "0.94"}
            and self.quantization == "modelopt_fp4"
            and self.kv_cache_dtype in {"none", "fp8_e4m3"}
            and self.attention_backend == "triton"
            and self.fp4_gemm_backend == "flashinfer_cutlass"
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
            and re.fullmatch(r"lmsysorg/sglang@sha256:[0-9a-f]{64}", self.runtime_image)
            and re.fullmatch(r"[0-9a-f]{40}", self.runtime_source_revision)
        )


# Filled only from a completed live battery and an immutable profile manifest.
# An empty registry deliberately makes every private-LAN endpoint fail closed.
ACCEPTED_SECONDARY_RUNTIME_PROFILES: Mapping[str, SecondaryRuntimeProfile] = MappingProxyType({})


def get_secondary_runtime_profile(profile_id: str) -> SecondaryRuntimeProfile | None:
    profile = ACCEPTED_SECONDARY_RUNTIME_PROFILES.get(profile_id)
    if profile is None or not profile.is_well_formed:
        return None
    return profile
