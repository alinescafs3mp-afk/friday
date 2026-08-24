"""Code-owned routing and fallback boundary for secondary advisory work."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, TypeVar

import httpx

from friday.model_input_hygiene import secondary_model_messages_are_secret_free

from .client import SecondaryEndpointClient
from .contracts import (
    ADVISORY_WORKLOADS,
    EffectClass,
    ModelModality,
    ModelRequest,
    ModelWorkload,
    SecondaryAttempt,
    SecondaryEndpointConfig,
    SecondaryFailure,
    SecondaryMode,
    SecondaryResult,
    SecondaryState,
    SecondaryStatus,
    secondary_configuration_is_admissible,
)
from .profiles import get_secondary_runtime_profile

if TYPE_CHECKING:
    from friday.config import FridaySettings

T = TypeVar("T")

_READMISSION_FAILURES = frozenset(
    {
        SecondaryFailure.CONNECT_FAILED,
        SecondaryFailure.TIMEOUT,
        SecondaryFailure.HTTP_TRANSIENT,
        SecondaryFailure.HTTP_REJECTED,
        SecondaryFailure.AUTH_REJECTED,
        SecondaryFailure.WRONG_PROFILE,
        SecondaryFailure.WRONG_MODEL,
        SecondaryFailure.MALFORMED_RESPONSE,
        SecondaryFailure.TOOL_CALL_REJECTED,
        SecondaryFailure.REASONING_LEAK,
        SecondaryFailure.DEGENERATION,
        SecondaryFailure.CANCELLED,
    }
)


def _request_contains_image(request: ModelRequest) -> bool:
    """Reject nested OpenAI image parts before any admission/probe traffic."""

    pending: list[object] = list(request.messages)
    visited = 0
    while pending and visited < 2_048:
        value = pending.pop()
        visited += 1
        if isinstance(value, Mapping):
            kind = str(value.get("type", "")).strip().casefold()
            if kind in {"image", "image_url", "input_image"} or "image_url" in value:
                return True
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    return bool(pending)


class SecondaryBrainScheduler:
    """Explicit required/optional/shadow forms; never owns primary authority."""

    def __init__(
        self,
        *,
        mode: SecondaryMode,
        allowed_workloads: frozenset[ModelWorkload],
        allow_private_text: bool,
        client: SecondaryEndpointClient | None,
        unavailable_state: SecondaryState,
    ) -> None:
        self.mode = mode
        self.allowed_workloads = allowed_workloads & ADVISORY_WORKLOADS
        self.allow_private_text = allow_private_text
        self._client = client
        self._unavailable_state = unavailable_state
        self._local_skipped_total = 0
        self._local_fallback_total = 0
        self._startup_probe_task: asyncio.Task[None] | None = None
        self._probe_lock = asyncio.Lock()
        self._epoch_admitted = False
        self._last_probe_success_monotonic: float | None = None
        self._probe_success_total = 0
        self._probe_failure_total = 0
        self._shadow_tasks: set[asyncio.Task[None]] = set()
        self._selected_by_workload = {workload: 0 for workload in ModelWorkload}
        self._success_by_workload = {workload: 0 for workload in ModelWorkload}
        self._latency_sum_by_workload = {workload: 0.0 for workload in ModelWorkload}
        self._latency_max_by_workload = {workload: 0.0 for workload in ModelWorkload}
        self._skipped_by_reason = {failure: 0 for failure in SecondaryFailure}
        self._fallback_by_reason = {failure: 0 for failure in SecondaryFailure}
        self._skipped_by_workload_reason = {
            (workload, failure): 0 for workload in ModelWorkload for failure in SecondaryFailure
        }
        self._fallback_by_workload_reason = {
            (workload, failure): 0 for workload in ModelWorkload for failure in SecondaryFailure
        }
        self._shadow_valid_total = 0
        self._shadow_invalid_total = 0
        self._shadow_skipped_total = 0
        self._closed = False

    @property
    def served_model_alias(self) -> str:
        """Expose identity only, never the policy-bypassing transport client."""

        return self._client.config.served_model_alias if self._client is not None else ""

    def new_advisory_deadline(self) -> float:
        """Return one end-to-end optional budget, including all admission probes."""

        budget = self._client.config.call_budget_sec if self._client is not None else 0.001
        return time.monotonic() + min(30.0, budget)

    @classmethod
    def from_settings(
        cls,
        settings: FridaySettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> SecondaryBrainScheduler:
        try:
            mode = SecondaryMode(settings.secondary_llm_mode)
        except ValueError:
            mode = SecondaryMode.DISABLED

        workloads: set[ModelWorkload] = set()
        for raw_workload in settings.secondary_llm_workloads:
            try:
                workloads.add(ModelWorkload(raw_workload.strip().casefold()))
            except ValueError:
                continue

        if not settings.secondary_llm_enabled or mode is SecondaryMode.DISABLED:
            return cls(
                mode=SecondaryMode.DISABLED,
                allowed_workloads=frozenset(workloads),
                allow_private_text=settings.secondary_llm_allow_private_text,
                client=None,
                unavailable_state=SecondaryState.DISABLED,
            )

        profile = get_secondary_runtime_profile(settings.secondary_llm_profile)
        endpoint = SecondaryEndpointConfig(
            base_url=settings.secondary_llm_base_url,
            served_model_alias=settings.secondary_llm_model,
            api_key=settings.secondary_llm_api_key,
            ca_file=settings.secondary_llm_ca_file,
            ca_sha256=profile.gateway_ca_certificate_sha256 if profile is not None else "",
            connect_timeout_sec=settings.secondary_llm_connect_timeout_sec,
            read_timeout_sec=settings.secondary_llm_read_timeout_sec,
            call_budget_sec=settings.secondary_llm_call_budget_sec,
            admission_timeout_sec=settings.secondary_llm_admission_timeout_sec,
            health_interval_sec=settings.secondary_llm_health_interval_sec,
            cooldown_sec=settings.secondary_llm_cooldown_sec,
            max_context_tokens=settings.secondary_llm_max_context_tokens,
            max_concurrency=settings.secondary_llm_max_concurrency,
            max_output_tokens=profile.max_output_tokens if profile is not None else 0,
            profile_id=settings.secondary_llm_profile,
            profile_manifest_sha256=profile.manifest_sha256 if profile is not None else "",
        )
        effective_workloads = frozenset(workloads) & ADVISORY_WORKLOADS
        if (
            not secondary_configuration_is_admissible(
                endpoint,
                primary_base_url=settings.llm_base_url,
                primary_model=settings.llm_model,
                primary_timeout_sec=settings.llm_timeout_sec,
                workload_names=settings.secondary_llm_workloads,
                mode=mode.value,
            )
            or not effective_workloads
        ):
            return cls(
                mode=mode,
                allowed_workloads=effective_workloads,
                allow_private_text=settings.secondary_llm_allow_private_text,
                client=None,
                unavailable_state=SecondaryState.MISCONFIGURED,
            )
        try:
            client = SecondaryEndpointClient(endpoint, transport=transport)
        except Exception:
            # Optional TLS/transport construction (including an invalid CA file)
            # may never turn into a primary startup failure or retain its raw error.
            return cls(
                mode=mode,
                allowed_workloads=effective_workloads,
                allow_private_text=settings.secondary_llm_allow_private_text,
                client=None,
                unavailable_state=SecondaryState.MISCONFIGURED,
            )
        return cls(
            mode=mode,
            allowed_workloads=effective_workloads,
            allow_private_text=settings.secondary_llm_allow_private_text,
            client=client,
            unavailable_state=SecondaryState.PROBING,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._startup_probe_task is not None:
            self._startup_probe_task.cancel()
            await asyncio.gather(self._startup_probe_task, return_exceptions=True)
            self._startup_probe_task = None
        for task in tuple(self._shadow_tasks):
            task.cancel()
        if self._shadow_tasks:
            await asyncio.gather(*tuple(self._shadow_tasks), return_exceptions=True)
            self._shadow_tasks.clear()
        if self._client is not None:
            try:
                await asyncio.wait_for(self._client.aclose(), timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                # An optional transport must never mask or interrupt teardown of
                # storage, workers, Obsidian, MCP or the primary model runtime.
                pass

    def start(self) -> None:
        """Start one non-blocking process-epoch probe; never a noisy loop."""

        if self._closed or self._client is None or self._startup_probe_task is not None:
            return
        self._startup_probe_task = asyncio.create_task(self._startup_probe())

    async def _startup_probe(self) -> None:
        if self._client is None:
            return
        deadline = time.monotonic() + min(
            self._client.config.call_budget_sec,
            self._client.config.health_interval_sec,
        )
        try:
            await self._ensure_epoch_admitted(deadline)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The task is intentionally detached from primary startup.  The
            # client already projects all expected failures as closed enums.
            return

    def _health_is_fresh(self) -> bool:
        if self._client is None:
            return False
        if self._last_probe_success_monotonic is None:
            return False
        return (
            time.monotonic() - self._last_probe_success_monotonic <= self._client.config.health_interval_sec
        )

    def _admission_is_fresh(self) -> bool:
        return self._epoch_admitted and self._health_is_fresh()

    async def _ensure_epoch_admitted(
        self,
        absolute_deadline_monotonic: float,
    ) -> SecondaryFailure | None:
        """Demand-probe one process epoch without queueing behind another probe."""

        if self._client is None:
            return SecondaryFailure.MISCONFIGURED
        if self._admission_is_fresh():
            return None
        try:
            await asyncio.wait_for(
                self._probe_lock.acquire(),
                timeout=self._client.config.admission_timeout_sec,
            )
        except TimeoutError:
            return SecondaryFailure.ADMISSION_BUSY
        try:
            if self._admission_is_fresh():
                return None
            failure = await self._client.probe_models(absolute_deadline_monotonic=absolute_deadline_monotonic)
            if failure is not None:
                self._epoch_admitted = False
                self._probe_failure_total += 1
                return failure
            self._last_probe_success_monotonic = time.monotonic()
            # The expensive generation canary is once per admitted process epoch.
            # A stale health window needs only the exact inventory probe; the
            # following typed real call becomes the fresh generation signal.
            if self._epoch_admitted:
                self._probe_success_total += 1
                return None
            canary = ModelRequest(
                workload=ModelWorkload.VERIFY,
                messages=(
                    {
                        "role": "system",
                        "content": "Return final content only. Never use tools.",
                    },
                    {"role": "user", "content": "Reply with exactly: ready"},
                ),
                max_output_tokens=16,
                absolute_deadline_monotonic=absolute_deadline_monotonic,
            )
            attempt = await self._client.call(canary)
            if attempt.result is None:
                self._probe_failure_total += 1
                return attempt.failure or SecondaryFailure.MALFORMED_RESPONSE
            if attempt.result.visible_content.strip().casefold() != "ready":
                await self._client.invalidate(SecondaryFailure.MALFORMED_RESPONSE)
                self._probe_failure_total += 1
                return SecondaryFailure.MALFORMED_RESPONSE
            self._epoch_admitted = True
            self._last_probe_success_monotonic = time.monotonic()
            self._probe_success_total += 1
            return None
        finally:
            self._probe_lock.release()

    async def __aenter__(self) -> SecondaryBrainScheduler:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    def status(self) -> SecondaryStatus:
        if self._client is not None:
            client_status = self._client.status()
            return replace(
                client_status,
                skipped_total=client_status.skipped_total + self._local_skipped_total,
            )
        failure = (
            SecondaryFailure.DISABLED
            if self._unavailable_state is SecondaryState.DISABLED
            else SecondaryFailure.MISCONFIGURED
        )
        return SecondaryStatus(
            state=self._unavailable_state,
            last_failure=failure,
            selected_total=0,
            success_total=0,
            skipped_total=self._local_skipped_total,
            fallback_total=self._local_fallback_total,
            active_requests=0,
            context_cap_tokens=0,
            served_model_match=False,
        )

    def public_status(self) -> dict[str, object]:
        """Return the compact projection safe for the public health route."""

        status = self.status()
        return {
            "schema": "friday.optional-secondary-health.v1",
            "role": "optional_advisory",
            "enabled": self.mode is not SecondaryMode.DISABLED,
            "configured": self._client is not None,
            "mode": self.mode.value,
            "state": status.state.value,
            "available": status.state is SecondaryState.HEALTHY and self._admission_is_fresh(),
        }

    def diagnostics_status(self) -> dict[str, object]:
        """Return bounded owner diagnostics without endpoint or content data."""

        status = self.status()
        return {
            **self.public_status(),
            "last_failure": status.last_failure.value if status.last_failure is not None else None,
            "active_requests": status.active_requests,
            "selected_total": sum(self._selected_by_workload.values()),
            "success_total": sum(self._success_by_workload.values()),
            "endpoint_request_total": status.selected_total,
            "endpoint_success_total": status.success_total,
            "skipped_total": status.skipped_total,
            "primary_fallback_total": status.fallback_total,
            "context_cap_tokens": status.context_cap_tokens,
            "served_model_match": status.served_model_match,
            "profile": self._client.config.profile_id if self._client is not None else "",
            "profile_manifest_match": status.profile_manifest_match,
            "last_success_age_sec": (
                round(status.last_success_age_sec, 3) if status.last_success_age_sec is not None else None
            ),
            "circuit_retry_after_sec": round(status.cooldown_retry_after_sec, 3),
            "probe_success_total": self._probe_success_total,
            "probe_failure_total": self._probe_failure_total,
            "model_inventory_probe_success_total": status.probe_success_total,
            "model_inventory_probe_failure_total": status.probe_failure_total,
            "workloads": {
                workload.value: {
                    "selected_total": self._selected_by_workload[workload],
                    "success_total": self._success_by_workload[workload],
                    "latency_count": self._success_by_workload[workload],
                    "latency_sum_sec": round(self._latency_sum_by_workload[workload], 6),
                    "latency_max_sec": round(self._latency_max_by_workload[workload], 6),
                    "skip_reasons": {
                        reason.value: self._skipped_by_workload_reason[(workload, reason)]
                        for reason in SecondaryFailure
                        if self._skipped_by_workload_reason[(workload, reason)]
                    },
                    "fallback_reasons": {
                        reason.value: self._fallback_by_workload_reason[(workload, reason)]
                        for reason in SecondaryFailure
                        if self._fallback_by_workload_reason[(workload, reason)]
                    },
                }
                for workload in sorted(self.allowed_workloads, key=lambda value: value.value)
            },
            "skip_reasons": {
                reason.value: count for reason, count in self._skipped_by_reason.items() if count
            },
            "fallback_reasons": {
                reason.value: count for reason, count in self._fallback_by_reason.items() if count
            },
            "shadow": {
                "valid_total": self._shadow_valid_total,
                "invalid_total": self._shadow_invalid_total,
                "skipped_total": self._shadow_skipped_total,
                "in_flight": len(self._shadow_tasks),
            },
        }

    def _eligibility_failure(
        self,
        request: ModelRequest,
        *,
        shadow: bool,
    ) -> SecondaryFailure | None:
        required_mode = SecondaryMode.SHADOW if shadow else SecondaryMode.ASSIST
        if self.mode is not required_mode:
            return (
                SecondaryFailure.DISABLED
                if self.mode is SecondaryMode.DISABLED
                else SecondaryFailure.MODE_DISALLOWED
            )
        if self._client is None:
            return SecondaryFailure.MISCONFIGURED
        if request.workload not in self.allowed_workloads:
            return SecondaryFailure.WORKLOAD_DISALLOWED
        if request.modality is not ModelModality.TEXT or _request_contains_image(request):
            return SecondaryFailure.UNSUPPORTED_MODALITY
        if request.effect_class not in {EffectClass.NONE, EffectClass.READ_ONLY}:
            return SecondaryFailure.EFFECT_DENIED
        if request.contains_private_text and not self.allow_private_text:
            return SecondaryFailure.PRIVATE_TEXT_DISALLOWED
        assert self._client is not None
        try:
            secret_free = secondary_model_messages_are_secret_free(
                request.messages,
                additional_secrets=(self._client.config.api_key,),
            )
        except Exception:
            secret_free = False
        if not secret_free:
            return SecondaryFailure.SECRET_MATERIAL_DENIED
        protocol_failure = self._client.validate_request(request)
        if protocol_failure is not None:
            return protocol_failure
        return None

    async def attempt(self, request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        failure = self._eligibility_failure(request, shadow=shadow)
        if failure is not None:
            self._record_skip(request.workload, failure, local=True)
            return SecondaryAttempt.rejected(failure)
        assert self._client is not None
        admission_failure = await self._ensure_epoch_admitted(request.absolute_deadline_monotonic)
        if admission_failure is not None:
            self._record_skip(request.workload, admission_failure, local=True)
            return SecondaryAttempt.rejected(admission_failure)
        self._selected_by_workload[request.workload] += 1
        try:
            attempt = await self._client.call(request)
        except Exception:
            # The secondary is never an availability dependency.  Cancellation
            # still propagates because asyncio.CancelledError is a BaseException.
            self._record_skip(request.workload, SecondaryFailure.CONNECT_FAILED, local=True)
            self._epoch_admitted = False
            return SecondaryAttempt.rejected(SecondaryFailure.CONNECT_FAILED)
        if attempt.result is not None:
            # Protocol-valid transport is a health signal, but it becomes a
            # workload success only after the caller's typed validator accepts it.
            self._last_probe_success_monotonic = time.monotonic()
            return attempt
        attempt_failure = attempt.failure or SecondaryFailure.MALFORMED_RESPONSE
        self._record_skip(request.workload, attempt_failure)
        if attempt_failure in _READMISSION_FAILURES:
            self._epoch_admitted = False
        return attempt

    def _record_skip(
        self,
        workload: ModelWorkload,
        failure: SecondaryFailure,
        *,
        local: bool = False,
    ) -> None:
        if local:
            self._local_skipped_total += 1
        self._skipped_by_reason[failure] += 1
        self._skipped_by_workload_reason[(workload, failure)] += 1

    def _record_fallback(self, workload: ModelWorkload, failure: SecondaryFailure) -> None:
        self._local_fallback_total += 1
        self._fallback_by_reason[failure] += 1
        self._fallback_by_workload_reason[(workload, failure)] += 1
        if self._client is not None:
            self._client.record_fallback()

    def _record_success(self, request: ModelRequest, result: SecondaryResult) -> None:
        self._success_by_workload[request.workload] += 1
        self._latency_sum_by_workload[request.workload] += result.latency_sec
        self._latency_max_by_workload[request.workload] = max(
            self._latency_max_by_workload[request.workload],
            result.latency_sec,
        )

    @staticmethod
    def _validated(
        result: SecondaryResult,
        validator: Callable[[SecondaryResult], bool] | None,
    ) -> bool:
        try:
            return validator is None or bool(validator(result))
        except Exception:
            return False

    async def _reject_valid_result(
        self,
        request: ModelRequest,
        failure: SecondaryFailure = SecondaryFailure.MALFORMED_RESPONSE,
    ) -> None:
        self._record_skip(request.workload, failure)
        self._epoch_admitted = False
        if self._client is not None:
            await self._client.invalidate(failure)

    async def secondary_preferred_required_result(
        self,
        request: ModelRequest,
        primary_fallback: Callable[[], Awaitable[T]],
        *,
        validator: Callable[[SecondaryResult], bool] | None = None,
    ) -> SecondaryResult | T:
        """Use one secondary attempt or invoke the primary fallback exactly once."""

        attempt = await self.attempt(request)
        if attempt.result is not None:
            if self._validated(attempt.result, validator):
                self._record_success(request, attempt.result)
                return attempt.result
            await self._reject_valid_result(request)
            reason = SecondaryFailure.MALFORMED_RESPONSE
        else:
            reason = attempt.failure or SecondaryFailure.MALFORMED_RESPONSE
        self._record_fallback(request.workload, reason)
        return await primary_fallback()

    async def secondary_optional_advice(
        self,
        request: ModelRequest,
        *,
        validator: Callable[[SecondaryResult], bool] | None = None,
    ) -> SecondaryResult | None:
        """Return advisory output or skip; never duplicate primary work."""

        attempt = await self.attempt(request)
        if attempt.result is None:
            return None
        if not self._validated(attempt.result, validator):
            await self._reject_valid_result(request)
            return None
        self._record_success(request, attempt.result)
        return attempt.result

    async def run_shadow(
        self,
        request_factory: Callable[[], ModelRequest],
        primary: Callable[[], Awaitable[T]],
        *,
        validator: Callable[[SecondaryResult], bool] | None = None,
    ) -> T:
        """Return primary first; evaluate and discard shadow work asynchronously."""

        primary_result = await primary()
        try:
            request = request_factory()
        except Exception:
            self._shadow_invalid_total += 1
            return primary_result
        task = asyncio.create_task(self._run_shadow_attempt(request, validator))
        self._shadow_tasks.add(task)
        task.add_done_callback(self._shadow_tasks.discard)
        return primary_result

    async def _run_shadow_attempt(
        self,
        request: ModelRequest,
        validator: Callable[[SecondaryResult], bool] | None,
    ) -> None:
        try:
            attempt = await self.attempt(request, shadow=True)
            if attempt.result is None:
                self._shadow_skipped_total += 1
            elif self._validated(attempt.result, validator):
                self._record_success(request, attempt.result)
                self._shadow_valid_total += 1
            else:
                await self._reject_valid_result(request)
                self._shadow_invalid_total += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            self._shadow_invalid_total += 1

    async def drain_shadow(self) -> None:
        """Test/operator drain for already launched shadow comparisons."""

        if self._shadow_tasks:
            await asyncio.gather(*tuple(self._shadow_tasks), return_exceptions=True)


def build_secondary_brain(
    settings: FridaySettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SecondaryBrainScheduler:
    return SecondaryBrainScheduler.from_settings(settings, transport=transport)
