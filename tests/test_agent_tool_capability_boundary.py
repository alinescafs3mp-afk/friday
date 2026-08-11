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

from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _claims_an_unconfirmed_supported_deed,
    _confirmed_workspace_create_filename,
    _explicit_workspace_create_intent,
    _workspace_create_channel_request,
    _workspace_create_success_evidence,
    _workspace_reply_attachment_selector_message,
)
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


_EXACT_WORKSPACE_PROMPT = (
    "Используй именно workspace_create и создай в MCP outbox файл mcp-metadata.txt. "
    "Первая строка — только значение номера документа без подписи. Вторая строка — "
    "только значение контрольного маркера без подписи. Никаких других строк."
)


def test_workspace_reply_selector_masks_only_output_line_requirements() -> None:
    from friday.agent_runtime import (
        _ATTACHMENT_SELECTIVE_REFERENCE,
        _attachment_reference_kind,
        _attachment_selector_message,
    )

    projected = _workspace_reply_attachment_selector_message(_EXACT_WORKSPACE_PROMPT)
    assert _ATTACHMENT_SELECTIVE_REFERENCE.search(projected) is None
    assert _attachment_reference_kind(projected) == ""

    explicit_input = _workspace_reply_attachment_selector_message(
        _EXACT_WORKSPACE_PROMPT + " Возьми данные по второму документу."
    )
    assert _ATTACHMENT_SELECTIVE_REFERENCE.search(explicit_input) is not None
    assert _attachment_reference_kind(explicit_input) == "explicit"

    same_output_line_input = _workspace_reply_attachment_selector_message(
        "Используй workspace_create и создай out.txt. Первая строка — значение из второго документа."
    )
    assert _ATTACHMENT_SELECTIVE_REFERENCE.search(same_output_line_input) is not None
    assert _attachment_reference_kind(same_output_line_input) == "explicit"

    # Without a structural pointer the ordinary selector path is untouched.
    no_pointer_projection = _attachment_selector_message(
        "Используй workspace_create и создай out.txt по второму документу."
    )
    assert _ATTACHMENT_SELECTIVE_REFERENCE.search(no_pointer_projection) is not None
    assert _attachment_reference_kind(no_pointer_projection) == "explicit"


class _ForcedWorkspaceLLM:
    enabled = True
    total_budget_sec = 120.0

    def __init__(
        self,
        *,
        answer_instead: bool = False,
        bundle_alternate: bool = False,
        followup_alternate: bool = False,
    ) -> None:
        self.answer_instead = answer_instead
        self.bundle_alternate = bundle_alternate
        self.followup_alternate = followup_alternate
        self.calls = 0
        self.tool_choices: list[str | None] = []
        self.offered: list[set[str]] = []

    async def chat(
        self,
        messages,
        *,
        temperature=None,
        max_tokens=None,
        tools=None,
        tool_choice=None,
    ):
        del messages, temperature, max_tokens
        self.calls += 1
        offered = {
            str((item.get("function") or {}).get("name") or "")
            for item in (tools or [])
            if isinstance(item, dict)
        }
        self.tool_choices.append(tool_choice)
        self.offered.append(offered)
        common = {"_queue_wait_sec": 0.0, "_offered_tool_names": sorted(offered)}
        if self.calls == 1 and self.answer_instead:
            return {**common, "content": "Я якобы создал файл.", "tool_calls": None}
        if self.calls == 1:
            tool_calls = [
                {
                    "id": "call-workspace-create",
                    "function": {
                        "name": "workspace_create",
                        "arguments": json.dumps(
                            {
                                "filename": "model-chosen.txt",
                                "content": "DOC-42\nCONTROL-MARKER\n",
                            }
                        ),
                    },
                }
            ]
            if self.bundle_alternate:
                tool_calls.append(
                    {
                        "id": "call-alternate",
                        "function": {
                            "name": "memory_search",
                            "arguments": json.dumps({"query": "synthetic"}),
                        },
                    }
                )
            return {
                **common,
                "content": "",
                "tool_calls": tool_calls,
            }
        if self.calls == 2 and self.followup_alternate:
            return {
                **common,
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-unrequested-followup",
                        "function": {
                            "name": "memory_search",
                            "arguments": json.dumps({"query": "unrequested"}),
                        },
                    }
                ],
            }
        return {**common, "content": "Файл во внешнем outbox создан.", "tool_calls": None}


