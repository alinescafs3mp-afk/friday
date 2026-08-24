"""Secret-safe, dependency-free helpers for the secondary SGLang node tools."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import ssl
import stat
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

MAX_RESPONSE_BYTES = 1_048_576
EXPECTED_MODEL = "friday-secondary-gptoss20b"
EXPECTED_HARDWARE_RUNTIME_RECEIPT_SHA256 = "0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f"
EXPECTED_SOURCE_MODEL_MANIFEST_SHA256 = "438df0a0b2f6b4164c2fd9d9ed309925abbc94ed8deb056b692d2ccad7887fd9"
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
_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{2,79}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONTEXT_LADDER = frozenset(
    {4096, 8192, 12288, 16384, 24576, 32768, 40960, 49152, 65536}
)
_MEMORY_FRACTIONS = frozenset(
    {"0.86", "0.88", "0.90", "0.92", "0.94", "0.95", "0.96", "0.97"}
)
_REASONING_EFFORTS = frozenset({"low", "medium", "high"})
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
    "deterministic_inference_enabled",
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
_HARMONY_MARKERS = (
    "<|analysis|>",
    "<|call|>",
    "<|channel|>",
    "<|constrain|>",
    "<|end|>",
    "<|final|>",
    "<|message|>",
    "<|recipient|>",
    "<|return|>",
    "<|start|>",
)
_REASONING_MARKERS = ("<think>", "</think>")
_NUMERICAL_FAILURE = re.compile(r"(?:^|[^a-z])(nan|[+-]?inf(?:inity)?)(?:$|[^a-z])", re.IGNORECASE)


class EndpointError(RuntimeError):
    """A bounded endpoint or protocol failure without untrusted response data."""


@dataclass(frozen=True, slots=True)
class EndpointIdentity:
    model_alias: str
    profile_id: str
    profile_sha256: str
    profile_bytes: bytes
    ca_sha256: str
    ca_pem: str
    endpoint_base_url: str
    context_tokens: int
    mem_fraction_static: str


_ENDPOINT_IDENTITY: EndpointIdentity | None = None


def _reject_json_constant(_value: str) -> None:
    raise EndpointError("runtime profile contains a non-finite number")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EndpointError("runtime profile contains a duplicate key")
        value[key] = item
    return value


def _closed_profile_list(value: Any, allowed: frozenset[str]) -> bool:
    return bool(
        isinstance(value, list)
        and value
        and all(isinstance(item, str) for item in value)
        and value == sorted(set(value))
        and set(value) <= allowed
    )


def _profile_engine_surface_is_valid(value: dict[str, Any]) -> bool:
    context = value.get("context_tokens")
    output = value.get("max_output_tokens")
    page_size = value.get("page_size")
    graph_backend = value.get("cuda_graph_backend_decode")
    graph_max_bs = value.get("cuda_graph_max_bs_decode")
    graph_bs = value.get("cuda_graph_bs_decode")
    kv_cache_dtype = value.get("kv_cache_dtype")
    expected_scale = {"bf16": "not_applicable", "fp8_e4m3": "implicit_unit"}.get(
        kv_cache_dtype if isinstance(kv_cache_dtype, str) else ""
    )
    expected_graph_shape = (0, []) if graph_backend == "disabled" else (1, [1])
    evidence_keys = (
        "sglang_compat_patch_sha256",
        "runtime_manifest_sha256",
        "gateway_ca_certificate_sha256",
        "quality_evidence_sha256",
        "capacity_evidence_sha256",
        "soak_evidence_sha256",
        "failure_evidence_sha256",
    )
    return bool(
        type(context) is int
        and context in _CONTEXT_LADDER
        and type(value.get("max_total_tokens")) is int
        and value["max_total_tokens"] == context
        and value.get("mem_fraction_static") in _MEMORY_FRACTIONS
        and type(value.get("max_running_requests")) is int
        and value["max_running_requests"] == 1
        and type(output) is int
        and 64 <= output <= 4096
        and output < context
        and type(value.get("chunked_prefill_size")) is int
        and value["chunked_prefill_size"] in {512, 1024, 1536, 2048}
        and kv_cache_dtype in {"bf16", "fp8_e4m3"}
        and value.get("kv_cache_scale_policy") == expected_scale
        and value.get("attention_backend") == "triton"
        and value.get("prefill_attention_backend") == "triton"
        and value.get("decode_attention_backend") == "triton"
        and value.get("sampling_backend") == "pytorch"
        and value.get("mm_feature_transport") == "cpu"
        and value.get("moe_runner_backend") == "flashinfer_mxfp4"
        and value.get("deterministic_inference_enabled") is False
        and type(page_size) is int
        and page_size in {1, 16}
        and type(value.get("radix_cache_enabled")) is bool
        and type(value.get("overlap_schedule_enabled")) is bool
        and value.get("hybrid_swa_memory_enabled") is True
        and value.get("swa_full_tokens_ratio") in {"0.25", "0.50", "0.80", "1.00"}
        and graph_backend in {"disabled", "full"}
        and type(graph_max_bs) is int
        and isinstance(graph_bs, list)
        and all(type(item) is int for item in graph_bs)
        and (graph_max_bs, graph_bs) == expected_graph_shape
        and value.get("cuda_graph_backend_prefill") == "disabled"
        and _closed_profile_list(value.get("allowed_modes"), _MODES)
        and _closed_profile_list(value.get("allowed_workloads"), _WORKLOADS)
        and value.get("no_cpu_offload") is True
        and all(isinstance(value.get(key), str) and _SHA256.fullmatch(value[key]) for key in evidence_keys)
        and value.get("sglang_compat_patch_sha256") != "0" * 64
    )


def _read_bounded_regular(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= maximum_bytes:
            raise EndpointError(f"{label} is not a bounded regular file")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != metadata.st_size:
            raise EndpointError(f"{label} identity changed")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(maximum_bytes + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > maximum_bytes or (
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
            raise EndpointError(f"{label} identity changed")
        return raw
    except EndpointError:
        raise
    except OSError as exc:
        raise EndpointError(f"{label} cannot be read") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class SanitizedCompletion:
    content: str
    latency_sec: float
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    reasoning_present: bool


@dataclass(frozen=True, slots=True)
class StreamedCompletion:
    completion: SanitizedCompletion
    ttft_sec: float


def _load_ca_pem(ca_file: Path, expected_sha256: str = "") -> tuple[str, str]:
    try:
        raw = _read_bounded_regular(ca_file, maximum_bytes=65_536, label="private CA file")
        digest = hashlib.sha256(raw).hexdigest()
        pem = raw.decode("ascii", errors="strict")
    except EndpointError:
        raise
    except Exception as exc:
        raise EndpointError("private CA file cannot be loaded") from exc
    if (
        (expected_sha256 and digest != expected_sha256)
        or "-----BEGIN CERTIFICATE-----" not in pem
        or "-----END CERTIFICATE-----" not in pem
    ):
        raise EndpointError("private CA identity is invalid")
    return pem, digest


def configure_expected_model(profile_manifest: Path, ca_file: Path | None = None) -> str:
    """Bind all certification calls to one canonical runtime profile epoch."""

    global EXPECTED_MODEL, _ENDPOINT_IDENTITY
    try:
        raw = _read_bounded_regular(
            profile_manifest,
            maximum_bytes=65_536,
            label="runtime profile",
        )
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_strict_json_object,
        )
    except EndpointError:
        raise
    except Exception as exc:
        raise EndpointError("runtime profile cannot be loaded") from exc
    canonical = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    profile_id = value.get("profile_id") if isinstance(value, dict) else None
    alias = value.get("served_model_alias") if isinstance(value, dict) else None
    try:
        engine_projection = {key: value[key] for key in _ENGINE_KEYS}
    except (KeyError, TypeError):
        raise EndpointError("runtime profile engine projection is incomplete") from None
    binding = hashlib.sha256(
        (
            json.dumps(
                engine_projection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    if (
        not isinstance(value, dict)
        or set(value) != _PROFILE_KEYS
        or raw != canonical
        or value.get("schema") != "friday.secondary-runtime-profile.v6"
        or value.get("status") not in {"candidate", "accepted"}
        or not isinstance(profile_id, str)
        or _PROFILE_ID.fullmatch(profile_id) is None
        or value.get("engine_binding_sha256") != binding
        or value.get("hardware_runtime_receipt_sha256") != EXPECTED_HARDWARE_RUNTIME_RECEIPT_SHA256
        or value.get("source_model_repository") != "openai/gpt-oss-20b"
        or value.get("source_model_revision") != "6cee5e81ee83917806bbde320786a8fb61efebee"
        or value.get("source_model_manifest_sha256") != EXPECTED_SOURCE_MODEL_MANIFEST_SHA256
        or value.get("runtime_image") != EXPECTED_RUNTIME_IMAGE
        or value.get("runtime_image_config_digest") != EXPECTED_RUNTIME_IMAGE_CONFIG_DIGEST
        or value.get("runtime_image_oci_manifest_digest") != EXPECTED_RUNTIME_IMAGE_OCI_MANIFEST_DIGEST
        or value.get("runtime_source_revision") != EXPECTED_RUNTIME_SOURCE_REVISION
        or value.get("endpoint_base_url") != "https://192.168.1.35:8443/v1"
        or value.get("model_path") != "/source/snapshot"
        or value.get("quantization") != "mxfp4"
        or value.get("dtype") != "bfloat16"
        or value.get("moe_runner_backend") != "flashinfer_mxfp4"
        or value.get("mxfp4_moe_precision") != "default"
        or value.get("mm_feature_transport") != "cpu"
        or not _profile_engine_surface_is_valid(value)
        or profile_id != f"gptoss20b-{binding}"
        or alias != f"friday-secondary-{profile_id}"
    ):
        raise EndpointError("runtime profile identity is invalid")
    ca_sha256 = value.get("gateway_ca_certificate_sha256")
    if not isinstance(ca_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", ca_sha256) is None:
        raise EndpointError("runtime profile CA identity is invalid")
    ca_pem = ""
    if ca_file is not None:
        ca_pem, _observed_ca_sha256 = _load_ca_pem(ca_file, ca_sha256)
    EXPECTED_MODEL = alias
    _ENDPOINT_IDENTITY = EndpointIdentity(
        model_alias=alias,
        profile_id=profile_id,
        profile_sha256=hashlib.sha256(raw).hexdigest(),
        profile_bytes=raw,
        ca_sha256=ca_sha256,
        ca_pem=ca_pem,
        endpoint_base_url=str(value["endpoint_base_url"]),
        context_tokens=int(value["context_tokens"]),
        mem_fraction_static=str(value["mem_fraction_static"]),
    )
    return alias


def evidence_identity() -> dict[str, str]:
    """Return the closed candidate epoch projection for content-free evidence."""

    identity = _ENDPOINT_IDENTITY
    if identity is None:
        raise EndpointError("endpoint identity was not configured")
    return {
        "candidate_profile_id": identity.profile_id,
        "candidate_profile_sha256": identity.profile_sha256,
        "served_model_alias": identity.model_alias,
        "gateway_ca_certificate_sha256": identity.ca_sha256,
    }


def configured_profile_context_tokens() -> int:
    """Return the exact candidate context bound by ``configure_expected_model``."""

    identity = _ENDPOINT_IDENTITY
    if identity is None:
        raise EndpointError("endpoint identity was not configured")
    return identity.context_tokens


def configured_profile_mem_fraction_static() -> str:
    """Return the exact candidate memory fraction bound by the runtime profile."""

    identity = _ENDPOINT_IDENTITY
    if identity is None:
        raise EndpointError("endpoint identity was not configured")
    return identity.mem_fraction_static


def load_api_key(path: Path) -> str:
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise EndpointError("API key file is unavailable") from exc
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > 4096:
        raise EndpointError("API key path is not a bounded regular file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EndpointError("API key file cannot be read") from exc
    if b"\x00" in raw or b"\r" in raw.strip(b"\r\n") or b"\n" in raw.strip(b"\r\n"):
        raise EndpointError("API key file must contain exactly one line")
    try:
        key = raw.decode("utf-8").strip("\r\n")
    except UnicodeDecodeError as exc:
        raise EndpointError("API key file is not UTF-8") from exc
    if len(key) < 32 or len(key) > 512 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise EndpointError("API key does not meet the bounded printable-key contract")
    return key


def normalize_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise EndpointError("endpoint URL must be a plain HTTP(S) origin with an optional /v1 path")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise EndpointError("plain HTTP is permitted only for an explicit loopback probe")
    path = parsed.path.rstrip("/")
    if path not in {"", "/v1"}:
        raise EndpointError("endpoint URL path must be empty or /v1")
    netloc = parsed.netloc
    return urlunsplit((parsed.scheme, netloc, "/v1", "", ""))


def build_tls_context(url: str, ca_file: Path | None) -> ssl.SSLContext | None:
    if urlsplit(url).scheme != "https":
        return None
    if ca_file is None:
        raise EndpointError("HTTPS certification requires an explicit private CA file")
    try:
        if _ENDPOINT_IDENTITY is not None:
            if not _ENDPOINT_IDENTITY.ca_pem:
                raise EndpointError("HTTPS profile has no pinned CA bytes")
            pem = _ENDPOINT_IDENTITY.ca_pem
        else:
            pem, _digest = _load_ca_pem(ca_file)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.load_verify_locations(cadata=pem)
        return context
    except (OSError, ssl.SSLError) as exc:
        raise EndpointError("private CA file cannot be loaded") from exc


def validate_profile_headers(headers: Any) -> None:
    """Require one exact gateway profile identity header on every response."""

    identity = _ENDPOINT_IDENTITY
    if identity is None or not identity.ca_pem:
        return
    try:
        profile_ids = headers.get_all("X-Friday-Secondary-Profile-Id") or []
        profile_hashes = headers.get_all("X-Friday-Secondary-Profile-Sha256") or []
    except Exception as exc:
        raise EndpointError("endpoint profile headers are unavailable") from exc
    if profile_ids != [identity.profile_id] or profile_hashes != [identity.profile_sha256]:
        raise EndpointError("endpoint profile headers differ from the candidate epoch")


def request_json(
    method: str,
    url: str,
    *,
    api_key: str,
    payload: dict[str, Any] | None,
    timeout_sec: float,
    ca_file: Path | None = None,
) -> tuple[dict[str, Any], float]:
    if timeout_sec <= 0 or timeout_sec > 600:
        raise EndpointError("timeout is outside the allowed bound")
    body = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "friday-secondary-node-probe/1",
    }
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > 8 * 1024 * 1024:
            raise EndpointError("request exceeds the 8 MiB certification bound")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    started = time.monotonic()
    try:
        # URL scheme, authority, credentials and path were validated above.
        with urllib.request.urlopen(  # noqa: S310  # nosec B310
            request,
            timeout=timeout_sec,
            context=build_tls_context(url, ca_file),
        ) as response:
            status = int(response.status)
            validate_profile_headers(response.headers)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise EndpointError(f"endpoint returned HTTP {int(exc.code)}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EndpointError("endpoint request failed") from exc
    latency = time.monotonic() - started
    if status != 200:
        raise EndpointError(f"endpoint returned HTTP {status}")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise EndpointError("endpoint response exceeds 1 MiB")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EndpointError("endpoint returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise EndpointError("endpoint returned a non-object JSON body")
    return value, latency


def request_text(
    method: str,
    url: str,
    *,
    api_key: str,
    timeout_sec: float,
    ca_file: Path | None = None,
) -> tuple[str, float]:
    """Fetch one bounded UTF-8 observation endpoint without retaining its body."""

    if method != "GET" or timeout_sec <= 0 or timeout_sec > 600:
        raise EndpointError("text request is outside the allowed contract")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/plain",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "friday-secondary-node-probe/1",
        },
        method=method,
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(  # noqa: S310  # nosec B310
            request,
            timeout=timeout_sec,
            context=build_tls_context(url, ca_file),
        ) as response:
            status = int(response.status)
            validate_profile_headers(response.headers)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise EndpointError(f"endpoint returned HTTP {int(exc.code)}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EndpointError("endpoint request failed") from exc
    latency = time.monotonic() - started
    if status != 200:
        raise EndpointError(f"endpoint returned HTTP {status}")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise EndpointError("endpoint response exceeds 1 MiB")
    try:
        return raw.decode("utf-8"), latency
    except UnicodeDecodeError as exc:
        raise EndpointError("endpoint returned non-UTF-8 text") from exc


def runtime_process_epoch(
    base_url: str,
    *,
    api_key: str,
    timeout_sec: float,
    ca_file: Path | None,
) -> str:
    """Return the exact gateway-served SGLang process-start epoch."""

    normalized = normalize_base_url(base_url)
    health_url = f"{normalized.removesuffix('/v1')}/health"
    request_text(
        "GET",
        health_url,
        api_key=api_key,
        timeout_sec=timeout_sec,
        ca_file=ca_file,
    )
    first_body, _latency = request_text(
        "GET",
        f"{normalized}/friday-runtime-epoch",
        api_key=api_key,
        timeout_sec=timeout_sec,
        ca_file=ca_file,
    )
    request_text(
        "GET",
        health_url,
        api_key=api_key,
        timeout_sec=timeout_sec,
        ca_file=ca_file,
    )
    second_body, _latency = request_text(
        "GET",
        f"{normalized}/friday-runtime-epoch",
        api_key=api_key,
        timeout_sec=timeout_sec,
        ca_file=ca_file,
    )
    if first_body != second_body or re.fullmatch(
        r"[1-9][0-9]*(?:\.[0-9]+)?", first_body
    ) is None:
        raise EndpointError("runtime process epoch is missing or ambiguous")
    try:
        value = Decimal(first_body)
    except (InvalidOperation, ValueError):
        raise EndpointError("runtime process epoch is invalid") from None
    if not value.is_finite() or value <= 0:
        raise EndpointError("runtime process epoch is invalid")
    return format(value.normalize(), "f")


def verify_remote_profile_epoch(
    base_url: str,
    *,
    api_key: str,
    timeout_sec: float,
    ca_file: Path,
) -> None:
    """Authenticate the gateway's exact profile bytes before certification traffic."""

    identity = _ENDPOINT_IDENTITY
    normalized = normalize_base_url(base_url)
    if (
        identity is None
        or not identity.ca_pem
        or base_url != identity.endpoint_base_url
        or normalized != identity.endpoint_base_url
        or urlsplit(normalized).scheme != "https"
    ):
        raise EndpointError("HTTPS endpoint identity was not configured")
    ca_pem, ca_sha256 = _load_ca_pem(ca_file, identity.ca_sha256)
    if ca_sha256 != identity.ca_sha256 or ca_pem != identity.ca_pem:
        raise EndpointError("private CA identity changed after profile configuration")
    request = urllib.request.Request(
        f"{normalized}/friday-profile",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "friday-secondary-profile-probe/1",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310  # nosec B310
            request,
            timeout=timeout_sec,
            context=build_tls_context(normalized, ca_file),
        ) as response:
            if int(response.status) != 200:
                raise EndpointError("profile endpoint was rejected")
            validate_profile_headers(response.headers)
            raw = response.read(65_537)
    except EndpointError:
        raise
    except urllib.error.HTTPError as exc:
        raise EndpointError(f"profile endpoint returned HTTP {int(exc.code)}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EndpointError("profile endpoint request failed") from exc
    if len(raw) > 65_536 or raw != identity.profile_bytes:
        raise EndpointError("remote profile bytes differ from the local candidate")


def _bounded_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return count if 0 <= count <= 10_000_000 else 0


def _has_repeated_token_degeneration(content: str) -> bool:
    words = content.casefold().split()
    if len(words) < 20:
        return False
    run = 1
    for index in range(1, len(words)):
        if words[index] == words[index - 1]:
            run += 1
            if run >= 16:
                return True
        else:
            run = 1
    tail = content[-4096:]
    return any(
        len(tail) >= width * 12 and tail[-width:] * 12 == tail[-width * 12 :] for width in range(4, 65)
    )


def parse_completion(
    body: dict[str, Any],
    *,
    expected_model: str | None = None,
    latency_sec: float,
) -> SanitizedCompletion:
    if expected_model is None:
        expected_model = EXPECTED_MODEL
    if body.get("model") != expected_model:
        raise EndpointError("completion returned the wrong served-model alias")
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise EndpointError("completion must contain exactly one choice")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise EndpointError("completion has no message object")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise EndpointError("completion has empty final content")
    if len(content.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise EndpointError("completion content exceeds the response bound")
    lowered = content.casefold()
    if any(marker in lowered for marker in (*_HARMONY_MARKERS, *_REASONING_MARKERS)):
        raise EndpointError("reasoning or control marker leaked into final content")
    if _NUMERICAL_FAILURE.search(content):
        raise EndpointError("numerical failure marker leaked into final content")
    if _has_repeated_token_degeneration(content):
        raise EndpointError("repeated-token degeneration detected")
    usage_value = body.get("usage")
    usage: dict[str, Any] = usage_value if isinstance(usage_value, dict) else {}
    prompt_tokens = _bounded_count(usage.get("prompt_tokens"))
    completion_tokens = _bounded_count(usage.get("completion_tokens"))
    finish_reason_value = choice.get("finish_reason")
    finish_reason = (
        finish_reason_value if finish_reason_value in {"stop", "length", "content_filter"} else "other"
    )
    reasoning = message.get("reasoning_content")
    reasoning_present = isinstance(reasoning, str) and bool(reasoning)
    if not math.isfinite(latency_sec) or latency_sec < 0:
        raise EndpointError("completion latency is invalid")
    return SanitizedCompletion(
        content=content,
        latency_sec=latency_sec,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=finish_reason,
        reasoning_present=reasoning_present,
    )


def chat_completion(
    base_url: str,
    *,
    api_key: str,
    messages: list[dict[str, str]],
    timeout_sec: float,
    max_tokens: int,
    temperature: float = 1.0,
    extra: dict[str, Any] | None = None,
    ca_file: Path | None = None,
) -> SanitizedCompletion:
    if not 1 <= max_tokens <= 4096:
        raise EndpointError("max_tokens is outside the certification bound")
    if temperature != 1.0:
        raise EndpointError("GPT-OSS sampling parameters are fixed for certification")
    payload: dict[str, Any] = {
        "model": EXPECTED_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "reasoning_effort": "low",
        "temperature": 1.0,
        "top_p": 1.0,
        "seed": 0,
    }
    if extra:
        requested_effort = extra.get("reasoning_effort", payload["reasoning_effort"])
        if requested_effort not in _REASONING_EFFORTS:
            raise EndpointError("GPT-OSS reasoning effort is outside the certification set")
        payload["reasoning_effort"] = requested_effort
        for key in ("temperature", "top_p", "seed"):
            if key in extra and extra[key] != payload[key]:
                raise EndpointError("GPT-OSS sampling parameters cannot be overridden")
        payload.update({key: value for key, value in extra.items() if key not in payload})
    body, latency = request_json(
        "POST",
        f"{normalize_base_url(base_url)}/chat/completions",
        api_key=api_key,
        payload=payload,
        timeout_sec=timeout_sec,
        ca_file=ca_file,
    )
    return parse_completion(body, latency_sec=latency)


def stream_chat_completion(
    base_url: str,
    *,
    api_key: str,
    messages: list[dict[str, str]],
    timeout_sec: float,
    max_tokens: int,
    ca_file: Path | None = None,
) -> StreamedCompletion:
    if not 1 <= max_tokens <= 4096:
        raise EndpointError("max_tokens is outside the certification bound")
    payload = {
        "model": EXPECTED_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "reasoning_effort": "low",
        "temperature": 1.0,
        "top_p": 1.0,
        "seed": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 8 * 1024 * 1024:
        raise EndpointError("request exceeds the 8 MiB certification bound")
    request = urllib.request.Request(
        f"{normalize_base_url(base_url)}/chat/completions",
        data=encoded,
        headers={
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "friday-secondary-capacity-probe/1",
        },
        method="POST",
    )
    started = time.monotonic()
    first_token_at: float | None = None
    total_bytes = 0
    content_parts: list[str] = []
    reasoning_present = False
    usage: dict[str, Any] = {}
    finish_reason = "stop"
    done = False
    try:
        # URL scheme, authority, credentials and path were validated above.
        with urllib.request.urlopen(  # noqa: S310  # nosec B310
            request,
            timeout=timeout_sec,
            context=build_tls_context(normalize_base_url(base_url), ca_file),
        ) as response:
            if int(response.status) != 200:
                raise EndpointError(f"endpoint returned HTTP {int(response.status)}")
            validate_profile_headers(response.headers)
            for raw_line in response:
                total_bytes += len(raw_line)
                if total_bytes > MAX_RESPONSE_BYTES:
                    raise EndpointError("streaming response exceeds 1 MiB")
                try:
                    line = raw_line.decode("utf-8").strip()
                except UnicodeDecodeError as exc:
                    raise EndpointError("streaming response is not UTF-8") from exc
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    raise EndpointError("streaming response contains an invalid SSE event")
                event_text = line[5:].strip()
                if event_text == "[DONE]":
                    done = True
                    break
                try:
                    event = json.loads(event_text)
                except json.JSONDecodeError as exc:
                    raise EndpointError("streaming response contains malformed JSON") from exc
                if not isinstance(event, dict):
                    raise EndpointError("streaming response contains a non-object event")
                if "error" in event:
                    raise EndpointError("streaming endpoint returned an error event")
                if event.get("model") != EXPECTED_MODEL:
                    raise EndpointError("streaming response returned the wrong model alias")
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                choices = event.get("choices")
                if choices == []:
                    continue
                if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                    raise EndpointError("streaming response has an invalid choice collection")
                choice = choices[0]
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    raise EndpointError("streaming response has no delta object")
                piece = delta.get("content")
                if piece is not None and not isinstance(piece, str):
                    raise EndpointError("streaming content delta is invalid")
                if piece:
                    if first_token_at is None:
                        first_token_at = time.monotonic()
                    content_parts.append(piece)
                reasoning = delta.get("reasoning_content")
                if reasoning is not None and not isinstance(reasoning, str):
                    raise EndpointError("streaming reasoning delta is invalid")
                if reasoning:
                    reasoning_present = True
                    if first_token_at is None:
                        first_token_at = time.monotonic()
                if delta.get("tool_calls"):
                    raise EndpointError("capacity probe unexpectedly produced a tool call")
                if choice.get("finish_reason") is not None:
                    finish_reason = str(choice["finish_reason"])
    except urllib.error.HTTPError as exc:
        raise EndpointError(f"endpoint returned HTTP {int(exc.code)}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EndpointError("streaming endpoint request failed") from exc
    if not done or first_token_at is None:
        raise EndpointError("streaming response ended without a complete token stream")
    elapsed = time.monotonic() - started
    synthetic = {
        "model": EXPECTED_MODEL,
        "choices": [
            {
                "message": {
                    "content": "".join(content_parts),
                    "reasoning_content": "present" if reasoning_present else None,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }
    completion = parse_completion(synthetic, latency_sec=elapsed)
    return StreamedCompletion(completion=completion, ttft_sec=first_token_at - started)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temp_name)


def write_new_json(path: Path, value: dict[str, Any]) -> None:
    """Create one final evidence file without ever replacing an existing target."""
    parent = path.absolute().parent
    if not parent.is_dir() or path.exists() or path.is_symlink():
        raise EndpointError("final evidence output path is not new")
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
            stat.S_IRUSR | stat.S_IWUSR,
        )
        view = memoryview(encoded)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("short write")
            written += count
        os.fsync(descriptor)
    except OSError as exc:
        raise EndpointError("final evidence output could not be created") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
