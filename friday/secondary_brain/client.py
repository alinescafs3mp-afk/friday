"""Independent fail-soft HTTP client for the detachable advisory endpoint."""

from __future__ import annotations

import asyncio
import ssl
import time
from collections.abc import Callable
from typing import Any

import httpx

from .contracts import (
    ModelRequest,
    SecondaryAttempt,
    SecondaryEndpointConfig,
    SecondaryFailure,
    SecondaryState,
    SecondaryStatus,
)
from .gpt_oss import GptOssProtocolAdapter, ProtocolRejection

_ENDPOINT_FAILURES = frozenset(
    {
        SecondaryFailure.CONNECT_FAILED,
        SecondaryFailure.TIMEOUT,
        SecondaryFailure.HTTP_TRANSIENT,
        SecondaryFailure.HTTP_REJECTED,
        SecondaryFailure.AUTH_REJECTED,
        SecondaryFailure.WRONG_MODEL,
        SecondaryFailure.MALFORMED_RESPONSE,
        SecondaryFailure.TOOL_CALL_REJECTED,
        SecondaryFailure.REASONING_LEAK,
        SecondaryFailure.DEGENERATION,
        SecondaryFailure.CANCELLED,
    }
)


class SecondaryEndpointClient:
    """One endpoint, one HTTP pool, one semaphore and one circuit."""

    def __init__(
        self,
        config: SecondaryEndpointConfig,
        *,
        adapter: GptOssProtocolAdapter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not config.is_complete:
            raise ValueError("secondary endpoint configuration is incomplete")
        self.config = config
        self._adapter = adapter or GptOssProtocolAdapter()
        self._clock = clock
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        self._state_lock = asyncio.Lock()
        self._state = SecondaryState.PROBING
        self._cooldown_until = 0.0
        self._half_open_in_flight = False
        self._last_failure: SecondaryFailure | None = None
        self._selected_total = 0
        self._success_total = 0
        self._skipped_total = 0
        self._fallback_total = 0
        self._active_requests = 0
        self._served_model_match = False
        timeout = httpx.Timeout(
            config.read_timeout_sec,
            connect=config.connect_timeout_sec,
            write=config.read_timeout_sec,
            pool=config.admission_timeout_sec,
        )
        tls_verifier: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=config.ca_file) if config.ca_file else True
        )
        self._http = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
            verify=tls_verifier,
            transport=transport,
            trust_env=False,
        )

    def __repr__(self) -> str:
        return (
            f"SecondaryEndpointClient(state={self._state.value!r}, active_requests={self._active_requests})"
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> SecondaryEndpointClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    def status(self) -> SecondaryStatus:
        return SecondaryStatus(
            state=self._state,
            last_failure=self._last_failure,
            selected_total=self._selected_total,
            success_total=self._success_total,
            skipped_total=self._skipped_total,
            fallback_total=self._fallback_total,
            active_requests=self._active_requests,
            context_cap_tokens=self.config.max_context_tokens,
            served_model_match=self._served_model_match,
        )

    def record_fallback(self) -> None:
        self._fallback_total += 1

    async def _admit(self) -> SecondaryFailure | None:
        try:
            await asyncio.wait_for(
                self._state_lock.acquire(),
                timeout=self.config.admission_timeout_sec,
            )
        except TimeoutError:
            return SecondaryFailure.ADMISSION_BUSY
        try:
            now = self._clock()
            half_open = False
            if self._half_open_in_flight:
                return SecondaryFailure.COOLDOWN
            if self._state is SecondaryState.COOLDOWN:
                if now < self._cooldown_until or self._half_open_in_flight:
                    return SecondaryFailure.COOLDOWN
                half_open = True
                self._half_open_in_flight = True
                self._state = SecondaryState.PROBING

            # The state lock makes this check and decrement atomic.  acquire()
            # cannot suspend while a semaphore token exists, so model calls are
            # never queued behind the optional endpoint.
            if self._semaphore.locked():
                if half_open:
                    self._half_open_in_flight = False
                    self._state = SecondaryState.COOLDOWN
                return SecondaryFailure.ADMISSION_BUSY
            await self._semaphore.acquire()
            self._active_requests += 1
            self._selected_total += 1
            return None
        finally:
            self._state_lock.release()

    async def _finish(self, failure: SecondaryFailure | None) -> None:
        async with self._state_lock:
            self._active_requests = max(0, self._active_requests - 1)
            self._semaphore.release()
            self._half_open_in_flight = False
            if failure is None:
                self._state = SecondaryState.HEALTHY
                self._last_failure = None
                self._success_total += 1
                self._served_model_match = True
                return
            self._last_failure = failure
            self._skipped_total += 1
            if failure is SecondaryFailure.WRONG_MODEL:
                self._served_model_match = False
            if failure in _ENDPOINT_FAILURES:
                self._state = SecondaryState.COOLDOWN
                self._cooldown_until = self._clock() + self.config.cooldown_sec
            else:
                self._state = SecondaryState.DEGRADED

    async def call(self, request: ModelRequest) -> SecondaryAttempt:
        """Make one bounded generation attempt; never retry internally."""

        try:
            payload = self._adapter.build_payload(self.config, request)
        except ProtocolRejection as rejection:
            self._last_failure = rejection.failure
            self._skipped_total += 1
            return SecondaryAttempt.rejected(rejection.failure)
        except Exception:
            self._last_failure = SecondaryFailure.MALFORMED_RESPONSE
            self._skipped_total += 1
            return SecondaryAttempt.rejected(SecondaryFailure.MALFORMED_RESPONSE)

        now = self._clock()
        remaining = min(
            self.config.call_budget_sec,
            request.absolute_deadline_monotonic - now,
        )
        if remaining <= 0.0:
            self._last_failure = SecondaryFailure.DEADLINE
            self._skipped_total += 1
            return SecondaryAttempt.rejected(SecondaryFailure.DEADLINE)

        admission_failure = await self._admit()
        if admission_failure is not None:
            self._last_failure = admission_failure
            self._skipped_total += 1
            return SecondaryAttempt.rejected(admission_failure)

        failure: SecondaryFailure | None = None
        task: asyncio.Task[httpx.Response] | None = None
        started = self._clock()
        try:
            task = asyncio.create_task(self._http.post(self.config.chat_completions_url, json=payload))
            try:
                response = await asyncio.wait_for(task, timeout=remaining)
            except TimeoutError:
                failure = SecondaryFailure.TIMEOUT
                return SecondaryAttempt.rejected(failure)

            if response.status_code in {401, 403}:
                failure = SecondaryFailure.AUTH_REJECTED
                return SecondaryAttempt.rejected(failure)
            if response.status_code == 429 or response.status_code >= 500:
                failure = SecondaryFailure.HTTP_TRANSIENT
                return SecondaryAttempt.rejected(failure)
            if response.status_code < 200 or response.status_code >= 300:
                failure = SecondaryFailure.HTTP_REJECTED
                return SecondaryAttempt.rejected(failure)
            if len(response.content) > 1_048_576:
                failure = SecondaryFailure.MALFORMED_RESPONSE
                return SecondaryAttempt.rejected(failure)
            try:
                body: Any = response.json()
            except Exception:
                failure = SecondaryFailure.MALFORMED_RESPONSE
                return SecondaryAttempt.rejected(failure)
            try:
                result = self._adapter.parse_response(
                    self.config,
                    request,
                    body,
                    latency_sec=self._clock() - started,
                )
            except ProtocolRejection as rejection:
                failure = rejection.failure
                return SecondaryAttempt.rejected(failure)
            return SecondaryAttempt.success(result)
        except asyncio.CancelledError:
            failure = SecondaryFailure.CANCELLED
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise
        except httpx.TimeoutException:
            failure = SecondaryFailure.TIMEOUT
            return SecondaryAttempt.rejected(failure)
        except (httpx.NetworkError, httpx.ProtocolError):
            failure = SecondaryFailure.CONNECT_FAILED
            return SecondaryAttempt.rejected(failure)
        except Exception:
            # Third-party transports can throw arbitrary exception objects which
            # may embed response bodies or credentials.  Retain only this enum.
            failure = SecondaryFailure.CONNECT_FAILED
            return SecondaryAttempt.rejected(failure)
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await self._finish(failure)