@pytest.mark.parametrize(
    "prompt",
    [
        'Используй "workspace_create" и создай файл quoted.txt.',
        "Как использовать workspace_create и создать example.txt?",
        "Не используй workspace_create и не создавай denied.txt.",
        "Используй workspace_create и создай denied.txt, но не создавай его на самом деле.",
        "Сохрани фразу workspace_create в файл mention.txt.",
        "`Используй workspace_create и создай файл pasted.txt.`",
        "```text\nИспользуй workspace_create и создай файл pasted.txt.\n```",
        "Use workspace_create and create file denied.txt, but do not actually create it.",
        "Use workspace_create and create file denied.txt, but don’t actually create it.",
        "Используй workspace_create и создай файл denied.txt, но не нужно создавать его.",
        "Используй workspace_create и создай файл denied.txt, но создавать его не нужно.",
        "Используй workspace_create и создай файл denied.txt, но не следует создавать его.",
        "Use workspace_create and create denied.txt, but you should not actually create it.",
        "Используй workspace_create и создай файл denied.txt, но не надо его создавать.",
        "Используй workspace_create и создай файл denied.txt, но его создавать не надо.",
        "Используй workspace_create и создай файл denied.txt, но создание не требуется.",
        "Используй workspace_create и создай файл denied.txt, но не требуется создавать.",
        "Используй workspace_create и создай файл denied.txt, но я не хочу его создавать.",
        "Use workspace_create and create denied.txt, but there is no need to create it.",
        "Use workspace_create and create denied.txt, but I do not want you to create it.",
        "Use workspace_create and create denied.txt, but do not really create it.",
        "Используй workspace_create и создай файл denied.txt, но не делай этого.",
        "Use workspace_create and create denied.txt, but refrain from creating it.",
        "Используй workspace_create и создай файл denied.txt, но отмени создание.",
    ],
)
def test_workspace_create_mentions_do_not_grant_mutation_authority(prompt: str) -> None:
    assert _workspace_create_channel_request(prompt) is False
    assert _explicit_workspace_create_intent(prompt) is None


def test_workspace_command_outside_an_inert_code_span_remains_actionable() -> None:
    prompt = "`пример данных` Используй workspace_create и создай файл outside.txt."

    assert _workspace_create_channel_request(prompt) is True
    intent = _explicit_workspace_create_intent(prompt)
    assert intent is not None
    assert intent.filename == "outside.txt"


def test_workspace_ambiguous_nearby_negation_fails_closed() -> None:
    prompt = "Используй workspace_create и создай файл result.txt; не нужно ничего кроме workspace_create."

    assert _workspace_create_channel_request(prompt) is False
    assert _explicit_workspace_create_intent(prompt) is None


@pytest.mark.parametrize(
    ("claim", "unconfirmed"),
    [
        ("Файл во внешнем MCP outbox создан.", False),
        ("Файл mcp-metadata.txt во внешнем MCP outbox создан.", False),
        ("Файл other.txt во внешнем MCP outbox создан.", True),
        ("Файл mcp-metadata.txt во внешнем MCP outbox создан и прикреплён в чат.", True),
        ("Файл mcp-metadata.txt во внешнем MCP outbox создан и отправлен вам.", True),
    ],
)
def test_external_workspace_deed_proves_only_exact_outbox_creation(
    claim: str,
    unconfirmed: bool,
) -> None:
    descriptor = "workspace_create: внешний MCP outbox; файл mcp-metadata.txt создан и сохранён"

    assert (
        _claims_an_unconfirmed_supported_deed(
            claim,
            has_file=False,
            reminder_succeeded=False,
            external_file_descriptors=[descriptor],
        )
        is unconfirmed
    )


