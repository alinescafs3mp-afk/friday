from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest

from friday.agent_runtime.llm import (
    LLMDeadlineError,
    LLMRouter,
    _fit_messages_to_context,
    _system_first,
)


def test_qwen_prompt_collapses_all_system_messages_in_order():
    messages = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "old question"},
        {"role": "system", "content": "retrieved knowledge"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new question"},
    ]
    normalized = _system_first(messages)
    assert [item["role"] for item in normalized] == ["system", "user", "assistant", "user"]
    assert normalized[0]["content"] == "policy\n\nretrieved knowledge"
    assert normalized[-1]["content"] == "new question"


def test_qwen_prompt_noop_for_one_leading_system_message():
    messages = [{"role": "system", "content": "policy"}, {"role": "user", "content": "hello"}]
    assert _system_first(messages) is messages


def test_context_fitting_keeps_latest_turn_and_tool_protocol_group():
    messages = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "old" * 20_000},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "memory_search", "arguments": json.dumps({"query": "alpha"})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result" * 10_000},
        {"role": "user", "content": "latest question"},
    ]
    fitted = _fit_messages_to_context(messages, max_model_len=2048, max_output_tokens=512)
    assert fitted[0]["role"] == "system"
    assert fitted[-1]["content"] == "latest question"
    roles = [item["role"] for item in fitted]
    if "tool" in roles:
        tool_index = roles.index("tool")
        assert tool_index > 0
        assert fitted[tool_index - 1].get("tool_calls")


def test_qwen_payload_disables_model_thinking_and_has_one_system(settings):
    router = LLMRouter(replace(settings, llm_enabled=True))
    payload = router._prepare_payload(
        [
            {"role": "system", "content": "A"},
            {"role": "user", "content": "context"},
            {"role": "system", "content": "B"},
            {"role": "user", "content": "question"},
        ],
        temperature=None,
        max_tokens=256,
        tools=None,
    )
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert sum(item["role"] == "system" for item in payload["messages"]) == 1
    assert payload["messages"][0]["role"] == "system"


