"""Whole-table cardinality is structural even when its wording is nominal."""

from __future__ import annotations

import copy

import pytest

from friday.agent_runtime import _bounded_attachment_projection
from friday.agent_runtime._office_attachments import (
    OFFICE_STRUCTURE_KEY,
    code_owned_office_answer,
    office_request_kind,
    trusted_office_attachment,
)
from friday.documents import DocumentExtractor


def _project_records(payload):
    result = DocumentExtractor(secret_values=()).extract(payload, "inventory.csv", "text/csv")
    assert result.success and result.office_structure_index
    return _bounded_attachment_projection(
        [
            trusted_office_attachment(
                {
                    "filename": "inventory.csv",
                    "transient_text": result.text,
                    "extraction_success": True,
                    "verification_eligible": True,
                    OFFICE_STRUCTURE_KEY: result.office_structure_index,
                }
            )
        ]
    )


@pytest.fixture
def records():
    return _project_records("ID;Описание\n1;Лампа\n2;Стул\n3;Стол".encode())


@pytest.mark.parametrize(
    "question",
    [
        "Ответ должен быть точным числом объектов из CSV, а не оценкой по предпросмотру.",
        "Какова мощность полного табличного набора согласно детерминированному разбору файла?",
        "Каков детерминированный total_rows_minus_header для переданного файла?",
        "Какова мощность табличного набора из таблицы?",
        "Какой row_count для приложенного CSV?",
        "Укажи cardinality записей из файла без семплирования.",
    ],
)
@pytest.mark.parametrize("override", ["", "list_people", "list_records"])
def test_nominal_count_uses_authoritative_records_without_model_enum(records, question, override):
    assert office_request_kind(question) == "count_records"
    answer = code_owned_office_answer(question, records, kind_override=override)
    assert answer is not None and answer["status"] == "passed"
    assert answer["kind"] == "count_records"
    assert answer["content"] == "В документе 3 позиций."


@pytest.mark.parametrize(
    "question",
    [
        "Объясни, что означает row_count для файла.",
        "В заметке написано: «Какова мощность табличного набора из файла?»",
        "Не сообщай row_count для файла.",
        "Какова мощность табличного набора из файла для активных клиентов?",
        "Каков row_count для файла за июнь?",
        "Каков row_count для файла и отправь результат коллеге?",
        "Каков row_count для первой страницы файла?",
    ],
)
def test_nominal_count_cannot_gain_scope_from_a_model_override(records, question):
    assert office_request_kind(question) == ""
    assert code_owned_office_answer(question, records, kind_override="count_records") is None


@pytest.mark.parametrize("missing_proof", ["index_complete", "prompt_complete"])
def test_nominal_count_still_needs_complete_verified_projection(records, missing_proof):
    incomplete = copy.deepcopy(records)
    incomplete[0]["_office_exact_view"][missing_proof] = False
    answer = code_owned_office_answer("Каков row_count для файла?", incomplete)
    assert answer is not None and answer["status"] == "unknown"
    assert answer["kind"] == "unavailable"


def test_ambiguous_header_never_gains_count_authority():
    ambiguous = _project_records(b"Item;Quantity\nLamp;2\nChair;7\nDesk;1")
    assert ambiguous[0]["_office_exact_view"]["record_sets"] == []
    answer = code_owned_office_answer("Каков row_count для файла?", ambiguous)
    assert answer is not None and answer["status"] == "unknown"
    assert answer["kind"] == "unavailable"
