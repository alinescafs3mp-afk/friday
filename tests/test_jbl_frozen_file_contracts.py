"""Synthetic end-to-end regressions for the frozen JBL file contracts.

No fixture in this module reads a live conversation or invokes a model/network
provider.  The fakes expose only the bounded capabilities required by each
contract and fail on every unexpected call.
"""

from __future__ import annotations

import copy
import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from docx import Document

from friday.agent_runtime import (
    _FALSE_CURRENT_MODEL_OUTAGE,
    _PERSON_DOCUMENT_INVENTORY,
    _PRIVATE_WEB_SEARCH_BLOCKED,
    _WEB_ISOLATION_DEICTIC,
    AgentContext,
    AgentRuntime,
    _attachment_evidence_chunks,
    _attachment_web_fact_targets,
    _attachment_web_literals_are_grounded,
    _multi_attachment_summary_count,
    _OwnedAttachment,
    _project_attachments_for_request,
    _reconcile_attachment_web_literals,
    asks_for_the_web,
)
from friday.agent_runtime._office_attachments import (
    OFFICE_EXACT_UNAVAILABLE_MESSAGE,
    OFFICE_STRUCTURE_KEY,
    trusted_office_attachment,
    validate_runtime_office_index,
)
from friday.documents import DocumentExtractor
from friday.execution_kernel import ToolResult
from friday.permissions import ActorContext
from friday.storage.models import RawObject, new_id
from friday.telegram_bridge._markup import to_telegram_html


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="test")


class _AllowAll:
    def authorize(self, actor, capability, **kwargs):  # noqa: ANN001, ARG002
        return SimpleNamespace(allowed=True)


def _tool(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "synthetic bounded read",
            "parameters": {"type": "object"},
        },
    }


async def _prepare_without_retrieval(
    user_id: str,
    _message: str,
    conversation_id: str,
    **kwargs: Any,
) -> AgentContext:
    history = list(kwargs.get("prior_history") or [])
    previous_user = next(
        (
            str(item.get("content") or "")
            for item in reversed(history)
            if str(item.get("role") or "") == "user"
        ),
        "",
    )
    return AgentContext(
        conversation_id=conversation_id,
        user_id=user_id,
        person_id=str(kwargs.get("person_id") or user_id),
        conversation_history=history,
        previous_user_turn=previous_user,
    )


class _NeverModel:
    enabled = True
    total_budget_sec = 1.0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        raise AssertionError("the code-owned turn reached a model")


class _InventoryKernel:
    authorization = _AllowAll()

    def __init__(self, *, available: bool = True, malformed: bool = False) -> None:
        self.available = available
        self.malformed = malformed
        self.calls: list[dict[str, Any]] = []

    def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
        return [_tool("user_activity")] if self.available else []

    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        assert tool == "user_activity"
        assert params["documents_only"] is True
        assert params["offset"] == 0
        self.calls.append(dict(params))
        if self.malformed:
            return ToolResult(
                tool,
                True,
                {
                    "человек": "JBL",
                    "период": {"с": params["since"], "по": params["until"]},
                    "документов с подтверждённым автором": 0,
                    "документов без отметки автора": 0,
                    "документы": [],
                    "пагинация": {},
                },
            )
        return ToolResult(
            tool,
            True,
            {
                "человек": "JBL",
                "период": {"с": params["since"], "по": params["until"]},
                "документов с подтверждённым автором": 2,
                "документов без отметки автора": 0,
                "документы": [
                    {"что": "alpha.pdf", "когда": params["since"]},
                    {"что": "beta.docx", "когда": params["until"]},
                ],
                "пагинация": {
                    "смещение": 0,
                    "показано": 2,
                    "из подтверждённых": 2,
                    "следующее смещение": None,
                    "подтверждённый перечень показан полностью": True,
                },
            },
        )


