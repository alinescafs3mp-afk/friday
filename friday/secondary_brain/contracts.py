"""Closed, content-safe contracts for Friday's optional advisory model."""

from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeAlias
from urllib.parse import urlsplit

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class SecondaryMode(StrEnum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    ASSIST = "assist"


class ModelWorkload(StrEnum):
    DIALOGUE = "dialogue"
    FINAL_SYNTHESIS = "final_synthesis"
    TOOL_CONTROL = "tool_control"
    EFFECT_PLANNING = "effect_planning"
    VISION = "vision"
    CLASSIFY = "classify"
    EXTRACT = "extract"
    QUERY_REWRITE = "query_rewrite"
    SUMMARIZE = "summarize"
    DOCUMENT_MAP = "document_map"
    CRITIQUE = "critique"
    VERIFY = "verify"
    PLAN_CANDIDATE = "plan_candidate"


ADVISORY_WORKLOADS = frozenset(
    {
        ModelWorkload.CLASSIFY,
        ModelWorkload.EXTRACT,
        ModelWorkload.QUERY_REWRITE,
        ModelWorkload.SUMMARIZE,
        ModelWorkload.DOCUMENT_MAP,
        ModelWorkload.CRITIQUE,
        ModelWorkload.VERIFY,
        ModelWorkload.PLAN_CANDIDATE,
    }
)


class ModelPriority(StrEnum):
    FOREGROUND = "foreground"
    BACKGROUND = "background"


class EffectClass(StrEnum):
    NONE = "none"
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    HIGH_IMPACT = "high_impact"


class ModelModality(StrEnum):
    TEXT = "text"
    IMAGE = "image"


class SecondaryState(StrEnum):
    DISABLED = "disabled"
    MISCONFIGURED = "misconfigured"
    PROBING = "probing"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    COOLDOWN = "cooldown"


class SecondaryFailure(StrEnum):
    DISABLED = "disabled"
    MISCONFIGURED = "misconfigured"
    MODE_DISALLOWED = "mode_disallowed"
    WORKLOAD_DISALLOWED = "workload_disallowed"
    PRIVATE_TEXT_DISALLOWED = "private_text_disallowed"
    SECRET_MATERIAL_DENIED = "secret_material_denied"
    UNSUPPORTED_MODALITY = "unsupported_modality"
    EFFECT_DENIED = "effect_denied"
    CONTEXT_EXCEEDED = "context_exceeded"
    ADMISSION_BUSY = "admission_busy"
    COOLDOWN = "cooldown"
    DEADLINE = "deadline"
    CONNECT_FAILED = "connect_failed"
    TIMEOUT = "timeout"
    HTTP_TRANSIENT = "http_transient"
    HTTP_REJECTED = "http_rejected"
    AUTH_REJECTED = "auth_rejected"
    WRONG_PROFILE = "wrong_profile"
    WRONG_MODEL = "wrong_model"
    MALFORMED_RESPONSE = "malformed_response"
    TOOL_CALL_REJECTED = "tool_call_rejected"
    REASONING_LEAK = "reasoning_leak"
    DEGENERATION = "degeneration"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One bounded, read-only candidate call.

    Message bodies are deliberately excluded from repr/equality so an exception,
    trace or assertion cannot accidentally persist a prompt.
    """

    workload: ModelWorkload
    messages: tuple[Mapping[str, Any], ...] = field(repr=False, compare=False)
    max_output_tokens: int
    absolute_deadline_monotonic: float
    priority: ModelPriority = ModelPriority.BACKGROUND
    effect_class: EffectClass = EffectClass.NONE
    modality: ModelModality = ModelModality.TEXT
    require_structured_output: bool = False
    require_independent_model: bool = False
    contains_private_text: bool = False

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("secondary request needs at least one message")
        if self.max_output_tokens < 1:
            raise ValueError("secondary request max_output_tokens must be positive")
        if not math.isfinite(self.absolute_deadline_monotonic):
            raise ValueError("secondary request deadline must be finite")


@dataclass(frozen=True, slots=True)
class ModelUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class SecondaryResult:
    """Sanitized advisory output; never contains raw reasoning or tool calls."""

    visible_content: str = field(repr=False)
    structured_output: JsonValue = field(default=None, repr=False)
    served_model_alias: str = ""
    usage: ModelUsage = ModelUsage(0, 0, 0)
    latency_sec: float = 0.0
    reasoning_was_separated: bool = False
    endpoint_role: str = "secondary"


@dataclass(frozen=True, slots=True)
class SecondaryAttempt:
    result: SecondaryResult | None = field(default=None, repr=False)
    failure: SecondaryFailure | None = None

    @property
    def succeeded(self) -> bool:
        return self.result is not None and self.failure is None

    @classmethod
    def success(cls, result: SecondaryResult) -> SecondaryAttempt:
        return cls(result=result)

    @classmethod
    def rejected(cls, failure: SecondaryFailure) -> SecondaryAttempt:
        return cls(failure=failure)


def _load_pinned_ca_pem(path_value: str, expected_sha256: str) -> str:
    """Load one exact, bounded CA file without following a path-level symlink."""

    if not path_value or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        return ""
    path = Path(path_value)
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= 65_536:
            return ""
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != metadata.st_size:
            return ""
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(65_537)
            after = os.fstat(stream.fileno())
        if len(raw) > 65_536 or hashlib.sha256(raw).hexdigest() != expected_sha256:
            return ""
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            return ""
        pem = raw.decode("ascii", errors="strict")
        if "-----BEGIN CERTIFICATE-----" not in pem or "-----END CERTIFICATE-----" not in pem:
            return ""
        return pem
    except (OSError, UnicodeError):
        return ""
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class SecondaryEndpointConfig:
    base_url: str = field(repr=False)
    served_model_alias: str
    api_key: str = field(repr=False)
    ca_file: str = field(default="", repr=False)
    ca_sha256: str = field(default="", repr=False)
    connect_timeout_sec: float = 1.0
    read_timeout_sec: float = 12.0
    call_budget_sec: float = 15.0
    admission_timeout_sec: float = 0.10
    health_interval_sec: float = 30.0
    cooldown_sec: float = 60.0
    max_context_tokens: int = 0
    max_concurrency: int = 1
    max_output_tokens: int = 0
    profile_id: str = ""
    profile_manifest_sha256: str = ""

    @property
    def is_complete(self) -> bool:
        try:
            parsed = urlsplit(self.base_url)
            hostname = parsed.hostname
        except ValueError:
            return False
        loopback_host = hostname == "localhost"
        private_ip = False
        try:
            address = ipaddress.ip_address(hostname or "")
        except ValueError:
            pass
        else:
            loopback_host = address.is_loopback
            private_ip = address.is_private and not address.is_loopback
        transport_is_safe = bool(parsed.scheme == "https" or (parsed.scheme == "http" and loopback_host))
        # The accepted laptop contour uses a private IP and a private CA.  Make
        # both explicit so a typo cannot silently send bearer credentials and
        # personal text over plain LAN HTTP or the ambient trust store.
        private_ip_trust_is_explicit = not private_ip or bool(self.ca_file and self.ca_sha256)
        profile_is_explicit = bool(
            self.profile_id and re.fullmatch(r"[0-9a-f]{64}", self.profile_manifest_sha256)
        )
        ca_is_available = bool(
            (parsed.scheme == "http" and loopback_host and not self.ca_file and not self.ca_sha256)
            or (parsed.scheme == "https" and _load_pinned_ca_pem(self.ca_file, self.ca_sha256))
        )
        host_is_local = loopback_host or private_ip
        valid_url = bool(
            parsed.scheme in {"http", "https"}
            and hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and parsed.path.rstrip("/") == "/v1"
            and transport_is_safe
            and private_ip_trust_is_explicit
            and profile_is_explicit
            and ca_is_available
            and host_is_local
        )
        return bool(
            valid_url
            and self.served_model_alias
            and re.fullmatch(r"[0-9a-f]{64}", self.api_key)
            and self.max_context_tokens > 0
            and 0 < self.max_output_tokens <= self.max_context_tokens
            and self.max_concurrency == 1
            and self.connect_timeout_sec > 0.0
            and self.connect_timeout_sec <= self.read_timeout_sec
            and self.read_timeout_sec <= self.call_budget_sec <= 30.0
            and self.admission_timeout_sec > 0.0
            and self.admission_timeout_sec <= 0.25
            and self.health_interval_sec > 0.0
            and self.cooldown_sec > 0.0
        )

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/models"

    @property
    def profile_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/friday-profile"


def _endpoint_origin(value: str) -> tuple[str, int] | None:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not hostname:
        return None
    normalized_host = hostname.casefold()
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        if normalized_host == "localhost":
            normalized_host = "<loopback>"
    else:
        normalized_host = "<loopback>" if address.is_loopback else address.compressed
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    return normalized_host, effective_port


def secondary_configuration_is_admissible(
    endpoint: SecondaryEndpointConfig,
    *,
    primary_base_url: str,
    primary_model: str,
    primary_timeout_sec: float,
    workload_names: Iterable[str],
    mode: str,
) -> bool:
    """One pure completeness/independence predicate for every projection."""

    allowed = {workload.value for workload in ADVISORY_WORKLOADS}
    workloads = {str(value).strip().casefold() for value in workload_names}
    from .profiles import get_secondary_runtime_profile

    profile = get_secondary_runtime_profile(endpoint.profile_id)
    profile_matches = bool(
        profile is not None
        and profile.endpoint_base_url.rstrip("/") == endpoint.base_url.rstrip("/")
        and profile.served_model_alias == endpoint.served_model_alias
        and profile.manifest_sha256 == endpoint.profile_manifest_sha256
        and profile.gateway_ca_certificate_sha256 == endpoint.ca_sha256
        and profile.max_context_tokens == endpoint.max_context_tokens
        and profile.max_concurrency == endpoint.max_concurrency
        and profile.max_output_tokens == endpoint.max_output_tokens
        and mode in profile.allowed_modes
        and workloads
        and workloads <= profile.allowed_workloads
    )
    return bool(
        endpoint.is_complete
        and math.isfinite(primary_timeout_sec)
        and endpoint.call_budget_sec < primary_timeout_sec
        and _endpoint_origin(endpoint.base_url) != _endpoint_origin(primary_base_url)
        and endpoint.served_model_alias != primary_model
        and workloads & allowed
        and profile_matches
    )


@dataclass(frozen=True, slots=True)
class SecondaryStatus:
    state: SecondaryState
    last_failure: SecondaryFailure | None
    selected_total: int
    success_total: int
    skipped_total: int
    fallback_total: int
    active_requests: int
    context_cap_tokens: int
    served_model_match: bool
    last_success_age_sec: float | None = None
    cooldown_retry_after_sec: float = 0.0
    probe_success_total: int = 0
    probe_failure_total: int = 0
    profile_manifest_match: bool = False