@pytest.mark.asyncio
async def test_foreground_activity_is_a_watchdog_signal_without_an_extra_request(
    settings,
    monkeypatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_chat(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"content": "healthy"}

    router = LLMRouter(replace(settings, llm_enabled=True))
    monkeypatch.setattr(router, "_chat_impl", blocked_chat)
    task = asyncio.create_task(router.chat([{"role": "user", "content": "hello"}]))
    await entered.wait()

    assert router.generation_watchdog_activity(recent_success_sec=120.0) == (True, False)

    release.set()
    assert (await task)["content"] == "healthy"
    assert router.generation_watchdog_activity(recent_success_sec=120.0) == (False, True)


@pytest.mark.asyncio
async def test_failed_foreground_call_is_not_reused_as_watchdog_health(
    settings,
    monkeypatch,
) -> None:
    async def failed_chat(*_args, **_kwargs):
        raise RuntimeError("synthetic failure")

    router = LLMRouter(replace(settings, llm_enabled=True))
    monkeypatch.setattr(router, "_chat_impl", failed_chat)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        await router.chat([{"role": "user", "content": "hello"}])

    assert router.generation_watchdog_activity(recent_success_sec=120.0) == (False, False)


def test_exact_tool_choice_is_emitted_only_for_an_effectively_offered_schema(settings):
    router = LLMRouter(replace(settings, llm_enabled=True))
    schema = {
        "type": "function",
        "function": {
            "name": "workspace_create",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    messages = [{"role": "user", "content": "synthetic"}]

    selected = router._prepare_payload(  # noqa: SLF001
        messages,
        temperature=None,
        max_tokens=256,
        tools=[schema],
        tool_choice="workspace_create",
    )
    unoffered = router._prepare_payload(  # noqa: SLF001
        messages,
        temperature=None,
        max_tokens=256,
        tools=[schema],
        tool_choice="memory_search",
    )
    router._tools_refused = True  # noqa: SLF001 - proved endpoint capability latch
    refused = router._prepare_payload(  # noqa: SLF001
        messages,
        temperature=None,
        max_tokens=256,
        tools=[schema],
        tool_choice="workspace_create",
    )

    assert selected["tool_choice"] == {
        "type": "function",
        "function": {"name": "workspace_create"},
    }
    assert "tool_choice" not in unoffered
    assert "tools" not in refused
    assert "tool_choice" not in refused


@pytest.mark.asyncio
async def test_vision_can_accept_valid_ocr_json_with_a_long_visual_separator(
    settings,
    monkeypatch,
):
    content = '{"text":"____________","confidence":0.9}'

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **_kwargs):
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "choices": [
                        {
                            "message": {"content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: _Client())
    router = LLMRouter(replace(settings, llm_enabled=True))

    with pytest.raises(RuntimeError, match="repeated-token degeneration"):
        await router.chat([{"role": "user", "content": "ordinary chat"}])

    result = await router.chat(
        [{"role": "user", "content": "bounded vision JSON"}],
        reject_repeated_token_degeneration=False,
    )
    assert result["content"] == content


@pytest.mark.asyncio
async def test_repeated_generation_gets_one_bounded_internal_recovery(
    settings,
    monkeypatch,
) -> None:
    looping = "loop " * 20
    payloads: list[dict] = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            payloads.append(dict(kwargs["json"]))
            content = looping if len(payloads) == 1 else "stable recovered answer"
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: _Client())
    router = LLMRouter(replace(settings, llm_enabled=True))

    result = await router.chat(
        [{"role": "user", "content": "bounded recovery"}],
        temperature=0.0,
        max_tokens=640,
    )

    assert result["content"] == "stable recovered answer"
    assert len(payloads) == 2
    assert payloads[0]["temperature"] == 0.0
    assert payloads[0]["max_tokens"] == 640
    assert payloads[1]["temperature"] == 0.1
    assert payloads[1]["max_tokens"] == 384


@pytest.mark.asyncio
async def test_retry_opt_out_rejects_degeneration_after_one_post(
    settings,
    monkeypatch,
) -> None:
    posts = 0

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **_kwargs):
            nonlocal posts
            posts += 1
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "choices": [{"message": {"content": "loop " * 20}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: _Client())
    router = LLMRouter(replace(settings, llm_enabled=True))

    with pytest.raises(RuntimeError, match="repeated-token degeneration"):
        await router.chat(
            [{"role": "user", "content": "one-shot accepted evidence"}],
            allow_retries=False,
        )

    assert posts == 1


@pytest.mark.asyncio
async def test_absolute_deadline_expires_in_queue_without_post_or_semaphore_leak(
    settings,
    monkeypatch,
) -> None:
    posts = 0

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **_kwargs):
            nonlocal posts
            posts += 1
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "choices": [{"message": {"content": "healthy"}, "finish_reason": "stop"}],
                    "usage": {},
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: _Client())
    router = LLMRouter(replace(settings, llm_enabled=True))
    initial_slots = router._foreground_sem._value  # noqa: SLF001
    for _slot in range(initial_slots):
        await router._foreground_sem.acquire()  # noqa: SLF001
    try:
        with pytest.raises(LLMDeadlineError) as raised:
            await router.chat(
                [{"role": "user", "content": "must expire before admission"}],
                absolute_deadline=time.monotonic() + 0.03,
            )
    finally:
        for _slot in range(initial_slots):
            router._foreground_sem.release()  # noqa: SLF001

    assert raised.value.phase == "admission"
    assert posts == 0
    result = await router.chat([{"role": "user", "content": "permit remains usable"}])
    assert result["content"] == "healthy"
    assert posts == 1
    assert router._foreground_sem._value == initial_slots  # noqa: SLF001


@pytest.mark.asyncio
async def test_absolute_deadline_bounds_submitted_request_and_closes_client(
    settings,
    monkeypatch,
) -> None:
    observed: dict[str, object] = {"cancelled": False, "closed": False}

    class _Client:
        def __init__(self, *args, **kwargs):
            observed["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            observed["closed"] = True
            return False

        async def post(self, _url, **_kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                observed["cancelled"] = True
                raise

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    router = LLMRouter(replace(settings, llm_enabled=True))
    initial_slots = router._foreground_sem._value  # noqa: SLF001

    with pytest.raises(LLMDeadlineError) as raised:
        await router.chat(
            [{"role": "user", "content": "submitted request must be bounded"}],
            absolute_deadline=time.monotonic() + 0.05,
        )

    timeout = observed["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert 0 < timeout.read <= 0.05
    assert 0 < timeout.connect <= 0.05
    assert raised.value.phase == "submitted"
    assert observed["cancelled"] is True
    assert observed["closed"] is True
    assert router._foreground_sem._value == initial_slots  # noqa: SLF001
    assert router._silent_until > time.monotonic()  # noqa: SLF001


@pytest.mark.asyncio
async def test_scoped_document_deadline_does_not_poison_the_next_user_turn(
    settings,
    monkeypatch,
) -> None:
    posts = 0
    first_cancelled = False

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **_kwargs):
            nonlocal posts, first_cancelled
            posts += 1
            if posts == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    first_cancelled = True
                    raise
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "choices": [{"message": {"content": "next turn is healthy"}, "finish_reason": "stop"}],
                    "usage": {},
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: _Client())
    router = LLMRouter(replace(settings, llm_enabled=True))

    with pytest.raises(LLMDeadlineError) as raised:
        await router.chat(
            [{"role": "user", "content": "bounded document stage"}],
            absolute_deadline=time.monotonic() + 0.03,
            allow_retries=False,
            open_silent_cooldown=False,
        )

    assert raised.value.phase == "submitted"
    assert first_cancelled is True
    assert router._silent_until == 0.0  # noqa: SLF001
    recovered = await router.chat([{"role": "user", "content": "independent next turn"}])
    assert recovered["content"] == "next turn is healthy"
    assert posts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ("http_503", "transport"))
async def test_retry_opt_out_sends_one_post_and_never_backs_off(
    settings,
    monkeypatch,
    failure_kind: str,
) -> None:
    posts: list[str] = []
    sleeps: list[float] = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **_kwargs):
            posts.append(url)
            request = httpx.Request("POST", url)
            if failure_kind == "http_503":
                return httpx.Response(503, request=request, text="temporarily unavailable")
            raise httpx.ConnectError("synthetic connection failure", request=request)

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: _Client())
    monkeypatch.setattr("friday.agent_runtime.llm.asyncio.sleep", record_sleep)
    router = LLMRouter(replace(settings, llm_enabled=True))

    with pytest.raises((httpx.HTTPStatusError, httpx.ConnectError)):
        await router.chat(
            [{"role": "user", "content": "one accepted-evidence synthesis"}],
            allow_retries=False,
        )

    assert len(posts) == 1
    assert sleeps == []