@pytest.mark.asyncio
async def test_named_day_inventory_and_its_completeness_followup_are_code_owned(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner", display_name="Owner")
    storage.ensure_user("jbl", preset_key="user", display_name="JBL")
    kernel = _InventoryKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_NeverModel(),
        kernel=kernel,
    )
    monkeypatch.setattr(runtime, "_prepare_context", _prepare_without_retrieval)

    first = await runtime.chat(
        "alice",
        "Какие документы вчера загружал JBL?",
        actor=_actor(),
    )
    second = await runtime.chat(
        "alice",
        "И всё?",
        actor=_actor(),
        conversation_id=first["conversation_id"],
    )

    assert len(kernel.calls) == 2
    assert kernel.calls[0] == kernel.calls[1]
    for reply in (first, second):
        assert "alpha.pdf" in reply["message"] and "beta.docx" in reply["message"]
        assert "2 из 2" in reply["message"]
        assert reply["tools_used"] == ["user_activity"]
    assert "Проверила выборку повторно" in second["message"]
    stored = storage.get_message(str(second["message_id"]), "alice")
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    assert metadata["structural"]["person_document_inventory"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unavailable", "malformed", "ambiguous"])
async def test_exact_inventory_fails_closed_before_generic_generation(
    settings,
    storage,
    monkeypatch,
    mode: str,
) -> None:
    storage.ensure_user("alice", preset_key="owner", display_name="Owner")
    if mode == "ambiguous":
        storage.ensure_user("jbl-one", display_name="JBL")
        storage.ensure_user("jbl-two", display_name="JBL")
    else:
        storage.ensure_user("jbl", display_name="JBL")
    kernel = _InventoryKernel(
        available=mode != "unavailable",
        malformed=mode == "malformed",
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_NeverModel(),
        kernel=kernel,
    )
    monkeypatch.setattr(runtime, "_prepare_context", _prepare_without_retrieval)

    reply = await runtime.chat(
        "alice",
        "Какие документы вчера загружал JBL?",
        actor=_actor(),
    )

    folded = reply["message"].casefold()
    assert "неизвест" in folded
    assert "это всё" in folded
    assert "alpha.pdf" not in reply["message"]
    if mode == "malformed":
        assert len(kernel.calls) == 1
    else:
        assert kernel.calls == []


class _PlainAnswerModel:
    enabled = True
    total_budget_sec = 1.0

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        self.calls.append(list(messages))
        return {"content": self.answer, "tool_calls": None, "_queue_wait_sec": 0.0}


@pytest.mark.asyncio
async def test_unrelated_completeness_question_is_not_hijacked_by_inventory(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    conversation = storage.create_conversation("alice", title="ordinary synthetic chat")
    storage.store_message(str(conversation["id"]), "alice", "user", "Назови один цвет")
    storage.store_message(str(conversation["id"]), "alice", "assistant", "Синий")
    kernel = _InventoryKernel()
    model = _PlainAnswerModel("Да, для текущего вопроса это полный ответ.")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )
    monkeypatch.setattr(runtime, "_prepare_context", _prepare_without_retrieval)

    reply = await runtime.chat(
        "alice",
        "Это всё?",
        actor=_actor(),
        conversation_id=str(conversation["id"]),
    )

    assert kernel.calls == []
    assert "документ" not in reply["message"].casefold()
    assert "неизвест" not in reply["message"].casefold()


def test_inventory_intent_does_not_hijack_a_document_content_question() -> None:
    assert _PERSON_DOCUMENT_INVENTORY.search("Какие документы сегодня загружал JBL?")
    assert not _PERSON_DOCUMENT_INVENTORY.search("Что в документе, который JBL загрузил сегодня?")
    assert not _PERSON_DOCUMENT_INVENTORY.search("Что написал JBL в документе, который загрузил сегодня?")


def test_today_inventory_window_stops_at_local_now(settings, storage, monkeypatch) -> None:
    runtime = AgentRuntime(settings, storage)
    fixed = datetime(2026, 8, 9, 12, 34, 56)
    monkeypatch.setattr(runtime, "_local_now", lambda: fixed)
    monkeypatch.setattr(runtime, "_local_today", lambda: fixed.date())

    since, until, label, complete = runtime._closed_document_day_window("сегодня")  # noqa: SLF001

    assert since.startswith("2026-08-08T21:00:00")
    assert until.startswith("2026-08-09T09:34:56")
    assert label == "2026-08-09 по состоянию на 12:34"
    assert complete is False


class _NoToolsKernel:
    authorization = _AllowAll()

    def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
        return []

    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        raise AssertionError(f"unexpected tool {tool}: {params}")


@pytest.mark.asyncio
async def test_successful_generation_cannot_replay_a_false_model_outage(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    stale = "К сожалению, не могу связаться с моделью — она не отвечает."
    model = _PlainAnswerModel(stale)
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=_NoToolsKernel(),
    )
    monkeypatch.setattr(runtime, "_prepare_context", _prepare_without_retrieval)

    reply = await runtime.chat("alice", "Повтори ответ по существу", actor=_actor())

    assert "модель" not in reply["message"].casefold()
    assert "повторите запрос" in reply["message"].casefold()
    stored = storage.get_message(str(reply["message_id"]), "alice")
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    assert metadata["structural"]["output_guards"]["false_model_outage_replaced"] is True


class _FailingModel:
    enabled = True
    total_budget_sec = 1.0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        raise RuntimeError("synthetic transport failure")


class _DisabledModel:
    enabled = False
    total_budget_sec = 1.0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        raise AssertionError("a disabled model was called")


@pytest.mark.asyncio
async def test_a_real_model_failure_keeps_the_truthful_outage_diagnosis(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_FailingModel(),
        kernel=_NoToolsKernel(),
    )
    monkeypatch.setattr(runtime, "_prepare_context", _prepare_without_retrieval)

    reply = await runtime.chat("alice", "Ответь по существу", actor=_actor())

    assert reply["context"]["llm_failed"] is True
    assert "не могу связаться с моделью" in reply["message"].casefold()


@pytest.mark.asyncio
async def test_an_intentionally_disabled_model_is_not_rewritten_as_available(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_DisabledModel(),
        kernel=_NoToolsKernel(),
    )
    monkeypatch.setattr(runtime, "_prepare_context", _prepare_without_retrieval)

    reply = await runtime.chat("alice", "Ответь по существу", actor=_actor())

    assert reply["context"]["llm_failed"] is False
    assert "модель недоступна" in reply["message"].casefold()
    assert "повторите запрос ещё раз" not in reply["message"].casefold()


@pytest.mark.asyncio
async def test_a_repair_cannot_reintroduce_a_false_model_outage_at_the_final_boundary(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=_NeverModel(),
    )
    verification_calls = 0

    async def generate(context, message, attachments):  # noqa: ANN001, ARG001
        return {
            "content": "Первичный содержательный ответ по синтетическому документу.",
            "tools_used": [],
            "_model_generated": True,
        }

    async def verify(query, response, context, *, tool_evidence=None):  # noqa: ANN001, ARG001
        nonlocal verification_calls
        verification_calls += 1
        if verification_calls == 1:
            return {"status": "failed", "ok": False, "score": 0.0, "issues": ["synthetic"]}
        return {"status": "passed", "ok": True, "score": 1.0, "issues": []}

    async def repair(*args, **kwargs):  # noqa: ANN002, ANN003
        return "К сожалению, не могу связаться с моделью — она не отвечает."

    monkeypatch.setattr(runtime, "_prepare_context", _prepare_without_retrieval)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_verify_response", verify)
    monkeypatch.setattr(runtime, "_repair_once", repair)

    reply = await runtime.chat(
        "alice",
        "Что указано в приложенном документе?",
        actor=_actor(),
        attachments=[
            {
                "filename": "synthetic.txt",
                "transient_text": "Проверяемый синтетический факт.",
                "extraction_success": True,
                "verification_eligible": True,
            }
        ],
        enable_tools=False,
    )

    assert verification_calls == 2
    assert "модель" not in reply["message"].casefold()
    assert "повторите запрос" in reply["message"].casefold()
    assert reply["verification_status"] == "unknown"


def test_false_outage_matcher_ignores_conditional_and_historical_discussion() -> None:
    assert _FALSE_CURRENT_MODEL_OUTAGE.search("К сожалению, не могу связаться с моделью — она не отвечает.")
    assert not _FALSE_CURRENT_MODEL_OUTAGE.search("Если модель недоступна, сообщи оператору.")
    assert not _FALSE_CURRENT_MODEL_OUTAGE.search("Вчера модель была недоступна десять минут.")
    assert not _FALSE_CURRENT_MODEL_OUTAGE.search("Он написал: «Сейчас модель недоступна».")


def _trusted_synthetic_docx(*, incomplete: bool) -> dict[str, Any]:
    document = Document()
    document.add_heading("Synthetic status", level=1)
    table = document.add_table(rows=3, cols=2)
    for row, values in zip(
        table.rows,
        (("Item", "State"), ("Alpha", "Ready"), ("Beta", "Review")),
        strict=True,
    ):
        for cell, value in zip(row.cells, values, strict=True):
            cell.text = value
    payload = io.BytesIO()
    document.save(payload)
    result = DocumentExtractor(secret_values=()).extract(
        payload.getvalue(),
        "synthetic-summary.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert result.success is True and isinstance(result.office_structure_index, dict)
    index = copy.deepcopy(result.office_structure_index)
    if incomplete:
        index["complete"] = False
        index["coverage"]["reasons"] = ["text_budget"]
    assert validate_runtime_office_index(index, result.text) == index
    return trusted_office_attachment(
        {
            "filename": "synthetic-summary.docx",
            "transient_text": result.text,
            "extraction_success": True,
            "verification_eligible": True,
            OFFICE_STRUCTURE_KEY: index,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete", [False, True])
async def test_bare_docx_summary_is_not_misclassified_as_an_exact_inventory(
    settings,
    storage,
    monkeypatch,
    incomplete: bool,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_NeverModel(),
    )
    draft = (
        "Сводка документа: указаны две позиции — Alpha и Beta. "
        "Это полный обзор всех двух позиций в прочитанной структуре."
    )

    async def generate(context, message, attachments):  # noqa: ANN001, ARG001
        assert attachments and attachments[0].get("_office_structured") is True
        return {"content": draft, "tools_used": [], "_model_generated": True}

    monkeypatch.setattr(runtime, "_generate_response", generate)
    reply = await runtime.chat(
        "alice",
        "Загружен документ: synthetic-summary.docx",
        actor=_actor(),
        attachments=[_trusted_synthetic_docx(incomplete=incomplete)],
        enable_tools=False,
        synthetic_document_notice=True,
    )

    assert reply["message"] == draft
    assert reply["message"] != OFFICE_EXACT_UNAVAILABLE_MESSAGE
    assert "пришлите" not in reply["message"].casefold()
    assert reply["attachment_context_readable_count"] == 1
    if incomplete:
        assert reply["attachment_coverage_complete"] is False
        assert reply["verification_status"] == "unknown"
        caution = reply["verification_caution"].casefold()
        assert "вложение удалось прочитать" in caution
        assert "полный состав документа не доказан" in caution
        assert "пришлите" not in caution
    else:
        assert reply["attachment_coverage_complete"] is True
        assert reply["verification_status"] != "unknown"


def test_stale_web_isolation_rejects_reference_only_requests() -> None:
    requests = (
        "Найди в интернете то же самое",
        "Найди в интернете по тому вопросу",
        "Найди в интернете сведения об этом",
        "Найди в интернете оттуда",
        "Найди в интернете дополнительную информацию о нём",
        "Найди в интернете по ранее присланным данным",
    )
    assert all(_WEB_ISOLATION_DEICTIC.search(request) for request in requests)


def test_natural_multi_document_count_is_bounded_and_negation_safe() -> None:
    assert _multi_attachment_summary_count("Обобщи последние три загруженных документа") == 3
    assert _multi_attachment_summary_count("Сделай общую сводку по трём последним документам") == 3
    assert _multi_attachment_summary_count("Подготовь сводку по 4 последним файлам") == 4
    assert _multi_attachment_summary_count("Не составляй сводку трёх документов") is None
    assert _multi_attachment_summary_count("Повтори фразу «обобщи три документа»") is None


@pytest.mark.parametrize(
    ("question", "predicate"),
    [
        ("Какую должность занимает иванов в документе?", "занимает"),
        ("Кем работает иванов в документе?", "работает"),
    ],
)
def test_a_rare_lowercase_surname_outranks_a_repeated_role_predicate(
    settings,
    storage,
    question: str,
    predicate: str,
) -> None:
    source = (
        (f"{predicate} общую позицию " + "A" * 40 + "\n") * 900
        + "X" * 30_000
        + "\nИванов\nДолжность: главный инженер\n"
    )
    attachment = _OwnedAttachment(
        {
            "filename": "lowercase-surname.txt",
            "transient_text": source,
            "extraction_success": True,
            "verification_eligible": True,
        }
    )

    projected, state = _project_attachments_for_request(question, [attachment])
    body = str(projected[0].get("transient_text") or "")
    evidence = "".join(item["output"].split("\n", 1)[1] for item in _attachment_evidence_chunks(projected))
    runtime = AgentRuntime(settings, storage)
    synthesis = "\n".join(
        str(item.get("content") or "")
        for item in runtime._build_initial_messages(  # noqa: SLF001
            AgentContext(conversation_id="conv", user_id="alice"),
            question,
            projected,
            tool_enabled=False,
        )
    )

    assert state.status == "matched" and state.scan_complete is True
    assert "Иванов\nДолжность: главный инженер" in body
    assert body in evidence
    assert body in synthesis


@pytest.mark.parametrize("truncated", [False, True])
@pytest.mark.asyncio
async def test_missing_required_surname_is_closed_not_predicate_matched(
    settings,
    storage,
    truncated: bool,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    source = ("занимает должность Петров " + "A" * 40 + "\n") * 1200
    attachment = _OwnedAttachment(
        {
            "filename": "surname-absent.txt",
            "transient_text": source,
            "extraction_success": True,
            "verification_eligible": True,
            "text_truncated": truncated,
        }
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_NeverModel(),
    )

    reply = await runtime.chat(
        "alice",
        "Какую должность занимает иванов в документе?",
        actor=_actor(),
        attachments=[attachment],
        enable_tools=False,
    )

    assert reply["attachment_query_status"] == ("unknown" if truncated else "not_found")
    assert reply["attachment_query_files_matched"] == 0
    if truncated:
        assert "доказательно проверить" in reply["message"].casefold()
    else:
        assert "не найден" in reply["message"].casefold()


def test_repeated_surname_retains_the_tail_occurrence_beside_its_position(
    settings,
    storage,
) -> None:
    prefix = "Иванов упомянут в списке.\n" * 1200
    source = prefix + "X" * max(0, 72_000 - len(prefix)) + "\nИванов\nДолжность: главный инженер\n"
    question = "Какая должность у иванова в документе?"
    attachment = _OwnedAttachment(
        {
            "filename": "repeated-surname.txt",
            "transient_text": source,
            "extraction_success": True,
            "verification_eligible": True,
        }
    )

    projected, state = _project_attachments_for_request(question, [attachment])
    body = str(projected[0].get("transient_text") or "")
    evidence = "".join(item["output"].split("\n", 1)[1] for item in _attachment_evidence_chunks(projected))
    runtime = AgentRuntime(settings, storage)
    synthesis = "\n".join(
        str(item.get("content") or "")
        for item in runtime._build_initial_messages(  # noqa: SLF001
            AgentContext(conversation_id="conv", user_id="alice"),
            question,
            projected,
            tool_enabled=False,
        )
    )

    assert state.status == "matched" and state.scan_complete is True
    assert "Иванов\nДолжность: главный инженер" in body
    assert body in evidence
    assert body in synthesis


@pytest.mark.parametrize(
    "question",
    [
        "Подскажи, какая должность у иванова в документе?",
        "Кем в документе работает иванов?",
        "Что за должность у иванова в документе?",
        "Укажи должность иванова в документе",
        "Что указано про иванова в документе?",
        "Можешь сказать, какая должность у иванова в документе?",
        "Какова должность иванова в документе?",
        "Должность иванова в документе?",
        "Определи должность иванова сейчас по данным документа",
        "Какая должность у Иванова, по-твоему, в документе?",
        "Какую роль занимает бизнес-аналитик Иванов в документе?",
    ],
)
def test_natural_surname_lookup_order_and_politeness_keep_the_strong_anchor(
    question: str,
) -> None:
    source = "X" * 72_000 + "\nИванов\nДолжность: главный инженер\n"
    attachment = _OwnedAttachment(
        {
            "filename": "natural-surname.txt",
            "transient_text": source,
            "extraction_success": True,
            "verification_eligible": True,
        }
    )

    projected, state = _project_attachments_for_request(question, [attachment])

    assert state.status == "matched" and state.scan_complete is True
    assert "Иванов\nДолжность: главный инженер" in str(projected[0]["transient_text"])


@pytest.mark.parametrize(
    "question",
    [
        "Что думаешь об этом документе?",
        "Скажи, что думаешь об этом документе?",
        "Подскажи, что ты думаешь об этом документе?",
        "Скажи кратко, о чём документ.",
        "Какая основная мысль документа?",
        "Какой главный вывод документа?",
        "Где здесь слабые места документа?",
        "Каково твоё мнение о документе?",
        "Кто, по-твоему, автор этого документа?",
    ],
)
def test_open_document_synthesis_cannot_be_hijacked_by_lookup_words(question: str) -> None:
    attachment = _OwnedAttachment(
        {
            "filename": "context-only.txt",
            "transient_text": "Полностью прочитанный синтетический текст.",
            "extraction_success": True,
            "verification_eligible": True,
        }
    )

    _projected, projection = _project_attachments_for_request(question, [attachment])

    assert projection.applied is False


def test_a_weak_factual_lookup_cannot_claim_complete_absence() -> None:
    attachment = _OwnedAttachment(
        {
            "filename": "weak-lookup.txt",
            "transient_text": "Полностью прочитанный синтетический текст.",
            "extraction_success": True,
            "verification_eligible": True,
        }
    )

    _projected, weak_lookup = _project_attachments_for_request(
        "Какая должность у инженера в документе?", [attachment]
    )

    assert weak_lookup.status == "unknown"


@pytest.mark.parametrize(
    "literal",
    [
        "SYNTHETIC-NODE-42",
        "owner42@example.invalid",
        "https://document.invalid/Case-42",
    ],
)
def test_unquoted_machine_literal_is_a_strong_document_anchor(literal: str) -> None:
    attachment = _OwnedAttachment(
        {
            "filename": "machine-literal.txt",
            "transient_text": "X" * 72_000 + f"\nExact literal: {literal}\n",
            "extraction_success": True,
            "verification_eligible": True,
        }
    )

    projected, state = _project_attachments_for_request(f"Найди {literal} в документе.", [attachment])

    assert state.status == "matched" and state.scan_complete is True
    assert literal in str(projected[0]["transient_text"])


def test_document_urls_are_exact_inert_evidence_not_web_provenance() -> None:
    exact = "https://Document.Invalid/CasePath?Q=AbC"
    allowed = _attachment_web_fact_targets([{"tool": "attachment", "output": f"Endpoint literal: {exact}"}])

    domain_answer, domain_changed = _reconcile_attachment_web_literals(
        "В документе указан домен document.invalid.",
        allowed=allowed,
    )
    assert domain_changed is True
    assert "document.invalid" in domain_answer
    assert _attachment_web_literals_are_grounded(domain_answer, allowed)

    mutated, _ = _reconcile_attachment_web_literals(
        "В документе указан https://Document.Invalid/casepath?Q=AbC и https://invented.invalid/x.",
        allowed=allowed,
    )
    assert "casepath" not in mutated
    assert "invented.invalid" not in mutated

    provenance, _ = _reconcile_attachment_web_literals(
        f"По данным интернета: {exact}",
        allowed=allowed,
    )
    assert "По данным интернета" not in provenance
    assert "В документе" in provenance
    assert _attachment_web_literals_are_grounded(provenance, allowed)


def test_hostile_document_url_syntax_stays_visible_without_a_telegram_link() -> None:
    private_url = "http://127.0.0.1/private"
    allowed = _attachment_web_fact_targets([{"tool": "attachment", "output": f"literal {private_url}"}])
    reconciled, _ = _reconcile_attachment_web_literals(
        f"В документе указано `[x]({private_url})`.",
        allowed=allowed,
    )
    rendered = to_telegram_html(reconciled)

    assert "127.0.0.1/private" in rendered
    assert "href=" not in rendered.casefold()


class _WebKernel:
    authorization = _AllowAll()

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
        return [_tool("web_research")]

    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        assert tool == "web_research"
        self.calls.append((str(tool), dict(params)))
        public_text = "At normal pressure the synthetic boiling point is 100 C."
        return ToolResult(
            tool,
            True,
            {
                "outbound_attempted": True,
                "sources": [
                    {
                        "url": "https://public.synthetic.example.com/fact",
                        "title": "Synthetic public source",
                        "text": public_text,
                        "text_length": len(public_text),
                        "status_code": 200,
                        "error": "",
                        "truncated": False,
                    }
                ],
                "requested_sources": 1,
                "completed_sources": 1,
                "failed_sources": 0,
                "timed_out_sources": 0,
                "search_timed_out": False,
            },
        )


class _WebAnswerModel:
    enabled = True
    total_budget_sec = 1.0

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        self.calls.append(list(messages))
        return {
            "content": (
                "Синтетический публичный факт подтверждён: https://public.synthetic.example.com/fact"
            ),
            "tool_calls": None,
            "_queue_wait_sec": 0.0,
        }


def _private_attachment_history(storage, *, old: bool) -> str:
    conversation = storage.create_conversation("alice", title="private synthetic lineage")
    conversation_id = str(conversation["id"])
    storage.store_message(
        conversation_id,
        "alice",
        "user",
        "PRIVATE-HISTORY-CANARY-DO-NOT-SEND",
        metadata={
            "had_attachments": True,
            "attachment_count": 1,
            "private_context_lineage": True,
        },
    )
    storage.store_message(
        conversation_id,
        "alice",
        "assistant",
        "PRIVATE-ANSWER-CANARY-DO-NOT-SEND",
        metadata={"attachment_context_used": True, "private_context_lineage": True},
    )
    if old:
        stale = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
        with storage.transaction() as conn:
            conn.execute(
                "UPDATE messages SET created_at=? WHERE conversation_id=? AND user_id=?",
                (stale, conversation_id, "alice"),
            )
    return conversation_id


def test_fresh_public_news_is_an_explicit_web_request_but_local_news_is_not() -> None:
    assert asks_for_the_web("Покажешь свежие новости за прошедшие сутки?")
    assert asks_for_the_web("Свежие новости за прошедшие сутки покажешь?")
    assert asks_for_the_web("Расскажи последние новости за вчера")
    assert not asks_for_the_web("Покажи новости в документе за вчера")
    assert not asks_for_the_web("В документе сохранены вчерашние новости")


@pytest.mark.asyncio
async def test_recent_private_file_then_self_contained_fresh_news_uses_isolated_web_only(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    conversation_id = _private_attachment_history(storage, old=False)
    kernel = _WebKernel()
    model = _WebAnswerModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )

    async def no_private_prepare(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("isolated news called private context preparation")

    monkeypatch.setattr(runtime, "_prepare_context", no_private_prepare)
    request = "Покажешь свежие новости за прошедшие сутки?"
    reply = await runtime.chat(
        "alice",
        request,
        actor=_actor(),
        conversation_id=conversation_id,
    )

    assert kernel.calls == [
        (
            "web_research",
            {"query": runtime.web_query_from(request), "max_sources": 3},
        )
    ]
    assert reply["tools_used"] == ["web_research"]
    assert reply["web_evidence_status"] == "sourced"
    exposed = json.dumps(model.calls, ensure_ascii=False)
    assert "PRIVATE-HISTORY-CANARY" not in exposed
    assert "PRIVATE-ANSWER-CANARY" not in exposed
    assert request in exposed


@pytest.mark.asyncio
async def test_old_private_lineage_allows_only_a_self_contained_isolated_web_turn(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    conversation_id = _private_attachment_history(storage, old=True)
    kernel = _WebKernel()
    model = _WebAnswerModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )

    async def no_private_prepare(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("isolated web called private context preparation")

    monkeypatch.setattr(runtime, "_prepare_context", no_private_prepare)
    request = "Найди в интернете температуру кипения воды при нормальном давлении"
    reply = await runtime.chat(
        "alice",
        request,
        actor=_actor(),
        conversation_id=conversation_id,
    )

    assert kernel.calls == [
        (
            "web_research",
            {"query": runtime.web_query_from(request), "max_sources": 3},
        )
    ]
    assert reply["tools_used"] == ["web_research"]
    assert reply["web_evidence_status"] == "sourced"
    exposed = json.dumps(model.calls, ensure_ascii=False)
    assert "PRIVATE-HISTORY-CANARY" not in exposed
    assert "PRIVATE-ANSWER-CANARY" not in exposed
    assert request in exposed
    stored = storage.get_message(str(reply["message_id"]), "alice")
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    assert metadata["private_context_lineage"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query_text",
    [
        "Найди в интернете то же самое",
        "Найди в интернете по тому вопросу",
        "Найди в интернете сведения об этом",
        "Найди в интернете оттуда",
        "Найди в интернете дополнительную информацию о нём",
        "Найди в интернете по ранее присланным данным",
    ],
)
async def test_old_private_lineage_still_blocks_reference_only_web_requests(
    settings,
    storage,
    query_text: str,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    conversation_id = _private_attachment_history(storage, old=True)
    kernel = _WebKernel()
    model = _WebAnswerModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )

    reply = await runtime.chat(
        "alice",
        query_text,
        actor=_actor(),
        conversation_id=conversation_id,
    )

    assert reply["message"] == _PRIVATE_WEB_SEARCH_BLOCKED
    assert kernel.calls == [] and model.calls == []
    stored = storage.get_message(str(reply["message_id"]), "alice")
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    assert metadata["structural"]["private_web_search_blocked"] is True


@pytest.mark.asyncio
async def test_recent_attachment_still_allows_a_self_contained_isolated_web_turn(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    conversation_id = _private_attachment_history(storage, old=False)
    for index in range(21):
        storage.store_message(
            conversation_id,
            "alice",
            "user" if index % 2 == 0 else "assistant",
            f"neutral-{index}",
            metadata={"private_context_lineage": True},
        )
    kernel = _WebKernel()
    model = _WebAnswerModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )

    request = "Найди в интернете температуру кипения воды"
    reply = await runtime.chat(
        "alice",
        request,
        actor=_actor(),
        conversation_id=conversation_id,
    )

    assert kernel.calls == [
        (
            "web_research",
            {"query": runtime.web_query_from(request), "max_sources": 3},
        )
    ]
    assert reply["tools_used"] == ["web_research"]
    assert reply["web_evidence_status"] == "sourced"
    exposed = json.dumps(model.calls, ensure_ascii=False)
    assert "PRIVATE-HISTORY-CANARY" not in exposed
    assert "PRIVATE-ANSWER-CANARY" not in exposed
    assert request in exposed


def _stored_file(
    storage,
    *,
    filename: str,
    text: str,
    tenant: str = "alice",
    uploader: str = "alice",
) -> RawObject:
    storage.ensure_user(tenant)
    if uploader != tenant:
        storage.ensure_user(uploader)
    raw = RawObject(
        id=new_id("raw"),
        user_id=tenant,
        source="upload",
        source_ref=new_id("source"),
        raw_content=text,
        content_type="file",
        metadata_json={
            "filename": filename,
            "uploaded_by": uploader,
            "extraction_success": True,
            "text_extraction_success": True,
        },
    )
    storage.store_raw_object(raw)
    return raw


def _current_attachment(raw: RawObject) -> dict[str, Any]:
    metadata = raw.metadata_json if isinstance(raw.metadata_json, dict) else {}
    return {
        "raw_object_id": raw.id,
        "filename": str(metadata.get("filename") or "attachment"),
        "transient_text": raw.raw_content,
        "extraction_success": True,
        "empty_text": not bool(raw.raw_content),
    }


def _patch_attachment_synthesis(runtime, monkeypatch):  # noqa: ANN001
    seen: list[tuple[str, list[dict[str, Any]]]] = []

    async def generate(context, message, attachments):  # noqa: ANN001, ARG001
        snapshot = [dict(item) for item in (attachments or [])]
        seen.append((str(message), snapshot))
        names = [str(item.get("filename") or "attachment") for item in snapshot]
        return {"content": "Синтетическая сводка: " + ", ".join(names), "tools_used": []}

    monkeypatch.setattr(runtime, "_prepare_context", _prepare_without_retrieval)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    return seen


@pytest.mark.asyncio
async def test_three_separate_upload_turns_restore_one_exact_complete_active_set(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    files = [
        _stored_file(storage, filename=f"doc-{index}.txt", text=f"DOC-{index}|" + chr(64 + index) * 14_994)
        for index in range(1, 4)
    ]
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=_NeverModel())
    seen = _patch_attachment_synthesis(runtime, monkeypatch)
    conversation_id: str | None = None
    for index, raw in enumerate(files, start=1):
        uploaded = await runtime.chat(
            "alice",
            f"Это документ {index}",
            actor=_actor(),
            conversation_id=conversation_id,
            attachments=[_current_attachment(raw)],
            enable_tools=False,
        )
        conversation_id = str(uploaded["conversation_id"])

    summary = await runtime.chat(
        "alice",
        "Обобщи эти три документа",
        actor=_actor(),
        conversation_id=conversation_id,
        attachments=[],
        enable_tools=False,
    )

    final_attachments = seen[-1][1]
    assert [item["raw_object_id"] for item in final_attachments] == [raw.id for raw in files]
    assert [item["filename"] for item in final_attachments] == [f"doc-{index}.txt" for index in range(1, 4)]
    assert all(len(str(item.get("transient_text") or "")) == 15_000 for item in final_attachments)
    assert summary["restored_attachment_count"] == 3
    assert summary["attachment_context_expected_count"] == 3
    assert summary["attachment_context_readable_count"] == 3
    assert summary["attachment_coverage_complete"] is True
    assert "повторно" not in summary["message"].casefold()


@pytest.mark.asyncio
async def test_current_third_upload_caption_combines_it_with_two_prior_upload_origins(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    files = [
        _stored_file(storage, filename=f"caption-{index}.txt", text=f"CAPTION-{index}")
        for index in range(1, 4)
    ]
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=_NeverModel())
    seen = _patch_attachment_synthesis(runtime, monkeypatch)
    conversation_id: str | None = None
    for index, raw in enumerate(files[:2], start=1):
        uploaded = await runtime.chat(
            "alice",
            f"Файл {index}",
            actor=_actor(),
            conversation_id=conversation_id,
            attachments=[_current_attachment(raw)],
            enable_tools=False,
        )
        conversation_id = str(uploaded["conversation_id"])

    summary = await runtime.chat(
        "alice",
        "Сделай общую сводку по трём последним документам",
        actor=_actor(),
        conversation_id=conversation_id,
        attachments=[_current_attachment(files[2])],
        enable_tools=False,
    )

    assert [item["raw_object_id"] for item in seen[-1][1]] == [raw.id for raw in files]
    assert summary["restored_attachment_count"] == 2
    assert summary["attachment_context_expected_count"] == 3
    assert summary["attachment_context_readable_count"] == 3
    assert summary["attachment_context_available"] is True


@pytest.mark.asyncio
async def test_an_authoritatively_empty_document_is_an_available_set_member(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    files = [
        _stored_file(storage, filename="nonempty-a.txt", text="ALPHA"),
        _stored_file(storage, filename="empty.txt", text=""),
        _stored_file(storage, filename="nonempty-b.txt", text="BETA"),
    ]
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=_NeverModel())
    seen = _patch_attachment_synthesis(runtime, monkeypatch)
    conversation_id: str | None = None
    for index, raw in enumerate(files, start=1):
        uploaded = await runtime.chat(
            "alice",
            f"Материал {index}",
            actor=_actor(),
            conversation_id=conversation_id,
            attachments=[_current_attachment(raw)],
            enable_tools=False,
        )
        conversation_id = str(uploaded["conversation_id"])

    summary = await runtime.chat(
        "alice",
        "Обобщи все три документа",
        actor=_actor(),
        conversation_id=conversation_id,
        enable_tools=False,
    )

    assert len(seen[-1][1]) == 3
    assert seen[-1][1][1]["empty_text"] is True
    assert summary["attachment_context_readable_count"] == 3
    assert summary["attachment_context_available"] is True
    assert "недоста" not in summary["message"].casefold()


@pytest.mark.asyncio
async def test_explicit_four_file_summary_with_only_three_uploads_is_honestly_incomplete(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    files = [
        _stored_file(storage, filename=f"only-{index}.txt", text=f"ONLY-{index}") for index in range(1, 4)
    ]
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=_NeverModel())
    seen = _patch_attachment_synthesis(runtime, monkeypatch)
    conversation_id: str | None = None
    for index, raw in enumerate(files, start=1):
        uploaded = await runtime.chat(
            "alice",
            f"Загрузка {index}",
            actor=_actor(),
            conversation_id=conversation_id,
            attachments=[_current_attachment(raw)],
            enable_tools=False,
        )
        conversation_id = str(uploaded["conversation_id"])
    generated_before = len(seen)

    summary = await runtime.chat(
        "alice",
        "Подготовь сводку по 4 последним файлам",
        actor=_actor(),
        conversation_id=conversation_id,
        enable_tools=False,
    )

    assert len(seen) == generated_before, "the model was asked to fill a missing fourth file"
    assert summary["attachment_context_expected_count"] == 4
    assert summary["attachment_context_readable_count"] == 3
    assert "3 из 4" in summary["message"]
    assert "неизвест" in summary["message"].casefold()


def test_multi_restore_never_backfills_an_unowned_upload_slot(settings, storage) -> None:
    storage.ensure_user("alice", preset_key="owner")
    owned = [
        _stored_file(storage, filename=f"owned-{index}.txt", text=f"OWNED-{index}") for index in range(2)
    ]
    foreign = _stored_file(
        storage,
        filename="foreign.txt",
        text="FOREIGN-CANARY",
        tenant="foreign-tenant",
        uploader="foreign-tenant",
    )
    now = datetime.now(UTC).isoformat()
    history = [
        {
            "role": "user",
            "content": f"upload-{index}",
            "created_at": now,
            "metadata_json": json.dumps(
                {
                    "had_attachments": True,
                    "attachment_count": 1,
                    "attachment_origin": "upload",
                    "conversation_attachment_raw_ids": [raw.id],
                }
            ),
        }
        for index, raw in enumerate([*owned, foreign], start=1)
    ]
    runtime = AgentRuntime(settings, storage)

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "Обобщи эти три документа",
        history,
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )

    assert [item["raw_object_id"] for item in restored] == [raw.id for raw in owned]
    assert expected == 3
    assert "FOREIGN-CANARY" not in json.dumps(restored, ensure_ascii=False)
