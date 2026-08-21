"""Adversarial regressions for the live 0.206.3 output-boundary hotfixes."""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import (
    _ATTACHMENT_EVIDENCE_MISMATCH_REJECTION,
    _BARE_UPLOAD_REVIEW_TASK,
    _UNCONFIRMED_SUPPORTED_DEED,
    AgentContext,
    AgentRuntime,
    _attachment_verdict_with_deterministic_drift,
    _brainfuck_bounded_output,
    _brainfuck_explanation_followup_response,
    _claims_an_unconfirmed_supported_deed,
    _informational_consent_history,
)
from friday.execution_kernel import ToolResult
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext
from tests.test_legacy_doc_extraction import build_doc


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="test")


def _tool_schema(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "synthetic adversarial capability",
            "parameters": {"type": "object", "properties": {}},
        },
    }


class _DuplicateAttackKernel:
    """Advertise both archive tools and record every actual kernel entry."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def get_tool_definitions(self, actor: ActorContext, *, topic: str = "") -> list[dict[str, Any]]:
        del actor, topic
        return [_tool_schema("conflict_list"), _tool_schema("resolve_duplicates")]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: ActorContext | None = None,
    ) -> ToolResult:
        del actor
        self.executed.append((name, dict(arguments)))
        data = (
            {"items": [], "total": 0, "shown": 0, "complete": True}
            if name == "conflict_list"
            else {"candidates": [], "count": 0, "scan": {"complete": True}, "complete": True}
        )
        return ToolResult(name, True, data=data)


class _HostileDuplicateModel:
    """Try both archive calls once, then provide ordinary explanatory prose."""

    enabled = True
    model = "hostile-duplicate-consent"
    total_budget_sec = 3.0

    def __init__(self, *, attack_without_schema: bool) -> None:
        self.calls: list[dict[str, Any]] = []
        self.attack_without_schema = attack_without_schema
        self.attacked = False

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001
        offered = {
            str((item.get("function") or {}).get("name") or item.get("name") or "") for item in (tools or [])
        }
        self.calls.append(
            {
                "messages": [dict(item) for item in messages],
                "offered": offered,
                "kwargs": dict(kwargs),
            }
        )
        if not self.attacked and (offered or self.attack_without_schema):
            self.attacked = True
            return {
                "content": (
                    "Конфликт знаний — это несовместимые утверждения, а дубликаты — "
                    "две записи об одной сущности. Здесь я только объясняю разницу."
                ),
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_conflicts",
                        "type": "function",
                        "function": {"name": "conflict_list", "arguments": '{"limit": 5}'},
                    },
                    {
                        "id": "call_duplicates",
                        "type": "function",
                        "function": {"name": "resolve_duplicates", "arguments": "{}"},
                    },
                ],
                "_queue_wait_sec": 0.0,
            }
        return {
            "content": (
                "Конфликт знаний — это несовместимые утверждения, а дубликаты — "
                "две записи об одной сущности. Здесь я только объясняю разницу."
            ),
            "finish_reason": "stop",
            "tool_calls": None,
            "_queue_wait_sec": 0.0,
        }


def _effect_candidate_counts(storage: Any) -> dict[str, int]:
    return {
        table: int(storage.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
        for table in ("inbox", "entity_resolution_candidates", "knowledge_conflicts", "audit_log")
    }


@pytest.mark.asyncio
async def test_terse_informational_consent_denies_hostile_duplicate_tools_for_the_full_turn(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    conversation = storage.create_conversation("alice", title="informational consent")
    storage.store_message(
        str(conversation["id"]),
        "alice",
        "user",
        "Чем отличаются конфликты знаний от дубликатов сущностей?",
    )
    storage.store_message(
        str(conversation["id"]),
        "alice",
        "assistant",
        "Могу объяснить, чем отличаются конфликты знаний от дубликатов сущностей.",
    )
    kernel = _DuplicateAttackKernel()
    model = _HostileDuplicateModel(attack_without_schema=True)
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )

    async def forbidden_context(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("informational consent fell through to retrieval/context preparation")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    before = _effect_candidate_counts(storage)
    reply = await runtime.chat(
        "alice",
        "давай",
        actor=_actor(),
        conversation_id=str(conversation["id"]),
        enable_tools=True,
    )

    assert kernel.executed == []
    assert _effect_candidate_counts(storage) == before
    assert model.calls and all(call["offered"] == set() for call in model.calls)
    assert reply["tools_used"] == []
    assert reply["files"] == []
    assert reply["voice"] is None
    assert "только объясняю" in str(reply["message"])


@pytest.mark.asyncio
async def test_an_explicit_non_terse_duplicate_request_keeps_both_capabilities_reachable(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _DuplicateAttackKernel()
    model = _HostileDuplicateModel(attack_without_schema=False)
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )

    async def prepared(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            search_query=message,
            outward_verdict=("архив", None),
            answer_mode="personal_knowledge",
        )

    monkeypatch.setattr(runtime, "_prepare_context", prepared)
    reply = await runtime.chat(
        "alice",
        "Покажи конфликты знаний и запусти поиск дубликатов сущностей прямо сейчас.",
        actor=_actor(),
        enable_tools=True,
    )

    assert next(call["offered"] for call in model.calls if call["offered"]) == {
        "conflict_list",
        "resolve_duplicates",
    }
    assert [name for name, _arguments in kernel.executed] == [
        "conflict_list",
        "resolve_duplicates",
    ]
    assert reply["tools_used"] == ["conflict_list", "resolve_duplicates"]


def _odt_payload(text: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr(
            "content.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:text><text:p>"""
            + text
            + """</text:p></office:text></office:body>
</office:document-content>""",
        )
    return stream.getvalue()


async def _registered_document(
    settings: Any,
    storage: Any,
    *,
    filename: str,
    source_text: str,
) -> dict[str, str]:
    if filename.endswith(".doc"):
        payload = build_doc(source_text)
        mime_type = "application/msword"
    else:
        payload = _odt_payload(source_text)
        mime_type = "application/vnd.oasis.opendocument.text"
    outcome = await IngestionPipeline(
        settings,
        storage,
        KnowledgeGraph(storage),
    ).ingest_file(
        "alice",
        None,
        payload,
        filename=filename,
        mime_type=mime_type,
        metadata={"uploaded_by": "alice"},
        source_ref=f"hotfix-02063:{filename}",
    )
    return {"raw_object_id": str(outcome["raw_object_id"])}