@pytest.mark.parametrize(
    "prompt",
    [
        'Используй workspace_create и создай файл "quoted.txt".',
        "Используй workspace_create и создай a.txt и b.txt.",
        "Используй workspace_create и создай unsupported.docx.",
    ],
)
def test_workspace_create_ambiguous_or_unsafe_names_fail_closed(prompt: str) -> None:
    assert _workspace_create_channel_request(prompt) is True
    assert _explicit_workspace_create_intent(prompt) is None


@pytest.mark.asyncio
async def test_exact_workspace_intent_forces_one_call_and_code_owns_filename(
    settings,
    storage,
):
    storage.ensure_user("alice")
    llm = _ForcedWorkspaceLLM()
    kernel = _RecordingKernel()
    runtime = AgentRuntime(settings, storage, llm=llm, kernel=kernel)  # type: ignore[arg-type]
    context = AgentContext(
        conversation_id="synthetic-forced-workspace",
        user_id="alice",
        interaction_mode="dialogue",
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        _EXACT_WORKSPACE_PROMPT,
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=[_tool_schema("workspace_create"), _tool_schema("memory_search")],
        attachments=None,
    )

    assert llm.tool_choices == ["workspace_create", None]
    assert llm.offered[0] == {"workspace_create", "memory_search"}
    assert llm.offered[1] == set()
    assert kernel.executed == [
        (
            "workspace_create",
            {"filename": "mcp-metadata.txt", "content": "DOC-42\nCONTROL-MARKER\n"},
        )
    ]
    assert result["content"] == "Файл во внешнем outbox создан."
    assert result["tools_used"] == ["workspace_create"]
    assert (
        _confirmed_workspace_create_filename(
            _EXACT_WORKSPACE_PROMPT,
            result["tool_evidence"],
        )
        == "mcp-metadata.txt"
    )
    assert (
        _confirmed_workspace_create_filename(
            _EXACT_WORKSPACE_PROMPT,
            [
                {
                    "tool": "workspace_create",
                    "output": _workspace_create_success_evidence("wrong-name.txt"),
                }
            ],
        )
        == ""
    )


@pytest.mark.asyncio
async def test_forced_workspace_plain_answer_never_mutates_or_retries(
    settings,
    storage,
):
    storage.ensure_user("alice")
    llm = _ForcedWorkspaceLLM(answer_instead=True)
    kernel = _RecordingKernel()
    runtime = AgentRuntime(settings, storage, llm=llm, kernel=kernel)  # type: ignore[arg-type]

    result = await runtime._agentic_loop(  # noqa: SLF001
        AgentContext(
            conversation_id="synthetic-workspace-plain-answer",
            user_id="alice",
            interaction_mode="dialogue",
        ),
        _EXACT_WORKSPACE_PROMPT,
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=[_tool_schema("workspace_create")],
        attachments=None,
    )

    assert llm.calls == 1
    assert llm.tool_choices == ["workspace_create"]
    assert kernel.executed == []
    assert result["tools_used"] == []
    assert "Файл не создан" in result["content"]


@pytest.mark.asyncio
async def test_forced_workspace_round_rejects_all_bundled_side_effects(
    settings,
    storage,
):
    storage.ensure_user("alice")
    llm = _ForcedWorkspaceLLM(bundle_alternate=True)
    kernel = _RecordingKernel()
    runtime = AgentRuntime(settings, storage, llm=llm, kernel=kernel)  # type: ignore[arg-type]

    result = await runtime._agentic_loop(  # noqa: SLF001
        AgentContext(
            conversation_id="synthetic-workspace-bundled-call",
            user_id="alice",
            interaction_mode="dialogue",
        ),
        _EXACT_WORKSPACE_PROMPT,
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=[_tool_schema("workspace_create"), _tool_schema("memory_search")],
        attachments=None,
    )

    assert llm.calls == 1
    assert kernel.executed == []
    assert result["tools_used"] == []
    assert "единственный требуемый вызов" in result["content"]


