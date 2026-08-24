"""Code-owned identities for secondary runtimes admitted to Friday."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PROFILE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,79}")


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
