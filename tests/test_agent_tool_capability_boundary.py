"""The model may execute only schemas used for the successful generation.

Tool calls are model output, including native provider calls and the JSON
fallback emitted as assistant text.  Neither form grants a capability.  The
runtime must compare the requested name with the schemas actually offered for
that generation, while the transport must report when it retried a rejected
tool request without schemas.

All strings and payloads in this module are synthetic.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.agent_runtime.llm import LLMRouter
from friday.execution_kernel import ToolResult
from friday.permissions import ActorContext


def _tool_schema(name: str, *, description: str = "synthetic") -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


class _RecordingKernel:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, name, arguments, *, actor=None):  # noqa: ANN001, ARG002
        self.executed.append((name, dict(arguments)))
        return ToolResult(name, True, data={"unexpected": True})


class _HallucinatedUnofferedToolLLM:
    enabled = True
    total_budget_sec = 120.0

    def __init__(self, protocol: str) -> None:
        self.protocol = protocol
        self.calls = 0
        self.seen: list[list[dict[str, Any]]] = []
        self.offered: list[set[str]] = []

    async def chat(self, messages, *, temperature=None, max_tokens=None, tools=None):
        del temperature, max_tokens
        self.calls += 1
        self.seen.append(copy.deepcopy(messages))
        offered = {
            str((item.get("function") or {}).get("name") or "")
            for item in (tools or [])
            if isinstance(item, dict)
        }
        self.offered.append(offered)
        common = {"_queue_wait_sec": 0.0, "_offered_tool_names": sorted(offered)}
        if self.calls == 1 and self.protocol == "native":
            return {
                **common,
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-unoffered-native",
                        "function": {
                            "name": "code_run",
                            "arguments": json.dumps({"code": "print('synthetic')"}),
                        },
                    }
                ],
            }
        if self.calls == 1:
            return {
                **common,
                "content": json.dumps(
                    {
                        "name": "code_run",
                        "arguments": {"code": "print('synthetic')"},
                    }
                ),
                "tool_calls": None,
            }
        return {**common, "content": "Готово без закрытого инструмента.", "tool_calls": None}


@pytest.mark.parametrize("protocol", ["native", "textual"])
@pytest.mark.asyncio
async def test_a_hallucinated_unoffered_tool_never_reaches_the_kernel(
    protocol,
    settings,
    storage,
):
    """Mutation: delete the unoffered-name guard and ``code_run`` executes."""

    storage.ensure_user("alice")
    llm = _HallucinatedUnofferedToolLLM(protocol)
    kernel = _RecordingKernel()
    runtime = AgentRuntime(settings, storage, llm=llm, kernel=kernel)  # type: ignore[arg-type]
    context = AgentContext(
        conversation_id="synthetic-capability-boundary",
        user_id="alice",
        interaction_mode="dialogue",
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Синтетическая проверка границы возможностей",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=[_tool_schema("memory_search")],
        attachments=None,
    )

    assert llm.offered and llm.offered[0] == {"memory_search"}
    assert kernel.executed == []
    assert result["tools_used"] == []
    assert result["tool_evidence"] == []
    second_round = "\n".join(
        str(item.get("content") or "") for item in llm.seen[1] if item.get("role") == "tool"
    )
    assert second_round == "Инструмент недоступен в этом ходе."


class _SyntheticChatEndpoint:
    """A deterministic OpenAI-compatible endpoint with an optional tool refusal."""

    def __init__(self, *, refuse_tools_once: bool) -> None:
        self.refuse_tools_once = refuse_tools_once
        self.payloads: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        payload = copy.deepcopy(kwargs["json"])
        self.payloads.append(payload)
        request = httpx.Request("POST", url)
        if self.refuse_tools_once and len(self.payloads) == 1:
            return httpx.Response(
                400,
                request=request,
                text=(
                    '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set'
                ),
            )
        if self.refuse_tools_once and len(self.payloads) == 2:
            return httpx.Response(
                200,
                request=request,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-schema-less",
                                        "type": "function",
                                        "function": {
                                            "name": "code_run",
                                            "arguments": json.dumps(
                                                {"code": "print('schema-less synthetic')"}
                                            ),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "Готово без исполнения.", "tool_calls": []},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )


def _install_endpoint(monkeypatch, endpoint: _SyntheticChatEndpoint) -> None:
    class _Context:
        async def __aenter__(self):
            return endpoint

        async def __aexit__(self, *args):  # noqa: ANN002
            return False

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: _Context())


class _RecordingRouter(LLMRouter):
    def __init__(self, settings) -> None:  # noqa: ANN001
        super().__init__(settings)
        self.results: list[dict[str, Any]] = []

    async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
        result = await super().chat(*args, **kwargs)
        self.results.append(copy.deepcopy(result))
        return result


@pytest.mark.asyncio
async def test_schema_less_transport_fallback_revokes_the_nominal_capability(
    settings,
    storage,
    monkeypatch,
):
    """A schema rejected by vLLM is absent from the generation that follows."""

    endpoint = _SyntheticChatEndpoint(refuse_tools_once=True)
    _install_endpoint(monkeypatch, endpoint)
    tuned = replace(settings, llm_enabled=True)
    router = _RecordingRouter(tuned)
    kernel = _RecordingKernel()
    runtime = AgentRuntime(tuned, storage, llm=router, kernel=kernel)  # type: ignore[arg-type]
    context = AgentContext(
        conversation_id="synthetic-schema-fallback",
        user_id="alice",
        interaction_mode="dialogue",
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Синтетическая проверка fallback",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=[_tool_schema("code_run")],
        attachments=None,
    )

    assert "tools" in endpoint.payloads[0]
    assert all("tools" not in payload for payload in endpoint.payloads[1:])
    assert router.results[0].get("_offered_tool_names") == []
    assert kernel.executed == []
    assert result["tools_used"] == []
    assert result["tool_evidence"] == []


@pytest.mark.asyncio
async def test_transport_capability_signal_is_bounded_and_copies_no_schema_content(
    settings,
    monkeypatch,
):
    """Only bounded capability identifiers may accompany the model response."""

    endpoint = _SyntheticChatEndpoint(refuse_tools_once=False)
    _install_endpoint(monkeypatch, endpoint)
    router = LLMRouter(replace(settings, llm_enabled=True))
    schema_secret = "SYNTHETIC-SCHEMA-DESCRIPTION-SECRET"
    tools = [_tool_schema(f"synthetic_tool_{index:03d}", description=schema_secret) for index in range(80)]

    result = await router.chat(
        [{"role": "user", "content": "Синтетическая проверка сигнала"}],
        tools=tools,
    )

    names = result.get("_offered_tool_names")
    assert isinstance(names, list)
    assert 0 < len(names) < len(tools)
    assert names == [f"synthetic_tool_{index:03d}" for index in range(len(names))]
    assert schema_secret not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_a_tool_refusal_latch_keeps_later_chats_schema_less(
    settings,
    monkeypatch,
):
    """One proven endpoint limitation must not cost another rejected request per chat."""

    endpoint = _SyntheticChatEndpoint(refuse_tools_once=True)
    _install_endpoint(monkeypatch, endpoint)
    router = LLMRouter(replace(settings, llm_enabled=True))
    tools = [_tool_schema("code_run")]

    first = await router.chat(
        [{"role": "user", "content": "Первая синтетическая проверка"}],
        tools=tools,
    )
    second = await router.chat(
        [{"role": "user", "content": "Вторая синтетическая проверка"}],
        tools=tools,
    )

    assert ["tools" in payload for payload in endpoint.payloads] == [True, False, False]
    assert first.get("_offered_tool_names") == []
    assert second.get("_offered_tool_names") == []
