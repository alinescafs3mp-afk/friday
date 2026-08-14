"""K05/K06 acceptance for ordinary Office turns and exact-path failures.

An answer-intent parser must not be run over ordinary model prose.  Exact and
whole-set postconditions remain fail closed, but every refusal shown to a person
must describe the practical problem and a reachable next step, not internals.
"""

from __future__ import annotations

import copy
import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

import friday.agent_runtime as agent_runtime_module
from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _bounded_attachment_projection,
    _requires_complete_attachment_evidence,
)
from friday.agent_runtime._office_attachments import (
    OFFICE_STRUCTURE_KEY,
    code_owned_office_answer,
    office_exact_request_detected,
    office_exhaustive_scope,
    trusted_office_attachment,
    validate_runtime_office_index,
)
from friday.documents import DocumentExtractor
from friday.permissions import ActorContext

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "package_c_document_holdout.json"


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _xlsx(rows: list[list[str]], *, role_width: int = 0) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "SYNTHETIC-ATTACHMENT"
    for row_number, values in enumerate(rows):
        rendered = list(values)
        if row_number and role_width:
            rendered[-1] += "X" * role_width
        sheet.append(rendered)
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def _people_attachment(*, incomplete: bool = False, role_width: int = 0) -> dict[str, Any]:
    case = _fixture()["k19_controls"][0]
    result = DocumentExtractor(secret_values=()).extract(
        _xlsx(case["rows"], role_width=role_width),
        str(case["filename"]),
    )
    assert result.success is True
    assert isinstance(result.office_structure_index, dict)
    index = copy.deepcopy(result.office_structure_index)
    if incomplete:
        index["complete"] = False
        index["coverage"]["reasons"] = ["text_budget"]
        assert validate_runtime_office_index(index, result.text) == index
    attachment = {
        "filename": case["filename"],
        "transient_text": result.text,
        "extraction_success": True,
        "verification_eligible": True,
        OFFICE_STRUCTURE_KEY: index,
    }
    if incomplete:
        # `text_budget` means the extractor stopped before the source tail.  An
        # index-only mutation paired with an otherwise complete text carrier is
        # internally contradictory and the full-fit projector correctly treats
        # those bytes as complete.  Model the real parser contract instead.
        attachment["text_truncated"] = True
    return trusted_office_attachment(attachment)


class _NeverCalledLLM:
    enabled = True
    model = "synthetic-package-c-never-called"
    total_budget_sec = 5.0

    async def chat(self, messages, **kwargs):  # pragma: no cover - every model seam is patched
        del messages, kwargs
        raise AssertionError("unexpected model call")


def _runtime(settings, storage, monkeypatch) -> AgentRuntime:  # noqa: ANN001
    storage.ensure_user("synthetic-user", preset_key="owner")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_NeverCalledLLM(),
    )

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            answer_mode="general_conversation",
        )

    async def no_exact_intent(question: str) -> str:
        del question
        return ""

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_office_intent_arbiter", no_exact_intent)
    return runtime


async def _model_turn(
    runtime: AgentRuntime, case: dict[str, Any], attachments: list[dict[str, Any]], monkeypatch
):  # noqa: ANN001, E501
    async def generate(context, message, projected):  # noqa: ANN001
        del context, message, projected
        return {"content": case["synthetic_model_answer"], "tools_used": []}

    monkeypatch.setattr(runtime, "_generate_response", generate)
    return await runtime.chat(
        "synthetic-user",
        case["question"],
        actor=ActorContext(user_id="synthetic-user", preset_key="owner", source="test"),
        attachments=attachments,
        enable_tools=False,
    )


@pytest.mark.parametrize("case", _fixture()["k05_preservation_cases"], ids=lambda case: case["id"])
@pytest.mark.asyncio
async def test_k05_ordinary_attachment_answer_is_preserved_byte_for_byte(
    case: dict[str, Any],
    settings,
    storage,
    monkeypatch,
) -> None:
    assert office_exact_request_detected(case["question"]) is False
    # Ordinary answer prose is not itself a new exact-set question.
    assert office_exact_request_detected(case["synthetic_model_answer"]) is False
    runtime = _runtime(settings, storage, monkeypatch)

    reply = await _model_turn(runtime, case, [_people_attachment()], monkeypatch)

    assert reply["message"] == case["synthetic_model_answer"]
    assert reply["message_format"] == "markdown"
    assert reply["verification_status"] != "unknown"
    stored = storage.get_message(str(reply["message_id"]), "synthetic-user")
    assert stored is not None and stored["content"] == case["synthetic_model_answer"]


