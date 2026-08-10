"""Closed tag inventories and lexically authorised late file effects."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _fast_tag_inventory_intent,
    _is_direct_file_request,
)
from friday.execution_kernel import ToolResult
from friday.permissions import ActorContext


def _tool(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def _frozen_tag_questions() -> list[pytest.ParamSpec]:
    params: list[pytest.ParamSpec] = []
    fixture_dir = Path(__file__).with_name("fixtures")
    for battery in ("a", "b"):
        manifest = json.loads(
            (fixture_dir / f"synthetic_live_battery_{battery}.json").read_text(encoding="utf-8")
        )
        for pass_spec in manifest["passes"]:
            profile = pass_spec["oracle_profile"]
            for index, question in enumerate(pass_spec["questions"], 1):
                expects_inventory = profile == "k03_tag_inventory" or (
                    profile == "tools_and_fallback" and index % 2 == 1
                )
                if expects_inventory:
                    params.append(
                        pytest.param(
                            question,
                            id=f"{battery.upper()}-{pass_spec['pass_id']}-{index:02d}",
                        )
                    )
    return params


class _TagKernel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, name: str, arguments: dict[str, Any], *, actor: Any) -> ToolResult:
        del actor
        self.calls.append((name, dict(arguments)))
        if name == "list_tags":
            return ToolResult(
                name,
                True,
                {
                    "tags": [
                        {"tag": "syn-tag-alpha", "count": 2},
                        {"tag": "syn-tag-beta", "count": 1},
                        {"tag": "syn-tag-gamma", "count": 1},
                    ],
                    "count": 3,
                    "total": 3,
                    "truncated": False,
                },
            )
        if name == "kg_stats":
            return ToolResult(
                name,
                True,
                {
                    "knowledge_object_count": 7,
                    "raw_object_count": 5,
                    "file_count": 3,
                    "entity_count": 2,
                    "relation_count": 1,
                },
            )
        raise AssertionError(f"unexpected aggregate call: {name}")


class _TagLLM:
    enabled = True
    total_budget_sec = 5.0

    def __init__(self, *, semantic: str = "tag_inventory", remainder: str = "") -> None:
        self.semantic = semantic
        self.remainder = remainder
        self.calls: list[str] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, str]:
        del kwargs
        system = str(messages[0].get("content") or "")
        self.calls.append(system)
        if "Часть просьбы человека уже решена" in system:
            return {"content": json.dumps({"остаток": self.remainder}, ensure_ascii=False)}
        if "tag_inventory|none" in system:
            return {"content": json.dumps({"intent": self.semantic})}
        if "Классифицируй запрос числа" in system:
            return {"content": json.dumps({"scope": "none", "metric": "none"})}
        raise AssertionError("unexpected model seam")


async def _run_tag_prefetch(
    question: str,
    *,
    llm: _TagLLM | None = None,
    outward_kind: str = "знание",
) -> tuple[AgentContext, _TagKernel, _TagLLM, list[dict[str, Any]], list[str]]:
    kernel = _TagKernel()
    chosen_llm = llm or _TagLLM()
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = kernel
    runtime.llm = chosen_llm
    context = AgentContext(
        conversation_id="synthetic",
        user_id="synthetic",
        # The aggregate route must not depend on a broad outer arbiter choosing
        # the same noun for every natural inventory wording.
        outward_verdict=(outward_kind, None),
    )
    tools = [_tool("kg_stats"), _tool("list_tags"), _tool("memory_search")]
    tools_used: list[str] = []
    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        question,
        ActorContext(user_id="synthetic", preset_key="owner", source="test"),
        tools,
        [],
        tools_used,
        [],
        context,
    )
    return context, kernel, chosen_llm, tools, tools_used


@pytest.mark.parametrize("question", _frozen_tag_questions())
@pytest.mark.asyncio
async def test_every_frozen_tag_variant_is_one_exact_list_tags_read(question: str) -> None:
    assert _fast_tag_inventory_intent(question) is True

    context, kernel, llm, tools, tools_used = await _run_tag_prefetch(
        question,
        outward_kind="архив",
    )

    assert kernel.calls == [("list_tags", {})]
    assert tools_used == ["list_tags"]
    assert {item["function"]["name"] for item in tools} == {"memory_search"}
    assert context.structural_answer.count("Теги личного архива:") == 1
    assert context.structural_answer.count("syn-tag-alpha — 2") == 1
    assert context.structural_answer.count("syn-tag-beta — 1") == 1
    assert context.structural_answer.count("syn-tag-gamma — 1") == 1
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert not any("Часть просьбы человека уже решена" in call for call in llm.calls)
    assert not any("Классифицируй запрос числа" in call for call in llm.calls)


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("Покажи документы по тегу Альфа.", id="specific-tag"),
        pytest.param("Что написано про систему тегов в документе Альфа?", id="content"),
        pytest.param("Покажи список меток в приложенном файле.", id="local-attachment"),
        pytest.param("Не показывай список тегов моего архива.", id="negated"),
        pytest.param(
            "Почему фраза «покажи список тегов моей базы» звучит как команда?",
            id="quoted-mention",
        ),
        pytest.param("Сколько всего тегов в моём архиве?", id="scalar-not-inventory"),
    ],
)
@pytest.mark.asyncio
async def test_non_inventory_tag_controls_keep_both_aggregate_capabilities_closed(
    question: str,
) -> None:
    context, kernel, llm, tools, tools_used = await _run_tag_prefetch(question)

    assert kernel.calls == []
    assert tools_used == []
    assert {item["function"]["name"] for item in tools} == {"memory_search"}
    assert not any("tag_inventory|none" in call for call in llm.calls)
    assert "Теги личного архива:" not in context.structural_answer


@pytest.mark.asyncio
async def test_a_bounded_semantic_synonym_can_open_only_the_tag_inventory_route() -> None:
    question = "Покажи фасетную разметку моего архива."
    assert _fast_tag_inventory_intent(question) is False

    context, kernel, llm, _, tools_used = await _run_tag_prefetch(question)

    assert sum("tag_inventory|none" in call for call in llm.calls) == 1
    assert kernel.calls == [("list_tags", {})]
    assert tools_used == ["list_tags"]
    assert context.structural_answer.count("Теги личного архива:") == 1


@pytest.mark.asyncio
async def test_a_tag_remainder_cannot_reopen_the_settled_inventory() -> None:
    llm = _TagLLM(remainder="метки и частоты")

    context, kernel, _, _, tools_used = await _run_tag_prefetch(
        "Покажи метки и частоты моего архива.",
        llm=llm,
    )

    assert kernel.calls == [("list_tags", {})]
    assert tools_used == ["list_tags"]
    assert context.structural_answer.count("Теги личного архива:") == 1
    assert "повтори её отдельным сообщением" not in context.structural_answer
    assert context.remainder_known is True
    assert context.open_remainder == ""


@pytest.mark.asyncio
async def test_anaphoric_tag_counts_do_not_open_the_whole_archive_counter() -> None:
    question = "Какими метками размечены записи и каковы их точные счётчики?"

    context, kernel, _, _, tools_used = await _run_tag_prefetch(
        question,
        outward_kind="архив",
    )

    assert kernel.calls == [("list_tags", {})]
    assert tools_used == ["list_tags"]
    assert context.structural_answer.count("Теги личного архива:") == 1
    assert context.remainder_known is True
    assert context.open_remainder == ""


@pytest.mark.parametrize(
    ("question", "expected_remainder"),
    [
        pytest.param(
            "Сколько записей подходит под учебный запрос «Кобальт»? И покажи доступные теги.",
            "Сколько записей подходит под учебный запрос «Кобальт»",
            id="local-count-before-tags",
        ),
        pytest.param(
            "Покажи доступные теги и сколько записей подходит под учебный запрос «Кобальт»?",
            "сколько записей подходит под учебный запрос «Кобальт»",
            id="tags-before-local-count",
        ),
        pytest.param(
            "Покажи доступные теги и заодно скажи, сколько записей подходит под учебный запрос «Кобальт»?",
            "скажи, сколько записей подходит под учебный запрос «Кобальт»",
            id="tags-before-local-count-with-modifier",
        ),
        pytest.param(
            "Сколько записей подходит под учебный запрос «Кобальт», а потом покажи доступные теги.",
            "Сколько записей подходит под учебный запрос «Кобальт»",
            id="local-count-before-tags-with-modifier",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_local_count_compound_stays_open_without_global_counter_authority(
    question: str,
    expected_remainder: str,
) -> None:
    context, kernel, llm, tools, tools_used = await _run_tag_prefetch(
        question,
        outward_kind="архив",
    )

    assert kernel.calls == [("list_tags", {})]
    assert tools_used == ["list_tags"]
    assert {item["function"]["name"] for item in tools} == {"memory_search"}
    assert context.remainder_known is True
    assert context.open_remainder == expected_remainder
    assert not any("Часть просьбы человека уже решена" in call for call in llm.calls)


@pytest.mark.parametrize(
    "question",
    [
        pytest.param(
            "Покажи теги и скажи, сколько всего файлов в архиве.",
            id="tags-then-count",
        ),
        pytest.param(
            "Покажи теги, затем скажи, сколько всего файлов в архиве.",
            id="tags-then-count-with-modifier",
        ),
        pytest.param(
            "Скажи, сколько всего файлов в архиве, а потом покажи теги.",
            id="count-then-tags-with-modifier",
        ),
    ],
)
@pytest.mark.asyncio
async def test_tag_and_global_count_settle_both_owned_capabilities_once(question: str) -> None:

    context, kernel, llm, tools, tools_used = await _run_tag_prefetch(
        question,
        outward_kind="архив",
    )

    assert kernel.calls == [("kg_stats", {}), ("list_tags", {})]
    assert tools_used == ["kg_stats", "list_tags"]
    assert {item["function"]["name"] for item in tools} == {"memory_search"}
    assert context.structural_answer.startswith("В личном архиве: Файлов — 3.")
    assert context.structural_answer.count("Теги личного архива:") == 1
    assert context.remainder_known is True
    assert context.open_remainder == ""
    assert sum("Часть просьбы человека уже решена" in call for call in llm.calls) == 1


@pytest.mark.parametrize(
    "tail",
    [
        "создай документ Word",
        "создай документ Word с этим списком тегов",
        "создай заметку про этот список тегов",
        "проверь состояние сервиса",
        "проверь теги в приложенном файле",
        "напомни завтра позвонить",
        "напомни завтра проверить счётчики тегов",
    ],
)
@pytest.mark.asyncio
async def test_tag_inventory_preserves_one_independent_action_tail(tail: str) -> None:
    llm = _TagLLM(remainder="неверный остаток")

    context, kernel, _, _, tools_used = await _run_tag_prefetch(
        f"Покажи все теги моего архива и {tail}.",
        llm=llm,
        outward_kind="архив",
    )

    assert kernel.calls == [("list_tags", {})]
    assert tools_used == ["list_tags"]
    assert context.structural_answer.count("Теги личного архива:") == 1
    assert context.remainder_known is True
    assert context.open_remainder == tail
    assert not any("Часть просьбы человека уже решена" in call for call in llm.calls)


@pytest.mark.parametrize(
    ("connector", "tail"),
    [
        pytest.param("и ещё", "создай документ Word", id="create-after-and-more"),
        pytest.param("и заодно", "проверь состояние сервиса", id="check-after-and-also"),
        pytest.param(", затем", "напомни завтра позвонить", id="remind-after-then"),
        pytest.param("и потом", "напомни завтра проверить счётчики тегов", id="remind-after-later"),
    ],
)
@pytest.mark.asyncio
async def test_tag_inventory_preserves_action_tails_after_natural_connectors(
    connector: str,
    tail: str,
) -> None:
    llm = _TagLLM(remainder="неверный остаток")

    context, kernel, _, tools, tools_used = await _run_tag_prefetch(
        f"Покажи все теги моего архива {connector} {tail}.",
        llm=llm,
        outward_kind="архив",
    )

    assert kernel.calls == [("list_tags", {})]
    assert tools_used == ["list_tags"]
    assert {item["function"]["name"] for item in tools} == {"memory_search"}
    assert context.structural_answer.count("Теги личного архива:") == 1
    assert context.remainder_known is True
    assert context.open_remainder == tail
    assert not any("Часть просьбы человека уже решена" in call for call in llm.calls)


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param(
            "объясни фразу «и потом напомни завтра позвонить»",
            id="angle-quotes",
        ),
        pytest.param(
            "объясни фразу “и потом напомни завтра позвонить”",
            id="curly-quotes",
        ),
    ],
)
@pytest.mark.asyncio
async def test_tag_remainder_connectors_inside_a_quote_do_not_split_the_tail_again(
    tail: str,
) -> None:
    llm = _TagLLM(remainder="неверный остаток")

    context, kernel, _, tools, tools_used = await _run_tag_prefetch(
        f"Покажи все теги моего архива и {tail}.",
        llm=llm,
        outward_kind="архив",
    )

    assert kernel.calls == [("list_tags", {})]
    assert tools_used == ["list_tags"]
    assert {item["function"]["name"] for item in tools} == {"memory_search"}
    assert context.remainder_known is True
    assert context.open_remainder == tail
    assert not any("Часть просьбы человека уже решена" in call for call in llm.calls)


def _file_fixture_questions() -> list[str]:
    manifest = json.loads(
        (Path(__file__).with_name("fixtures") / "synthetic_live_battery_a.json").read_text(encoding="utf-8")
    )
    profiles = {"k12_markdown_transport", "attachment_same_turn", "telegram_fake_transport"}
    return [
        question
        for pass_spec in manifest["passes"]
        if pass_spec["oracle_profile"] in profiles
        for question in pass_spec["questions"]
    ]


@pytest.mark.parametrize("question", _file_fixture_questions())
def test_formatting_attachment_and_telegram_prompts_are_not_direct_file_requests(
    question: str,
) -> None:
    assert _is_direct_file_request(question) is False


@pytest.mark.parametrize(
    "question",
    [
        "Сделай Word с синтетическим отчётом.",
        "Можешь оформить сводку в PDF?",
        "Пришли файл с двумя проверочными строками.",
        "Подготовь картинку с синтетической схемой.",
    ],
)
def test_a_visible_creation_request_is_direct_file_authority(question: str) -> None:
    assert _is_direct_file_request(question) is True


class _PatchedLLM:
    enabled = True
    model = "synthetic-late-file-double"
    total_budget_sec = 5.0

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, str]:
        del messages, kwargs
        raise AssertionError("unexpected model call")


class _LateFileKernel:
    def __init__(self, *, success: bool) -> None:
        self.success = success
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, name: str, arguments: dict[str, Any], *, actor: Any) -> ToolResult:
        del actor
        self.calls.append((name, dict(arguments)))
        attachment = (
            {
                "kind": "document",
                "filename": "synthetic-late.docx",
                "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "content_base64": "c3ludGhldGlj",
            }
            if self.success
            else None
        )
        return ToolResult(
            name, self.success, error=None if self.success else "synthetic", attachment=attachment
        )


def _chat_runtime(settings: Any, storage: Any, monkeypatch: pytest.MonkeyPatch, kernel: Any) -> AgentRuntime:
    storage.ensure_user("alice", preset_key="owner")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_PatchedLLM(),
        kernel=kernel,
    )

    async def prepare(user_id: str, message: str, conversation_id: str, **kwargs: Any) -> AgentContext:
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            outward_verdict=("файл", None),
            answer_mode="general_conversation",
        )

    async def generate(context: AgentContext, message: str, attachments: Any) -> dict[str, Any]:
        del context, message, attachments
        return {
            "content": (
                "Синтетический отчёт\n\n"
                "Раздел один содержит проверяемый факт.\n\n"
                "Раздел два содержит проверяемый вывод."
            ),
            "tools_used": [],
        }

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    return runtime


@pytest.mark.parametrize("success", [True, False], ids=["success", "failure"])
@pytest.mark.asyncio
async def test_every_actual_late_make_file_attempt_is_ledgered_once(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    success: bool,
) -> None:
    kernel = _LateFileKernel(success=success)
    runtime = _chat_runtime(settings, storage, monkeypatch, kernel)

    reply = await runtime.chat(
        "alice",
        "Сделай Word с синтетическим отчётом.",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        enable_tools=False,
    )

    assert [name for name, _ in kernel.calls] == ["make_file"]
    assert reply["tools_used"] == ["make_file"]
    assert bool(reply["files"]) is success


@pytest.mark.asyncio
async def test_a_lone_file_verdict_never_authorises_a_late_make_file(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _LateFileKernel(success=True)
    runtime = _chat_runtime(settings, storage, monkeypatch, kernel)

    reply = await runtime.chat(
        "alice",
        "Оформи короткий Telegram-ответ с жирным выделением.",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        enable_tools=False,
    )

    assert kernel.calls == []
    assert reply["tools_used"] == []
    assert reply["files"] == []
