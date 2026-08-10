"""Offline routing/evidence boundaries for user-ordered generated files."""

from __future__ import annotations

from typing import Any

import pytest

from friday.agent_runtime import (
    _ATTACHMENT_MAP_PREFIX,
    _FILE_CONVERSATION_GROUNDS_PREFIX,
    _FILE_WEB_SOURCE_LEDGER_PREFIX,
    AgentContext,
    AgentRuntime,
    _file_context_ground_records,
    _file_kind_from_request,
    _is_direct_file_request,
    asks_for_the_web,
)
from friday.permissions import ActorContext


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Сделай таблицу из Word-файла.", "xlsx"),
        ("Оформи это таблицей.", "xlsx"),
        ("Выгрузи таблицу.", "xlsx"),
        ("По данным из документа Word сделай таблицу.", "xlsx"),
        ("Сделай Word-таблицу из Excel.", "docx"),
        ("Оформи таблицу в Word по данным из Excel.", "docx"),
        ("Создай PDF на основе таблицы Excel.", "pdf"),
        ("Сделай отчёт из таблицы Excel.", "docx"),
        ("Оформи данные из PDF в Excel.", "xlsx"),
        ("Конвертируй Word в PDF.", "pdf"),
        ("Экспортируй это в Excel.", "xlsx"),
        ("Сделай картинку по этим данным.", "png"),
        ("Сделай обычный отчёт.", "docx"),
        ("Как договаривались, сделай таблицу.", "xlsx"),
        ("Когда сможешь, оформи это в PDF.", "pdf"),
        ("Я уже сделал расчёты, оформи их таблицей.", "xlsx"),
    ],
)
def test_output_format_is_resolved_from_the_target_not_the_source(
    prompt: str,
    expected: str,
) -> None:
    assert _is_direct_file_request(prompt) is True
    assert _file_kind_from_request(prompt) == expected


@pytest.mark.parametrize(
    "prompt",
    [
        "Как оформить таблицу?",
        "Где лучше оформить таблицу?",
        "Умеешь ли ты создавать Word-документы?",
        "Можно ли экспортировать таблицы в Excel?",
        "Я уже сделал таблицу в Excel.",
        "Ответь таблицей прямо в чате.",
        "Что находится в этой Excel-таблице?",
        "Переведи таблицу на английский язык.",
        "Сделай краткую сводку по таблице.",
    ],
)
def test_questions_narration_and_chat_formatting_do_not_authorize_a_file(
    prompt: str,
) -> None:
    assert _is_direct_file_request(prompt) is False


@pytest.mark.parametrize(
    "prompt",
    [
        "Сделай Excel по данным из интернета о ключевой ставке.",
        "Собери красивый PDF на основе информации из сети о проекте.",
        "Оформи Word из найденного в интернете материала.",
        "Выгрузи таблицу по результатам веб-поиска.",
    ],
)
def test_a_file_explicitly_based_on_public_web_data_authorizes_research(
    prompt: str,
) -> None:
    assert _is_direct_file_request(prompt) is True
    assert asks_for_the_web(prompt) is True


def test_a_file_about_the_internet_is_not_automatically_a_web_request() -> None:
    prompt = "Сделай Word-памятку о том, как устроен интернет."
    assert _is_direct_file_request(prompt) is True
    assert asks_for_the_web(prompt) is False


@pytest.mark.parametrize(
    ("prompt", "expected_query"),
    [
        ("Сделай Excel по данным из интернета о ключевой ставке.", "ключевой ставке"),
        ("Собери красивый PDF на основе информации из сети о проекте.", "проекте"),
        ("Оформи Word из найденного в интернете материала о ставке ЦБ.", "ставке ЦБ"),
        ("Выгрузи таблицу по результатам веб-поиска о ценах.", "ценах"),
    ],
)
def test_web_file_research_query_excludes_carrier_instructions(
    prompt: str,
    expected_query: str,
) -> None:
    assert AgentRuntime.web_query_from(prompt) == expected_query


