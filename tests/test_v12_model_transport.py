from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any

import httpx
import pytest

from friday.agent_runtime.llm import LLMRouter
from friday.v12_model_runtime import AttestedV12ModelRuntime
from friday.v12_model_transport import (
    RouterV12CompletionTransport,
    RouterV12MetricsTransport,
    V12ModelTransportError,
    V12ModelTransportFailure,
    create_attested_v12_model_runtime,
)


def _router(settings, **changes: Any) -> LLMRouter:
    values = {
        "llm_enabled": True,
        "llm_model": "dispatcher",
        "llm_api_key": "private-v12-key",
        "llm_base_url": "http://127.0.0.1:8001/v1",
        **changes,
    }
    configured = replace(settings, **values)
    return LLMRouter(configured)


@pytest.mark.asyncio
async def test_completion_uses_exact_router_contract(settings, monkeypatch) -> None:
    router = _router(settings)
    observed: dict[str, Any] = {}

    async def fake_chat(messages, **kwargs):
        observed["messages"] = messages
        observed.update(kwargs)
        return {
            "content": "synthetic answer",
            "finish_reason": "stop",
            "tool_calls": [],
            "usage": {"prompt_tokens": 19},
            "_served_model_alias": "dispatcher",
        }

    monkeypatch.setattr(router, "chat", fake_chat)
    transport = RouterV12CompletionTransport(router)
    deadline = time.monotonic() + 2.0
    completion = await transport.chat(
        [{"role": "user", "content": "synthetic"}],
        temperature=0.0,
        max_tokens=64,
        priority="background",
        tools=None,
        tool_choice=None,
        reject_repeated_token_degeneration=True,
        allow_retries=False,
        absolute_deadline=deadline,
        open_silent_cooldown=False,
        require_full_context=True,
    )

    assert transport.bound_router is router
    assert completion.content == "synthetic answer"
    assert completion.served_model_alias == "dispatcher"
    assert completion.prompt_tokens == 19
    assert observed["require_exact_response_model"] is True
    assert observed["absolute_deadline"] == deadline
    assert observed["allow_retries"] is False
    assert observed["tools"] is None


@pytest.mark.asyncio
async def test_completion_rejects_alias_without_echoing_untrusted_content(
    settings,
    monkeypatch,
) -> None:
    router = _router(settings)
    secret = "private-v12-key"
    hostile = "https://hostile.invalid/private"

    async def fake_chat(*args, **kwargs):
        return {
            "content": hostile + secret,
            "finish_reason": "stop",
            "tool_calls": [],
            "usage": {"prompt_tokens": 1},
            "_served_model_alias": hostile,
        }

    monkeypatch.setattr(router, "chat", fake_chat)
    transport = RouterV12CompletionTransport(router)
    with pytest.raises(V12ModelTransportError) as captured:
        await transport.chat(
            [{"role": "user", "content": "synthetic"}],
            temperature=0.0,
            max_tokens=64,
            priority="background",
            tools=None,
            tool_choice=None,
            reject_repeated_token_degeneration=True,
            allow_retries=False,
            absolute_deadline=time.monotonic() + 1.0,
            open_silent_cooldown=False,
            require_full_context=True,
        )
    assert captured.value.code is V12ModelTransportFailure.COMPLETION_REJECTED
    assert secret not in str(captured.value)
    assert hostile not in str(captured.value)
    assert secret not in repr(captured.value)
    assert hostile not in repr(captured.value)
    assert secret not in repr(transport)
    assert router.base_url not in repr(transport)


@pytest.mark.asyncio
async def test_metrics_are_same_origin_authenticated_and_bounded(settings) -> None:
    router = _router(settings)
    observed: list[httpx.Request] = []
    body = b"process_start_time_seconds 1\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, content=body)

    transport = RouterV12MetricsTransport(
        router,
        http_transport=httpx.MockTransport(handler),
    )
    result = await transport.fetch_metrics(
        maximum_bytes=1024,
        absolute_deadline=time.monotonic() + 1.0,
    )

    assert result == body
    assert len(observed) == 1
    assert str(observed[0].url) == "http://127.0.0.1:8001/metrics"
    assert observed[0].headers["authorization"] == "Bearer private-v12-key"
    assert observed[0].headers["accept-encoding"] == "identity"
    assert transport.bound_router is router


