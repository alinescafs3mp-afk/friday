"""Bounded classifier output and cancellable model requests.

All prompts and responses in this module are synthetic.  The tests never load the
runtime environment or contact a model endpoint.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import httpx
import pytest

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.agent_runtime.llm import CLASSIFIER_MAX_TOKENS, LLMRouter


class _ClassifierLLM:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, _messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {
            "content": (
                '{"вид":"другое","напоминание":"нет","действие":"ничего",'
                '"остаток":"","запрос":"","кто":"","дни":[],"правило":""}'
            )
        }


@pytest.mark.asyncio
async def test_every_runtime_classifier_has_the_shared_output_ceiling(settings) -> None:
    runtime = AgentRuntime.__new__(AgentRuntime)
    llm = _ClassifierLLM()
    runtime.settings = settings
    runtime.llm = llm

    await runtime._standing_rule_by_arbiter("синтетическое правило", [])  # noqa: SLF001
    await runtime._remainder_after("синтетическая составная просьба", "решённая часть")  # noqa: SLF001

    context = AgentContext(conversation_id="conv_synthetic", user_id="synthetic")
    context.outward_verdict = ("действие", None)
    await runtime._prefetch_a_reminder_if_asked(  # noqa: SLF001
        "синтетическое напоминание",
        context,
        None,  # type: ignore[arg-type] -- the negative verdict never reaches the kernel
        [{"function": {"name": "remind"}}],
        [],
        [],
        [],
    )
    await runtime._is_a_timeline_question("что было в синтетический день")  # noqa: SLF001
    await runtime._is_small_talk_by_arbiter("синтетическая реплика")  # noqa: SLF001
    await runtime._web_query_by_arbiter("синтетический вопрос")  # noqa: SLF001

    assert len(llm.calls) == 6
    assert {call.get("max_tokens") for call in llm.calls} == {CLASSIFIER_MAX_TOKENS}
    assert settings.llm_max_tokens > CLASSIFIER_MAX_TOKENS


class _HangingClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.request_cancelled = False
        self.cancellation_drained = False
        self.closed = False

    async def __aenter__(self) -> _HangingClient:
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        self.closed = True
        return False

    async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.request_cancelled = True
            # Cleanup needs one scheduling point.  The router must await it before
            # closing the client and propagating cancellation to its caller.
            await asyncio.sleep(0)
            self.cancellation_drained = True
            raise


class _ReadTimeoutClient:
    def __init__(self) -> None:
        self.closed = False

    async def __aenter__(self) -> _ReadTimeoutClient:
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        self.closed = True
        return False

    async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout")


class _DisconnectIgnoringServer:
    """A client disconnects while detached remote work keeps running."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release_server = asyncio.Event()
        self.server_work: asyncio.Task[bool] | None = None
        self.request_cancelled = False
        self.closed = False

    async def __aenter__(self) -> _DisconnectIgnoringServer:
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        self.closed = True
        return False

    async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
        self.server_work = asyncio.create_task(self.release_server.wait())
        self.started.set()
        try:
            await asyncio.shield(self.server_work)
        except asyncio.CancelledError:
            self.request_cancelled = True
            raise
        raise AssertionError("synthetic server work must not complete during the request")


@pytest.mark.asyncio
async def test_cancelling_chat_drains_the_http_request_and_closes_the_client(settings, monkeypatch) -> None:
    client = _HangingClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: client)
    router = LLMRouter(
        replace(
            settings,
            llm_enabled=True,
            llm_base_url="http://synthetic.invalid/v1",
        )
    )

    task = asyncio.create_task(router.chat([{"role": "user", "content": "synthetic"}]))
    await asyncio.wait_for(client.started.wait(), timeout=1.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.request_cancelled
    assert client.cancellation_drained
    assert client.closed


@pytest.mark.asyncio
async def test_client_cleanup_does_not_claim_that_the_remote_generation_was_aborted(
    settings, monkeypatch
) -> None:
    client = _DisconnectIgnoringServer()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: client)
    router = LLMRouter(
        replace(
            settings,
            llm_enabled=True,
            llm_base_url="http://synthetic.invalid/v1",
        )
    )

    task = asyncio.create_task(router.chat([{"role": "user", "content": "synthetic"}]))
    await asyncio.wait_for(client.started.wait(), timeout=1.0)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client.request_cancelled
        assert client.closed
        assert client.server_work is not None and not client.server_work.done()
    finally:
        # The real server needs its own abort/restart mechanism.  The synthetic
        # task is released explicitly so the test itself leaves no background work.
        client.release_server.set()
        if client.server_work is not None:
            await client.server_work


@pytest.mark.asyncio
async def test_read_timeout_closes_the_http_client_without_a_retry(settings, monkeypatch) -> None:
    client = _ReadTimeoutClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: client)
    router = LLMRouter(
        replace(
            settings,
            llm_enabled=True,
            llm_base_url="http://synthetic.invalid/v1",
        )
    )

    with pytest.raises(httpx.ReadTimeout):
        await router.chat([{"role": "user", "content": "synthetic"}])

    assert client.closed
