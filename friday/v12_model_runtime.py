"""Sealed composition boundary for the V12 model probe and canary runtime.

This module deliberately contains no HTTP implementation.  The production
completion path still needs the served ``model`` field preserved by
``LLMRouter`` and the metrics path needs a bounded endpoint transport.  Those
two narrow seams are injected here, are bound to one exact router instance, and
are exercised without network access in the unit tests.

Configuration is not an attestation.  A runtime becomes usable only after the
code-owned live probe has passed, its backend-specific process epoch is still
current, and the process-local gate has issued an exact least-privilege lease.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol, TypeVar
from urllib.parse import SplitResult, urlsplit, urlunsplit

from friday.agent_runtime.llm import LLMRouter
from friday.config import PROFILES, FridaySettings, RuntimeProfile
from friday.model_input_hygiene import model_messages_are_secret_free
from friday.model_probe import (
    CANCELLATION_TIMEOUT_SEC,
    CONTEXT_OUTPUT_RESERVE_TOKENS,
    CONTEXT_SAFETY_RESERVE_TOKENS,
    LOAD_TIMEOUT_SEC,
    MAX_COMPLETION_CHARS,
    CancellationProbeRequest,
    CancellationProbeResult,
    ContextProbeRequest,
    ModelLoadSample,
    ModelProbeError,
    ModelProbeFailure,
    PlanProbeCase,
    ProbeCompletion,
    SynthesisProbeRequest,
    V12ModelProbeClient,
    VerifierProbeRequest,
    _cancellation_request_witness_sha256,
    run_v12_live_probe,
)
from friday.model_profiles import (
    QWEN36_27B_V12_PROFILE,
    QWEN38_27B_SGLANG_V12_PROFILE,
    ModelGateReason,
    ModelProfileLease,
    ModelRequirements,
    V12LiveAttestation,
    V12ModelGate,
    V12ModelProfileSpec,
    v12_model_profile_for,
)
from friday.orchestration.planner import V12Planner

MAX_VLLM_METRICS_BYTES = 65_536
MAX_SGLANG_METRICS_BYTES = 131_072
MAX_METRICS_BYTES = MAX_SGLANG_METRICS_BYTES
MAX_MODEL_INVENTORY_BYTES = 65_536
MAX_SERVER_INFO_BYTES = 65_536
MAX_DEPLOYMENT_WITNESS_BYTES = 8_192
MAX_METRICS_LINE_CHARS = 4_096
MAX_METRIC_COUNT = 1_000_000
_MAX_INSTALLATION_CONTEXT_TOKENS = (1 << 63) - 1
CANCELLATION_POLL_INTERVAL_SEC = 0.01
CANCELLATION_STABLE_ZERO_OBSERVATIONS = 2
CANCELLATION_STABLE_ZERO_INTERVAL_SEC = 0.05
LOCAL_CANCELLATION_DRAIN_SEC = 0.05
MAX_ATTESTED_CHAT_INPUT_UTF8_BYTES = 5_500
BASE_ATTESTED_CHAT_CONTEXT_TOKENS = 8_192
MAX_MEASURED_CHAT_CONTEXT_TOKENS = 40_960

_PROCESS_PRIVATE_SALT = secrets.token_bytes(32)
_PROCESS_SALT_PID = os.getpid()
_T = TypeVar("_T")

_METRIC_SAMPLE_RE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)"
    r"(?:\{(?P<labels>[^{}\r\n]{0,1024})\})?\s+"
    r"(?P<value>[^\s]+)(?:\s+[0-9]+)?\s*$"
)
_VLLM_REQUIRED_METRICS = frozenset(
    {
        "process_start_time_seconds",
        "vllm:num_requests_running",
        "vllm:num_requests_waiting",
    }
)
_SGLANG_REQUIRED_METRICS = frozenset(
    {
        "sglang:num_running_reqs",
        "sglang:num_queue_reqs",
    }
)
_LOWER_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_DEPLOYMENT_WITNESS_SCHEMA = "friday.sglang-deployment-witness.v1"


class _MetricsAdapter(StrEnum):
    VLLM = "vllm"
    SGLANG_QWEN38 = "sglang_qwen38"


@dataclass(frozen=True, slots=True)
class _SglangServerInfoProjection:
    random_seed: int


@dataclass(frozen=True, slots=True)
class _DeploymentWitnessProjection:
    engine_random_seed: int
    canonical_json: bytes = field(repr=False)
    process_epoch_sha256: str


class V12ModelRuntimeFailure(StrEnum):
    """Content-free failure vocabulary for the sealed runtime boundary."""

    COMPOSITION_REJECTED = "composition_rejected"
    SETTINGS_REJECTED = "settings_rejected"
    DEADLINE_EXHAUSTED = "deadline_exhausted"
    METRICS_CALL_FAILED = "metrics_call_failed"
    METRICS_INVALID = "metrics_invalid"
    SERVED_ALIAS_REJECTED = "served_alias_rejected"
    COMPLETION_INVALID = "completion_invalid"
    LIVE_PROBE_REJECTED = "live_probe_rejected"
    LEASE_REJECTED = "lease_rejected"
    EPOCH_CHANGED = "epoch_changed"
    MODEL_CALL_FAILED = "model_call_failed"


class V12ModelRuntimeError(RuntimeError):
    """A sanitized exception which never retains transport/model content."""

    def __init__(self, code: V12ModelRuntimeFailure) -> None:
        self.code = code
        super().__init__(code.value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code.value!r})"


class V12ServedAliasError(V12ModelRuntimeError):
    """Typed, content-free transport signal for served-model identity drift."""

    def __init__(self) -> None:
        super().__init__(V12ModelRuntimeFailure.SERVED_ALIAS_REJECTED)


@dataclass(frozen=True, slots=True)
class V12ServedCompletion:
    """Bounded projection which preserves the server-reported model alias."""

    content: str = field(repr=False)
    finish_reason: str = field(repr=False)
    tool_calls: tuple[str, ...] = field(repr=False)
    prompt_tokens: int
    served_model_alias: str = field(repr=False)


class V12PendingCompletion(Protocol):
    """A locally-owned submitted request used only by the cancellation probe."""

    @property
    def bound_router(self) -> LLMRouter: ...

    @property
    def submitted_model_alias(self) -> str: ...

    def is_pending(self) -> bool: ...

    def submission_started(self) -> bool: ...

    async def cancel_and_drain(self, *, absolute_deadline: float) -> bool: ...


class V12CompletionTransport(Protocol):
    """Narrow blocker pending a served-model projection in ``LLMRouter``.

    A production implementation must delegate to ``bound_router``.  Returning a
    completion through some second client is rejected composition: it would no
    longer share the router semaphore, breaker, authentication or endpoint.
    """

    @property
    def bound_router(self) -> LLMRouter: ...

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None,
        max_tokens: int | None,
        priority: str,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        reject_repeated_token_degeneration: bool,
        allow_retries: bool,
        absolute_deadline: float,
        open_silent_cooldown: bool,
        require_full_context: bool,
    ) -> V12ServedCompletion: ...

    async def start_cancellable(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None,
        max_tokens: int | None,
        priority: str,
        absolute_deadline: float,
        require_full_context: bool,
    ) -> V12PendingCompletion: ...


class V12MetricsTransport(Protocol):
    """Bounded observation seam; every target derives from the exact router."""

    @property
    def bound_router(self) -> LLMRouter: ...

    async def fetch_metrics(
        self,
        *,
        maximum_bytes: int,
        absolute_deadline: float,
    ) -> bytes: ...

    async def fetch_model_inventory(
        self,
        *,
        maximum_bytes: int,
        absolute_deadline: float,
    ) -> bytes: ...

    async def fetch_server_info(
        self,
        *,
        maximum_bytes: int,
        absolute_deadline: float,
    ) -> bytes: ...

    async def fetch_deployment_witness(
        self,
        *,
        maximum_bytes: int,
        absolute_deadline: float,
    ) -> bytes: ...


def _runtime_error(code: V12ModelRuntimeFailure) -> V12ModelRuntimeError:
    return V12ModelRuntimeError(code)


def _deadline_remaining(absolute_deadline: float) -> float:
    remaining = absolute_deadline - time.monotonic()
    if not math.isfinite(absolute_deadline) or remaining <= 0.0:
        raise _runtime_error(V12ModelRuntimeFailure.DEADLINE_EXHAUSTED)
    return remaining


def _consume_abandoned(task: asyncio.Future[Any]) -> None:
    if task.cancelled():
        return
    with suppress(asyncio.CancelledError, Exception):
        task.exception()


async def _cancel_and_boundedly_drain(task: asyncio.Future[Any]) -> None:
    task.cancel()
    done, _ = await asyncio.wait((task,), timeout=LOCAL_CANCELLATION_DRAIN_SEC)
    if not done:
        task.add_done_callback(_consume_abandoned)


async def _bounded_await(
    operation: Awaitable[_T],
    *,
    absolute_deadline: float,
    failure: V12ModelRuntimeFailure,
) -> _T:
    timeout = _deadline_remaining(absolute_deadline)
    try:
        task = asyncio.ensure_future(operation)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise _runtime_error(failure) from None
    try:
        done, _ = await asyncio.wait((task,), timeout=timeout)
    except asyncio.CancelledError:
        await _cancel_and_boundedly_drain(task)
        raise
    if not done:
        await _cancel_and_boundedly_drain(task)
        raise _runtime_error(V12ModelRuntimeFailure.DEADLINE_EXHAUSTED) from None
    try:
        return task.result()
    except asyncio.CancelledError:
        raise
    except V12ModelRuntimeError:
        raise
    except Exception:
        raise _runtime_error(failure) from None


def _decimal_text(value: float | int) -> str:
    if isinstance(value, bool):
        raise _runtime_error(V12ModelRuntimeFailure.SETTINGS_REJECTED)
    try:
        decimal = Decimal(str(value))
    except InvalidOperation:
        raise _runtime_error(V12ModelRuntimeFailure.SETTINGS_REJECTED) from None
    if not decimal.is_finite():
        raise _runtime_error(V12ModelRuntimeFailure.SETTINGS_REJECTED)
    return str(decimal.normalize())


def _normalize_manifest(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return {"decimal": _decimal_text(value)}
    if isinstance(value, Mapping):
        return {str(key): _normalize_manifest(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_manifest(item) for item in value]
    raise _runtime_error(V12ModelRuntimeFailure.SETTINGS_REJECTED)


def _normalized_base_url(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _runtime_error(V12ModelRuntimeFailure.SETTINGS_REJECTED)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise _runtime_error(V12ModelRuntimeFailure.SETTINGS_REJECTED) from None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise _runtime_error(V12ModelRuntimeFailure.SETTINGS_REJECTED)
    try:
        normalized_host = host.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        raise _runtime_error(V12ModelRuntimeFailure.SETTINGS_REJECTED) from None
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    default_port = 80 if parsed.scheme.casefold() == "http" else 443
    netloc = normalized_host if port in (None, default_port) else f"{normalized_host}:{port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(SplitResult(parsed.scheme.casefold(), netloc, path, "", ""))


def _profile_manifest(profile: V12ModelProfileSpec) -> Mapping[str, Any]:
    return {
        "allowed_capabilities": sorted(item.value for item in profile.allowed_capabilities),
        "allowed_effects": sorted(item.value for item in profile.allowed_effects),
        "max_context_tokens": profile.max_context_tokens,
        "max_prepared_evidence_items": profile.max_prepared_evidence_items,
        "max_tool_calls": profile.max_tool_calls,
        "max_tool_rounds": profile.max_tool_rounds,
        "max_tool_steps": profile.max_tool_steps,
        "minimum_context_tokens": profile.minimum_context_tokens,
        "planner_contract_sha256": profile.planner_contract_sha256,
        "probe_suite_sha256": profile.probe_suite_sha256,
        "profile_id": profile.profile_id,
        "required_capabilities": sorted(item.value for item in profile.required_capabilities),
        "runtime_profile_name": profile.runtime_profile_name,
        "served_model_alias": profile.served_model_alias,
        "verifier_required": profile.verifier_required,
    }


def _effective_process_salt() -> bytes:
    # A fork inherits module memory.  Mixing in the current PID prevents the
    # child from retaining the parent's endpoint binding even before its first
    # independently sampled vLLM epoch.
    return hashlib.sha256(
        _PROCESS_PRIVATE_SALT
        + b"\0"
        + str(_PROCESS_SALT_PID).encode("ascii")
        + b"\0"
        + str(os.getpid()).encode("ascii")
    ).digest()


def _derive_endpoint_binding(
    router: LLMRouter,
    profile: V12ModelProfileSpec,
) -> str:
    settings = router.settings
    if type(settings) is not FridaySettings:
        raise _runtime_error(V12ModelRuntimeFailure.SETTINGS_REJECTED)
    salt = _effective_process_salt()
    api_key = settings.llm_api_key
    if not isinstance(api_key, str):
        raise _runtime_error(V12ModelRuntimeFailure.SETTINGS_REJECTED)
    auth_binding = hashlib.sha256(salt + b"\0auth\0" + api_key.encode("utf-8")).hexdigest()
    manifest = {
        "schema": "friday.v12-endpoint-binding.v1",
        "router": {
            "base_url": _normalized_base_url(settings.llm_base_url),
            "enabled": settings.llm_enabled,
            "foreground_slots": settings.llm_foreground_slots,
            "max_tokens": settings.llm_max_tokens,
            "model": settings.llm_model,
            "timeout_sec": {"decimal": _decimal_text(settings.llm_timeout_sec)},
            "type": "friday.agent_runtime.llm.LLMRouter",
        },
        "auth": {"configured": bool(api_key), "private_binding_sha256": auth_binding},
        "runtime_profile": _normalize_manifest(asdict(settings.profile)),
        "v12_profile": _profile_manifest(profile),
    }
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(salt + b"\0endpoint\0" + encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class _RuntimeSeal:
    router: LLMRouter = field(repr=False)
    profile: V12ModelProfileSpec = field(repr=False)
    endpoint_binding_sha256: str = field(repr=False)

    def validate(self) -> None:
        if (
            type(self.router) is not LLMRouter
            or v12_model_profile_for(
                self.profile.runtime_profile_name,
                self.profile.served_model_alias,
            )
            is not self.profile
            or self.router.settings.profile is not PROFILES.get(self.profile.runtime_profile_name)
            or self.router.model != self.profile.served_model_alias
            or self.router.enabled is not True
            or _installation_context_tokens(self.router, self.profile) < self.profile.minimum_context_tokens
            or _derive_endpoint_binding(self.router, self.profile) != self.endpoint_binding_sha256
        ):
            raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED)


def _transport_router(value: object) -> object:
    try:
        return value.bound_router  # type: ignore[attr-defined]
    except Exception:
        raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED) from None


def _parse_labels(value: str | None, *, served_model_alias: str) -> None:
    if value is None or value == "":
        return
    expected_model = f'model_name="{served_model_alias}"'
    if value not in {
        expected_model,
        f'engine="0",{expected_model}',
        f'{expected_model},engine="0"',
    }:
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)


def _parse_decimal_metric(value: str, *, positive: bool, integral: bool) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID) from None
    if not number.is_finite() or (number <= 0 if positive else number < 0):
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
    if integral and (number != number.to_integral_value() or number > MAX_METRIC_COUNT):
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
    return number


def _metrics_adapter_for(profile: V12ModelProfileSpec) -> _MetricsAdapter:
    runtime_profile = PROFILES.get(profile.runtime_profile_name)
    if (
        v12_model_profile_for(profile.runtime_profile_name, profile.served_model_alias) is not profile
        or runtime_profile is None
    ):
        raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED)
    if runtime_profile.inference_backend == "vllm":
        return _MetricsAdapter.VLLM
    if (
        runtime_profile.inference_backend == "sglang"
        and profile is QWEN38_27B_SGLANG_V12_PROFILE
        and runtime_profile.sglang_extra_args is not None
    ):
        return _MetricsAdapter.SGLANG_QWEN38
    raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED)


def _installation_context_tokens(
    router: LLMRouter,
    profile: V12ModelProfileSpec,
) -> int:
    """Return the strictest code-owned launch cap for the exact endpoint."""

    runtime_profile = router.settings.profile
    caps: list[object] = [runtime_profile.max_model_len]
    if _metrics_adapter_for(profile) is _MetricsAdapter.SGLANG_QWEN38:
        launch = runtime_profile.sglang_extra_args
        if launch is None:
            raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED)
        caps.append(launch.max_total_tokens)
    validated: list[int] = []
    for value in caps:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            or value > _MAX_INSTALLATION_CONTEXT_TOKENS
        ):
            raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED)
        validated.append(value)
    return min(validated)


def _parse_metrics(body: object, *, served_model_alias: str) -> ModelLoadSample:
    """Parse the original exact vLLM metric contract."""

    if type(body) is not bytes or not body or len(body) > MAX_VLLM_METRICS_BYTES:
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeError:
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID) from None
    observed: dict[str, tuple[str | None, str]] = {}
    for raw_line in text.splitlines():
        if len(raw_line) > MAX_METRICS_LINE_CHARS:
            raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _METRIC_SAMPLE_RE.fullmatch(line)
        if match is None:
            if any(line.startswith(name) for name in _VLLM_REQUIRED_METRICS):
                raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
            continue
        name = match.group("name")
        if name not in _VLLM_REQUIRED_METRICS:
            continue
        if name in observed:
            raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
        observed[name] = (match.group("labels"), match.group("value"))
    if set(observed) != _VLLM_REQUIRED_METRICS:
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)

    epoch_labels, epoch_value = observed["process_start_time_seconds"]
    if epoch_labels not in (None, ""):
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
    epoch = _parse_decimal_metric(epoch_value, positive=True, integral=False)

    load_values: dict[str, float] = {}
    for metric_name, field_name in (
        ("vllm:num_requests_running", "running"),
        ("vllm:num_requests_waiting", "waiting"),
    ):
        labels, raw_value = observed[metric_name]
        _parse_labels(labels, served_model_alias=served_model_alias)
        count = _parse_decimal_metric(raw_value, positive=False, integral=True)
        load_values[field_name] = float(int(count))

    normalized_epoch = str(epoch.normalize()).encode("ascii")
    return ModelLoadSample(
        running=load_values["running"],
        waiting=load_values["waiting"],
        process_epoch_sha256=hashlib.sha256(normalized_epoch).hexdigest(),
    )


def _parse_sglang_metrics_unsafe(
    body: object,
    *,
    served_model_alias: str,
) -> tuple[float, float]:
    if type(body) is not bytes or not body or len(body) > MAX_SGLANG_METRICS_BYTES:
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeError:
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID) from None

    expected_labels = (
        f'engine_type="unified",model_name="{served_model_alias}",moe_ep_rank="0",pp_rank="0",tp_rank="0"'
    )
    observed: dict[str, str] = {}
    for raw_line in text.splitlines():
        if len(raw_line) > MAX_METRICS_LINE_CHARS:
            raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _METRIC_SAMPLE_RE.fullmatch(line)
        if match is None:
            if any(line.startswith(name) for name in _SGLANG_REQUIRED_METRICS):
                raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
            continue
        name = match.group("name")
        if name not in _SGLANG_REQUIRED_METRICS:
            continue
        if name in observed or match.group("labels") != expected_labels:
            raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
        observed[name] = match.group("value")
    if set(observed) != _SGLANG_REQUIRED_METRICS:
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)

    running = _parse_decimal_metric(
        observed["sglang:num_running_reqs"],
        positive=False,
        integral=True,
    )
    waiting = _parse_decimal_metric(
        observed["sglang:num_queue_reqs"],
        positive=False,
        integral=True,
    )
    return float(int(running)), float(int(waiting))


def _parse_sglang_metrics(
    body: object,
    *,
    served_model_alias: str,
) -> tuple[float, float]:
    """Return only a sanitized projection; rejected raw bytes never escape."""

    try:
        projection = _parse_sglang_metrics_unsafe(
            body,
            served_model_alias=served_model_alias,
        )
    except Exception:  # noqa: BLE001 — collapse every untrusted parser failure
        projection = None
    body = None
    if projection is None:
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
    return projection


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("invalid number")


def _exact_json_equal(observed: object, expected: object) -> bool:
    if type(observed) is not type(expected):
        return False
    if isinstance(expected, dict):
        if not isinstance(observed, dict):
            return False
        return observed.keys() == expected.keys() and all(
            _exact_json_equal(observed[key], expected_value) for key, expected_value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(observed, list):
            return False
        return len(observed) == len(expected) and all(
            _exact_json_equal(observed_value, expected_value)
            for observed_value, expected_value in zip(observed, expected, strict=True)
        )
    return observed == expected


def _sglang_server_info_expected(
    runtime_profile: RuntimeProfile,
    *,
    served_model_alias: str,
) -> dict[str, object]:
    launch = runtime_profile.sglang_extra_args
    if launch is None:
        raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED)
    weight_version = launch.weight_version
    if not isinstance(weight_version, str) or not weight_version:
        raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED)
    try:
        multimodal_limit = json.loads(
            launch.limit_mm_data_per_request,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED) from None
    if not isinstance(multimodal_limit, dict):
        raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED)
    return {
        "status": "ready",
        "version": runtime_profile.runtime_reported_version,
        "model_path": f"/models/{runtime_profile.model_dir_name}",
        "served_model_name": served_model_alias,
        "context_length": runtime_profile.max_model_len,
        "max_running_requests": runtime_profile.max_num_seqs,
        "max_total_tokens": launch.max_total_tokens,
        "max_total_num_tokens": launch.max_total_tokens,
        "mem_fraction_static": launch.mem_fraction_static,
        "kv_cache_dtype": runtime_profile.kv_cache_dtype,
        "chunked_prefill_size": launch.chunked_prefill_size,
        "mamba_ssm_dtype": launch.mamba_ssm_dtype,
        "max_mamba_cache_size": launch.max_mamba_cache_size,
        "disable_radix_cache": not launch.radix_cache_enabled,
        "disable_cuda_graph": runtime_profile.eager_mode,
        "cuda_graph_backend_decode": launch.cuda_graph_backend_decode,
        "cuda_graph_max_bs_decode": launch.cuda_graph_max_bs_decode,
        "cuda_graph_bs_decode": list(launch.cuda_graph_bs_decode),
        "cuda_graph_backend_prefill": launch.cuda_graph_backend_prefill,
        "attention_backend": launch.attention_backend,
        "reasoning_parser": launch.reasoning_parser,
        "tool_call_parser": launch.tool_call_parser,
        "mm_feature_transport": launch.mm_feature_transport,
        "limit_mm_data_per_request": multimodal_limit,
        "enable_metrics": launch.metrics_enabled,
        "weight_version": weight_version,
        "speculative_algorithm": launch.speculative_algorithm,
        "speculative_draft_model_path": None,
        "speculative_num_steps": None,
    }


def _parse_sglang_server_info_unsafe(
    body: object,
    *,
    profile: V12ModelProfileSpec,
) -> _SglangServerInfoProjection:
    """Validate a safe projection and return its generated engine seed.

    The raw document can contain credentials.  Only the code-owned allowlist
    below reaches the canonical projection; neither raw values nor field names
    are retained in errors or returned state.
    """

    if type(body) is not bytes or not body or len(body) > MAX_SERVER_INFO_BYTES:
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID) from None
    runtime_profile = PROFILES.get(profile.runtime_profile_name)
    if (
        not isinstance(value, dict)
        or runtime_profile is None
        or _metrics_adapter_for(profile) is not _MetricsAdapter.SGLANG_QWEN38
    ):
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)

    expected = _sglang_server_info_expected(
        runtime_profile,
        served_model_alias=profile.served_model_alias,
    )
    for key, expected_value in expected.items():
        if key not in value or not _exact_json_equal(value[key], expected_value):
            raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
    random_seed = value.get("random_seed")
    if (
        isinstance(random_seed, bool)
        or not isinstance(random_seed, int)
        or random_seed < 1
        or random_seed > (1 << 30) - 1
    ):
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)

    return _SglangServerInfoProjection(random_seed=random_seed)


def _parse_sglang_server_info(
    body: object,
    *,
    profile: V12ModelProfileSpec,
) -> _SglangServerInfoProjection:
    """Expose a sanitized error without retaining the raw secret-bearing body."""

    try:
        projection = _parse_sglang_server_info_unsafe(body, profile=profile)
    except Exception:  # noqa: BLE001 — collapse every untrusted parser failure
        projection = None
    body = None
    if projection is None:
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
    return projection


def _sglang_deployment_witness_expected(
    runtime_profile: RuntimeProfile,
    *,
    profile: V12ModelProfileSpec,
) -> dict[str, str]:
    raw_expected: dict[str, object] = {
        "schema": _DEPLOYMENT_WITNESS_SCHEMA,
        "profile_id": profile.profile_id,
        "engine_image_id": runtime_profile.engine_image_id,
        "engine_base_image_digest": runtime_profile.engine_base_image_digest,
        "engine_base_image_id": runtime_profile.engine_base_image_id,
        "runtime_source_revision": runtime_profile.runtime_source_revision,
        "runtime_reported_version": runtime_profile.runtime_reported_version,
        "model_repository": runtime_profile.model_repository,
        "model_revision": runtime_profile.model_revision,
        "model_snapshot_manifest_sha256": runtime_profile.model_snapshot_manifest_sha256,
        "model_quantization": runtime_profile.model_quantization,
        "served_model_alias": profile.served_model_alias,
        "launch_manifest_sha256": runtime_profile.launch_manifest_sha256,
        "proxy_image_id": runtime_profile.proxy_image_id,
        "proxy_policy_sha256": runtime_profile.proxy_policy_sha256,
    }
    if any(not isinstance(value, str) or not value for value in raw_expected.values()):
        raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED)
    return {key: value for key, value in raw_expected.items() if isinstance(value, str)}


def _parse_sglang_deployment_witness_unsafe(
    body: object,
    *,
    profile: V12ModelProfileSpec,
) -> _DeploymentWitnessProjection:
    if type(body) is not bytes or not body or len(body) > MAX_DEPLOYMENT_WITNESS_BYTES:
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID) from None
    runtime_profile = PROFILES.get(profile.runtime_profile_name)
    if (
        not isinstance(value, dict)
        or runtime_profile is None
        or _metrics_adapter_for(profile) is not _MetricsAdapter.SGLANG_QWEN38
    ):
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)

    expected = _sglang_deployment_witness_expected(runtime_profile, profile=profile)
    expected_keys = {*expected, "engine_start_nonce", "engine_random_seed"}
    if set(value) != expected_keys or any(
        value.get(key) != expected_value for key, expected_value in expected.items()
    ):
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
    engine_start_nonce = value.get("engine_start_nonce")
    engine_random_seed = value.get("engine_random_seed")
    if (
        not isinstance(engine_start_nonce, str)
        or _LOWER_SHA256_RE.fullmatch(engine_start_nonce) is None
        or isinstance(engine_random_seed, bool)
        or not isinstance(engine_random_seed, int)
        or engine_random_seed < 1
        or engine_random_seed > (1 << 30) - 1
    ):
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)

    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _DeploymentWitnessProjection(
        engine_random_seed=engine_random_seed,
        canonical_json=canonical,
        process_epoch_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _parse_sglang_deployment_witness(
    body: object,
    *,
    profile: V12ModelProfileSpec,
) -> _DeploymentWitnessProjection:
    """Expose a sanitized error without retaining a rejected witness body."""

    try:
        projection = _parse_sglang_deployment_witness_unsafe(body, profile=profile)
    except Exception:  # noqa: BLE001 — collapse every untrusted parser failure
        projection = None
    body = None
    if projection is None:
        raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
    return projection


async def _sample_sglang_load_without_raw_retention(
    metrics_transport: V12MetricsTransport,
    *,
    profile: V12ModelProfileSpec,
    metrics_deadline: float,
    absolute_deadline: float,
) -> tuple[ModelLoadSample | None, V12ModelRuntimeFailure | None]:
    """Observe SGLang while keeping every raw response inside a non-raising frame."""

    witness_before_body: object | None = None
    metrics_body: object | None = None
    server_info_body: object | None = None
    witness_after_body: object | None = None
    try:
        witness_before_body = await _bounded_await(
            metrics_transport.fetch_deployment_witness(
                maximum_bytes=MAX_DEPLOYMENT_WITNESS_BYTES,
                absolute_deadline=metrics_deadline,
            ),
            absolute_deadline=metrics_deadline,
            failure=V12ModelRuntimeFailure.METRICS_CALL_FAILED,
        )
        witness_before = _parse_sglang_deployment_witness(
            witness_before_body,
            profile=profile,
        )
        metrics_body = await _bounded_await(
            metrics_transport.fetch_metrics(
                maximum_bytes=MAX_SGLANG_METRICS_BYTES,
                absolute_deadline=metrics_deadline,
            ),
            absolute_deadline=metrics_deadline,
            failure=V12ModelRuntimeFailure.METRICS_CALL_FAILED,
        )
        running, waiting = _parse_sglang_metrics(
            metrics_body,
            served_model_alias=profile.served_model_alias,
        )
        server_info_body = await _bounded_await(
            metrics_transport.fetch_server_info(
                maximum_bytes=MAX_SERVER_INFO_BYTES,
                absolute_deadline=metrics_deadline,
            ),
            absolute_deadline=metrics_deadline,
            failure=V12ModelRuntimeFailure.METRICS_CALL_FAILED,
        )
        server_projection = _parse_sglang_server_info(
            server_info_body,
            profile=profile,
        )
        witness_after_body = await _bounded_await(
            metrics_transport.fetch_deployment_witness(
                maximum_bytes=MAX_DEPLOYMENT_WITNESS_BYTES,
                absolute_deadline=metrics_deadline,
            ),
            absolute_deadline=metrics_deadline,
            failure=V12ModelRuntimeFailure.METRICS_CALL_FAILED,
        )
        witness_after = _parse_sglang_deployment_witness(
            witness_after_body,
            profile=profile,
        )
        _deadline_remaining(absolute_deadline)
        if (
            witness_before != witness_after
            or server_projection.random_seed != witness_before.engine_random_seed
        ):
            return None, V12ModelRuntimeFailure.METRICS_INVALID
        return (
            ModelLoadSample(
                running=running,
                waiting=waiting,
                process_epoch_sha256=witness_before.process_epoch_sha256,
            ),
            None,
        )
    except asyncio.CancelledError:
        witness_before_body = None
        metrics_body = None
        server_info_body = None
        witness_after_body = None
        raise
    except V12ModelRuntimeError as exc:
        return None, exc.code
    except Exception:  # noqa: BLE001 — discard all raw transport exception graphs
        return None, V12ModelRuntimeFailure.METRICS_CALL_FAILED


def _parse_model_inventory(body: object, *, served_model_alias: str) -> None:
    """Require one exact OpenAI-compatible served-model identity."""

    if type(body) is not bytes or not body or len(body) > MAX_MODEL_INVENTORY_BYTES:
        raise _runtime_error(V12ModelRuntimeFailure.SERVED_ALIAS_REJECTED)
    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise _runtime_error(V12ModelRuntimeFailure.SERVED_ALIAS_REJECTED) from None
    if not isinstance(value, dict) or value.get("object") != "list":
        raise _runtime_error(V12ModelRuntimeFailure.SERVED_ALIAS_REJECTED)
    data = value.get("data")
    if not isinstance(data, list) or len(data) != 1:
        raise _runtime_error(V12ModelRuntimeFailure.SERVED_ALIAS_REJECTED)
    item = data[0]
    if not isinstance(item, dict) or item.get("object") != "model" or item.get("id") != served_model_alias:
        raise _runtime_error(V12ModelRuntimeFailure.SERVED_ALIAS_REJECTED)


def _valid_projection(value: object, *, expected_alias: str) -> V12ServedCompletion:
    if type(value) is not V12ServedCompletion:
        raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID)
    assert isinstance(value, V12ServedCompletion)
    if value.served_model_alias != expected_alias:
        raise _runtime_error(V12ModelRuntimeFailure.SERVED_ALIAS_REJECTED)
    if (
        not isinstance(value.content, str)
        or not value.content
        or len(value.content) > MAX_COMPLETION_CHARS
        or not isinstance(value.finish_reason, str)
        or not value.finish_reason
        or len(value.finish_reason) > 64
        or not isinstance(value.tool_calls, tuple)
        or any(not isinstance(item, str) or not item or len(item) > 128 for item in value.tool_calls)
        or not isinstance(value.prompt_tokens, int)
        or isinstance(value.prompt_tokens, bool)
        or value.prompt_tokens < 0
    ):
        raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID)
    try:
        value.content.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID) from None
    return value


def _validate_attested_chat_input(
    router: LLMRouter,
    leased_context_tokens: int,
    messages: object,
    max_tokens: object,
) -> None:
    if (
        not isinstance(messages, list)
        or not messages
        or isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens <= 0
        or isinstance(leased_context_tokens, bool)
        or not isinstance(leased_context_tokens, int)
        or leased_context_tokens <= 0
    ):
        raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID)
    try:
        encoded = json.dumps(
            messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError):
        raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID) from None
    measured_context_tokens = min(
        leased_context_tokens,
        MAX_MEASURED_CHAT_CONTEXT_TOKENS,
    )
    attested_input_limit = (
        MAX_ATTESTED_CHAT_INPUT_UTF8_BYTES * measured_context_tokens
    ) // BASE_ATTESTED_CHAT_CONTEXT_TOKENS
    if (
        len(encoded) > attested_input_limit
        or not model_messages_are_secret_free(messages)
        or router.estimate_messages_tokens(messages) + max_tokens + CONTEXT_SAFETY_RESERVE_TOKENS
        > leased_context_tokens
    ):
        raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID)


def _authoritative_usage_fits_lease(
    projection: object,
    lease: object,
    requirements: object,
    max_tokens: object,
) -> bool:
    """Close the measured context bound with post-transport token usage."""

    if (
        type(projection) is not V12ServedCompletion
        or type(lease) is not ModelProfileLease
        or type(requirements) is not ModelRequirements
        or not isinstance(projection.prompt_tokens, int)
        or isinstance(projection.prompt_tokens, bool)
        or projection.prompt_tokens < 0
        or not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens <= 0
        or lease.required_context_tokens != requirements.required_context_tokens
    ):
        return False
    return bool(
        projection.prompt_tokens + max_tokens + CONTEXT_SAFETY_RESERVE_TOKENS <= lease.required_context_tokens
    )


class _PlannerBridge:
    def __init__(self, client: V12ProductionProbeClient) -> None:
        self._client = client
        self.last_projection: V12ServedCompletion | None = None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        priority: str = "foreground",
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        reject_repeated_token_degeneration: bool = True,
        allow_retries: bool = True,
        absolute_deadline: float | None = None,
        open_silent_cooldown: bool = True,
        require_full_context: bool = False,
    ) -> dict[str, Any]:
        if absolute_deadline is None:
            raise _runtime_error(V12ModelRuntimeFailure.DEADLINE_EXHAUSTED)
        projection = await self._client._complete_projection(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            priority=priority,
            tools=tools,
            tool_choice=tool_choice,
            reject_repeated_token_degeneration=reject_repeated_token_degeneration,
            allow_retries=allow_retries,
            absolute_deadline=absolute_deadline,
            open_silent_cooldown=open_silent_cooldown,
            require_full_context=require_full_context,
        )
        self.last_projection = projection
        return {
            "content": projection.content,
            "finish_reason": projection.finish_reason,
            "tool_calls": list(projection.tool_calls),
            "usage": {"prompt_tokens": projection.prompt_tokens},
        }


class V12ProductionProbeClient(V12ModelProbeClient):
    """Exact probe client over one sealed router and two injected transports."""

    def __init__(
        self,
        seal: _RuntimeSeal,
        completion_transport: V12CompletionTransport,
        metrics_transport: V12MetricsTransport,
        *,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._seal = seal
        self._completion_transport = completion_transport
        self._metrics_transport = metrics_transport
        self._sleep = sleeper
        self._ensure_composed()

    def _ensure_composed(self) -> None:
        self._seal.validate()
        if (
            _transport_router(self._completion_transport) is not self._seal.router
            or _transport_router(self._metrics_transport) is not self._seal.router
        ):
            raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED)

    async def verify_model_inventory(self, *, absolute_deadline: float) -> None:
        self._ensure_composed()
        inventory_deadline = min(
            absolute_deadline,
            time.monotonic() + LOAD_TIMEOUT_SEC,
        )
        body = await _bounded_await(
            self._metrics_transport.fetch_model_inventory(
                maximum_bytes=MAX_MODEL_INVENTORY_BYTES,
                absolute_deadline=inventory_deadline,
            ),
            absolute_deadline=inventory_deadline,
            failure=V12ModelRuntimeFailure.METRICS_CALL_FAILED,
        )
        _parse_model_inventory(
            body,
            served_model_alias=self._seal.profile.served_model_alias,
        )

    async def sample_load(self, *, absolute_deadline: float) -> ModelLoadSample:
        try:
            self._ensure_composed()
            adapter = _metrics_adapter_for(self._seal.profile)
            metrics_deadline = min(
                absolute_deadline,
                time.monotonic() + LOAD_TIMEOUT_SEC,
            )
            if adapter is _MetricsAdapter.SGLANG_QWEN38:
                sample, runtime_failure = await _sample_sglang_load_without_raw_retention(
                    self._metrics_transport,
                    profile=self._seal.profile,
                    metrics_deadline=metrics_deadline,
                    absolute_deadline=absolute_deadline,
                )
                if sample is None:
                    raise _runtime_error(runtime_failure or V12ModelRuntimeFailure.METRICS_INVALID)
                return sample
            body = await _bounded_await(
                self._metrics_transport.fetch_metrics(
                    maximum_bytes=MAX_VLLM_METRICS_BYTES,
                    absolute_deadline=metrics_deadline,
                ),
                absolute_deadline=metrics_deadline,
                failure=V12ModelRuntimeFailure.METRICS_CALL_FAILED,
            )
            _deadline_remaining(absolute_deadline)
            return _parse_metrics(
                body,
                served_model_alias=self._seal.profile.served_model_alias,
            )
        except asyncio.CancelledError:
            raise
        except V12ModelRuntimeError as exc:
            failure = (
                ModelProbeFailure.DEADLINE_EXHAUSTED
                if exc.code is V12ModelRuntimeFailure.DEADLINE_EXHAUSTED
                else ModelProbeFailure.LOAD_INVALID
                if exc.code is V12ModelRuntimeFailure.METRICS_INVALID
                else ModelProbeFailure.LOAD_CALL_FAILED
            )
            raise ModelProbeError(failure) from None
        except Exception:
            raise ModelProbeError(ModelProbeFailure.LOAD_CALL_FAILED) from None

    async def _complete_projection(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None,
        max_tokens: int | None,
        priority: str,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        reject_repeated_token_degeneration: bool,
        allow_retries: bool,
        absolute_deadline: float,
        open_silent_cooldown: bool,
        require_full_context: bool,
    ) -> V12ServedCompletion:
        self._ensure_composed()
        projection = await _bounded_await(
            self._completion_transport.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                priority=priority,
                tools=tools,
                tool_choice=tool_choice,
                reject_repeated_token_degeneration=reject_repeated_token_degeneration,
                allow_retries=allow_retries,
                absolute_deadline=absolute_deadline,
                open_silent_cooldown=open_silent_cooldown,
                require_full_context=require_full_context,
            ),
            absolute_deadline=absolute_deadline,
            failure=V12ModelRuntimeFailure.MODEL_CALL_FAILED,
        )
        _deadline_remaining(absolute_deadline)
        return _valid_projection(
            projection,
            expected_alias=self._seal.profile.served_model_alias,
        )

    async def _prompt_completion(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_tokens: int,
        absolute_deadline: float,
    ) -> ProbeCompletion:
        messages = [{"role": "user", "content": prompt}]
        if system_prompt is not None:
            messages.insert(0, {"role": "system", "content": system_prompt})
        projection = await self._complete_projection(
            messages,
            temperature=0.0,
            max_tokens=max_tokens,
            priority="background",
            tools=None,
            tool_choice=None,
            reject_repeated_token_degeneration=True,
            allow_retries=False,
            absolute_deadline=absolute_deadline,
            open_silent_cooldown=False,
            require_full_context=True,
        )
        return ProbeCompletion(
            content=projection.content,
            finish_reason=projection.finish_reason,
            tool_calls=projection.tool_calls,
            prompt_tokens=projection.prompt_tokens,
        )

    async def complete_plan(
        self,
        case: PlanProbeCase,
        *,
        absolute_deadline: float,
    ) -> ProbeCompletion:
        try:
            bridge = _PlannerBridge(self)
            plan = await V12Planner(bridge).plan(case.turn, turn_deadline=absolute_deadline)
            projection = bridge.last_projection
            if projection is None:
                raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID)
            return ProbeCompletion(
                content=json.dumps(
                    plan.payload(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                finish_reason=projection.finish_reason,
                tool_calls=projection.tool_calls,
                prompt_tokens=projection.prompt_tokens,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ModelProbeError(ModelProbeFailure.PLAN_CALL_FAILED) from None

    async def complete_synthesis(
        self,
        request: SynthesisProbeRequest,
        *,
        absolute_deadline: float,
    ) -> ProbeCompletion:
        try:
            return await self._prompt_completion(
                request.prompt,
                system_prompt=request.system_prompt,
                max_tokens=512,
                absolute_deadline=absolute_deadline,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ModelProbeError(ModelProbeFailure.SYNTHESIS_CALL_FAILED) from None

    async def complete_verifier(
        self,
        request: VerifierProbeRequest,
        *,
        absolute_deadline: float,
    ) -> ProbeCompletion:
        try:
            return await self._prompt_completion(
                request.prompt,
                system_prompt=request.system_prompt,
                max_tokens=256,
                absolute_deadline=absolute_deadline,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ModelProbeError(ModelProbeFailure.VERIFIER_CALL_FAILED) from None

    async def complete_context(
        self,
        request: ContextProbeRequest,
        *,
        absolute_deadline: float,
    ) -> ProbeCompletion:
        try:
            return await self._prompt_completion(
                request.prompt,
                max_tokens=CONTEXT_OUTPUT_RESERVE_TOKENS,
                absolute_deadline=absolute_deadline,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ModelProbeError(ModelProbeFailure.CONTEXT_CALL_FAILED) from None

    @staticmethod
    def _pending_state(handle: V12PendingCompletion) -> bool:
        try:
            value = handle.is_pending()
        except Exception:
            raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID) from None
        if not isinstance(value, bool):
            raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID)
        return value

    @staticmethod
    def _submission_started(handle: V12PendingCompletion) -> bool:
        try:
            value = handle.submission_started()
        except Exception:
            raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID) from None
        if not isinstance(value, bool):
            raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID)
        return value

    async def _sleep_until(self, target: float, *, absolute_deadline: float) -> None:
        delay = max(0.0, min(target - time.monotonic(), CANCELLATION_POLL_INTERVAL_SEC))
        if delay <= 0.0:
            return
        await _bounded_await(
            self._sleep(delay),
            absolute_deadline=absolute_deadline,
            failure=V12ModelRuntimeFailure.DEADLINE_EXHAUSTED,
        )

    async def _cleanup_pending(self, handle: V12PendingCompletion) -> None:
        try:
            if self._pending_state(handle):
                cleanup_deadline = time.monotonic() + LOCAL_CANCELLATION_DRAIN_SEC
                await _bounded_await(
                    handle.cancel_and_drain(absolute_deadline=cleanup_deadline),
                    absolute_deadline=cleanup_deadline,
                    failure=V12ModelRuntimeFailure.MODEL_CALL_FAILED,
                )
        except (asyncio.CancelledError, Exception):
            pass

    async def cancel_and_drain(
        self,
        request: CancellationProbeRequest,
        *,
        absolute_deadline: float,
    ) -> CancellationProbeResult:
        handle: V12PendingCompletion | None = None
        try:
            self._ensure_composed()
            baseline = await self.sample_load(absolute_deadline=absolute_deadline)
            if baseline.running != 0.0 or baseline.waiting != 0.0:
                raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID)
            started = time.monotonic()
            cancellation_deadline = min(
                absolute_deadline,
                started + CANCELLATION_TIMEOUT_SEC,
            )
            handle = await _bounded_await(
                self._completion_transport.start_cancellable(
                    [{"role": "user", "content": request.prompt}],
                    temperature=0.0,
                    max_tokens=2_048,
                    priority="background",
                    absolute_deadline=cancellation_deadline,
                    require_full_context=True,
                ),
                absolute_deadline=cancellation_deadline,
                failure=V12ModelRuntimeFailure.MODEL_CALL_FAILED,
            )
            if (
                _transport_router(handle) is not self._seal.router
                or handle.submitted_model_alias != self._seal.profile.served_model_alias
                or not self._pending_state(handle)
            ):
                raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED)

            # The request is accepted only after our code-owned HTTP seam has
            # begun the send and the same endpoint then moves from exact idle
            # to a positive running/waiting count while this request is still
            # pending.  A task waiting on the router semaphore is not evidence
            # of server-side acceptance.
            positive_seen = False
            while time.monotonic() < cancellation_deadline:
                if not self._pending_state(handle):
                    raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID)
                if not self._submission_started(handle):
                    await self._sleep_until(
                        time.monotonic() + CANCELLATION_POLL_INTERVAL_SEC,
                        absolute_deadline=cancellation_deadline,
                    )
                    continue
                sample = await self.sample_load(absolute_deadline=cancellation_deadline)
                if sample.process_epoch_sha256 != baseline.process_epoch_sha256:
                    raise _runtime_error(V12ModelRuntimeFailure.EPOCH_CHANGED)
                if sample.running > 0.0 or sample.waiting > 0.0:
                    positive_seen = True
                    break
                await self._sleep_until(
                    time.monotonic() + CANCELLATION_POLL_INTERVAL_SEC,
                    absolute_deadline=cancellation_deadline,
                )
            if not positive_seen:
                raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID)

            cancel_not_before = started + max(0, request.cancel_after_ms) / 1_000.0
            while time.monotonic() < cancel_not_before:
                if not self._pending_state(handle):
                    raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID)
                await self._sleep_until(cancel_not_before, absolute_deadline=cancellation_deadline)
            confirmation = await self.sample_load(absolute_deadline=cancellation_deadline)
            if (
                confirmation.process_epoch_sha256 != baseline.process_epoch_sha256
                or (confirmation.running == 0.0 and confirmation.waiting == 0.0)
                or not self._pending_state(handle)
            ):
                raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID)

            cancel_started = time.monotonic()
            drained = await _bounded_await(
                handle.cancel_and_drain(absolute_deadline=cancellation_deadline),
                absolute_deadline=cancellation_deadline,
                failure=V12ModelRuntimeFailure.MODEL_CALL_FAILED,
            )
            if drained is not True or self._pending_state(handle):
                raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID)

            drain_deadline = min(
                cancellation_deadline,
                cancel_started + max(0, request.queue_drain_timeout_ms) / 1_000.0,
            )
            stable_zero = 0
            while time.monotonic() < drain_deadline:
                sample = await self.sample_load(absolute_deadline=drain_deadline)
                if sample.process_epoch_sha256 != baseline.process_epoch_sha256:
                    raise _runtime_error(V12ModelRuntimeFailure.EPOCH_CHANGED)
                if sample.running == 0.0 and sample.waiting == 0.0:
                    stable_zero += 1
                    if stable_zero >= CANCELLATION_STABLE_ZERO_OBSERVATIONS:
                        elapsed_ms = max(0, int((time.monotonic() - cancel_started) * 1_000))
                        return CancellationProbeResult(
                            phase="submitted",
                            accepted_request_witness_sha256=(_cancellation_request_witness_sha256(request)),
                            local_task_drained=True,
                            remote_queue_drain_ms=elapsed_ms,
                        )
                else:
                    stable_zero = 0
                await _bounded_await(
                    self._sleep(CANCELLATION_STABLE_ZERO_INTERVAL_SEC),
                    absolute_deadline=drain_deadline,
                    failure=V12ModelRuntimeFailure.DEADLINE_EXHAUSTED,
                )
            raise _runtime_error(V12ModelRuntimeFailure.DEADLINE_EXHAUSTED)
        except asyncio.CancelledError:
            if handle is not None:
                await self._cleanup_pending(handle)
            raise
        except Exception:
            if handle is not None:
                await self._cleanup_pending(handle)
            raise ModelProbeError(ModelProbeFailure.CANCELLATION_INVALID) from None


class AttestedV12ModelRuntime:
    """Own live attestation, epoch validation and checked model calls."""

    def __init__(
        self,
        router: LLMRouter,
        completion_transport: V12CompletionTransport,
        metrics_transport: V12MetricsTransport,
        *,
        profile: V12ModelProfileSpec = QWEN36_27B_V12_PROFILE,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if type(router) is not LLMRouter:
            raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED)
        if v12_model_profile_for(profile.runtime_profile_name, profile.served_model_alias) is not profile:
            raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED)
        if _metrics_adapter_for(profile) is _MetricsAdapter.SGLANG_QWEN38:
            runtime_profile = PROFILES.get(profile.runtime_profile_name)
            if runtime_profile is None:
                raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED)
            _sglang_deployment_witness_expected(runtime_profile, profile=profile)
        binding = _derive_endpoint_binding(router, profile)
        self._seal = _RuntimeSeal(router, profile, binding)
        self._seal.validate()
        self._client = V12ProductionProbeClient(
            self._seal,
            completion_transport,
            metrics_transport,
            sleeper=sleeper,
        )
        try:
            self._gate = V12ModelGate(
                profile,
                endpoint_binding_sha256=binding,
                installation_context_tokens=_installation_context_tokens(router, profile),
            )
        except (TypeError, ValueError):
            raise _runtime_error(V12ModelRuntimeFailure.COMPOSITION_REJECTED) from None
        self._attestation_lock = asyncio.Lock()

    @property
    def profile(self) -> V12ModelProfileSpec:
        return self._seal.profile

    @property
    def probe_client(self) -> V12ModelProbeClient:
        return self._client

    def public_status(self) -> dict[str, object]:
        return self._gate.public_status()

    def available_context_tokens(self) -> int:
        """Return the exact live gate capacity, or zero on uncertainty."""

        try:
            value = self._gate.available_context_tokens()
        except Exception:
            return 0
        return value if type(value) is int and 0 < value < (1 << 63) else 0

    async def attest(self, *, absolute_deadline: float) -> V12LiveAttestation:
        acquired = False
        try:
            await _bounded_await(
                self._attestation_lock.acquire(),
                absolute_deadline=absolute_deadline,
                failure=V12ModelRuntimeFailure.DEADLINE_EXHAUSTED,
            )
            acquired = True
            self._seal.validate()
            await self._client.verify_model_inventory(absolute_deadline=absolute_deadline)
            attestation = await run_v12_live_probe(
                self._seal.profile,
                self._client,
                endpoint_binding_sha256=self._seal.endpoint_binding_sha256,
                absolute_deadline=absolute_deadline,
            )
            if not self._gate.install_live(attestation):
                raise _runtime_error(V12ModelRuntimeFailure.LIVE_PROBE_REJECTED)
            return attestation
        except asyncio.CancelledError:
            self._gate.revoke(ModelGateReason.ATTESTATION_REJECTED)
            raise
        except Exception:
            self._gate.revoke(ModelGateReason.ATTESTATION_REJECTED)
            raise _runtime_error(V12ModelRuntimeFailure.LIVE_PROBE_REJECTED) from None
        finally:
            if acquired:
                self._attestation_lock.release()

    async def _current_epoch(self, *, absolute_deadline: float) -> str:
        try:
            sample = await self._client.sample_load(absolute_deadline=absolute_deadline)
            return sample.process_epoch_sha256
        except asyncio.CancelledError:
            raise
        except Exception:
            self._gate.revoke(ModelGateReason.EPOCH_INVALID)
            raise _runtime_error(V12ModelRuntimeFailure.METRICS_CALL_FAILED) from None

    async def acquire(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease | None:
        epoch = await self._current_epoch(absolute_deadline=absolute_deadline)
        return self._gate.lease(requirements, process_epoch_sha256=epoch)

    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease | None:
        """Wiring-friendly name for :meth:`acquire`; never auto-reacquires."""

        return await self.acquire(requirements, absolute_deadline=absolute_deadline)

    async def validate(
        self,
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> bool:
        try:
            epoch = await self._current_epoch(absolute_deadline=absolute_deadline)
        except asyncio.CancelledError:
            raise
        except V12ModelRuntimeError:
            return False
        return self._gate.validate_lease(
            lease,
            requirements,
            process_epoch_sha256=epoch,
        )

    async def lease_is_current(
        self,
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> bool:
        """Wiring-friendly name for :meth:`validate`."""

        return await self.validate(
            lease,
            requirements,
            absolute_deadline=absolute_deadline,
        )

    def lease_is_process_current(
        self,
        lease: object,
        requirements: ModelRequirements,
    ) -> bool:
        """Synchronously recheck the exact local gate generation.

        Remote epoch freshness is sampled by :meth:`lease_is_current` before a
        synchronous publication section. This non-I/O check then closes local
        revoke/re-attestation races without yielding inside that transaction.
        """

        if type(lease) is not ModelProfileLease or type(requirements) is not ModelRequirements:
            return False
        return self._gate.validate_lease(
            lease,
            requirements,
            process_epoch_sha256=lease.process_epoch_sha256,
        )

    async def checked_chat(
        self,
        lease: object,
        requirements: ModelRequirements,
        messages: list[dict[str, Any]],
        *,
        absolute_deadline: float,
        temperature: float | None = 0.0,
        max_tokens: int | None = None,
        priority: str = "foreground",
    ) -> dict[str, Any]:
        before_epoch = await self._current_epoch(absolute_deadline=absolute_deadline)
        if not self._gate.validate_lease(
            lease,
            requirements,
            process_epoch_sha256=before_epoch,
        ):
            raise _runtime_error(V12ModelRuntimeFailure.LEASE_REJECTED)
        _validate_attested_chat_input(
            self._seal.router,
            requirements.required_context_tokens,
            messages,
            max_tokens,
        )

        projection: V12ServedCompletion | None = None
        call_error: Exception | None = None
        try:
            projection = await self._client._complete_projection(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                priority=priority,
                tools=None,
                tool_choice=None,
                reject_repeated_token_degeneration=True,
                allow_retries=False,
                absolute_deadline=absolute_deadline,
                open_silent_cooldown=False,
                require_full_context=True,
            )
        except asyncio.CancelledError:
            # Cancellation is control flow, not a model failure.  Sampling the
            # endpoint again here would delay cancellation and, when the same
            # deadline is already spent, could mask it as a metrics failure.
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            call_error = exc

        after_epoch = await self._current_epoch(absolute_deadline=absolute_deadline)
        if before_epoch != after_epoch:
            self._gate.revoke(ModelGateReason.EPOCH_CHANGED)
            raise _runtime_error(V12ModelRuntimeFailure.EPOCH_CHANGED)
        if call_error is not None:
            if isinstance(call_error, V12ModelRuntimeError) and call_error.code in {
                V12ModelRuntimeFailure.SERVED_ALIAS_REJECTED,
                V12ModelRuntimeFailure.COMPLETION_INVALID,
            }:
                if call_error.code is V12ModelRuntimeFailure.SERVED_ALIAS_REJECTED:
                    self._gate.revoke(ModelGateReason.ATTESTATION_REJECTED)
                raise _runtime_error(call_error.code) from None
            raise _runtime_error(V12ModelRuntimeFailure.MODEL_CALL_FAILED) from None
        if (
            projection is None
            or projection.finish_reason != "stop"
            or projection.tool_calls
            or not self._gate.validate_lease(
                lease,
                requirements,
                process_epoch_sha256=after_epoch,
            )
            or not _authoritative_usage_fits_lease(
                projection,
                lease,
                requirements,
                max_tokens,
            )
        ):
            raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID)
        return {
            "content": projection.content,
            "finish_reason": projection.finish_reason,
            "tool_calls": [],
            "usage": {"prompt_tokens": projection.prompt_tokens},
        }

    async def complete(
        self,
        lease: object,
        requirements: ModelRequirements,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None,
        priority: str,
        absolute_deadline: float,
        temperature: float | None = 0.0,
    ) -> dict[str, Any]:
        """Wiring-friendly checked completion; a stale lease is never replaced."""

        return await self.checked_chat(
            lease,
            requirements,
            messages,
            absolute_deadline=absolute_deadline,
            temperature=temperature,
            max_tokens=max_tokens,
            priority=priority,
        )


__all__ = [
    "AttestedV12ModelRuntime",
    "MAX_DEPLOYMENT_WITNESS_BYTES",
    "MAX_METRICS_BYTES",
    "MAX_SERVER_INFO_BYTES",
    "MAX_SGLANG_METRICS_BYTES",
    "MAX_VLLM_METRICS_BYTES",
    "MAX_ATTESTED_CHAT_INPUT_UTF8_BYTES",
    "MAX_MODEL_INVENTORY_BYTES",
    "V12CompletionTransport",
    "V12MetricsTransport",
    "V12ModelRuntimeError",
    "V12ModelRuntimeFailure",
    "V12PendingCompletion",
    "V12ServedAliasError",
    "V12ProductionProbeClient",
    "V12ServedCompletion",
]
