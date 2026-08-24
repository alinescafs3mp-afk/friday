"""Code-owned routing and fallback boundary for secondary advisory work."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, TypeVar

import httpx

from .client import SecondaryEndpointClient
from .contracts import (
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
)

if TYPE_CHECKING:
    from friday.config import FridaySettings

T = TypeVar("T")

_ADVISORY_WORKLOADS = frozenset(
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
        self.allowed_workloads = allowed_workloads & _ADVISORY_WORKLOADS
        self.allow_private_text = allow_private_text
        self._client = client
        self._unavailable_state = unavailable_state
        self._local_skipped_total = 0
        self._local_fallback_total = 0

    @property
    def client(self) -> SecondaryEndpointClient | None:
        return self._client

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

        endpoint = SecondaryEndpointConfig(
            base_url=settings.secondary_llm_base_url,
            served_model_alias=settings.secondary_llm_model,
            api_key=settings.secondary_llm_api_key,
            ca_file=settings.secondary_llm_ca_file,
            connect_timeout_sec=settings.secondary_llm_connect_timeout_sec,
            read_timeout_sec=settings.secondary_llm_read_timeout_sec,
            call_budget_sec=settings.secondary_llm_call_budget_sec,
            admission_timeout_sec=settings.secondary_llm_admission_timeout_sec,
            health_interval_sec=settings.secondary_llm_health_interval_sec,
            cooldown_sec=settings.secondary_llm_cooldown_sec,
            max_context_tokens=settings.secondary_llm_max_context_tokens,
            max_concurrency=settings.secondary_llm_max_concurrency,
        )
        effective_workloads = frozenset(workloads) & _ADVISORY_WORKLOADS
        if not endpoint.is_complete or not effective_workloads:
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
        if self._client is not None:
            await self._client.aclose()

    async def __aenter__(self) -> SecondaryBrainScheduler:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    def status(self) -> SecondaryStatus:
        if self._client is not None:
            return self._client.status()
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
        if request.modality is not ModelModality.TEXT:
            return SecondaryFailure.UNSUPPORTED_MODALITY
        if request.effect_class not in {EffectClass.NONE, EffectClass.READ_ONLY}:
            return SecondaryFailure.EFFECT_DENIED
        if request.contains_private_text and not self.allow_private_text:
            return SecondaryFailure.PRIVATE_TEXT_DISALLOWED
        return None

    async def attempt(self, request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        failure = self._eligibility_failure(request, shadow=shadow)
        if failure is not None:
            self._local_skipped_total += 1
            return SecondaryAttempt.rejected(failure)
        assert self._client is not None
        try:
            return await self._client.call(request)
        except Exception:
            # The secondary is never an availability dependency.  Cancellation
            # still propagates because asyncio.CancelledError is a BaseException.
            self._local_skipped_total += 1
            return SecondaryAttempt.rejected(SecondaryFailure.CONNECT_FAILED)

    async def secondary_preferred_required_result(
        self,
        request: ModelRequest,
        primary_fallback: Callable[[], Awaitable[T]],
    ) -> SecondaryResult | T:
        """Use one secondary attempt or invoke the primary fallback exactly once."""

        attempt = await self.attempt(request)
        if attempt.result is not None:
            return attempt.result
        self._local_fallback_total += 1
        if self._client is not None:
            self._client.record_fallback()
        return await primary_fallback()

    async def secondary_optional_advice(self, request: ModelRequest) -> SecondaryResult | None:
        """Return advisory output or skip; never duplicate primary work."""

        return (await self.attempt(request)).result

    async def run_shadow(
        self,
        request: ModelRequest,
        primary: Callable[[], Awaitable[T]],
    ) -> T:
        """Guarantee primary first, evaluate a bounded copy, and discard its result."""

        primary_result = await primary()
        await self.attempt(request, shadow=True)
        return primary_result


def build_secondary_brain(
    settings: FridaySettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SecondaryBrainScheduler:
    return SecondaryBrainScheduler.from_settings(settings, transport=transport)