@pytest.mark.asyncio
async def test_exact_office_literals_are_carried_as_plain_text_not_markdown(
    settings,
    storage,
    monkeypatch,
) -> None:
    rows = [
        ["ID", "Статус"],
        ["[SYNTHETIC-CELL](https://attacker.invalid/track)", "**SYNTHETIC-LITERAL**"],
        ["`RAW-MARKERS`", "~~SYNTHETIC-STATUS~~"],
    ]
    result = DocumentExtractor(secret_values=()).extract(
        _xlsx(rows),
        "synthetic-literal-carrier.xlsx",
    )
    assert result.success is True and isinstance(result.office_structure_index, dict)
    attachment = trusted_office_attachment(
        {
            "filename": "synthetic-literal-carrier.xlsx",
            "transient_text": result.text,
            "extraction_success": True,
            "verification_eligible": True,
            OFFICE_STRUCTURE_KEY: result.office_structure_index,
        }
    )
    runtime = _runtime(settings, storage, monkeypatch)

    reply = await runtime.chat(
        "synthetic-user",
        "Покажи все строки из файла.",
        actor=ActorContext(user_id="synthetic-user", preset_key="owner", source="test"),
        attachments=[attachment],
        enable_tools=False,
    )

    assert reply["message_format"] == "plain"
    for literal in (
        "[SYNTHETIC-CELL](https://attacker.invalid/track)",
        "**SYNTHETIC-LITERAL**",
        "`RAW-MARKERS`",
        "~~SYNTHETIC-STATUS~~",
    ):
        assert literal in reply["message"]


def test_k05_exact_questions_and_answer_only_completeness_controls_stay_guarded() -> None:
    controls = _fixture()["k05_rejection_controls"]

    for case in controls[:4]:
        assert office_exact_request_detected(case["question"]) is True, case["id"]
    for case in controls[4:]:
        assert office_exact_request_detected(case["question"]) is False, case["id"]
        assert office_exhaustive_scope(case["question"]) is True, case["id"]
        assert _requires_complete_attachment_evidence("", case["synthetic_model_answer"]) is True


@pytest.mark.parametrize(
    "question",
    [
        "Кто автор этого файла?",
        "Кто создал этот документ?",
        "Какие выводы в документе?",
        "Какие риски указаны в документе?",
        "Покажи заголовок документа.",
        "Скажи, о чём этот файл.",
        "Сколько страниц в документе?",
        "Назови автора файла и дай резюме.",
        "Какие люди могут редактировать этот документ?",
        "Какие сотрудники обычно создают такие документы?",
        "Скажи, какие люди подходят для работы с этим файлом.",
        "Какие люди упомянуты в названии документа?",
        "Какие сотрудники отвечают за формат документа?",
    ],
)
def test_ordinary_office_questions_are_not_mistaken_for_exact_set_requests(question: str) -> None:
    assert office_exact_request_detected(question) is False


@pytest.mark.parametrize(
    "question",
    [
        "Опиши первый лист документа.",
        "Что видно на первой странице файла?",
        "Дай обзор первого раздела документа.",
        "Что указано в строке 3 файла?",
        "Кто на странице 3 документа?",
        "Опиши колонку А таблицы.",
    ],
)
def test_local_office_questions_do_not_claim_whole_attachment_scope(question: str) -> None:
    assert office_exhaustive_scope(question) is False
    assert office_exact_request_detected(question) is False


@pytest.mark.parametrize(
    "question",
    [
        "Скажи имена из файла и оцени распределение ролей.",
        "Назови людей из файла и сравни их роли.",
        "Покажи сотрудников из документа и сделай вывод.",
        "Скажи всех людей из файла и оцени роли.",
    ],
)
def test_attached_people_list_with_an_open_remainder_stays_guarded(question: str) -> None:
    assert office_exact_request_detected(question) is True