@pytest.mark.asyncio
async def test_workspace_followup_is_schema_less_and_cannot_execute_another_effect(
    settings,
    storage,
):
    storage.ensure_user("alice")
    llm = _ForcedWorkspaceLLM(followup_alternate=True)
    kernel = _RecordingKernel()
    runtime = AgentRuntime(settings, storage, llm=llm, kernel=kernel)  # type: ignore[arg-type]

    result = await runtime._agentic_loop(  # noqa: SLF001
        AgentContext(
            conversation_id="synthetic-workspace-followup-effect",
            user_id="alice",
            interaction_mode="dialogue",
        ),
        _EXACT_WORKSPACE_PROMPT,
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=[_tool_schema("workspace_create"), _tool_schema("memory_search")],
        attachments=None,
    )

    assert llm.tool_choices == ["workspace_create", None, None]
    assert llm.offered == [{"workspace_create", "memory_search"}, set(), set()]
    assert kernel.executed == [
        (
            "workspace_create",
            {"filename": "mcp-metadata.txt", "content": "DOC-42\nCONTROL-MARKER\n"},
        )
    ]
    assert result["tools_used"] == ["workspace_create"]


@pytest.mark.asyncio
async def test_workspace_intent_without_authorized_schema_fails_before_model(
    settings,
    storage,
):
    storage.ensure_user("mallory")
    llm = _ForcedWorkspaceLLM()
    kernel = _RecordingKernel()
    runtime = AgentRuntime(settings, storage, llm=llm, kernel=kernel)  # type: ignore[arg-type]

    result = await runtime._agentic_loop(  # noqa: SLF001
        AgentContext(
            conversation_id="synthetic-workspace-unavailable",
            user_id="mallory",
            interaction_mode="dialogue",
        ),
        _EXACT_WORKSPACE_PROMPT,
        ActorContext(user_id="mallory", preset_key="guest", source="test"),
        tools=[_tool_schema("memory_search")],
        attachments=None,
    )

    assert llm.calls == 0
    assert kernel.executed == []
    assert result["tools_used"] == []
    assert "workspace_create недоступен" in result["content"]


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
async def test_forced_workspace_schema_refusal_strips_choice_and_never_retries_mutation(
    settings,
    storage,
    monkeypatch,
):
    endpoint = _SyntheticChatEndpoint(refuse_tools_once=True)
    _install_endpoint(monkeypatch, endpoint)
    tuned = replace(settings, llm_enabled=True)
    router = _RecordingRouter(tuned)
    kernel = _RecordingKernel()
    runtime = AgentRuntime(tuned, storage, llm=router, kernel=kernel)  # type: ignore[arg-type]

    result = await runtime._agentic_loop(  # noqa: SLF001
        AgentContext(
            conversation_id="synthetic-forced-workspace-schema-refusal",
            user_id="alice",
            interaction_mode="dialogue",
        ),
        _EXACT_WORKSPACE_PROMPT,
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=[_tool_schema("workspace_create")],
        attachments=None,
    )

    assert len(endpoint.payloads) == 2
    assert endpoint.payloads[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "workspace_create"},
    }
    assert "tools" in endpoint.payloads[0]
    assert "tools" not in endpoint.payloads[1]
    assert "tool_choice" not in endpoint.payloads[1]
    assert router.results[0].get("_offered_tool_names") == []
    assert kernel.executed == []
    assert result["tools_used"] == []
    assert "Файл не создан" in result["content"]


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
