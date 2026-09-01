"""Focused compatibility seams for the R8A model-control rework."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.agent_runtime.llm import _strip_tool_call_markup
from friday.agent_runtime.tool_protocol import classify_tool_turn, contains_internal_tool_output
from friday.execution_kernel import ToolResult
from friday.permissions import ActorContext


def _tool_schema(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "synthetic compatibility probe",
            "parameters": {"type": "object", "properties": {}},
        },
    }


class _TextualCallModel:
    enabled = True
    total_budget_sec = 120.0

    def __init__(self) -> None:
        self.calls = 0
        self.offered: list[set[str]] = []

    async def chat(self, messages, *, tools=None, **kwargs):  # noqa: ANN001, ANN003
        del messages, kwargs
        self.calls += 1
        offered = {
            str((item.get("function") or {}).get("name") or "")
            for item in (tools or [])
            if isinstance(item, dict)
        }
        self.offered.append(offered)
        common = {"_queue_wait_sec": 0.0, "_offered_tool_names": sorted(offered)}
        if self.calls == 1:
            return {
                **common,
                "content": json.dumps(
                    {"name": "memory_search", "arguments": {"query": "synthetic"}},
                    separators=(",", ":"),
                ),
                "tool_calls": None,
            }
        return {**common, "content": "Synthetic answer.", "tool_calls": None}


class _RecordingKernel:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, name, arguments, *, actor=None):  # noqa: ANN001, ARG002
        self.executed.append((name, dict(arguments)))
        return ToolResult(name, True, data={"results": []})


def test_dominant_missing_colon_slots_survive_candidate_decoys() -> None:
    content = ("{x} " * 64) + "{name memory_search arguments " + ("{x} " * 40) + ("p" * 2_000)

    assert contains_internal_tool_output(content) is True
    assert classify_tool_turn(content).kind == "protocol_error"


def test_apostrophe_before_dominant_malformed_root_cannot_hide_it() -> None:
    content = "Here's the carrier: {name memory_search arguments " + ("p" * 2_000)

    assert contains_internal_tool_output(content) is True
    assert classify_tool_turn(content).kind == "protocol_error"


def test_dominant_malformed_root_after_the_old_scan_window_fails_closed() -> None:
    content = ("p" * 64_001) + "{name memory_search arguments " + ("x" * 100_000)

    assert contains_internal_tool_output(content) is True
    assert classify_tool_turn(content).kind == "protocol_error"


def test_short_closed_malformed_example_does_not_own_following_prose() -> None:
    examples = (
        "Example: {name demo arguments {}}. ",
        "Example: {name demo arguments it's invalid}. ",
        "Example: {name demo arguments users' data}. ",
    )

    for prefix in examples:
        content = prefix + ("This is ordinary documentation. " * 40)
        assert contains_internal_tool_output(content) is False
        assert classify_tool_turn(content).kind == "answer"


def test_malformed_spelling_inside_ordinary_json_string_remains_data() -> None:
    literal = "{name demo arguments " + ("literal documentation " * 80)
    contents = (
        json.dumps({"description": literal}, separators=(",", ":")),
        json.dumps([literal], separators=(",", ":")),
    )

    for content in contents:
        assert contains_internal_tool_output(content) is False
        assert classify_tool_turn(content).kind == "answer"


def test_quoted_malformed_syntax_in_prose_remains_an_answer() -> None:
    content = (
        'Documentation quotes "{name demo arguments ' + ("literal padding " * 200) + '" as malformed syntax.'
    )

    assert contains_internal_tool_output(content) is False
    assert classify_tool_turn(content).kind == "answer"


@pytest.mark.parametrize(("opening", "closing"), [("\u00ab", "\u00bb"), ("\u201c", "\u201d")])
def test_typographic_quoted_malformed_syntax_in_prose_remains_an_answer(
    opening: str,
    closing: str,
) -> None:
    content = (
        f"Documentation quotes {opening}{{name demo arguments "
        + ("literal padding " * 200)
        + f"{closing} as malformed syntax."
    )

    assert contains_internal_tool_output(content) is False
    assert classify_tool_turn(content).kind == "answer"


def test_repeated_unclosed_tag_names_remain_literal_documentation() -> None:
    content = (
        "Documentation says <tool_call> opens a block; it also repeats <tool_call> as the literal spelling."
    )

    turn = classify_tool_turn(content)
    assert turn.kind == "answer"
    assert turn.text == content
    assert contains_internal_tool_output(content) is False
    assert _strip_tool_call_markup(content) == content


def test_leading_unclosed_literal_tag_name_remains_documentation() -> None:
    content = "<tool_call> is the literal spelling of an opening tag."

    assert _strip_tool_call_markup(content) == content


def test_unclosed_structural_tag_body_stays_fail_closed_when_padded() -> None:
    controls = (
        (
            'Prefix <tool_call>{"name":"memory_save","arguments":{"content":"x"}} '
            + ("ordinary-looking tail " * 80)
        ),
        "Prefix <tool_call>memory_save",
        "Prefix <tool_call>name memory_save arguments " + ("p" * 800),
        'Prefix <tool_call>\nmemory_save\n{"content":"x"}',
    )

    assert all(classify_tool_turn(content).kind == "protocol_error" for content in controls)
    assert all(_strip_tool_call_markup(content) == "Prefix" for content in controls)

    late_code_style = 'x <tool_call>memory_search.search(query="x")'
    assert classify_tool_turn(late_code_style).kind == "protocol_error"


@pytest.mark.parametrize("label", ["python", "bash", "text", "custom"])
def test_only_json_owned_fences_can_normalize_tool_calls(label: str) -> None:
    payload = '{"name":"memory_save","arguments":{"content":"x"}}'

    fenced = f"```{label}\n{payload}\n```"
    turn = classify_tool_turn(fenced)
    assert turn.kind == "protocol_error"
    assert turn.calls == ()


@pytest.mark.parametrize("label", ["", "json"])
def test_released_json_fence_dialects_keep_exact_normalization(label: str) -> None:
    payload = '{"name":"memory_save","arguments":{"content":"x"}}'

    turn = classify_tool_turn(f"```{label}\n{payload}\n```")
    assert turn.kind == "tool"
    assert [(call.name, call.arguments) for call in turn.calls] == [("memory_save", {"content": "x"})]


def test_canonical_textual_call_keeps_exact_normalization() -> None:
    turn = classify_tool_turn('{"name":"memory_search","arguments":{"query":"synthetic"}}')

    assert turn.kind == "tool"
    assert [(call.name, call.arguments) for call in turn.calls] == [("memory_search", {"query": "synthetic"})]


@pytest.mark.asyncio
async def test_offered_canonical_textual_call_dispatches_once(settings, storage) -> None:
    storage.ensure_user("alice")
    model = _TextualCallModel()
    kernel = _RecordingKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        AgentContext(
            conversation_id="synthetic-r8a-textual-call",
            user_id="alice",
            interaction_mode="dialogue",
        ),
        "Find the synthetic record.",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=[_tool_schema("memory_search")],
        attachments=None,
    )

    assert model.offered and model.offered[0] == {"memory_search"}
    assert kernel.executed == [("memory_search", {"query": "synthetic"})]
    assert result["tools_used"] == ["memory_search"]
    assert result["content"] == "Synthetic answer."
