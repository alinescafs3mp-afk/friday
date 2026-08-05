from __future__ import annotations

import json
from dataclasses import replace

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