@pytest.mark.parametrize("case", _fixture()["k05_rejection_controls"][4:], ids=lambda case: case["id"])
@pytest.mark.asyncio
async def test_k05_removing_answer_intent_parsing_does_not_allow_a_complete_set_claim(
    case: dict[str, Any],
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime = _runtime(settings, storage, monkeypatch)

    reply = await _model_turn(runtime, case, [_people_attachment()], monkeypatch)

    assert reply["message"] != case["synthetic_model_answer"]
    assert reply["verification_status"] == "unknown"


@pytest.mark.parametrize(
    "model_answer",
    [
        "Сотрудников: 3 — Иван, Пётр и Анна.",
        "Других людей не обнаружено.",
        "Перечислено 16 должностей.",
    ],
)
@pytest.mark.asyncio
async def test_k05_incomplete_office_evidence_rejects_natural_whole_set_claims(
    model_answer: str,
    settings,
    storage,
    monkeypatch,
) -> None:
    case = {
        "question": "Что главное в этом файле?",
        "synthetic_model_answer": model_answer,
    }
    assert office_exact_request_detected(case["question"]) is False
    assert office_exhaustive_scope(case["question"]) is True
    runtime = _runtime(settings, storage, monkeypatch)

    reply = await _model_turn(runtime, case, [_people_attachment(incomplete=True)], monkeypatch)

    assert reply["message"] != model_answer
    assert reply["verification_status"] == "unknown"
    assert reply["verified"] is False


def _k06_projection(case: dict[str, Any], monkeypatch) -> list[dict[str, Any]]:  # noqa: ANN001
    mode = case["mode"]
    if mode == "missing_index":
        return _bounded_attachment_projection(
            [
                {
                    "filename": "synthetic-missing-index.xlsx",
                    "transient_text": "SYNTHETIC-NONAUTHORITATIVE-TEXT",
                    "extraction_success": True,
                }
            ]
        )
    if mode == "incomplete_index":
        return _bounded_attachment_projection([_people_attachment(incomplete=True)])
    if mode == "multiple_attachments":
        return _bounded_attachment_projection(
            [
                _people_attachment(),
                {
                    "filename": "synthetic-sibling.txt",
                    "transient_text": "SYNTHETIC-SIBLING-TEXT",
                    "extraction_success": True,
                },
            ]
        )
    if mode == "prompt_budget":
        monkeypatch.setattr(agent_runtime_module, "_ATTACHMENT_CONTEXT_CHARS", 400)
        return _bounded_attachment_projection([_people_attachment()])
    raise AssertionError(mode)  # pragma: no cover - fixture enum is frozen


def _assert_human_facing_exact_failure(content: str) -> None:
    lowered = " ".join(content.casefold().split())
    fixture = _fixture()
    assert content.strip()
    assert any(word in lowered for word in ("файл", "таблиц", "документ"))
    assert any(phrase in lowered for phrase in ("не могу", "не удалось", "не хватает"))
    assert any(
        fragment in lowered
        for fragment in ("прикреп", "пришл", "повтор", "уточн", "выбер", "назов", "раздел")
    ), "the refusal gives no reachable next step"
    for fragment in fixture["acceptance"]["banned_user_facing_fragments"]:
        assert fragment not in lowered


@pytest.mark.parametrize("case", _fixture()["k06_human_refusals"], ids=lambda case: case["id"])
def test_k06_every_exact_path_failure_is_human_facing_and_actionable(
    case: dict[str, Any], monkeypatch
) -> None:
    projected = _k06_projection(case, monkeypatch)

    answer = code_owned_office_answer(
        case["question"],
        projected,
        kind_override=case["exact_kind"],
    )

    assert answer is not None and answer["status"] == case["expected_status"] == "unknown"
    assert answer["kind"] == "unavailable"
    _assert_human_facing_exact_failure(answer["content"])


@pytest.mark.asyncio
async def test_k06_runtime_guard_replacement_uses_the_same_human_vocabulary(
    settings,
    storage,
    monkeypatch,
) -> None:
    case = _fixture()["k05_rejection_controls"][2]
    runtime = _runtime(settings, storage, monkeypatch)

    reply = await _model_turn(runtime, case, [_people_attachment()], monkeypatch)

    assert reply["verification_status"] == "unknown"
    _assert_human_facing_exact_failure(reply["message"])


@pytest.mark.parametrize(
    "banned",
    _fixture()["acceptance"]["banned_user_facing_fragments"],
)
def test_k06_mutation_reintroducing_one_internal_fragment_is_rejected(banned: str) -> None:
    with pytest.raises(AssertionError):
        _assert_human_facing_exact_failure(
            f"Не могу проверить весь файл. Пришлите его повторно. Служебная причина: {banned}."
        )