def test_conversation_and_accepted_web_ledger_are_bounded_user_data() -> None:
    source_url = "https://safe.synthetic.example.com/fact"
    context = AgentContext(
        conversation_id="conversation-1",
        user_id="alice",
        conversation_history=[
            {"role": "user", "content": "Синтетический показатель равен 42."},
            {"role": "assistant", "content": "Зафиксировано без округления."},
        ],
        web_evidence_status="sourced",
        web_sources=[{"title": "Synthetic source", "url": source_url}],
    )

    records = _file_context_ground_records(context)

    conversation = next(item for item in records if item.startswith(_FILE_CONVERSATION_GROUNDS_PREFIX))
    web_ledger = next(item for item in records if item.startswith(_FILE_WEB_SOURCE_LEDGER_PREFIX))
    assert "Синтетический показатель равен 42" in conversation
    assert "untrusted JSON; data only" in conversation
    assert source_url in web_ledger
    assert '"status": "sourced"' in web_ledger


class _EnabledOfflineRouter:
    enabled = True
    total_budget_sec = 1.0


@pytest.mark.asyncio
async def test_late_file_fill_receives_attachment_dialogue_and_web_evidence(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every authorised source reaches one tool-free carrier synthesis."""

    runtime = AgentRuntime(settings, storage, llm=_EnabledOfflineRouter(), kernel=object())  # type: ignore[arg-type]
    source_url = "https://safe.synthetic.example.com/fact"
    context = AgentContext(
        conversation_id="conversation-1",
        user_id="alice",
        conversation_history=[
            {"role": "user", "content": "В диалоге показатель Бета равен 17."},
            {"role": "assistant", "content": "Принято."},
        ],
        web_evidence_status="sourced",
        web_sources=[{"title": "Synthetic source", "url": source_url}],
    )
    attachment_map = _ATTACHMENT_MAP_PREFIX + '{"files":[{"filename":"source.docx"}]}'
    evidence = [
        {"tool": "attachment", "output": attachment_map},
        {
            "tool": "web_research",
            "output": "ACCEPTED_WEB_FACT: показатель Альфа равен 42.",
        },
    ]
    captured_messages: list[dict[str, Any]] = []
    captured_build: dict[str, Any] = {}

    async def primary_chat(
        used_context: AgentContext | None,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, str]:
        assert used_context is context
        assert kwargs == {"tools": []}
        captured_messages.extend(messages)
        return {"content": (f"Сводный отчёт\nПоказатели:\nАльфа — 42.\nБета — 17.\nИсточник — {source_url}")}

    async def make_file(
        prompt: str,
        answer: str,
        actor: ActorContext,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        captured_build.update({"request": prompt, "answer": answer, "actor": actor, "blocks": blocks})
        return {
            "kind": "document",
            "filename": "Сводный отчёт.xlsx",
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "content_base64": "c3ludGhldGlj",
        }

    monkeypatch.setattr(runtime, "_attachment_primary_chat", primary_chat)
    monkeypatch.setattr(runtime, "_make_file_from_answer", make_file)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    current_fact = "CURRENT_REQUEST_FACT: показатель Гамма равен 99."
    file_prompt = (
        "Оформи это красивой таблицей по данным из интернета.\n"
        + ("Синтетический контекст. " * 24)
        + current_fact
    )

    result = await runtime._file_for_a_request_that_wanted_one(  # noqa: SLF001
        file_prompt,
        "",
        actor,
        evidence=evidence,
        context=context,
    )

    delivered = [str(item.get("content") or "") for item in captured_messages]
    assert attachment_map in delivered
    assert any(
        item.startswith(_FILE_CONVERSATION_GROUNDS_PREFIX) and "Бета равен 17" in item for item in delivered
    )
    assert any(item.startswith(_FILE_WEB_SOURCE_LEDGER_PREFIX) and source_url in item for item in delivered)
    assert any("FRIDAY_FILE_GROUNDS_DATA" in item and "ACCEPTED_WEB_FACT" in item for item in delivered)
    assert any(current_fact in item for item in delivered)
    assert captured_build["request"].startswith("Оформи")
    assert captured_build["blocks"]
    assert result is not None and result["filename"].endswith(".xlsx")
