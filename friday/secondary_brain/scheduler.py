"""Code-owned routing and fallback boundary for secondary advisory work."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, TypeVar

import httpx

from friday import semantic_supervisor_policy
from friday.model_input_hygiene import secondary_model_messages_are_secret_free

from .client import SecondaryEndpointClient
from .contracts import (
    ADVISORY_WORKLOADS,
    PLAN_CANDIDATE_LOCAL_FAILURES,
    SEMANTIC_SHADOW_WORKLOADS,
    EffectClass,
    ModelModality,
    ModelPriority,
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
from .profiles import (
    SecondaryProfileAdmission,
    SecondaryRuntimeAdmission,
    get_secondary_runtime_admission,
)

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
_SECONDARY_PRODUCT_CANDIDATE_PROFILE_SHA256 = (
    "51af2164fa07ff3c01813e318076f7ac8b37eeecb73e695b6ca7543061c93439"
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
        profile_admission: SecondaryProfileAdmission | None,
        document_map_mode: SecondaryMode = SecondaryMode.DISABLED,
        supervisor_mode: SecondaryMode = SecondaryMode.DISABLED,
        supervisor_admission: semantic_supervisor_policy.SupervisorPolicyAdmission | None = None,
        effect_shadow_mode: SecondaryMode = SecondaryMode.DISABLED,
        effect_shadow_admission: (
            semantic_supervisor_policy.SupervisorEffectShadowPolicyAdmission | None
        ) = None,
    ) -> None:
        self.mode = mode
        self.allow_private_text = allow_private_text
        self._client = client
        self._unavailable_state = unavailable_state
        self._profile_admission = profile_admission
        self._document_map_mode = document_map_mode
        if supervisor_admission is None:
            requested_mode = "shadow" if supervisor_mode is SecondaryMode.SHADOW else "off"
            supervisor_admission = semantic_supervisor_policy.disabled_supervisor_policy_admission(
                requested_mode=requested_mode
            )
        self._supervisor_admission = supervisor_admission
        supervisor_available = bool(
            supervisor_admission.workload_available
            and mode is not SecondaryMode.DISABLED
            and client is not None
        )
        self._supervisor_mode = SecondaryMode.SHADOW if supervisor_available else SecondaryMode.DISABLED
        if effect_shadow_admission is None:
            requested_effect_mode = "shadow" if effect_shadow_mode is SecondaryMode.SHADOW else "off"
            effect_shadow_admission = (
                semantic_supervisor_policy.disabled_supervisor_effect_shadow_policy_admission(
                    requested_mode=requested_effect_mode
                )
            )
        self._effect_shadow_admission = effect_shadow_admission
        effect_shadow_available = bool(
            effect_shadow_admission.workload_available
            and mode is not SecondaryMode.DISABLED
            and client is not None
        )
        self._effect_shadow_mode = SecondaryMode.SHADOW if effect_shadow_available else SecondaryMode.DISABLED
        generic_workloads = (allowed_workloads & ADVISORY_WORKLOADS) - SEMANTIC_SHADOW_WORKLOADS
        semantic_workloads: set[ModelWorkload] = set()
        if supervisor_available:
            semantic_workloads.add(ModelWorkload.PLAN_CANDIDATE)
        if effect_shadow_available:
            semantic_workloads.add(ModelWorkload.EFFECT_PLANNING)
        self.allowed_workloads = generic_workloads | frozenset(semantic_workloads)
        self._local_skipped_total = 0
        self._local_fallback_total = 0
        self._startup_probe_task: asyncio.Task[None] | None = None
        self._probe_lock = asyncio.Lock()
        # Ordinary attempts register here so a rare promotion witness can
        # observe one causal attempt without unrelated counter interleaving.
        self._observation_condition = asyncio.Condition()
        self._ordinary_attempts_in_flight = 0
        self._exclusive_observation = False
        # Semantic shadow work is lower priority than every pre-existing product
        # workload.  This gate makes registration/preemption atomic before the
        # shared single-concurrency endpoint is touched.
        self._plan_priority_lock = asyncio.Lock()
        self._plan_candidate_attempts: set[asyncio.Task[SecondaryAttempt] | asyncio.Task[bool]] = set()
        self._preempted_plan_attempts: set[asyncio.Task[SecondaryAttempt] | asyncio.Task[bool]] = set()
        self._non_plan_attempts_in_flight = 0
        self._epoch_admitted = False
        self._last_probe_success_monotonic: float | None = None
        self._probe_success_total = 0
        self._probe_failure_total = 0
        self._probe_failure_by_reason = {failure: 0 for failure in SecondaryFailure}
        self._shadow_tasks: set[asyncio.Task[None]] = set()
        self._selected_by_workload = {workload: 0 for workload in ModelWorkload}
        self._success_by_workload = {workload: 0 for workload in ModelWorkload}
        self._latency_sum_by_workload = {workload: 0.0 for workload in ModelWorkload}
        self._latency_max_by_workload = {workload: 0.0 for workload in ModelWorkload}
        self._queue_wait_count_by_workload = {workload: 0 for workload in ModelWorkload}
        self._queue_wait_sum_by_workload = {workload: 0.0 for workload in ModelWorkload}
        self._queue_wait_max_by_workload = {workload: 0.0 for workload in ModelWorkload}
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

    @property
    def advisory_profile_limits(self) -> tuple[int, int] | None:
        """Expose the admitted non-secret context/output limits to prompt builders."""

        if self._client is None:
            return None
        return (
            self._client.config.max_context_tokens,
            self._client.config.max_output_tokens,
        )

    def new_advisory_deadline(self) -> float:
        """Return one end-to-end optional budget, including all admission probes."""

        budget = self._client.config.call_budget_sec if self._client is not None else 0.001
        return time.monotonic() + min(30.0, budget)

    def workload_mode(self, workload: ModelWorkload) -> SecondaryMode:
        """Return the code-owned rollout mode for one advisory workload."""

        if workload is ModelWorkload.DOCUMENT_MAP:
            if self.mode is SecondaryMode.DISABLED or self._client is None:
                return SecondaryMode.DISABLED
            return self._document_map_mode
        if workload is ModelWorkload.PLAN_CANDIDATE:
            if self.mode is SecondaryMode.DISABLED or self._client is None:
                return SecondaryMode.DISABLED
            return self._supervisor_mode
        if workload is ModelWorkload.EFFECT_PLANNING:
            if self.mode is SecondaryMode.DISABLED or self._client is None:
                return SecondaryMode.DISABLED
            return self._effect_shadow_mode
        return self.mode

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
        try:
            document_map_mode = SecondaryMode(settings.secondary_llm_document_map_mode)
        except ValueError:
            document_map_mode = SecondaryMode.DISABLED
        requested_supervisor = getattr(settings, "semantic_supervisor_mode", "off")
        requested_effect_shadow = getattr(settings, "semantic_supervisor_effect_mode", "off")

        def supervisor_admission(
            runtime_state: str,
            runtime_admission: SecondaryRuntimeAdmission | None,
        ) -> semantic_supervisor_policy.SupervisorPolicyAdmission:
            profile = runtime_admission.profile if runtime_admission is not None else None
            return semantic_supervisor_policy.evaluate_supervisor_policy_admission(
                requested_mode=requested_supervisor,
                task_allowlist=getattr(settings, "semantic_supervisor_tasks", ()),
                max_steps=getattr(settings, "semantic_supervisor_max_steps", 6),
                max_review_rounds=getattr(
                    settings,
                    "semantic_supervisor_max_review_rounds",
                    1,
                ),
                timeout_sec=getattr(settings, "semantic_supervisor_timeout_sec", 12.0),
                allow_private_text=settings.secondary_llm_allow_private_text,
                secondary_runtime_state=runtime_state,
                profile_admission=(runtime_admission.kind.value if runtime_admission is not None else ""),
                runtime_profile_id=profile.profile_id if profile is not None else "",
                runtime_profile_manifest_sha256=(profile.manifest_sha256 if profile is not None else ""),
            )

        def effect_shadow_admission(
            runtime_state: str,
            runtime_admission: SecondaryRuntimeAdmission | None,
        ) -> semantic_supervisor_policy.SupervisorEffectShadowPolicyAdmission:
            profile = runtime_admission.profile if runtime_admission is not None else None
            return semantic_supervisor_policy.evaluate_supervisor_effect_shadow_policy_admission(
                requested_mode=requested_effect_shadow,
                allow_private_text=settings.secondary_llm_allow_private_text,
                secondary_runtime_state=runtime_state,
                profile_admission=(runtime_admission.kind.value if runtime_admission is not None else ""),
                runtime_profile_id=profile.profile_id if profile is not None else "",
                runtime_profile_manifest_sha256=(profile.manifest_sha256 if profile is not None else ""),
            )

        workloads: set[ModelWorkload] = set()
        generic_workload_names: list[str] = []
        for raw_workload in settings.secondary_llm_workloads:
            normalized_workload = raw_workload.strip().casefold()
            generic_workload_names.append(normalized_workload)
            try:
                workload = ModelWorkload(normalized_workload)
            except ValueError:
                continue
            if workload in SEMANTIC_SHADOW_WORKLOADS:
                continue
            workloads.add(workload)

        if not settings.secondary_llm_enabled or mode is SecondaryMode.DISABLED:
            return cls(
                mode=SecondaryMode.DISABLED,
                allowed_workloads=frozenset(workloads),
                allow_private_text=settings.secondary_llm_allow_private_text,
                client=None,
                unavailable_state=SecondaryState.DISABLED,
                profile_admission=None,
                document_map_mode=document_map_mode,
                supervisor_admission=supervisor_admission("disabled", None),
                effect_shadow_admission=effect_shadow_admission("disabled", None),
            )

        admission = get_secondary_runtime_admission(
            settings.secondary_llm_profile,
            mode=mode.value,
        )
        profile = admission.profile if admission is not None else None
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
        # Endpoint/profile admission is independent of ENV workload selection.
        # Use one runtime-certified neutral workload to validate the unchanged
        # transport identity without granting it to a semantic-only scheduler.
        base_workload_name = min(
            (
                profile.allowed_workloads
                - {
                    ModelWorkload.DOCUMENT_MAP.value,
                    ModelWorkload.PLAN_CANDIDATE.value,
                    ModelWorkload.EFFECT_PLANNING.value,
                }
            )
            if profile is not None
            else (),
            default="",
        )
        base_configuration_is_admissible = secondary_configuration_is_admissible(
            endpoint,
            primary_base_url=settings.llm_base_url,
            primary_model=settings.llm_model,
            primary_timeout_sec=settings.llm_timeout_sec,
            workload_names=(base_workload_name,),
            mode=mode.value,
            allow_private_text=settings.secondary_llm_allow_private_text,
            document_map_mode=SecondaryMode.DISABLED.value,
        )
        generic_configuration_is_admissible = bool(
            effective_workloads
            and secondary_configuration_is_admissible(
                endpoint,
                primary_base_url=settings.llm_base_url,
                primary_model=settings.llm_model,
                primary_timeout_sec=settings.llm_timeout_sec,
                workload_names=generic_workload_names,
                mode=mode.value,
                allow_private_text=settings.secondary_llm_allow_private_text,
                document_map_mode=document_map_mode.value,
            )
        )
        candidate_supervisor_admission = supervisor_admission("configured", admission)
        candidate_effect_shadow_admission = effect_shadow_admission("configured", admission)
        admitted_workloads = effective_workloads if generic_configuration_is_admissible else frozenset()
        if (
            admission is None
            or not base_configuration_is_admissible
            or not (
                generic_configuration_is_admissible
                or candidate_supervisor_admission.workload_available
                or candidate_effect_shadow_admission.workload_available
            )
        ):
            return cls(
                mode=mode,
                allowed_workloads=admitted_workloads,
                allow_private_text=settings.secondary_llm_allow_private_text,
                client=None,
                unavailable_state=SecondaryState.MISCONFIGURED,
                profile_admission=None,
                document_map_mode=document_map_mode,
                supervisor_admission=supervisor_admission("misconfigured", admission),
                effect_shadow_admission=effect_shadow_admission("misconfigured", admission),
            )
        try:
            client = SecondaryEndpointClient(
                endpoint,
                admission=admission,
                transport=transport,
            )
        except Exception:
            # Optional TLS/transport construction (including an invalid CA file)
            # may never turn into a primary startup failure or retain its raw error.
            return cls(
                mode=mode,
                allowed_workloads=admitted_workloads,
                allow_private_text=settings.secondary_llm_allow_private_text,
                client=None,
                unavailable_state=SecondaryState.MISCONFIGURED,
                profile_admission=None,
                document_map_mode=document_map_mode,
                supervisor_admission=supervisor_admission("misconfigured", admission),
                effect_shadow_admission=effect_shadow_admission("misconfigured", admission),
            )
        return cls(
            mode=mode,
            allowed_workloads=admitted_workloads,
            allow_private_text=settings.secondary_llm_allow_private_text,
            client=client,
            unavailable_state=SecondaryState.PROBING,
            profile_admission=admission.kind,
            document_map_mode=document_map_mode,
            supervisor_admission=candidate_supervisor_admission,
            effect_shadow_admission=candidate_effect_shadow_admission,
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

        if (
            self._closed
            or self._client is None
            or self._startup_probe_task is not None
            or self._exclusive_observation
        ):
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
        *,
        workload: ModelWorkload | None = None,
    ) -> tuple[SecondaryFailure | None, float]:
        """Demand-probe one process epoch without queueing behind another probe."""

        if self._client is None:
            return SecondaryFailure.MISCONFIGURED, 0.0
        if self._admission_is_fresh():
            return None, 0.0
        lock_started = time.monotonic()
        try:
            await asyncio.wait_for(
                self._probe_lock.acquire(),
                timeout=self._client.config.admission_timeout_sec,
            )
        except asyncio.CancelledError:
            if workload is not None:
                self._record_queue_wait(workload, time.monotonic() - lock_started)
            raise
        except TimeoutError:
            self._probe_failure_total += 1
            self._probe_failure_by_reason[SecondaryFailure.ADMISSION_BUSY] += 1
            return SecondaryFailure.ADMISSION_BUSY, max(0.0, time.monotonic() - lock_started)
        lock_wait_sec = max(0.0, time.monotonic() - lock_started)
        try:
            if self._admission_is_fresh():
                return None, lock_wait_sec
            failure = await self._client.probe_models(
                absolute_deadline_monotonic=absolute_deadline_monotonic,
                cancellation_is_local=workload in SEMANTIC_SHADOW_WORKLOADS,
            )
            if failure is not None:
                self._epoch_admitted = False
                self._probe_failure_total += 1
                self._probe_failure_by_reason[failure] += 1
                return failure, lock_wait_sec
            self._last_probe_success_monotonic = time.monotonic()
            # The expensive generation canary is once per admitted process epoch.
            # A stale health window needs only the exact inventory probe; the
            # following typed real call becomes the fresh generation signal.
            if self._epoch_admitted:
                self._probe_success_total += 1
                return None, lock_wait_sec
            canary = ModelRequest(
                workload=ModelWorkload.VERIFY,
                messages=(
                    {
                        "role": "system",
                        "content": "Return final content only. Never use tools.",
                    },
                    {"role": "user", "content": "Reply with exactly: ready"},
                ),
                # GPT-OSS reasons before its final channel.  A 16-token cap can
                # return HTTP 200 with only a length stop and no usable final.
                max_output_tokens=min(256, self._client.config.max_output_tokens),
                absolute_deadline_monotonic=absolute_deadline_monotonic,
            )
            attempt = await self._client.call(
                canary,
                cancellation_is_local=workload in SEMANTIC_SHADOW_WORKLOADS,
            )
            if attempt.result is None:
                self._probe_failure_total += 1
                failure = attempt.failure or SecondaryFailure.MALFORMED_RESPONSE
                self._probe_failure_by_reason[failure] += 1
                return failure, lock_wait_sec
            if attempt.result.visible_content.strip().casefold() != "ready":
                await self._client.invalidate(SecondaryFailure.MALFORMED_RESPONSE)
                self._probe_failure_total += 1
                self._probe_failure_by_reason[SecondaryFailure.MALFORMED_RESPONSE] += 1
                return SecondaryFailure.MALFORMED_RESPONSE, lock_wait_sec
            self._epoch_admitted = True
            self._last_probe_success_monotonic = time.monotonic()
            self._probe_success_total += 1
            return None, lock_wait_sec
        except asyncio.CancelledError:
            if workload is not None:
                self._record_queue_wait(workload, lock_wait_sec)
            raise
        finally:
            self._probe_lock.release()

    async def refresh_semantic_supervisor_runtime_admission(
        self,
        *,
        absolute_deadline_monotonic: float,
    ) -> bool:
        """Refresh only the content-free plan admission at lowest priority."""

        if (
            isinstance(absolute_deadline_monotonic, bool)
            or not isinstance(absolute_deadline_monotonic, int | float)
            or not math.isfinite(float(absolute_deadline_monotonic))
            or float(absolute_deadline_monotonic) <= time.monotonic()
            or self._closed
            or self._client is None
            or ModelWorkload.PLAN_CANDIDATE not in self.allowed_workloads
            or self.workload_mode(ModelWorkload.PLAN_CANDIDATE) is not SecondaryMode.SHADOW
        ):
            return False
        deadline = float(absolute_deadline_monotonic)
        try:
            async with asyncio.timeout(deadline - time.monotonic()):
                return await self._refresh_semantic_supervisor_runtime_admission_bounded(deadline)
        except TimeoutError:
            self._record_skip(ModelWorkload.PLAN_CANDIDATE, SecondaryFailure.DEADLINE, local=True)
            return False

    async def _refresh_semantic_supervisor_runtime_admission_bounded(
        self,
        absolute_deadline_monotonic: float,
    ) -> bool:
        outer = asyncio.current_task()
        if outer is None:
            self._record_skip(
                ModelWorkload.PLAN_CANDIDATE,
                SecondaryFailure.ADMISSION_BUSY,
                local=True,
            )
            return False
        async with self._plan_priority_lock:
            if self._non_plan_attempts_in_flight:
                self._record_skip(
                    ModelWorkload.PLAN_CANDIDATE,
                    SecondaryFailure.ADMISSION_BUSY,
                    local=True,
                )
                return False
            runner = asyncio.create_task(
                self._refresh_semantic_supervisor_runtime_admission_observed(absolute_deadline_monotonic)
            )
            self._plan_candidate_attempts.add(runner)
        try:
            return await runner
        except asyncio.CancelledError:
            if outer.cancelling():
                await asyncio.gather(runner, return_exceptions=True)
                raise
            self._record_skip(
                ModelWorkload.PLAN_CANDIDATE,
                SecondaryFailure.CANCELLED,
                local=True,
            )
            return False
        finally:
            self._plan_candidate_attempts.discard(runner)
            self._preempted_plan_attempts.discard(runner)

    async def _refresh_semantic_supervisor_runtime_admission_observed(
        self,
        absolute_deadline_monotonic: float,
    ) -> bool:
        async with self._observation_condition:
            if self._exclusive_observation:
                self._record_skip(
                    ModelWorkload.PLAN_CANDIDATE,
                    SecondaryFailure.ADMISSION_BUSY,
                    local=True,
                )
                return False
            self._ordinary_attempts_in_flight += 1
        try:
            return await self._refresh_semantic_supervisor_runtime_admission_unobserved(
                absolute_deadline_monotonic
            )
        finally:
            async with self._observation_condition:
                self._ordinary_attempts_in_flight -= 1
                self._observation_condition.notify_all()

    async def _refresh_semantic_supervisor_runtime_admission_unobserved(
        self,
        absolute_deadline_monotonic: float,
    ) -> bool:
        failure, queue_wait_sec = await self._ensure_epoch_admitted(
            absolute_deadline_monotonic,
            workload=ModelWorkload.PLAN_CANDIDATE,
        )
        self._record_queue_wait(ModelWorkload.PLAN_CANDIDATE, queue_wait_sec)
        if failure is None:
            return True
        self._record_skip(ModelWorkload.PLAN_CANDIDATE, failure, local=True)
        return False

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

    def _supervisor_status(self, status: SecondaryStatus) -> dict[str, object]:
        workload_available = bool(
            self._supervisor_admission.workload_available
            and ModelWorkload.PLAN_CANDIDATE in self.allowed_workloads
            and self.workload_mode(ModelWorkload.PLAN_CANDIDATE) is SecondaryMode.SHADOW
        )
        closed_reason = self._supervisor_admission.closed_reason
        if self._supervisor_admission.workload_available and not workload_available:
            closed_reason = semantic_supervisor_policy.SupervisorPolicyClosedReason.SECONDARY_MISCONFIGURED
        return {
            "workload": ModelWorkload.PLAN_CANDIDATE.value,
            "requested_mode": self._supervisor_admission.requested_mode,
            "effective_mode": self._supervisor_admission.effective_mode,
            "policy_id": self._supervisor_admission.policy_id,
            "policy_sha256": self._supervisor_admission.policy_sha256,
            "workload_available": workload_available,
            "runtime_available": bool(
                workload_available and status.state is SecondaryState.HEALTHY and self._admission_is_fresh()
            ),
            "closed_reason": closed_reason.value,
        }

    def _effect_shadow_status(self, status: SecondaryStatus) -> dict[str, object]:
        workload_available = bool(
            self._effect_shadow_admission.workload_available
            and ModelWorkload.EFFECT_PLANNING in self.allowed_workloads
            and self.workload_mode(ModelWorkload.EFFECT_PLANNING) is SecondaryMode.SHADOW
        )
        closed_reason = self._effect_shadow_admission.closed_reason
        if self._effect_shadow_admission.workload_available and not workload_available:
            closed_reason = semantic_supervisor_policy.SupervisorPolicyClosedReason.SECONDARY_MISCONFIGURED
        return {
            "workload": ModelWorkload.EFFECT_PLANNING.value,
            "requested_mode": self._effect_shadow_admission.requested_mode,
            "effective_mode": self._effect_shadow_admission.effective_mode,
            "policy_id": self._effect_shadow_admission.policy_id,
            "policy_sha256": self._effect_shadow_admission.policy_sha256,
            "workload_available": workload_available,
            "runtime_available": bool(
                workload_available and status.state is SecondaryState.HEALTHY and self._admission_is_fresh()
            ),
            "closed_reason": closed_reason.value,
        }

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
            "semantic_supervisor": self._supervisor_status(status),
            "effect_shadow": self._effect_shadow_status(status),
        }

    def diagnostics_status(self) -> dict[str, object]:
        """Return bounded owner diagnostics without endpoint or content data."""

        status = self.status()
        supervisor_status = self._supervisor_status(status)
        effect_shadow_status = self._effect_shadow_status(status)
        return {
            **self.public_status(),
            "last_failure": status.last_failure.value if status.last_failure is not None else None,
            "active_requests": status.active_requests,
            "selected_total": sum(self._selected_by_workload.values()),
            "success_total": sum(self._success_by_workload.values()),
            "endpoint_admission_total": status.selected_total,
            "endpoint_request_total": status.endpoint_request_total,
            "endpoint_success_total": status.endpoint_success_total,
            "skipped_total": status.skipped_total,
            "primary_fallback_total": status.fallback_total,
            "context_cap_tokens": status.context_cap_tokens,
            "served_model_match": status.served_model_match,
            "profile": self._client.config.profile_id if self._client is not None else "",
            "profile_admission": (
                self._profile_admission.value if self._profile_admission is not None else ""
            ),
            "profile_manifest_match": status.profile_manifest_match,
            "last_success_age_sec": (
                round(status.last_success_age_sec, 3) if status.last_success_age_sec is not None else None
            ),
            "circuit_retry_after_sec": round(status.cooldown_retry_after_sec, 3),
            "probe_success_total": self._probe_success_total,
            "probe_failure_total": self._probe_failure_total,
            "model_inventory_probe_success_total": status.probe_success_total,
            "model_inventory_probe_failure_total": status.probe_failure_total,
            "probe_failure_reasons": {
                reason.value: count for reason, count in self._probe_failure_by_reason.items() if count
            },
            "protocol_rejection_total": status.protocol_rejection_total,
            "protocol_rejection_reasons": {
                reason.value: count
                for reason, count in sorted(
                    (self._client.protocol_rejection_counts().items() if self._client is not None else ()),
                    key=lambda item: item[0].value,
                )
                if count
            },
            "queue_wait": {
                "count": status.queue_wait_count,
                "sum_sec": round(status.queue_wait_sum_sec, 6),
                "max_sec": round(status.queue_wait_max_sec, 6),
            },
            "workloads": {
                workload.value: {
                    "routing_mode": self.workload_mode(workload).value,
                    **(
                        {
                            "available": supervisor_status["workload_available"],
                            "closed_reason": supervisor_status["closed_reason"],
                        }
                        if workload is ModelWorkload.PLAN_CANDIDATE
                        else (
                            {
                                "available": effect_shadow_status["workload_available"],
                                "closed_reason": effect_shadow_status["closed_reason"],
                            }
                            if workload is ModelWorkload.EFFECT_PLANNING
                            else {}
                        )
                    ),
                    "selected_total": self._selected_by_workload[workload],
                    "success_total": self._success_by_workload[workload],
                    "latency_count": self._success_by_workload[workload],
                    "latency_sum_sec": round(self._latency_sum_by_workload[workload], 6),
                    "latency_max_sec": round(self._latency_max_by_workload[workload], 6),
                    "queue_wait_count": self._queue_wait_count_by_workload[workload],
                    "queue_wait_sum_sec": round(self._queue_wait_sum_by_workload[workload], 6),
                    "queue_wait_max_sec": round(self._queue_wait_max_by_workload[workload], 6),
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
                for workload in sorted(
                    self.allowed_workloads | SEMANTIC_SHADOW_WORKLOADS,
                    key=lambda value: value.value,
                )
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

    def product_attestation_identity(self) -> dict[str, object]:
        """Return the exact non-secret runtime identity used by product attestations."""

        if self._client is None or self._profile_admission is None:
            return {}
        config = self._client.config
        return {
            "candidate_profile_id": config.profile_id,
            "candidate_profile_mode": self.mode.value,
            "candidate_profile_allow_private_text": self.allow_private_text,
            "candidate_profile_context_tokens": config.max_context_tokens,
            "candidate_profile_sha256": _SECONDARY_PRODUCT_CANDIDATE_PROFILE_SHA256,
            "candidate_profile_manifest_sha256": config.profile_manifest_sha256,
            "candidate_profile_admission": self._profile_admission.value,
            "served_model_alias": config.served_model_alias,
            "gateway_ca_certificate_sha256": config.ca_sha256,
        }

    def _eligibility_failure(
        self,
        request: ModelRequest,
        *,
        shadow: bool,
    ) -> SecondaryFailure | None:
        required_mode = SecondaryMode.SHADOW if shadow else SecondaryMode.ASSIST
        workload_mode = self.workload_mode(request.workload)
        if workload_mode is not required_mode:
            return (
                SecondaryFailure.DISABLED
                if workload_mode is SecondaryMode.DISABLED
                else SecondaryFailure.MODE_DISALLOWED
            )
        if self._client is None:
            return SecondaryFailure.MISCONFIGURED
        if self._profile_admission is SecondaryProfileAdmission.PROVISIONAL_SHADOW:
            if not shadow:
                return SecondaryFailure.MODE_DISALLOWED
            if (
                request.workload is not ModelWorkload.EXTRACT
                or request.priority is not ModelPriority.BACKGROUND
                or not request.require_structured_output
            ):
                return SecondaryFailure.WORKLOAD_DISALLOWED
            if request.contains_private_text:
                return SecondaryFailure.PRIVATE_TEXT_DISALLOWED
            if request.effect_class is not EffectClass.NONE:
                return SecondaryFailure.EFFECT_DENIED
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

    async def attempt(
        self,
        request: ModelRequest,
        *,
        shadow: bool = False,
        pre_dispatch_validator: Callable[[], bool] | None = None,
        dispatch_observer: Callable[[], None] | None = None,
    ) -> SecondaryAttempt:
        """Run one attempt; semantic plans yield to every existing workload."""

        if request.workload in SEMANTIC_SHADOW_WORKLOADS:
            if dispatch_observer is None:
                return await self._attempt_lowest_priority_plan(
                    request,
                    shadow=shadow,
                    pre_dispatch_validator=pre_dispatch_validator,
                )
            return await self._attempt_lowest_priority_plan(
                request,
                shadow=shadow,
                pre_dispatch_validator=pre_dispatch_validator,
                dispatch_observer=dispatch_observer,
            )
        if pre_dispatch_validator is not None:
            self._record_skip(request.workload, SecondaryFailure.WORKLOAD_DISALLOWED, local=True)
            return SecondaryAttempt.rejected(SecondaryFailure.WORKLOAD_DISALLOWED)
        return await self._attempt_preempting_plans(request, shadow=shadow)

    async def _attempt_lowest_priority_plan(
        self,
        request: ModelRequest,
        *,
        shadow: bool,
        pre_dispatch_validator: Callable[[], bool] | None,
        dispatch_observer: Callable[[], None] | None = None,
    ) -> SecondaryAttempt:
        outer = asyncio.current_task()
        if outer is None:
            self._record_skip(request.workload, SecondaryFailure.ADMISSION_BUSY, local=True)
            return SecondaryAttempt.rejected(SecondaryFailure.ADMISSION_BUSY)
        async with self._plan_priority_lock:
            if self._non_plan_attempts_in_flight:
                self._record_skip(request.workload, SecondaryFailure.ADMISSION_BUSY, local=True)
                return SecondaryAttempt.rejected(SecondaryFailure.ADMISSION_BUSY)
            if dispatch_observer is None:
                observed = self._attempt_observed(
                    request,
                    shadow=shadow,
                    pre_dispatch_validator=pre_dispatch_validator,
                )
            else:
                observed = self._attempt_observed(
                    request,
                    shadow=shadow,
                    pre_dispatch_validator=pre_dispatch_validator,
                    dispatch_observer=dispatch_observer,
                )
            runner = asyncio.create_task(observed)
            self._plan_candidate_attempts.add(runner)
        try:
            return await runner
        except asyncio.CancelledError:
            if outer.cancelling():
                await asyncio.gather(runner, return_exceptions=True)
                raise
            self._record_skip(request.workload, SecondaryFailure.CANCELLED, local=True)
            return SecondaryAttempt.rejected(SecondaryFailure.CANCELLED)
        finally:
            # These bookkeeping mutations contain no suspension point and are
            # atomic on this scheduler's event loop.  Keeping cleanup synchronous
            # prevents repeated cancellation from stranding a priority marker.
            self._plan_candidate_attempts.discard(runner)
            self._preempted_plan_attempts.discard(runner)

    async def _attempt_preempting_plans(
        self,
        request: ModelRequest,
        *,
        shadow: bool,
    ) -> SecondaryAttempt:
        async with self._plan_priority_lock:
            self._non_plan_attempts_in_flight += 1
            displaced = tuple(self._plan_candidate_attempts)
            newly_displaced = tuple(task for task in displaced if task not in self._preempted_plan_attempts)
            self._preempted_plan_attempts.update(newly_displaced)
            for task in newly_displaced:
                task.cancel()
        try:
            if displaced:
                await asyncio.gather(*displaced, return_exceptions=True)
            return await self._attempt_observed(
                request,
                shadow=shadow,
                pre_dispatch_validator=None,
            )
        finally:
            self._non_plan_attempts_in_flight -= 1

    async def _attempt_observed(
        self,
        request: ModelRequest,
        *,
        shadow: bool,
        pre_dispatch_validator: Callable[[], bool] | None,
        dispatch_observer: Callable[[], None] | None = None,
    ) -> SecondaryAttempt:
        """Respect the causal-observation barrier after priority admission."""

        async with self._observation_condition:
            if self._exclusive_observation:
                # Evidence never queues product traffic behind a laptop call.
                # The caller immediately takes its ordinary fail-soft path; the
                # resulting skip also invalidates the supposedly isolated proof.
                self._record_skip(request.workload, SecondaryFailure.ADMISSION_BUSY, local=True)
                return SecondaryAttempt.rejected(SecondaryFailure.ADMISSION_BUSY)
            self._ordinary_attempts_in_flight += 1
        try:
            if dispatch_observer is None:
                return await self._attempt_unobserved(
                    request,
                    shadow=shadow,
                    pre_dispatch_validator=pre_dispatch_validator,
                )
            return await self._attempt_unobserved(
                request,
                shadow=shadow,
                pre_dispatch_validator=pre_dispatch_validator,
                dispatch_observer=dispatch_observer,
            )
        finally:
            async with self._observation_condition:
                self._ordinary_attempts_in_flight -= 1
                self._observation_condition.notify_all()

    async def _attempt_unobserved(
        self,
        request: ModelRequest,
        *,
        shadow: bool = False,
        pre_dispatch_validator: Callable[[], bool] | None = None,
        dispatch_observer: Callable[[], None] | None = None,
    ) -> SecondaryAttempt:
        failure = self._eligibility_failure(request, shadow=shadow)
        if failure is not None:
            self._record_skip(request.workload, failure, local=True)
            return SecondaryAttempt.rejected(failure)
        assert self._client is not None
        admission_failure, probe_queue_wait_sec = await self._ensure_epoch_admitted(
            request.absolute_deadline_monotonic,
            workload=request.workload,
        )
        if admission_failure is not None:
            self._record_queue_wait(request.workload, probe_queue_wait_sec)
            self._record_skip(request.workload, admission_failure, local=True)
            return SecondaryAttempt.rejected(admission_failure)
        self._selected_by_workload[request.workload] += 1
        self._record_queue_wait(request.workload, probe_queue_wait_sec)
        try:
            if pre_dispatch_validator is None and dispatch_observer is None:
                attempt = await self._client.call(request)
            elif dispatch_observer is None:
                attempt = await self._client.call(
                    request,
                    pre_dispatch_validator=pre_dispatch_validator,
                )
            else:
                attempt = await self._client.call(
                    request,
                    pre_dispatch_validator=pre_dispatch_validator,
                    dispatch_observer=dispatch_observer,
                )
        except Exception:
            # The secondary is never an availability dependency.  Cancellation
            # still propagates because asyncio.CancelledError is a BaseException.
            self._record_skip(request.workload, SecondaryFailure.CONNECT_FAILED, local=True)
            self._epoch_admitted = False
            return SecondaryAttempt.rejected(SecondaryFailure.CONNECT_FAILED)
        self._extend_queue_wait(
            request.workload,
            additional_sec=attempt.queue_wait_sec,
            combined_sec=probe_queue_wait_sec + attempt.queue_wait_sec,
        )
        if attempt.result is not None:
            # Protocol-valid transport is a health signal, but it becomes a
            # workload success only after the caller's typed validator accepts it.
            self._last_probe_success_monotonic = time.monotonic()
            return attempt
        attempt_failure = attempt.failure or SecondaryFailure.MALFORMED_RESPONSE
        self._record_skip(request.workload, attempt_failure)
        if attempt_failure in _READMISSION_FAILURES and not (
            request.workload in SEMANTIC_SHADOW_WORKLOADS and attempt_failure in PLAN_CANDIDATE_LOCAL_FAILURES
        ):
            self._epoch_admitted = False
        return attempt

    def _record_queue_wait(self, workload: ModelWorkload, waited_sec: float) -> None:
        waited = max(0.0, waited_sec)
        self._queue_wait_count_by_workload[workload] += 1
        self._queue_wait_sum_by_workload[workload] += waited
        self._queue_wait_max_by_workload[workload] = max(
            self._queue_wait_max_by_workload[workload],
            waited,
        )

    def _extend_queue_wait(
        self,
        workload: ModelWorkload,
        *,
        additional_sec: float,
        combined_sec: float,
    ) -> None:
        self._queue_wait_sum_by_workload[workload] += max(0.0, additional_sec)
        self._queue_wait_max_by_workload[workload] = max(
            self._queue_wait_max_by_workload[workload],
            max(0.0, combined_sec),
        )

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

        return await self._secondary_preferred_required_result_observed_call(
            request,
            primary_fallback,
            validator=validator,
        )

    async def secondary_preferred_required_result_observed(
        self,
        request: ModelRequest,
        primary_fallback: Callable[[], Awaitable[T]],
        *,
        validator: Callable[[SecondaryResult], bool] | None = None,
    ) -> tuple[SecondaryResult | T, dict[str, object], dict[str, object]]:
        """Return one result plus snapshots bounded around that decision.

        This does not queue normal traffic.  Concurrent counter movement remains
        visible, so the external product oracle rejects the observation closed.
        """

        before = self.diagnostics_status()
        result = await self._secondary_preferred_required_result_observed_call(
            request,
            primary_fallback,
            validator=validator,
        )
        after = self.diagnostics_status()
        return result, before, after

    async def _secondary_preferred_required_result_observed_call(
        self,
        request: ModelRequest,
        primary_fallback: Callable[[], Awaitable[T]],
        *,
        validator: Callable[[SecondaryResult], bool] | None = None,
    ) -> SecondaryResult | T:
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

    async def evaluate_shadow(
        self,
        request: ModelRequest,
        *,
        validator: Callable[[SecondaryResult], bool] | None = None,
        invalidate_on_rejection: bool = True,
        pre_dispatch_validator: Callable[[], bool] | None = None,
        dispatch_observer: Callable[[], None] | None = None,
    ) -> SecondaryAttempt:
        """Evaluate and account for exactly one discarded advisory attempt."""

        try:
            if pre_dispatch_validator is None and dispatch_observer is None:
                attempt = await self.attempt(request, shadow=True)
            elif pre_dispatch_validator is None:
                attempt = await self.attempt(
                    request,
                    shadow=True,
                    dispatch_observer=dispatch_observer,
                )
            else:
                attempt = await self.attempt(
                    request,
                    shadow=True,
                    pre_dispatch_validator=pre_dispatch_validator,
                    dispatch_observer=dispatch_observer,
                )
            if attempt.result is None:
                self._shadow_skipped_total += 1
                return attempt
            if self._validated(attempt.result, validator):
                self._record_success(request, attempt.result)
                self._shadow_valid_total += 1
                return attempt
            if invalidate_on_rejection:
                await self._reject_valid_result(request)
            else:
                # A schema-valid transport response that fails an advisory
                # product policy is model-quality evidence, not an endpoint or
                # shared-runtime failure.  Keep other accepted workloads out of
                # the semantic supervisor's quality circuit.
                self._record_skip(request.workload, SecondaryFailure.MALFORMED_RESPONSE)
            self._shadow_invalid_total += 1
            return SecondaryAttempt.rejected(
                SecondaryFailure.MALFORMED_RESPONSE,
                queue_wait_sec=attempt.queue_wait_sec,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Runtime wrappers need a closed result even if an optional adapter,
            # validator or invalidation hook fails unexpectedly.
            self._shadow_invalid_total += 1
            return SecondaryAttempt.rejected(SecondaryFailure.MALFORMED_RESPONSE)

    async def run_shadow(
        self,
        request_factory: Callable[[], ModelRequest],
        primary: Callable[[], Awaitable[T]],
        *,
        validator: Callable[[SecondaryResult], bool] | None = None,
        valid_result_observer: Callable[[ModelRequest, SecondaryResult], Awaitable[None]] | None = None,
    ) -> T:
        """Return primary first; evaluate and discard shadow work asynchronously."""

        primary_result = await primary()
        try:
            request = request_factory()
        except Exception:
            self._shadow_invalid_total += 1
            return primary_result
        task = asyncio.create_task(
            self._run_shadow_attempt(
                request,
                validator,
                valid_result_observer=valid_result_observer,
            )
        )
        self._shadow_tasks.add(task)
        task.add_done_callback(self._shadow_tasks.discard)
        return primary_result

    async def run_shadow_observed(
        self,
        request_factory: Callable[[], ModelRequest],
        primary: Callable[[], Awaitable[T]],
        *,
        validator: Callable[[SecondaryResult], bool] | None = None,
        valid_result_observer: Callable[[ModelRequest, SecondaryResult], Awaitable[None]] | None = None,
        exclusive: bool = False,
    ) -> tuple[T, dict[str, object], dict[str, object]]:
        """Return primary plus snapshots around one synchronously drained shadow."""

        if not exclusive:
            primary_result = await primary()
            try:
                request = request_factory()
            except Exception:
                before = self.diagnostics_status()
                self._shadow_invalid_total += 1
                after = self.diagnostics_status()
                return primary_result, before, after
            before = self.diagnostics_status()
            await self._run_shadow_attempt(
                request,
                validator,
                valid_result_observer=valid_result_observer,
            )
            after = self.diagnostics_status()
            return primary_result, before, after

        async with self._observation_condition:
            startup_probe_active = (
                self._startup_probe_task is not None and not self._startup_probe_task.done()
            )
            if (
                self._exclusive_observation
                or self._ordinary_attempts_in_flight
                or self._shadow_tasks
                or startup_probe_active
            ):
                raise RuntimeError("secondary shadow observation is not idle")
            self._exclusive_observation = True
        try:
            primary_result = await primary()
            request = request_factory()
            before = self.diagnostics_status()
            await self._run_shadow_attempt(
                request,
                validator,
                valid_result_observer=valid_result_observer,
                observation_owner=True,
            )
            after = self.diagnostics_status()
            return primary_result, before, after
        finally:
            async with self._observation_condition:
                self._exclusive_observation = False
                self._observation_condition.notify_all()

    async def _run_shadow_attempt(
        self,
        request: ModelRequest,
        validator: Callable[[SecondaryResult], bool] | None,
        *,
        valid_result_observer: Callable[[ModelRequest, SecondaryResult], Awaitable[None]] | None = None,
        observation_owner: bool = False,
    ) -> None:
        try:
            if not observation_owner:
                attempt = await self.evaluate_shadow(request, validator=validator)
                if attempt.result is not None and valid_result_observer is not None:
                    try:
                        await valid_result_observer(request, attempt.result)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Evidence is optional to product traffic. A private
                        # receipt write must never alter the already-returned
                        # primary result or poison the secondary circuit.
                        pass
                return

            attempt = await self._attempt_unobserved(request, shadow=True)
            if attempt.result is None:
                self._shadow_skipped_total += 1
            elif self._validated(attempt.result, validator):
                self._record_success(request, attempt.result)
                self._shadow_valid_total += 1
                if valid_result_observer is not None:
                    try:
                        await valid_result_observer(request, attempt.result)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        raise
            else:
                await self._reject_valid_result(request)
                self._shadow_invalid_total += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            self._shadow_invalid_total += 1
            if observation_owner:
                raise

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
