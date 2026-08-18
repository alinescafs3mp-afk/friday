"""Concrete, bounded transports for the attested V12 model runtime.

The V12 attestation must observe the same router, endpoint, credentials and
model alias that later serve canary traffic.  This module is the deliberately
small production adapter which closes those seams:

* completions go through one existing :class:`LLMRouter` and require the
  server-reported response model to match exactly;
* load samples come from the ``/metrics`` endpoint on that router's exact
  origin, with redirects, proxy environment variables and unbounded bodies
  disabled; and
* the cancellation probe owns an explicit router task which is cancelled and
  locally drained before it reports success.

Transport-controlled strings never appear in exceptions or representations.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from enum import StrEnum
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx

from friday.agent_runtime.llm import LLMResponseModelMismatchError, LLMRouter
from friday.model_probe import MAX_COMPLETION_CHARS
from friday.v12_model_runtime import (
    MAX_METRICS_BYTES,
    AttestedV12ModelRuntime,
    V12ServedAliasError,
    V12ServedCompletion,
)

_MAX_TOOL_CALLS = 64
_METRICS_CHUNK_BYTES = 8_192
_METRICS_CONNECT_TIMEOUT_SEC = 2.0


class V12ModelTransportFailure(StrEnum):
    """Content-free transport failure vocabulary."""

    COMPOSITION_REJECTED = "composition_rejected"
    DEADLINE_EXHAUSTED = "deadline_exhausted"
    COMPLETION_REJECTED = "completion_rejected"
    METRICS_REJECTED = "metrics_rejected"


class V12ModelTransportError(RuntimeError):
    """Sanitized error which cannot retain an endpoint response or URL."""

    def __init__(self, code: V12ModelTransportFailure) -> None:
        self.code = code
        super().__init__(code.value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code.value!r})"


def _error(code: V12ModelTransportFailure) -> V12ModelTransportError:
    return V12ModelTransportError(code)


def _remaining(absolute_deadline: float) -> float:
    if isinstance(absolute_deadline, bool) or not isinstance(absolute_deadline, (int, float)):
        raise _error(V12ModelTransportFailure.DEADLINE_EXHAUSTED)
    remaining = float(absolute_deadline) - time.monotonic()
    if not math.isfinite(float(absolute_deadline)) or remaining <= 0.0:
        raise _error(V12ModelTransportFailure.DEADLINE_EXHAUSTED)
    return remaining


def _require_exact_router(router: object) -> LLMRouter:
    if type(router) is not LLMRouter or router.enabled is not True:
        raise _error(V12ModelTransportFailure.COMPOSITION_REJECTED)
    return router


def _consume_task(task: asyncio.Future[Any]) -> None:
    if task.cancelled():
        return
    with suppress(asyncio.CancelledError, Exception):
        task.exception()


def _project_completion(result: object, *, exact_alias: str) -> V12ServedCompletion:
    if not isinstance(result, Mapping):
        raise _error(V12ModelTransportFailure.COMPLETION_REJECTED)
    content = result.get("content")
    finish_reason = result.get("finish_reason")
    tool_calls = result.get("tool_calls")
    usage = result.get("usage")
    served_alias = result.get("_served_model_alias")
    if not isinstance(served_alias, str) or served_alias != exact_alias:
        raise V12ServedAliasError()
    if (
        not isinstance(content, str)
        or not content
        or len(content) > MAX_COMPLETION_CHARS
        or not isinstance(finish_reason, str)
        or not finish_reason
        or len(finish_reason) > 64
        or not isinstance(tool_calls, list)
        or len(tool_calls) > _MAX_TOOL_CALLS
        or not isinstance(usage, Mapping)
    ):
        raise _error(V12ModelTransportFailure.COMPLETION_REJECTED)
    prompt_tokens = usage.get("prompt_tokens")
    if isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int) or prompt_tokens < 0:
        raise _error(V12ModelTransportFailure.COMPLETION_REJECTED)
    try:
        content.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _error(V12ModelTransportFailure.COMPLETION_REJECTED) from None

    # The attested profile has no tool capability.  Preserve only the fact that
    # a call was emitted, never its untrusted name or arguments.  The probe and
    # checked runtime reject every non-empty tuple.
    projected_tool_calls = tuple("present" for _ in tool_calls)
    return V12ServedCompletion(
        content=content,
        finish_reason=finish_reason,
        tool_calls=projected_tool_calls,
        prompt_tokens=prompt_tokens,
        served_model_alias=served_alias,
    )


class _RouterPendingCompletion:
    """One locally-owned router request used by the cancellation probe."""

    __slots__ = ("_router", "_submission_event", "_submitted_model_alias", "_task")

    def __init__(
        self,
        router: LLMRouter,
        task: asyncio.Task[V12ServedCompletion],
        submission_event: asyncio.Event,
    ) -> None:
        self._router = router
        self._submission_event = submission_event
        self._submitted_model_alias = router.model
        self._task = task
        task.add_done_callback(_consume_task)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(pending={self.is_pending()!r})"

    @property
    def bound_router(self) -> LLMRouter:
        return self._router

    @property
    def submitted_model_alias(self) -> str:
        return self._submitted_model_alias

    def is_pending(self) -> bool:
        return not self._task.done()

    def submission_started(self) -> bool:
        return self._submission_event.is_set()

    async def cancel_and_drain(self, *, absolute_deadline: float) -> bool:
        remaining = _remaining(absolute_deadline)
        self._task.cancel()
        try:
            done, _ = await asyncio.wait((self._task,), timeout=remaining)
        except asyncio.CancelledError:
            # The owner may itself be cancelled.  Preserve that cancellation,
            # but first make a best-effort local drain within the same deadline.
            self._task.cancel()
            cleanup_remaining = max(0.0, float(absolute_deadline) - time.monotonic())
            if cleanup_remaining > 0.0:
                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait((self._task,), timeout=cleanup_remaining)
            raise
        if not done:
            return False
        _consume_task(self._task)
        return True


class RouterV12CompletionTransport:
    """Exact-model completion adapter over one existing ``LLMRouter``."""

    __slots__ = ("_router",)

    def __init__(self, router: LLMRouter) -> None:
        self._router = _require_exact_router(router)

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    @property
    def bound_router(self) -> LLMRouter:
        return self._router

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
        _request_submission_event: asyncio.Event | None = None,
    ) -> V12ServedCompletion:
        remaining = _remaining(absolute_deadline)
        if tools not in (None, []) or tool_choice is not None:
            raise _error(V12ModelTransportFailure.COMPLETION_REJECTED)
        try:
            async with asyncio.timeout(remaining):
                result = await self._router.chat(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    priority=priority,
                    tools=None,
                    tool_choice=None,
                    reject_repeated_token_degeneration=(reject_repeated_token_degeneration),
                    allow_retries=allow_retries,
                    absolute_deadline=absolute_deadline,
                    open_silent_cooldown=open_silent_cooldown,
                    require_full_context=require_full_context,
                    require_exact_response_model=True,
                    request_submitted_event=_request_submission_event,
                )
            _remaining(absolute_deadline)
            return _project_completion(result, exact_alias=self._router.model)
        except asyncio.CancelledError:
            raise
        except V12ModelTransportError:
            raise
        except V12ServedAliasError:
            raise
        except LLMResponseModelMismatchError:
            raise V12ServedAliasError() from None
        except TimeoutError:
            raise _error(V12ModelTransportFailure.DEADLINE_EXHAUSTED) from None
        except Exception:
            raise _error(V12ModelTransportFailure.COMPLETION_REJECTED) from None

    async def start_cancellable(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None,
        max_tokens: int | None,
        priority: str,
        absolute_deadline: float,
        require_full_context: bool,
    ) -> _RouterPendingCompletion:
        _remaining(absolute_deadline)
        submission_event = asyncio.Event()
        try:
            task = asyncio.create_task(
                self.chat(
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
                    require_full_context=require_full_context,
                    _request_submission_event=submission_event,
                ),
                name="friday-v12-cancellation-probe",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _error(V12ModelTransportFailure.COMPLETION_REJECTED) from None
        return _RouterPendingCompletion(self._router, task, submission_event)


def _metrics_url(router: LLMRouter) -> str:
    try:
        raw = router.base_url
        if not isinstance(raw, str) or not raw or raw != raw.strip():
            raise ValueError
        parsed = urlsplit(raw)
        # V12 supports the canonical OpenAI-compatible root only.  Accepting an
        # arbitrary prefix and walking to its parent would make ``/metrics``
        # endpoint identity ambiguous behind reverse proxies.
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"/v1", "/v1/"}
        ):
            raise ValueError
        # Access validates malformed/out-of-range ports before any client is
        # constructed.  The original netloc keeps IPv6 brackets intact.
        _ = parsed.port
        return urlunsplit(SplitResult(parsed.scheme.casefold(), parsed.netloc, "/metrics", "", ""))
    except (TypeError, ValueError, UnicodeError):
        raise _error(V12ModelTransportFailure.COMPOSITION_REJECTED) from None


def _models_url(router: LLMRouter) -> str:
    parsed = urlsplit(_metrics_url(router))
    return urlunsplit(SplitResult(parsed.scheme, parsed.netloc, "/v1/models", "", ""))


class RouterV12MetricsTransport:
    """Bounded same-origin vLLM metrics transport."""

    __slots__ = ("_http_transport", "_metrics_endpoint", "_models_endpoint", "_router")

    def __init__(
        self,
        router: LLMRouter,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._router = _require_exact_router(router)
        self._metrics_endpoint = _metrics_url(router)
        self._models_endpoint = _models_url(router)
        self._http_transport = http_transport

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    @property
    def bound_router(self) -> LLMRouter:
        return self._router

    async def _fetch_bounded(
        self,
        endpoint: str,
        *,
        maximum_bytes: int,
        absolute_deadline: float,
        accept: str,
    ) -> bytes:
        remaining = _remaining(absolute_deadline)
        if (
            isinstance(maximum_bytes, bool)
            or not isinstance(maximum_bytes, int)
            or maximum_bytes <= 0
            or maximum_bytes > MAX_METRICS_BYTES
        ):
            raise _error(V12ModelTransportFailure.METRICS_REJECTED)
        try:
            timeout = httpx.Timeout(
                remaining,
                connect=min(_METRICS_CONNECT_TIMEOUT_SEC, remaining),
            )
            headers = self._router._auth_headers()  # noqa: SLF001 - exact router binding
            headers = {**headers, "Accept": accept, "Accept-Encoding": "identity"}
            async with asyncio.timeout(remaining):
                async with httpx.AsyncClient(
                    timeout=timeout,
                    trust_env=False,
                    follow_redirects=False,
                    headers=headers,
                    transport=self._http_transport,
                ) as client:
                    async with client.stream("GET", endpoint) as response:
                        if response.status_code != 200:
                            raise _error(V12ModelTransportFailure.METRICS_REJECTED)
                        content_encoding = response.headers.get("content-encoding", "")
                        if content_encoding.casefold() not in {"", "identity"}:
                            raise _error(V12ModelTransportFailure.METRICS_REJECTED)
                        content_length = response.headers.get("content-length")
                        if content_length is not None:
                            try:
                                declared_length = int(content_length, 10)
                            except ValueError:
                                raise _error(V12ModelTransportFailure.METRICS_REJECTED) from None
                            if declared_length < 0 or declared_length > maximum_bytes:
                                raise _error(V12ModelTransportFailure.METRICS_REJECTED)

                        body = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=_METRICS_CHUNK_BYTES):
                            if len(body) + len(chunk) > maximum_bytes:
                                raise _error(V12ModelTransportFailure.METRICS_REJECTED)
                            body.extend(chunk)
            _remaining(absolute_deadline)
            if not body:
                raise _error(V12ModelTransportFailure.METRICS_REJECTED)
            return bytes(body)
        except asyncio.CancelledError:
            raise
        except V12ModelTransportError:
            raise
        except TimeoutError:
            raise _error(V12ModelTransportFailure.DEADLINE_EXHAUSTED) from None
        except Exception:
            raise _error(V12ModelTransportFailure.METRICS_REJECTED) from None

    async def fetch_metrics(
        self,
        *,
        maximum_bytes: int,
        absolute_deadline: float,
    ) -> bytes:
        return await self._fetch_bounded(
            self._metrics_endpoint,
            maximum_bytes=maximum_bytes,
            absolute_deadline=absolute_deadline,
            accept="text/plain",
        )

    async def fetch_model_inventory(
        self,
        *,
        maximum_bytes: int,
        absolute_deadline: float,
    ) -> bytes:
        return await self._fetch_bounded(
            self._models_endpoint,
            maximum_bytes=maximum_bytes,
            absolute_deadline=absolute_deadline,
            accept="application/json",
        )


def create_attested_v12_model_runtime(
    router: LLMRouter,
    *,
    metrics_http_transport: httpx.AsyncBaseTransport | None = None,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AttestedV12ModelRuntime:
    """Compose the production V12 runtime from one exact router instance.

    ``metrics_http_transport`` is solely an offline-test seam.  Omitting it uses
    httpx's ordinary network transport with proxy environment variables off.
    Construction performs no network request; callers must explicitly run the
    live attestation before installing any route.
    """

    exact_router = _require_exact_router(router)
    completion = RouterV12CompletionTransport(exact_router)
    metrics = RouterV12MetricsTransport(
        exact_router,
        http_transport=metrics_http_transport,
    )
    try:
        return AttestedV12ModelRuntime(
            exact_router,
            completion,
            metrics,
            sleeper=sleeper,
        )
    except asyncio.CancelledError:
        raise
    except V12ModelTransportError:
        raise
    except Exception:
        raise _error(V12ModelTransportFailure.COMPOSITION_REJECTED) from None


__all__ = [
    "RouterV12CompletionTransport",
    "RouterV12MetricsTransport",
    "V12ModelTransportError",
    "V12ModelTransportFailure",
    "create_attested_v12_model_runtime",
]
