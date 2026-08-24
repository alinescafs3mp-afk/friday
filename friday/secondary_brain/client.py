"""Independent fail-soft HTTP client for the detachable advisory endpoint."""

from __future__ import annotations

import asyncio
import hashlib
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
    _load_pinned_ca_pem,
)
from .gpt_oss import GptOssProtocolAdapter, ProtocolRejection

_ENDPOINT_FAILURES = frozenset(
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
_PROFILE_ID_HEADER = b"x-friday-secondary-profile-id"
_PROFILE_SHA_HEADER = b"x-friday-secondary-profile-sha256"
_PROTOCOL_REJECTIONS = frozenset(
    {
        SecondaryFailure.WRONG_PROFILE,
        SecondaryFailure.WRONG_MODEL,
        SecondaryFailure.MALFORMED_RESPONSE,
        SecondaryFailure.TOOL_CALL_REJECTED,
        SecondaryFailure.REASONING_LEAK,
        SecondaryFailure.DEGENERATION,
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
        from .profiles import get_secondary_runtime_profile

        accepted_profile = get_secondary_runtime_profile(config.profile_id)
        if (
            accepted_profile is None
            or accepted_profile.endpoint_base_url.rstrip("/") != config.base_url.rstrip("/")
            or accepted_profile.served_model_alias != config.served_model_alias
            or accepted_profile.manifest_sha256 != config.profile_manifest_sha256
            or accepted_profile.gateway_ca_certificate_sha256 != config.ca_sha256
            or accepted_profile.max_context_tokens != config.max_context_tokens
            or accepted_profile.max_concurrency != config.max_concurrency
            or accepted_profile.max_output_tokens != config.max_output_tokens
        ):
            raise ValueError("secondary endpoint differs from the accepted profile")
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
        self._profile_manifest_match = False
        self._last_success_at: float | None = None
        self._probe_success_total = 0
        self._probe_failure_total = 0
        self._queue_wait_count = 0
        self._queue_wait_sum_sec = 0.0
        self._queue_wait_max_sec = 0.0
        self._protocol_rejection_total = 0
        timeout = httpx.Timeout(
            config.read_timeout_sec,
            connect=config.connect_timeout_sec,
            write=config.read_timeout_sec,
            pool=config.admission_timeout_sec,
        )
        tls_verifier: ssl.SSLContext | bool = True
        if config.ca_file:
            pinned_ca_pem = _load_pinned_ca_pem(config.ca_file, config.ca_sha256)
            if not pinned_ca_pem:
                raise ValueError("secondary endpoint CA identity changed")
            tls_verifier = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            tls_verifier.verify_mode = ssl.CERT_REQUIRED
            tls_verifier.check_hostname = True
            tls_verifier.load_verify_locations(cadata=pinned_ca_pem)
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
        now = self._clock()
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
            last_success_age_sec=(
                max(0.0, now - self._last_success_at) if self._last_success_at is not None else None
            ),
            cooldown_retry_after_sec=(
                max(0.0, self._cooldown_until - now) if self._state is SecondaryState.COOLDOWN else 0.0
            ),
            probe_success_total=self._probe_success_total,
            probe_failure_total=self._probe_failure_total,
            profile_manifest_match=self._profile_manifest_match,
            queue_wait_count=self._queue_wait_count,
            queue_wait_sum_sec=self._queue_wait_sum_sec,
            queue_wait_max_sec=self._queue_wait_max_sec,
            protocol_rejection_total=self._protocol_rejection_total,
        )

    def record_fallback(self) -> None:
        self._fallback_total += 1

    def validate_request(self, request: ModelRequest) -> SecondaryFailure | None:
        """Run the pure protocol/context gate before any endpoint probe."""

        try:
            self._adapter.build_payload(self.config, request)
        except ProtocolRejection as rejection:
            if rejection.failure in _PROTOCOL_REJECTIONS:
                self._protocol_rejection_total += 1
            return rejection.failure
        except Exception:
            self._protocol_rejection_total += 1
            return SecondaryFailure.MALFORMED_RESPONSE
        return None

    async def invalidate(self, failure: SecondaryFailure) -> None:
        """Reject a post-transport semantic canary without retaining its body."""

        async with self._state_lock:
            self._last_failure = failure
            self._skipped_total += 1
            if failure is SecondaryFailure.WRONG_MODEL:
                self._served_model_match = False
            if failure is SecondaryFailure.WRONG_PROFILE:
                self._profile_manifest_match = False
            if failure in _PROTOCOL_REJECTIONS:
                self._protocol_rejection_total += 1
            if failure in _ENDPOINT_FAILURES:
                self._state = SecondaryState.COOLDOWN
                self._cooldown_until = self._clock() + self.config.cooldown_sec
            else:
                self._state = SecondaryState.DEGRADED

    @staticmethod
    def _http_failure(status_code: int) -> SecondaryFailure | None:
        if status_code in {401, 403}:
            return SecondaryFailure.AUTH_REJECTED
        if status_code == 429 or status_code >= 500:
            return SecondaryFailure.HTTP_TRANSIENT
        if status_code < 200 or status_code >= 300:
            return SecondaryFailure.HTTP_REJECTED
        return None

    def _profile_header_failure(self, response: httpx.Response) -> SecondaryFailure | None:
        values: dict[bytes, list[bytes]] = {_PROFILE_ID_HEADER: [], _PROFILE_SHA_HEADER: []}
        for name, value in response.headers.raw:
            normalized = name.lower()
            if normalized in values:
                values[normalized].append(value)
        if values[_PROFILE_ID_HEADER] != [self.config.profile_id.encode("ascii")]:
            return SecondaryFailure.WRONG_PROFILE
        if values[_PROFILE_SHA_HEADER] != [self.config.profile_manifest_sha256.encode("ascii")]:
            return SecondaryFailure.WRONG_PROFILE
        return None

    async def _admit(self) -> tuple[SecondaryFailure | None, float]:
        started = self._clock()
        acquired = False
        try:
            await asyncio.wait_for(
                self._state_lock.acquire(),
                timeout=self.config.admission_timeout_sec,
            )
            acquired = True
        except TimeoutError:
            pass
        finally:
            waited = max(0.0, self._clock() - started)
            self._queue_wait_count += 1
            self._queue_wait_sum_sec += waited
            self._queue_wait_max_sec = max(self._queue_wait_max_sec, waited)
        if not acquired:
            return SecondaryFailure.ADMISSION_BUSY, waited
        try:
            now = self._clock()
            half_open = False
            if self._half_open_in_flight:
                return SecondaryFailure.COOLDOWN, waited
            if self._state is SecondaryState.COOLDOWN:
                if now < self._cooldown_until or self._half_open_in_flight:
                    return SecondaryFailure.COOLDOWN, waited
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
                return SecondaryFailure.ADMISSION_BUSY, waited
            await self._semaphore.acquire()
            self._active_requests += 1
            self._selected_total += 1
            return None, waited
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
                self._last_success_at = self._clock()
                return
            self._last_failure = failure
            self._skipped_total += 1
            if failure is SecondaryFailure.WRONG_MODEL:
                self._served_model_match = False
            if failure is SecondaryFailure.WRONG_PROFILE:
                self._profile_manifest_match = False
            if failure in _PROTOCOL_REJECTIONS:
                self._protocol_rejection_total += 1
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
            if rejection.failure in _PROTOCOL_REJECTIONS:
                self._protocol_rejection_total += 1
            return SecondaryAttempt.rejected(rejection.failure)
        except Exception:
            self._last_failure = SecondaryFailure.MALFORMED_RESPONSE
            self._skipped_total += 1
            self._protocol_rejection_total += 1
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

        admission_failure, queue_wait_sec = await self._admit()
        if admission_failure is not None:
            self._last_failure = admission_failure
            self._skipped_total += 1
            return SecondaryAttempt.rejected(admission_failure, queue_wait_sec=queue_wait_sec)

        failure: SecondaryFailure | None = None
        task: asyncio.Task[httpx.Response] | None = None
        started = self._clock()
        try:
            task = asyncio.create_task(self._http.post(self.config.chat_completions_url, json=payload))
            try:
                response = await asyncio.wait_for(task, timeout=remaining)
            except TimeoutError:
                failure = SecondaryFailure.TIMEOUT
                return SecondaryAttempt.rejected(failure, queue_wait_sec=queue_wait_sec)

            failure = self._http_failure(response.status_code)
            if failure is not None:
                return SecondaryAttempt.rejected(failure, queue_wait_sec=queue_wait_sec)
            failure = self._profile_header_failure(response)
            if failure is not None:
                return SecondaryAttempt.rejected(failure, queue_wait_sec=queue_wait_sec)
            if len(response.content) > 1_048_576:
                failure = SecondaryFailure.MALFORMED_RESPONSE
                return SecondaryAttempt.rejected(failure, queue_wait_sec=queue_wait_sec)
            try:
                body: Any = response.json()
            except Exception:
                failure = SecondaryFailure.MALFORMED_RESPONSE
                return SecondaryAttempt.rejected(failure, queue_wait_sec=queue_wait_sec)
            try:
                result = self._adapter.parse_response(
                    self.config,
                    request,
                    body,
                    latency_sec=self._clock() - started,
                )
            except ProtocolRejection as rejection:
                failure = rejection.failure
                return SecondaryAttempt.rejected(failure, queue_wait_sec=queue_wait_sec)
            return SecondaryAttempt.success(result, queue_wait_sec=queue_wait_sec)
        except asyncio.CancelledError:
            failure = SecondaryFailure.CANCELLED
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise
        except httpx.TimeoutException:
            failure = SecondaryFailure.TIMEOUT
            return SecondaryAttempt.rejected(failure, queue_wait_sec=queue_wait_sec)
        except (httpx.NetworkError, httpx.ProtocolError):
            failure = SecondaryFailure.CONNECT_FAILED
            return SecondaryAttempt.rejected(failure, queue_wait_sec=queue_wait_sec)
        except Exception:
            # Third-party transports can throw arbitrary exception objects which
            # may embed response bodies or credentials.  Retain only this enum.
            failure = SecondaryFailure.CONNECT_FAILED
            return SecondaryAttempt.rejected(failure, queue_wait_sec=queue_wait_sec)
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await self._finish(failure)

    async def probe_models(self, *, absolute_deadline_monotonic: float) -> SecondaryFailure | None:
        """Verify the accepted profile manifest and exact served alias."""

        self._profile_manifest_match = False
        remaining = min(
            self.config.call_budget_sec,
            absolute_deadline_monotonic - self._clock(),
        )
        if remaining <= 0.0:
            self._last_failure = SecondaryFailure.DEADLINE
            self._skipped_total += 1
            self._probe_failure_total += 1
            return SecondaryFailure.DEADLINE
        admission_failure, _queue_wait_sec = await self._admit()
        if admission_failure is not None:
            self._last_failure = admission_failure
            self._skipped_total += 1
            self._probe_failure_total += 1
            return admission_failure

        failure: SecondaryFailure | None = None
        task: asyncio.Task[httpx.Response] | None = None
        try:
            if self.config.profile_manifest_sha256:
                task = asyncio.create_task(self._http.get(self.config.profile_url))
                try:
                    response = await asyncio.wait_for(task, timeout=remaining)
                except TimeoutError:
                    failure = SecondaryFailure.TIMEOUT
                    return failure
                failure = self._http_failure(response.status_code)
                if failure is not None:
                    return failure
                failure = self._profile_header_failure(response)
                if failure is not None:
                    return failure
                if len(response.content) > 65_536:
                    failure = SecondaryFailure.WRONG_PROFILE
                    return failure
                if hashlib.sha256(response.content).hexdigest() != self.config.profile_manifest_sha256:
                    failure = SecondaryFailure.WRONG_PROFILE
                    return failure
                from .profiles import get_secondary_runtime_profile

                profile = get_secondary_runtime_profile(self.config.profile_id)
                if profile is None or not profile.accepts_manifest(response.content):
                    failure = SecondaryFailure.WRONG_PROFILE
                    return failure
                self._profile_manifest_match = True

                remaining = min(
                    self.config.call_budget_sec,
                    absolute_deadline_monotonic - self._clock(),
                )
                if remaining <= 0.0:
                    failure = SecondaryFailure.DEADLINE
                    return failure
            task = asyncio.create_task(self._http.get(self.config.models_url))
            try:
                response = await asyncio.wait_for(task, timeout=remaining)
            except TimeoutError:
                failure = SecondaryFailure.TIMEOUT
                return failure
            failure = self._http_failure(response.status_code)
            if failure is not None:
                return failure
            failure = self._profile_header_failure(response)
            if failure is not None:
                return failure
            if len(response.content) > 1_048_576:
                failure = SecondaryFailure.MALFORMED_RESPONSE
                return failure
            try:
                body: Any = response.json()
            except Exception:
                failure = SecondaryFailure.MALFORMED_RESPONSE
                return failure
            rows = body.get("data") if isinstance(body, dict) else None
            if not isinstance(rows, list) or len(rows) > 128:
                failure = SecondaryFailure.MALFORMED_RESPONSE
                return failure
            identifiers: list[str] = []
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    failure = SecondaryFailure.MALFORMED_RESPONSE
                    return failure
                identifiers.append(row["id"])
            if identifiers != [self.config.served_model_alias]:
                failure = SecondaryFailure.WRONG_MODEL
                return failure
            return None
        except asyncio.CancelledError:
            failure = SecondaryFailure.CANCELLED
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise
        except httpx.TimeoutException:
            failure = SecondaryFailure.TIMEOUT
            return failure
        except (httpx.NetworkError, httpx.ProtocolError):
            failure = SecondaryFailure.CONNECT_FAILED
            return failure
        except Exception:
            failure = SecondaryFailure.CONNECT_FAILED
            return failure
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await self._finish(failure)
            if failure is None:
                self._probe_success_total += 1
            else:
                self._probe_failure_total += 1