async def _plain_attachment_context(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
    del message, kwargs
    return AgentContext(
        conversation_id=conversation_id,
        user_id=user_id,
        person_id=user_id,
        answer_mode="general_conversation",
    )


class _UnexpectedModel:
    enabled = True
    model = "patched-live-boundary"
    total_budget_sec = 3.0

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        del messages, kwargs
        raise AssertionError("the named generation/verifier seams were not patched")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename",
    [
        pytest.param("STRICT LIVE REVIEW.doc", id="doc"),
        pytest.param("STRICT LIVE REVIEW.odt", id="odt"),
    ],
)
async def test_bare_document_one_pass_uses_code_drift_then_one_bounded_repair(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    attachment = await _registered_document(
        settings,
        storage,
        filename=filename,
        source_text="Продажи по регионам: Север — 120; Юг — 80.",
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=_UnexpectedModel(),  # type: ignore[arg-type]
    )
    generation_questions: list[str] = []
    verification_questions: list[str] = []
    repair_questions: list[str] = []
    initial_mismatch = "По данным документа, Юг — 120 продаж."
    repaired_mismatch = "По данным документа, Север — 80 продаж."

    async def generate(context, question, attachments):  # noqa: ANN001
        del context, attachments
        generation_questions.append(str(question))
        return {"content": initial_mismatch, "tools_used": [], "_model_generated": True}

    async def verify(question, answer, context, *, tool_evidence):  # noqa: ANN001
        del answer, context, tool_evidence
        verification_questions.append(str(question))
        return {
            "status": "failed",
            "ok": False,
            "score": 0.0,
            "issues": ["synthetic evidence mismatch"],
        }

    async def repair(question, answer, context, verdict, *, tool_evidence):  # noqa: ANN001
        del answer, context, verdict, tool_evidence
        repair_questions.append(str(question))
        return repaired_mismatch

    monkeypatch.setattr(runtime, "_prepare_context", _plain_attachment_context)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_verify_response", verify)
    monkeypatch.setattr(runtime, "_repair_once", repair)
    reply = await runtime.chat(
        "alice",
        f"Загружен документ: {filename}",
        actor=_actor(),
        attachments=[attachment],
        enable_tools=False,
        synthetic_document_notice=True,
    )

    assert generation_questions == [f"Загружен документ: {filename}"]
    # The ordinary bare review never asks the same model to judge its own
    # initial prose. The code-owned high-confidence drift guard catches the
    # swapped labelled value, then the one permitted repair is rechecked once.
    assert verification_questions == [_BARE_UPLOAD_REVIEW_TASK]
    assert repair_questions == [_BARE_UPLOAD_REVIEW_TASK]
    assert filename not in _BARE_UPLOAD_REVIEW_TASK
    assert ".doc" not in _BARE_UPLOAD_REVIEW_TASK.casefold()
    assert initial_mismatch not in str(reply["message"])
    assert repaired_mismatch not in str(reply["message"])
    assert reply["message"] == _ATTACHMENT_EVIDENCE_MISMATCH_REJECTION
    assert "быстрый обзор" not in str(reply["message"]).casefold()
    assert reply["verification_status"] == "unknown"


@pytest.mark.asyncio
async def test_bare_upload_cannot_publish_an_inverted_source_negation(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fluent open review still crosses the evidence verifier."""

    storage.ensure_user("alice", preset_key="owner")
    filename = "NEGATED RELATION.odt"
    attachment = await _registered_document(
        settings,
        storage,
        filename=filename,
        source_text="Документ Альфа не подготовлен отделом снабжения.",
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=_UnexpectedModel(),  # type: ignore[arg-type]
    )
    false_positive = "По данным файла: Документ Альфа подготовлен отделом снабжения."
    verification_calls: list[tuple[str, str]] = []
    repair_calls: list[str] = []

    async def generate(context, question, attachments):  # noqa: ANN001
        del context, question, attachments
        return {"content": false_positive, "tools_used": [], "_model_generated": True}

    async def verify(question, answer, context, *, tool_evidence):  # noqa: ANN001
        del context, tool_evidence
        verification_calls.append((str(question), str(answer)))
        return {
            "status": "failed",
            "ok": False,
            "score": 0.0,
            "issues": ["source negation inverted"],
        }

    async def repair(question, answer, context, verdict, *, tool_evidence):  # noqa: ANN001
        del answer, context, verdict, tool_evidence
        repair_calls.append(str(question))
        return false_positive

    monkeypatch.setattr(runtime, "_prepare_context", _plain_attachment_context)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_verify_response", verify)
    monkeypatch.setattr(runtime, "_repair_once", repair)
    reply = await runtime.chat(
        "alice",
        f"Загружен документ: {filename}",
        actor=_actor(),
        attachments=[attachment],
        enable_tools=False,
        synthetic_document_notice=True,
    )

    assert [question for question, _answer in verification_calls] == [_BARE_UPLOAD_REVIEW_TASK]
    assert repair_calls == [_BARE_UPLOAD_REVIEW_TASK]
    assert false_positive not in str(reply["message"])
    assert "не подготовлен" not in false_positive
    assert reply["verified"] is False


_PASSIVE_SOURCE_EVIDENCE = ("Документ «Альфа» подготовлен отделом снабжения.",)


def _supported_deed_claim(claim: str) -> bool:
    return _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=False,
        reminder_succeeded=False,
        voice_succeeded=False,
        passive_source_state=True,
        passive_input_file_state_evidence=_PASSIVE_SOURCE_EVIDENCE,
    )


def test_an_evidenced_passive_input_file_relation_survives_the_supported_deed_guard() -> None:
    assert not _supported_deed_claim("Документ «Альфа» подготовлен отделом снабжения.")


@pytest.mark.parametrize(
    "claim",
    [
        pytest.param(
            "Документ «Бета» подготовлен отделом кадров.",
            id="fabricated-passive-relation",
        ),
        pytest.param("Я подготовила документ «Альфа».", id="active-self"),
        pytest.param("PDF готов.", id="pdf-ready"),
        pytest.param("Напоминание установлено.", id="reminder"),
        pytest.param("Аудио готово.", id="audio"),
        pytest.param(
            "Документ «Альфа» подготовлен отделом снабжения. PDF готов.",
            id="mixed-evidenced-and-fabricated",
        ),
    ],
)
def test_passive_source_scope_does_not_legalize_an_unsupported_deed(claim: str) -> None:
    assert _supported_deed_claim(claim)


@pytest.mark.asyncio
async def test_a_hostile_repair_cannot_add_a_supported_deed_to_a_bare_upload_review(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    filename = "SUPPORTED DEED REPAIR.odt"
    attachment = await _registered_document(
        settings,
        storage,
        filename=filename,
        source_text=_PASSIVE_SOURCE_EVIDENCE[0],
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=_UnexpectedModel(),  # type: ignore[arg-type]
    )
    repaired_deed = (
        "Документ «Альфа» подготовлен отделом снабжения. PDF готов. "
        "Напоминание установлено, аудио отправлено."
    )
    verification_calls: list[str] = []

    async def generate(context, question, attachments):  # noqa: ANN001
        del context, question, attachments
        return {
            "content": "Документ «Альфа» не подготовлен отделом снабжения.",
            "tools_used": [],
            "_model_generated": True,
        }

    async def verify(question, answer, context, *, tool_evidence):  # noqa: ANN001
        del answer, context, tool_evidence
        verification_calls.append(str(question))
        if len(verification_calls) == 1:
            return {"status": "failed", "ok": False, "score": 0.0, "issues": ["repair"]}
        return {"status": "passed", "ok": True, "score": 1.0, "issues": []}

    async def repair(question, answer, context, verdict, *, tool_evidence):  # noqa: ANN001
        del answer, context, verdict, tool_evidence
        assert question == _BARE_UPLOAD_REVIEW_TASK
        return repaired_deed

    monkeypatch.setattr(runtime, "_prepare_context", _plain_attachment_context)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_verify_response", verify)
    monkeypatch.setattr(runtime, "_repair_once", repair)
    reply = await runtime.chat(
        "alice",
        f"Загружен документ: {filename}",
        actor=_actor(),
        attachments=[attachment],
        enable_tools=False,
        synthetic_document_notice=True,
    )

    # The deterministic polarity guard owns the first rejection; the hostile
    # repair is removed by the supported-deed guard before it can be judged or
    # published.
    assert verification_calls == []
    assert reply["message"] == _UNCONFIRMED_SUPPORTED_DEED
    assert repaired_deed not in repr(reply)


_FALSE_PASS = {"status": "passed", "ok": True, "score": 1.0, "issues": []}


@pytest.mark.parametrize(
    ("answer", "evidence", "expected_issue"),
    [
        pytest.param(
            "Подразделение применяет БПЛА.",
            "Подразделение применяет радиосвязь.",
            "attachment_proper_name_not_in_evidence",
            id="acronym",
        ),
        pytest.param(
            "В списке 17 позиций.",
            "В списке 12 позиций.",
            "attachment_quantity_not_in_evidence",
            id="number",
        ),
        pytest.param(
            "Пункт дислокации: Молодогвардейск.",
            "Пункт дислокации: Северодонецк.",
            "attachment_proper_name_not_in_evidence",
            id="toponym",
        ),
        pytest.param(
            "Учтены 2 БПЛА (3 аппарата).",
            "Учтены 2 БПЛА. Раздел 3 описывает комплектацию.",
            "attachment_quantity_relation_not_in_evidence",
            id="parenthetical-unit-relation",
        ),
        pytest.param(
            "Север лидирует на 50.",
            "Продажи: Север — 120; Юг — 80.",
            "attachment_quantity_not_in_evidence",
            id="false-leader-delta",
        ),
        pytest.param(
            "В документе 1 риск.",
            'Вложение "plan.txt", фрагмент 1:\nРиски не перечислены.',
            "attachment_quantity_not_in_evidence",
            id="transport-fragment-is-not-source-quantity",
        ),
        pytest.param(
            "Отдел продаж лидирует на 40.",
            "Склад: 120 единиц. Бюджет: 80 рублей.",
            "attachment_quantity_not_in_evidence",
            id="unrelated-operands-do-not-prove-leader-delta",
        ),
        pytest.param(
            "Юг лидирует на 40.",
            "Север: 120. Юг: 80.",
            "attachment_quantity_not_in_evidence",
            id="inverted-leader-does-not-prove-delta",
        ),
        pytest.param(
            "Рост составил 25%.",
            "Было 100, стало 120.",
            "attachment_quantity_not_in_evidence",
            id="wrong-derived-growth",
        ),
        pytest.param(
            "Температура −5 °C.",
            "Температура 5 °C.",
            "attachment_quantity_not_in_evidence",
            id="unicode-negative-sign",
        ),
        pytest.param(
            "Температура -5 °C.",
            "Температура 5 °C.",
            "attachment_quantity_not_in_evidence",
            id="ascii-negative-sign",
        ),
        pytest.param(
            "Значение 12,5%.",
            "Значение 12,6%.",
            "attachment_quantity_not_in_evidence",
            id="decimal-percent-mismatch",
        ),
        pytest.param(
            "Код CASE-405.",
            "Код CASE-404.",
            "attachment_proper_name_not_in_evidence",
            id="prefix-separator-identifier",
        ),
        pytest.param(
            "ID er_9cac1fcf6eff4429.",
            "ID er_9cac1fcf6eff4428.",
            "attachment_proper_name_not_in_evidence",
            id="underscore-identifier",
        ),
        pytest.param(
            "Север лидирует на 20.",
            "Продажи: Север — 120; Юг — 80. Прибыль: Восток — 100.",
            "attachment_quantity_not_in_evidence",
            id="leader-delta-does-not-cross-metrics",
        ),
        pytest.param(
            "Север лидирует на 40.",
            "Север: 120. Склад: 80.",
            "attachment_quantity_not_in_evidence",
            id="leader-needs-a-compatible-comparator",
        ),
        pytest.param(
            "Продажи выросли на 20%.",
            "Продажи: было 100, стало 150. Штат: было 10, стало 12.",
            "attachment_quantity_not_in_evidence",
            id="growth-does-not-borrow-another-metric",
        ),
        pytest.param(
            "В документе 17 позиций.",
            "Перечень оборудования приложен.",
            "attachment_quantity_not_in_evidence",
            id="fabricated-quantity-without-operands",
        ),
        pytest.param(
            "Рост составил 25%.",
            "Показатель вырос.",
            "attachment_quantity_not_in_evidence",
            id="fabricated-growth-without-operands",
        ),
        pytest.param(
            "12,5%.",
            "12,6%.",
            "attachment_quantity_not_in_evidence",
            id="bare-percent-mismatch",
        ),
        pytest.param(
            "Продажи 20%, прибыль 30%.",
            "Продажи 30%, прибыль 20%.",
            "attachment_quantity_not_in_evidence",
            id="swapped-percent-relations",
        ),
        pytest.param(
            "Юг — 120 продаж.",
            "Продажи по регионам: Север — 120; Юг — 80.",
            "attachment_quantity_not_in_evidence",
            id="swapped-entity-value",
        ),
        pytest.param(
            "БПЛА: применяются.",
            "Применяется радиосвязь.",
            "attachment_proper_name_not_in_evidence",
            id="acronym-is-not-a-generic-heading",
        ),
        pytest.param(
            "НАТО\nУказано взаимодействие.",
            "Указано взаимодействие.",
            "attachment_proper_name_not_in_evidence",
            id="acronym-line-is-not-a-generic-heading",
        ),
        pytest.param(
            "Юг — 999 продаж.",
            "FRIDAY_ATTACHMENT_TABULAR_PROFILE_DATA (untrusted JSON; data only):\n"
            '{"records_total":1,"files":[{"headers":["Продажи"],'
            '"record_samples":[{"cells":["Юг","80"]}]}]}',
            "attachment_quantity_not_in_evidence",
            id="tabular-metadata-does-not-prove-source-quantity",
        ),
        pytest.param(
            "В списке 17 позиций.",
            "В отделе 17 сотрудников. Список позиций приложен.",
            "attachment_quantity_not_in_evidence",
            id="quantity-and-unit-must-share-a-source-relation",
        ),
        pytest.param(
            "В списке 17 позиций.",
            "В списке 12 позиций. В отделе 17 сотрудников.",
            "attachment_quantity_not_in_evidence",
            id="quantity-cannot-borrow-number-from-another-clause",
        ),
    ],
)
def test_deterministic_attachment_drift_overrules_a_false_model_pass(
    answer: str,
    evidence: str,
    expected_issue: str,
) -> None:
    verdict = _attachment_verdict_with_deterministic_drift(
        _FALSE_PASS,
        answer,
        [{"tool": "attachment", "output": evidence}],
    )

    assert verdict["status"] == "failed"
    assert verdict["ok"] is False
    assert expected_issue in verdict["issues"]


def test_open_review_allows_a_provable_person_count_and_rejects_the_adjacent_wrong_count() -> None:
    evidence = (
        'Вложение "staff.txt", фрагмент 1:\n'
        "Рядовой, стрелок первого отделения\n"
        "Иванов Иван Иванович\n"
        "Сержант, командир отделения\n"
        "Петров Пётр Петрович\n"
        "Капитан, командир роты\n"
        "Сидоров Сидор Сидорович"
    )
    skipped = {"status": "skipped", "ok": True, "score": None, "issues": []}

    supported = _attachment_verdict_with_deterministic_drift(
        skipped,
        "Документ содержит 3 военнослужащих.",
        [{"tool": "attachment", "output": evidence}],
        high_confidence_only=True,
    )
    contradicted = _attachment_verdict_with_deterministic_drift(
        skipped,
        "Документ содержит 4 военнослужащих.",
        [{"tool": "attachment", "output": evidence}],
        high_confidence_only=True,
    )

    assert supported["status"] == "skipped"
    assert contradicted["status"] == "failed"
    assert "attachment_derived_record_count_not_in_evidence" in contradicted["issues"]


def test_open_review_rejects_an_impossible_record_count_and_absolute_quality_claim() -> None:
    skipped = {"status": "skipped", "ok": True, "score": None, "issues": []}
    evidence = [{"tool": "attachment", "output": "Документ описывает назначение проекта."}]

    impossible = _attachment_verdict_with_deterministic_drift(
        skipped,
        "Документ содержит 777 записей.",
        evidence,
        high_confidence_only=True,
    )
    absolute = _attachment_verdict_with_deterministic_drift(
        skipped,
        "Документ безупречен.",
        evidence,
        high_confidence_only=True,
    )

    assert impossible["status"] == "failed"
    assert "attachment_derived_record_count_not_in_evidence" in impossible["issues"]
    assert absolute["status"] == "failed"
    assert "attachment_absolute_quality_claim" in absolute["issues"]


@pytest.mark.parametrize(
    ("answer", "evidence"),
    [
        pytest.param(
            "Подразделение применяет БПЛА.",
            "Подразделение применяет БПЛА.",
            id="grounded-acronym",
        ),
        pytest.param("В списке 17 позиций.", "В списке 17 позиций.", id="grounded-number"),
        pytest.param(
            "Пункт дислокации: Молодогвардейск.",
            "Пункт дислокации: Молодогвардейск.",
            id="grounded-toponym",
        ),
        pytest.param(
            "Учтены 2 БПЛА (3 аппарата).",
            "Учтены 2 БПЛА (3 аппарата).",
            id="grounded-parenthetical-unit",
        ),
        pytest.param(
            "Север лидирует на 40.",
            "Продажи: Север — 120; Юг — 80.",
            id="derived-leader-delta",
        ),
        pytest.param(
            "Первый риск — задержка.",
            "Риск — задержка.",
            id="ordinal-is-not-a-quantity",
        ),
        pytest.param(
            "ВЫВОДЫ\nРиск — задержка.",
            "Риск — задержка.",
            id="all-caps-heading-is-not-an-acronym",
        ),
        pytest.param(
            "Рост составил 20%.",
            "Было 100, стало 120.",
            id="derived-growth",
        ),
        pytest.param(
            "Север лидирует на 40.",
            "Год: 2026. Север: 120. Юг: 80.",
            id="leader-delta-ignores-year-metadata",
        ),
        pytest.param(
            "ИТОГО: риск — задержка.",
            "Риск — задержка.",
            id="inline-all-caps-heading",
        ),
        pytest.param(
            "Дата 21.08.2026.",
            "Дата 2026-08-21.",
            id="equivalent-date-spellings",
        ),
        pytest.param(
            "Север — 120 продаж.",
            "Север — 120 продаж; Юг — 80 продаж.",
            id="exact-quantity-among-multiple-same-unit-values",
        ),
        pytest.param(
            "Север лидирует на 40.",
            "Продажи: Север — 120; Юг — 80. Штат: Восток — 500.",
            id="leader-delta-does-not-mix-later-metric",
        ),
        pytest.param(
            "II. Основные риски: задержка.",
            "Основные риски: задержка.",
            id="roman-list-marker",
        ),
        pytest.param(
            "PDF содержит план работ.",
            "План работ.",
            id="authenticated-format-word",
        ),
        pytest.param(
            "FAQ по документу: риск — задержка.",
            "Риск — задержка.",
            id="response-format-word",
        ),
        pytest.param(
            "РИСКИ\nОсновной риск — задержка.",
            "Основной риск — задержка.",
            id="single-word-review-heading",
        ),
        pytest.param(
            "КЛЮЧЕВЫЕ РИСКИ\nОсновной риск — задержка.",
            "Основной риск — задержка.",
            id="multiword-review-heading",
        ),
        pytest.param(
            "## ОСНОВНЫЕ ТЕЗИСЫ\nДокумент описывает назначение.",
            "Документ описывает назначение.",
            id="markdown-review-heading",
        ),
        pytest.param(
            "Практический вывод: следует проверить сроки.",
            "Следует проверить сроки.",
            id="sentence-initial-adjective-is-not-toponym",
        ),
        pytest.param(
            "Технический раздел описывает назначение.",
            "Раздел описывает назначение.",
            id="technical-adjective-is-not-toponym",
        ),
        pytest.param(
            "Материал описывает назначение, структуру и основные риски.",
            "Материал описывает назначение, структуру и основные риски.",
            id="benign-prose",
        ),
    ],
)
def test_deterministic_attachment_drift_preserves_grounded_and_benign_passes(
    answer: str,
    evidence: str,
) -> None:
    assert (
        _attachment_verdict_with_deterministic_drift(
            _FALSE_PASS,
            answer,
            [{"tool": "attachment", "output": evidence}],
        )
        == _FALSE_PASS
    )


_TYPED_METRIC_EVIDENCE = "Продажи: Север — 120. Продажи: Юг — 80. Штат: Восток — 500. Штат: Запад — 490."


@pytest.mark.parametrize(
    ("answer", "expected_issue"),
    [
        pytest.param("Север лидирует на 40.", None, id="sales-delta"),
        pytest.param("Восток лидирует на 10.", None, id="staff-delta"),
        pytest.param(
            "Восток лидирует на 380.",
            "attachment_quantity_not_in_evidence",
            id="cross-metric-north",
        ),
        pytest.param(
            "Восток лидирует на 420.",
            "attachment_quantity_not_in_evidence",
            id="cross-metric-south",
        ),
    ],
)
def test_deterministic_drift_keeps_entity_values_inside_typed_metric_scopes(
    answer: str,
    expected_issue: str | None,
) -> None:
    verdict = _attachment_verdict_with_deterministic_drift(
        _FALSE_PASS,
        answer,
        [{"tool": "attachment", "output": _TYPED_METRIC_EVIDENCE}],
    )

    if expected_issue is None:
        assert verdict == _FALSE_PASS
    else:
        assert verdict["status"] == "failed"
        assert expected_issue in verdict["issues"]


@pytest.mark.parametrize(
    ("answer", "evidence", "expected_issue"),
    [
        pytest.param(
            "Восток — 120.",
            "Север — 120. Юг — 80.",
            "attachment_quantity_not_in_evidence",
            id="invented-region-borrows-value",
        ),
        pytest.param(
            "Отдел маркетинга — 120.",
            "Север — 120. Юг — 80.",
            "attachment_quantity_not_in_evidence",
            id="invented-department-borrows-value",
        ),
        pytest.param(
            "Маркетинг: 80.",
            "Север — 120. Юг — 80.",
            "attachment_quantity_not_in_evidence",
            id="invented-label-borrows-value",
        ),
        pytest.param(
            "Сотрудников: 17.",
            "Сотрудников: 12.",
            "attachment_quantity_not_in_evidence",
            id="single-entity-value",
        ),
        pytest.param(
            "В штате 2 234 сотрудника.",
            "В штате 1 234 сотрудника.",
            "attachment_quantity_not_in_evidence",
            id="grouped-thousands",
        ),
        pytest.param(
            "Бюджет — 10 млн рублей.",
            "Бюджет — 10 млн долларов.",
            "attachment_quantity_not_in_evidence",
            id="scaled-currency-unit",
        ),
        pytest.param(
            "Затраты — 10 человеко-часов.",
            "Затраты — 10 человеко-дней.",
            "attachment_quantity_not_in_evidence",
            id="hyphenated-unit",
        ),
    ],
)
def test_deterministic_drift_rejects_missing_entity_and_quantity_relations(
    answer: str,
    evidence: str,
    expected_issue: str,
) -> None:
    verdict = _attachment_verdict_with_deterministic_drift(
        _FALSE_PASS,
        answer,
        [{"tool": "attachment", "output": evidence}],
    )

    assert verdict["status"] == "failed"
    assert expected_issue in verdict["issues"]


@pytest.mark.parametrize(
    ("answer", "evidence"),
    [
        pytest.param(
            "Ответственный — Иванов.",
            "Ответственный — Петров.",
            id="ivanov-petrov",
        ),
        pytest.param(
            "Ответственный — Сидоров.",
            "Ответственный — Иванов.",
            id="sidorov-ivanov",
        ),
        pytest.param("Проект — Альфа.", "Проект — Бета.", id="alpha-beta"),
        pytest.param("Система — Orion.", "Система — Vega.", id="orion-vega"),
    ],
)
def test_deterministic_drift_rejects_ordinary_proper_name_substitution(
    answer: str,
    evidence: str,
) -> None:
    verdict = _attachment_verdict_with_deterministic_drift(
        _FALSE_PASS,
        answer,
        [{"tool": "attachment", "output": evidence}],
    )

    assert verdict["status"] == "failed"
    assert "attachment_proper_name_not_in_evidence" in verdict["issues"]


@pytest.mark.parametrize(
    ("answer", "evidence", "expected_issue"),
    [
        pytest.param(
            "БПЛА НАТО\nУказано взаимодействие.",
            "Указано взаимодействие.",
            "attachment_proper_name_not_in_evidence",
            id="two-factual-acronyms",
        ),
        pytest.param(
            "CASE NATO\nУказано взаимодействие.",
            "Указано взаимодействие.",
            "attachment_proper_name_not_in_evidence",
            id="factual-name-heading",
        ),
    ],
)
def test_multiword_heading_shape_cannot_hide_factual_tokens(
    answer: str,
    evidence: str,
    expected_issue: str,
) -> None:
    verdict = _attachment_verdict_with_deterministic_drift(
        _FALSE_PASS,
        answer,
        [{"tool": "attachment", "output": evidence}],
    )

    assert verdict["status"] == "failed"
    assert expected_issue in verdict["issues"]


@pytest.mark.parametrize(
    ("answer", "evidence", "expected_issue"),
    [
        pytest.param(
            "БПЛА не применяются.",
            "БПЛА применяются.",
            "attachment_relation_not_in_evidence",
            id="negation-reversal",
        ),
        pytest.param(
            "Допустимо не менее 10 сотрудников.",
            "Допустимо не более 10 сотрудников.",
            "attachment_quantity_relation_not_in_evidence",
            id="bound-reversal",
        ),
        pytest.param(
            "Показатель снизился на 20%.",
            "Показатель вырос на 20%.",
            "attachment_quantity_relation_not_in_evidence",
            id="percent-direction",
        ),
        pytest.param(
            "API зависит от CRM.",
            "CRM зависит от API.",
            "attachment_relation_not_in_evidence",
            id="relation-direction",
        ),
        pytest.param(
            "CASE-404 заменяет CASE-405.",
            "CASE-405 заменяет CASE-404.",
            "attachment_relation_not_in_evidence",
            id="identifier-relation-direction",
        ),
        pytest.param(
            "Заказ AB123 одобрен.",
            "Заказ AB124 одобрен.",
            "attachment_proper_name_not_in_evidence",
            id="bare-alphanumeric-identifier",
        ),
        pytest.param(
            "Контакт: alice@example.com.",
            "Контакт: bob@example.com.",
            "attachment_proper_name_not_in_evidence",
            id="email-identifier",
        ),
    ],
)
def test_deterministic_drift_rejects_semantic_polarity_and_identifier_drift(
    answer: str,
    evidence: str,
    expected_issue: str,
) -> None:
    verdict = _attachment_verdict_with_deterministic_drift(
        _FALSE_PASS,
        answer,
        [{"tool": "attachment", "output": evidence}],
    )

    assert verdict["status"] == "failed"
    assert expected_issue in verdict["issues"]


@pytest.mark.parametrize(
    ("answer", "evidence"),
    [
        pytest.param(
            "Продажи выросли на 20%.",
            "Продажи:\nБыло 100, стало 120.",
            id="growth-under-typed-header",
        ),
        pytest.param(
            "Используются БПЛА.",
            "Используются беспилотные летательные аппараты.",
            id="known-acronym-expansion",
        ),
        pytest.param("В штате 17 человек.", "В штате 17 сотрудников.", id="person-unit-synonym"),
        pytest.param("Масса — 12,50 кг.", "Масса — 12.5 кг.", id="decimal-spelling"),
        pytest.param(
            "Дата: 21.08.2026.",
            "Дата: 21 августа 2026 года.",
            id="russian-month-date",
        ),
        pytest.param(
            "КЛЮЧЕВЫЕ РИСКИ\nОсновной риск — задержка.",
            "Основной риск — задержка.",
            id="generic-two-word-heading",
        ),
    ],
)
def test_deterministic_drift_preserves_supported_semantic_equivalence(
    answer: str,
    evidence: str,
) -> None:
    assert (
        _attachment_verdict_with_deterministic_drift(
            _FALSE_PASS,
            answer,
            [{"tool": "attachment", "output": evidence}],
        )
        == _FALSE_PASS
    )


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("Маркетинг: Восток — 120.", id="novel-metric-existing-entity"),
        pytest.param("Маркетинг: Север — 120.", id="novel-metric-existing-pair"),
        pytest.param("Прибыль: Юг — 80.", id="novel-metric-existing-value"),
    ],
)
def test_an_invented_metric_scope_cannot_launder_source_entity_values(answer: str) -> None:
    evidence = "Продажи: Север — 120; Юг — 80. Штат: Восток — 500; Запад — 490."

    verdict = _attachment_verdict_with_deterministic_drift(
        _FALSE_PASS,
        answer,
        [{"tool": "attachment", "output": evidence}],
    )

    assert verdict["status"] == "failed"
    assert "attachment_quantity_not_in_evidence" in verdict["issues"]


@pytest.mark.parametrize(
    ("answer", "evidence", "passes"),
    [
        pytest.param(
            "Север лидирует на 40.",
            "Продажи: Север — 120; Юг — 80; Восток — 70.",
            True,
            id="runner-up-margin",
        ),
        pytest.param(
            "Север лидирует на 50.",
            "Продажи: Север — 120; Юг — 80; Восток — 70.",
            False,
            id="third-place-is-not-runner-up",
        ),
        pytest.param(
            "Север лидирует на 40.",
            "Продажи: Север — 120; Юг — 120; Восток — 80.",
            False,
            id="tied-maximum-is-not-a-leader",
        ),
    ],
)
def test_leader_delta_is_against_the_unique_runner_up(
    answer: str,
    evidence: str,
    passes: bool,
) -> None:
    verdict = _attachment_verdict_with_deterministic_drift(
        _FALSE_PASS,
        answer,
        [{"tool": "attachment", "output": evidence}],
    )

    assert (verdict == _FALSE_PASS) is passes


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param("Продажи составляют 20%.", id="same-percent-level"),
        pytest.param("Доля продаж — 20%.", id="same-percent-share"),
        pytest.param("Продажи: 20% рынка.", id="same-percent-market-share"),
        pytest.param(
            "Продажи 20%. Прибыль: было 100, стало 120.",
            id="other-metric-operands",
        ),
    ],
)
def test_a_same_anchor_percentage_is_not_itself_growth_evidence(evidence: str) -> None:
    verdict = _attachment_verdict_with_deterministic_drift(
        _FALSE_PASS,
        "Продажи выросли на 20%.",
        [{"tool": "attachment", "output": evidence}],
    )

    assert verdict["status"] == "failed"
    assert "attachment_quantity_not_in_evidence" in verdict["issues"]


def test_a_percent_share_cannot_prove_a_noun_growth_claim() -> None:
    verdict = _attachment_verdict_with_deterministic_drift(
        _FALSE_PASS,
        "Рост продаж составил 20%.",
        [{"tool": "attachment", "output": "Доля продаж составила 20%."}],
    )

    assert verdict["status"] == "failed"
    assert "attachment_quantity_not_in_evidence" in verdict["issues"]


@pytest.mark.parametrize(
    ("answer", "evidence"),
    [
        pytest.param(
            "Продажи составляют 20 процентов.",
            "Прибыль составляет 20 процентов.",
            id="metric",
        ),
        pytest.param(
            "На Север приходится 120 единиц.",
            "На Юг приходится 120 единиц.",
            id="region-prepositional",
        ),
        pytest.param(
            "Север продал 120 единиц.",
            "Юг продал 120 единиц.",
            id="region-subject",
        ),
        pytest.param(
            "В штате 17 сотрудников.",
            "В продажах 17 сотрудников.",
            id="organizational-scope",
        ),
        pytest.param(
            "Продажи выросли на 20 процентов.",
            "Прибыль выросла на 20 процентов.",
            id="growth-metric",
        ),
    ],
)
def test_natural_quantity_relations_remain_bound_to_their_subject(
    answer: str,
    evidence: str,
) -> None:
    verdict = _attachment_verdict_with_deterministic_drift(
        _FALSE_PASS,
        answer,
        [{"tool": "attachment", "output": evidence}],
    )

    assert verdict["status"] == "failed"
    assert "attachment_quantity_not_in_evidence" in verdict["issues"]


@pytest.mark.parametrize(
    ("answer", "evidence"),
    [
        pytest.param(
            "Документ Альфа подготовлен.",
            "Документ Альфа не был подготовлен.",
            id="negative-source-auxiliary",
        ),
        pytest.param(
            "Документ Альфа был подготовлен.",
            "Документ Альфа не подготовлен.",
            id="negative-source-no-auxiliary",
        ),
        pytest.param(
            "Поставка выполнена.",
            "Поставка не была выполнена.",
            id="delivery-auxiliary",
        ),
        pytest.param(
            "Документ Альфа подготовлен снабжением.",
            "Документ Альфа не подготовлен отделом снабжения.",
            id="agent-detail",
        ),
        pytest.param(
            "Альфа не готова, Бета готова.",
            "Альфа готова, Бета не готова.",
            id="two-entity-polarities",
        ),
        pytest.param(
            "Документ не подготовлен, отчёт готов.",
            "Документ подготовлен, отчёт не готов.",
            id="two-document-polarities",
        ),
        pytest.param(
            "Север не выполнил план, Юг выполнил.",
            "Север выполнил план, Юг не выполнил.",
            id="two-region-polarities",
        ),
    ],
)
def test_negation_is_bound_to_each_subject_predicate(answer: str, evidence: str) -> None:
    verdict = _attachment_verdict_with_deterministic_drift(
        _FALSE_PASS,
        answer,
        [{"tool": "attachment", "output": evidence}],
    )

    assert verdict["status"] == "failed"
    assert "attachment_relation_not_in_evidence" in verdict["issues"]


@pytest.mark.parametrize(
    ("answer", "evidence", "passes"),
    [
        pytest.param(
            "Более 10 сотрудников.",
            "Не менее 10 сотрудников.",
            False,
            id="inclusive-does-not-prove-strict-lower",
        ),
        pytest.param(
            "Не менее 10 сотрудников.", "Более 10 сотрудников.", True, id="strict-proves-inclusive-lower"
        ),
        pytest.param(
            "Менее 10 сотрудников.",
            "Не более 10 сотрудников.",
            False,
            id="inclusive-does-not-prove-strict-upper",
        ),
        pytest.param(
            "Не более 10 сотрудников.", "Менее 10 сотрудников.", True, id="strict-proves-inclusive-upper"
        ),
    ],
)
def test_bound_evidence_must_logically_entail_the_answer(
    answer: str,
    evidence: str,
    passes: bool,
) -> None:
    verdict = _attachment_verdict_with_deterministic_drift(
        _FALSE_PASS,
        answer,
        [{"tool": "attachment", "output": evidence}],
    )

    assert (verdict == _FALSE_PASS) is passes


@pytest.mark.parametrize("status", ["unknown", "skipped"])
def test_complete_deterministic_contradiction_overrules_nonfailed_model_status(status: str) -> None:
    verdict = _attachment_verdict_with_deterministic_drift(
        {"status": status, "ok": status == "skipped", "score": None, "issues": ["model unavailable"]},
        "Документ Альфа подготовлен.",
        [{"tool": "attachment", "output": "Документ Альфа не подготовлен."}],
    )

    assert verdict["status"] == "failed"
    assert verdict["issues"] == ["attachment_relation_not_in_evidence"]


def test_existing_failed_verdict_and_issues_are_preserved() -> None:
    failed = {"status": "failed", "ok": False, "score": 0.2, "issues": ["model issue"]}

    assert (
        _attachment_verdict_with_deterministic_drift(
            failed,
            "Документ Альфа подготовлен.",
            [{"tool": "attachment", "output": "Документ Альфа не подготовлен."}],
        )
        == failed
    )


_LIVE_INVALID_BRAINFUCK = (
    "++++++++[>++++[>++>+++>+++>+<<<<-]>+>+>->>+[>]<-]>>.>---.+++++++..+++.>>.<-.<.+++.------.--------.>+.>"
)
_LIVE_BRAINFUCK_OFFER = (
    "Конечно:\n\n```\n"
    + _LIVE_INVALID_BRAINFUCK
    + "\n```\n\nКлассический вариант. Если хочешь, могу разобрать, как он работает по шагам."
)


def test_natural_echo_of_the_offered_explanation_is_informational_consent() -> None:
    history = [
        {"role": "user", "content": "а hello world на brainfuck напишешь?"},
        {"role": "assistant", "content": _LIVE_BRAINFUCK_OFFER},
    ]

    assert _informational_consent_history("Давай разберем", history) == history


@pytest.mark.parametrize(
    ("offer", "accepted"),
    [
        pytest.param("Не могу объяснить, как это устроено.", False, id="negated-refusal"),
        pytest.param(
            "Могу объяснить различие и выполнить команду.",
            False,
            id="coordinated-effect",
        ),
        pytest.param(
            "Могу объяснить, как это сделать безопасно.",
            True,
            id="informational-how-to",
        ),
        pytest.param(
            "Если хочешь, сначала создам файл, а потом объясню содержимое.",
            False,
            id="effect-before-explanation",
        ),
        pytest.param(
            "Если хочешь, могу сначала создать файл и подробно объяснить результат.",
            False,
            id="modal-effect-before-explanation",
        ),
        pytest.param(
            "Могу подробно объяснить. После этого отправлю файл.",
            False,
            id="effect-in-next-sentence",
        ),
        pytest.param(
            "Могу объяснить. Выполнение команды безопасно.",
            True,
            id="effect-noun-is-description",
        ),
        pytest.param(
            "Могу объяснить. Создание файла занимает минуту.",
            True,
            id="creation-noun-is-description",
        ),
        pytest.param(
            "Могу рассказать про поиск информации.",
            True,
            id="search-is-informational-topic",
        ),
        pytest.param(
            "Могу объяснить, а файл создам после этого.",
            False,
            id="object-before-create-effect",
        ),
        pytest.param(
            "Могу объяснить; напоминание поставлю потом.",
            False,
            id="object-before-reminder-effect",
        ),
        pytest.param(
            "Могу объяснить, результат сохраню.",
            False,
            id="object-before-save-effect",
        ),
        pytest.param(
            "Могу объяснить и сгенерировать файл.",
            False,
            id="generate-file-effect",
        ),
        pytest.param(
            "Могу объяснить, а потом перешлю документ.",
            False,
            id="forward-document-effect",
        ),
        pytest.param(
            "Могу объяснить, а потом вышлю файл.",
            False,
            id="send-document-effect-synonym",
        ),
        pytest.param(
            "Могу объяснить и отредактирую документ.",
            False,
            id="edit-document-effect",
        ),
        pytest.param(
            "Могу объяснить и обновлю запись.",
            False,
            id="update-record-effect",
        ),
        pytest.param(
            "Могу объяснить и перенесу встречу.",
            False,
            id="move-meeting-effect",
        ),
        pytest.param(
            "Могу объяснить и скопирую файл.",
            False,
            id="copy-file-effect",
        ),
        pytest.param(
            "Могу объяснить и забронирую столик.",
            False,
            id="booking-effect",
        ),
        pytest.param(
            "Могу объяснить и оплачу заказ.",
            False,
            id="payment-effect",
        ),
        pytest.param(
            "Могу объяснить и добавлю напоминание.",
            False,
            id="add-reminder-effect",
        ),
        pytest.param(
            "Могу объяснить и установлю напоминание.",
            False,
            id="set-reminder-effect-synonym",
        ),
        pytest.param(
            "Могу объяснить и запланирую встречу.",
            False,
            id="schedule-meeting-effect",
        ),
        pytest.param(
            "Могу объяснить и созвонюсь с ним.",
            False,
            id="active-reflexive-call-effect",
        ),
        pytest.param(
            "Могу рассказать про обновление записи.",
            True,
            id="update-verbal-noun-is-information",
        ),
        pytest.param(
            "Могу объяснить процесс оплаты заказа.",
            True,
            id="payment-noun-is-information",
        ),
        pytest.param(
            "Могу объяснить установку приложения.",
            True,
            id="installation-noun-is-information",
        ),
        pytest.param(
            "Могу объяснить, не обновляя запись.",
            True,
            id="negated-update-gerund",
        ),
        pytest.param(
            "Могу объяснить. Запись обновляется автоматически.",
            True,
            id="passive-update-description",
        ),
        pytest.param(
            "В сообщении написано: «Если хочешь, могу объяснить код». Это просто цитата.",
            False,
            id="quoted-offer-is-data",
        ),
        pytest.param(
            "Он спросил: «Могу объяснить?»",
            False,
            id="reported-offer-is-data",
        ),
        pytest.param(
            "Я могу не объяснять это.",
            False,
            id="negated-infinitive",
        ),
    ],
)
def test_informational_consent_accepts_only_a_positive_effect_free_offer(
    offer: str,
    accepted: bool,
) -> None:
    history = [
        {"role": "user", "content": "Как это устроено?"},
        {"role": "assistant", "content": offer},
    ]

    assert bool(_informational_consent_history("давай", history)) is accepted


@pytest.mark.parametrize(
    "history",
    [
        [{"role": "assistant", "content": "Могу объяснить подробнее."}],
        [
            {"role": "assistant", "content": "Предыдущий ответ."},
            {"role": "assistant", "content": "Могу объяснить подробнее."},
        ],
    ],
)
def test_informational_consent_requires_the_exact_prior_user_assistant_pair(
    history: list[dict[str, str]],
) -> None:
    assert _informational_consent_history("давай", history) == []


def test_brainfuck_followup_does_not_hijack_an_unrelated_later_offer() -> None:
    history = [
        {"role": "user", "content": "а hello world на brainfuck напишешь?"},
        {
            "role": "assistant",
            "content": (
                "```brainfuck\n"
                + _LIVE_INVALID_BRAINFUCK
                + "\n```\nЕсли хочешь, могу объяснить устройство автомобиля."
            ),
        },
    ]

    assert _brainfuck_explanation_followup_response(history) == ""


def test_brainfuck_followup_does_not_treat_an_unrelated_program_as_its_offer() -> None:
    history = [
        {"role": "user", "content": "а hello world на brainfuck напишешь?"},
        {
            "role": "assistant",
            "content": (
                "```brainfuck\n"
                + _LIVE_INVALID_BRAINFUCK
                + "\n```\nЕсли хочешь, могу объяснить программу тренировок."
            ),
        },
    ]

    assert _brainfuck_explanation_followup_response(history) == ""


@pytest.mark.parametrize(
    "offer",
    [
        "Если хочешь, могу разобрать код.",
        "Если хочешь, могу разобрать программу.",
        "Если хочешь, могу объяснить код.",
    ],
)
def test_brainfuck_followup_accepts_an_immediate_bare_program_offer(offer: str) -> None:
    history = [
        {"role": "user", "content": "а hello world на brainfuck напишешь?"},
        {
            "role": "assistant",
            "content": f"```brainfuck\n{_LIVE_INVALID_BRAINFUCK}\n```\n{offer}",
        },
    ]

    accepted_history = _informational_consent_history("Давай разберем", history)
    response = _brainfuck_explanation_followup_response(accepted_history)
    programs = re.findall(r"```(?:brainfuck)?\s*([+\-<>\[\].,]+)\s*```", response)

    assert accepted_history == history
    assert "Предыдущий код был некорректным" in response
    assert any(_brainfuck_bounded_output(program) == b"Hello World!\n" for program in programs)


def test_brainfuck_followup_does_not_bind_a_bare_object_with_an_unrelated_qualifier() -> None:
    history = [
        {"role": "user", "content": "а hello world на brainfuck напишешь?"},
        {
            "role": "assistant",
            "content": (
                f"```brainfuck\n{_LIVE_INVALID_BRAINFUCK}\n```\nЕсли хочешь, могу объяснить код Python."
            ),
        },
    ]

    assert _brainfuck_explanation_followup_response(history) == ""


def test_brainfuck_followup_obeys_the_persisted_reply_edge() -> None:
    history = [
        {
            "id": "msg_1111111111111111",
            "role": "user",
            "content": "напиши A на brainfuck",
        },
        {
            "id": "msg_2222222222222222",
            "role": "assistant",
            "reply_to": "msg_0000000000000000",
            "content": "```brainfuck\n+++.\n```\nЕсли хочешь, могу объяснить этот код.",
        },
    ]

    assert _brainfuck_explanation_followup_response(history) == ""


@pytest.mark.parametrize(
    "assistant",
    [
        pytest.param(
            "```brainfuck\n"
            + _LIVE_INVALID_BRAINFUCK
            + "\n```\n```python\nprint(1)\n```\nЕсли хочешь, могу разобрать эту программу.",
            id="intervening-python-program",
        ),
        pytest.param(
            "```brainfuck\n"
            + _LIVE_INVALID_BRAINFUCK
            + "\n```\n~~~python\nprint(1)\n~~~\nЕсли хочешь, могу разобрать этот код.",
            id="intervening-tilde-python-program",
        ),
        pytest.param(
            "Не блок: x```brainfuck\n+++\n```\nЕсли хочешь, могу разобрать этот код.",
            id="brainfuck-fence-not-at-line-start",
        ),
        pytest.param(
            "```brainfuck\n   \n```\nЕсли хочешь, могу разобрать этот код.",
            id="empty-brainfuck-fence",
        ),
    ],
)
def test_brainfuck_followup_requires_a_nonempty_nearest_brainfuck_program(assistant: str) -> None:
    history = [
        {"role": "user", "content": "а hello world на brainfuck напишешь?"},
        {"role": "assistant", "content": assistant},
    ]

    assert _brainfuck_explanation_followup_response(history) == ""


@pytest.mark.asyncio
async def test_a_brainfuck_explanation_followup_cannot_certify_stale_invalid_code(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact 2026-08-21 live continuation must cross an executable oracle."""

    storage.ensure_user("alice", preset_key="owner")
    conversation = storage.create_conversation("alice", title="live Brainfuck continuation")
    previous_user = storage.store_message(
        str(conversation["id"]),
        "alice",
        "user",
        "а hello world на brainfuck напишешь?",
    )
    previous_assistant = storage.store_message(
        str(conversation["id"]),
        "alice",
        "assistant",
        _LIVE_BRAINFUCK_OFFER,
        reply_to=str(previous_user["id"]),
    )
    history = [previous_user, previous_assistant]
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_UnexpectedModel(),  # type: ignore[arg-type]
    )
    live_false_explanation = (
        "Разберём этот код по частям. Наш код:\n```\n"
        + _LIVE_INVALID_BRAINFUCK
        + "\n```\nПосле подготовки в памяти лежат коды символов. Итого: `Hello World!`"
    )

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            conversation_history=history,
            search_query=message,
            answer_mode="general_conversation",
        )

    async def generate(context, question, attachments):  # noqa: ANN001
        del context, attachments
        assert question == "Давай разберем"
        return {
            "content": live_false_explanation,
            "tools_used": [],
            "_model_generated": True,
        }

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    reply = await runtime.chat(
        "alice",
        "Давай разберем",
        actor=_actor(),
        conversation_id=str(conversation["id"]),
        enable_tools=False,
    )

    programs = re.findall(r"```(?:brainfuck)?\s*([+\-<>\[\].,]+)\s*```", str(reply["message"]))
    corrected = any(_brainfuck_bounded_output(program) == b"Hello World!\n" for program in programs)
    explicit_rejection = bool(
        re.search(r"(?:не\s+выводит|неверн|ошибоч|некоррект)", str(reply["message"]), re.IGNORECASE)
    )
    assert corrected or explicit_rejection
    assert live_false_explanation not in str(reply["message"])
