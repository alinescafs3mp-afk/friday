"""Sealed composition boundary for the V12 model probe and canary runtime.

This module deliberately contains no HTTP implementation.  The production
completion path still needs the served ``model`` field preserved by
``LLMRouter`` and the metrics path needs a bounded endpoint transport.  Those
two narrow seams are injected here, are bound to one exact router instance, and
are exercised without network access in the unit tests.

Configuration is not an attestation.  A runtime becomes usable only after the
code-owned live probe has passed, its process epoch is still present in vLLM's
metrics, and the process-local gate has issued an exact least-privilege lease.
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
from friday.config import PROFILES, FridaySettings
from friday.model_input_hygiene import model_messages_are_secret_free
from friday.model_probe import (
    CANCELLATION_TIMEOUT_SEC,
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
    ModelGateReason,
    ModelProfileLease,
    ModelRequirements,
    V12LiveAttestation,
    V12ModelGate,
    V12ModelProfileSpec,
    v12_model_profile_for,
)
from friday.orchestration.planner import V12Planner

MAX_METRICS_BYTES = 65_536
MAX_MODEL_INVENTORY_BYTES = 65_536
MAX_METRICS_LINE_CHARS = 4_096
MAX_METRIC_COUNT = 1_000_000
CANCELLATION_POLL_INTERVAL_SEC = 0.01
CANCELLATION_STABLE_ZERO_OBSERVATIONS = 2
CANCELLATION_STABLE_ZERO_INTERVAL_SEC = 0.05
LOCAL_CANCELLATION_DRAIN_SEC = 0.05
MAX_ATTESTED_CHAT_INPUT_UTF8_BYTES = 5_500

_PROCESS_PRIVATE_SALT = secrets.token_bytes(32)
_PROCESS_SALT_PID = os.getpid()
_T = TypeVar("_T")

_METRIC_SAMPLE_RE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)"
    r"(?:\{(?P<labels>[^{}\r\n]{0,1024})\})?\s+"
    r"(?P<value>[^\s]+)(?:\s+[0-9]+)?\s*$"
)
_REQUIRED_METRICS = frozenset(
    {
        "process_start_time_seconds",
        "vllm:num_requests_running",
        "vllm:num_requests_waiting",
    }
)


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
    """Bounded metrics seam; the transport must derive its target from the router."""

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
            or self.router.settings.profile.max_model_len < self.profile.max_context_tokens
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


def _parse_metrics(body: object, *, served_model_alias: str) -> ModelLoadSample:
    if type(body) is not bytes or not body or len(body) > MAX_METRICS_BYTES:
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
            if any(line.startswith(name) for name in _REQUIRED_METRICS):
                raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
            continue
        name = match.group("name")
        if name not in _REQUIRED_METRICS:
            continue
        if name in observed:
            raise _runtime_error(V12ModelRuntimeFailure.METRICS_INVALID)
        observed[name] = (match.group("labels"), match.group("value"))
    if set(observed) != _REQUIRED_METRICS:
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


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("invalid number")


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
    profile: V12ModelProfileSpec,
    messages: object,
    max_tokens: object,
) -> None:
    if (
        not isinstance(messages, list)
        or not messages
        or isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens <= 0
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
    if (
        len(encoded) > MAX_ATTESTED_CHAT_INPUT_UTF8_BYTES
        or not model_messages_are_secret_free(messages)
        or router.estimate_messages_tokens(messages) + max_tokens + 256 > profile.max_context_tokens
    ):
        raise _runtime_error(V12ModelRuntimeFailure.COMPLETION_INVALID)


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
            metrics_deadline = min(
                absolute_deadline,
                time.monotonic() + LOAD_TIMEOUT_SEC,
            )
            body = await _bounded_await(
                self._metrics_transport.fetch_metrics(
                    maximum_bytes=MAX_METRICS_BYTES,
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
                max_tokens=256,
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
        binding = _derive_endpoint_binding(router, profile)
        self._seal = _RuntimeSeal(router, profile, binding)
        self._seal.validate()
        self._client = V12ProductionProbeClient(
            self._seal,
            completion_transport,
            metrics_transport,
            sleeper=sleeper,
        )
        self._gate = V12ModelGate(profile, endpoint_binding_sha256=binding)
        self._attestation_lock = asyncio.Lock()

    @property
    def profile(self) -> V12ModelProfileSpec:
        return self._seal.profile

    @property
    def probe_client(self) -> V12ModelProbeClient:
        return self._client

    def public_status(self) -> dict[str, object]:
        return self._gate.public_status()

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
            self._seal.profile,
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
            if (
                isinstance(call_error, V12ModelRuntimeError)
                and call_error.code is V12ModelRuntimeFailure.SERVED_ALIAS_REJECTED
            ):
                self._gate.revoke(ModelGateReason.ATTESTATION_REJECTED)
                raise _runtime_error(V12ModelRuntimeFailure.SERVED_ALIAS_REJECTED) from None
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
    "MAX_METRICS_BYTES",
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
