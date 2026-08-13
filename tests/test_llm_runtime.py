from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from friday.agent_runtime.llm import LLMRouter, _fit_messages_to_context, _system_first


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