@pytest.mark.asyncio
async def test_metrics_do_not_follow_redirects_or_echo_target(settings) -> None:
    router = _router(settings)
    target = "https://hostile.invalid/private-v12-key"
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": target})

    transport = RouterV12MetricsTransport(
        router,
        http_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(V12ModelTransportError) as captured:
        await transport.fetch_metrics(
            maximum_bytes=1024,
            absolute_deadline=time.monotonic() + 1.0,
        )
    assert captured.value.code is V12ModelTransportFailure.METRICS_REJECTED
    assert len(requests) == 1
    assert target not in str(captured.value)
    assert target not in repr(captured.value)
    assert router.base_url not in repr(transport)
    assert router.settings.llm_api_key not in repr(transport)


@pytest.mark.asyncio
async def test_metrics_reject_declared_or_streamed_oversize(settings) -> None:
    router = _router(settings)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 65)

    transport = RouterV12MetricsTransport(
        router,
        http_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(V12ModelTransportError) as captured:
        await transport.fetch_metrics(
            maximum_bytes=64,
            absolute_deadline=time.monotonic() + 1.0,
        )
    assert captured.value.code is V12ModelTransportFailure.METRICS_REJECTED


@pytest.mark.asyncio
async def test_cancellable_completion_is_cancelled_and_locally_drained(
    settings,
    monkeypatch,
) -> None:
    router = _router(settings)
    started = asyncio.Event()
    finalized = asyncio.Event()

    async def fake_chat(*args, **kwargs):
        assert kwargs["require_exact_response_model"] is True
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    monkeypatch.setattr(router, "chat", fake_chat)
    transport = RouterV12CompletionTransport(router)
    pending = await transport.start_cancellable(
        [{"role": "user", "content": "synthetic long generation"}],
        temperature=0.0,
        max_tokens=2048,
        priority="background",
        absolute_deadline=time.monotonic() + 2.0,
        require_full_context=True,
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert pending.bound_router is router
    assert pending.submitted_model_alias == "dispatcher"
    assert pending.is_pending() is True

    assert await pending.cancel_and_drain(absolute_deadline=time.monotonic() + 1.0)
    assert pending.is_pending() is False
    assert finalized.is_set()
    assert router.base_url not in repr(pending)
    assert router.settings.llm_api_key not in repr(pending)


@pytest.mark.asyncio
async def test_expired_deadline_makes_no_metrics_request(settings) -> None:
    router = _router(settings)
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"unreachable")

    transport = RouterV12MetricsTransport(
        router,
        http_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(V12ModelTransportError) as captured:
        await transport.fetch_metrics(
            maximum_bytes=1024,
            absolute_deadline=time.monotonic() - 1.0,
        )
    assert captured.value.code is V12ModelTransportFailure.DEADLINE_EXHAUSTED
    assert calls == 0


def test_factory_is_network_silent_and_binds_one_exact_router(settings) -> None:
    router = _router(settings)
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("factory performed network I/O")

    runtime = create_attested_v12_model_runtime(
        router,
        metrics_http_transport=httpx.MockTransport(handler),
    )
    assert isinstance(runtime, AttestedV12ModelRuntime)
    assert runtime.probe_client is not None
    assert calls == 0


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:8001/openai/v1",
        "http://127.0.0.1:8001/v1?target=other",
        "http://user:private-v12-key@127.0.0.1:8001/v1",
    ],
)
def test_factory_rejects_ambiguous_metrics_origin_without_echo(
    settings,
    base_url: str,
) -> None:
    router = _router(settings, llm_base_url=base_url)
    with pytest.raises(V12ModelTransportError) as captured:
        create_attested_v12_model_runtime(router)
    assert captured.value.code is V12ModelTransportFailure.COMPOSITION_REJECTED
    assert base_url not in str(captured.value)
    assert base_url not in repr(captured.value)
