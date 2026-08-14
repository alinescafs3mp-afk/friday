"""Narrow product regressions for the authenticated file routing boundary.

All sources, models and ledgers in this module are synthetic.  No test reaches
the network, a service process, or a database outside ``FRIDAY_HOME``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import (
    _INCOMPLETE_ATTACHMENT_SOURCE_NOTICE,
    AgentContext,
    AgentRuntime,
    _attachment_requests_archive_tool,
    _OwnedAttachment,
    file_turn_authority,
)
from friday.permissions import ActorContext, AuthorizationService


def _owned(filename: str, text: str, **flags: Any) -> _OwnedAttachment:
    return _OwnedAttachment(
        {
            "filename": filename,
            "transient_text": text,
            "extraction_success": True,
            "verification_eligible": True,
            **flags,
        }
    )


class _NeverModel:
    enabled = True
    total_budget_sec = 1.0

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ARG002
        raise AssertionError("a code-owned routing result reached a model")


class _HierarchyModel:
    enabled = True
    total_budget_sec = 30.0

    def __init__(self) -> None:
        self.map_calls = 0

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ARG002
        contents = [str(item.get("content") or "") for item in messages]
        if any(item.startswith("FRIDAY_ATTACHMENT_CHUNK_DATA") for item in contents):
            self.map_calls += 1
            return {"content": "Карта прочитанного фрагмента."}
        if any(item.startswith("FRIDAY_ATTACHMENT_REDUCE_DATA") for item in contents):
            return {"content": "Сводная карта всех дочерних фрагментов."}
        return {"content": "Полный анализ построен по всей карте документа."}


@pytest.mark.parametrize(
    ("question", "source_names", "roles"),
    [
        (
            "Сравни файлы «alpha-plan.txt» и «beta-budget.txt»",
            ("alpha-plan.txt", "beta-budget.txt"),
            ("source_identity", "source_identity"),
        ),
        (
            "Что в «quarterly-status-report.pdf»?",
            ("quarterly-status-report.pdf",),
            ("source_identity",),
        ),
        (
            "Найди строку «report.pdf» в этом файле",
            (),
            ("body_literal",),
        ),
        ("Создай файл «out.txt»", (), ("output_literal",)),
        (
            "Сравни строки «alpha.txt» и «beta.txt»",
            (),
            ("inert", "inert"),
        ),
    ],
)
def test_quoted_filename_roles_require_independent_source_grammar(
    question: str,
    source_names: tuple[str, ...],
    roles: tuple[str, ...],
) -> None:
    authority = file_turn_authority(question)

    assert authority.source_filenames() == source_names
    assert tuple(item.role for item in authority.locators) == roles


@pytest.mark.parametrize(
    ("question", "archive", "local_read"),
    [
        ("собери документы за 26 число, и что там по проекту", True, False),
        ("Процитируй «собери документы за 26 число»", False, False),
        ("Собери документы с текстом проекта в один отчёт", False, False),
        ("Прочитай этот файл", False, True),
    ],
)
def test_archive_collection_is_not_a_current_file_read(
    question: str,
    archive: bool,
    local_read: bool,
) -> None:
    authority = file_turn_authority(question)

    assert _attachment_requests_archive_tool(question) is archive
    assert authority.proved("archive") is archive
    assert authority.proved("local_read") is local_read


@pytest.mark.parametrize(
    ("question", "local_read"),
    [
        ("Перечисли все результаты.", False),
        ("Перечисли каждую найденную модель.", False),
        ("Покажи все?", True),
        ("Покажи все найденные файлы.", True),
    ],
)
def test_weak_exhaustive_wording_needs_a_file_noun_or_end_of_turn(
    question: str,
    local_read: bool,
) -> None:
    assert file_turn_authority(question).proved("local_read") is local_read


@pytest.mark.asyncio
async def test_valid_prior_web_ledger_outranks_a_generic_file_denial(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = "router-ledger-guest"
    storage.ensure_user(user_id, preset_key="guest")
    conversation = storage.create_conversation(user_id, title="synthetic prior web ledger")
    storage.store_message(str(conversation["id"]), user_id, "user", "Найди публичный факт.")
    storage.store_message(
        str(conversation["id"]),
        user_id,
        "assistant",
        "Публичный факт.",
        metadata={
            "web_evidence_used": True,
            "web_evidence_status": "sourced",
            "web_sources": [
                {
                    "title": "Synthetic public source",
                    "url": "https://safe.synthetic.example.com/fact",
                }
            ],
        },
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_NeverModel(),
    )

    async def forbidden_prepare(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("a valid provenance ledger reached ambient retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_prepare)
    result = await runtime.chat(
        user_id,
        "Это все источники?",
        actor=ActorContext(user_id=user_id, preset_key="guest", source="test"),
        conversation_id=str(conversation["id"]),
        enable_tools=False,
    )

    assert result["message"].startswith("Источники предыдущего ответа")
    assert "Нет доступа к чтению файлов" not in result["message"]
    assert result["web_sources"] == [
        {"title": "Synthetic public source", "url": "https://safe.synthetic.example.com/fact"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "web_sources",
    [
        [],
        [{"title": "Private host", "url": "http://127.0.0.1/internal"}],
    ],
)
async def test_absent_or_invalid_prior_web_ledger_cannot_bypass_file_denial(
    settings,
    storage,
    web_sources: list[dict[str, str]],
) -> None:
    user_id = f"router-invalid-ledger-guest-{len(web_sources)}"
    storage.ensure_user(user_id, preset_key="guest")
    conversation = storage.create_conversation(user_id, title="invalid prior web ledger")
    storage.store_message(
        str(conversation["id"]),
        user_id,
        "assistant",
        "Untrusted provenance claim.",
        metadata={
            "web_evidence_used": True,
            "web_evidence_status": "sourced",
            "web_sources": web_sources,
        },
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_NeverModel(),
    )

    result = await runtime.chat(
        user_id,
        "Это все источники?",
        actor=ActorContext(user_id=user_id, preset_key="guest", source="test"),
        conversation_id=str(conversation["id"]),
        enable_tools=False,
    )

    assert result["message"] == "Нет доступа к чтению файлов для этого запроса."
    assert result["web_sources"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "Какая сегодня погода?",
        "Tell me about Alice",
        "summary of our conversation",
        "abstract the current chat",
        "Кратко по проекту",
    ],
)
async def test_unrelated_assistant_reply_does_not_inherit_resolved_file_carriers(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
    question: str,
) -> None:
    user_id = "router-unrelated-reply-owner"
    storage.ensure_user(user_id, preset_key="owner")
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage)
    observed_attachments: list[list[dict[str, Any]]] = []

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
        )

    async def generate(context, question, attachments):  # noqa: ANN001
        del context, question
        observed_attachments.append(list(attachments))
        return {"content": "Обычный ответ.", "tools_used": [], "_model_generated": False}

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    result = await runtime.chat(
        user_id,
        question,
        actor=AuthorizationService(storage).actor_for_user(user_id, source="test"),
        attachments=[_owned("private.txt", "PRIVATE-LINEAGE-MUST-STAY-CLOSED")],
        reply_assistant_reference=True,
        reply_assistant_message_id="msg_0123456789abcdef",
        enable_tools=False,
    )

    assert observed_attachments == [[]]
    assert result["attachment_context_expected_count"] == 0
    assert result["attachment_context_readable_count"] == 0
    assert "PRIVATE-LINEAGE-MUST-STAY-CLOSED" not in result["message"]


@pytest.mark.asyncio
async def test_complete_hierarchy_removes_only_the_bounded_projection_notice(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = "router-complete-hierarchy-owner"
    storage.ensure_user(user_id, preset_key="owner")
    model = _HierarchyModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
    )

    async def forbidden_prepare(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("a closed local hierarchy read reached ambient retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_prepare)
    source = "HEAD\n" + "x" * 100_000 + "\nTAIL"
    result = await runtime.chat(
        user_id,
        "Прочитай файл целиком.",
        actor=AuthorizationService(storage).actor_for_user(user_id, source="test"),
        attachments=[_owned("complete-long.txt", source)],
        enable_tools=False,
    )

    assert model.map_calls > 1
    assert result["attachment_coverage_complete"] is True
    assert result["attachment_verification_complete"] is True
    assert _INCOMPLETE_ATTACHMENT_SOURCE_NOTICE not in result["message"]
