"""Closed, content-safe contracts for Friday's optional advisory model."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
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


@dataclass(frozen=True, slots=True)
class SecondaryEndpointConfig:
    base_url: str = field(repr=False)
    served_model_alias: str
    api_key: str = field(repr=False)
    ca_file: str = field(default="", repr=False)
    connect_timeout_sec: float = 1.0
    read_timeout_sec: float = 12.0
    call_budget_sec: float = 15.0
    admission_timeout_sec: float = 0.10
    health_interval_sec: float = 30.0
    cooldown_sec: float = 60.0
    max_context_tokens: int = 0
    max_concurrency: int = 1

    @property
    def is_complete(self) -> bool:
        try:
            parsed = urlsplit(self.base_url)
            hostname = parsed.hostname
        except ValueError:
            return False
        valid_url = bool(
            parsed.scheme in {"http", "https"}
            and hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and parsed.path.rstrip("/") == "/v1"
        )
        return bool(
            valid_url
            and self.served_model_alias
            and self.api_key
            and self.max_context_tokens > 0
            and self.max_concurrency > 0
            and self.connect_timeout_sec > 0.0
            and self.read_timeout_sec > 0.0
            and self.call_budget_sec > 0.0
            and self.admission_timeout_sec > 0.0
            and self.health_interval_sec > 0.0
            and self.cooldown_sec >= 0.0
        )

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


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
