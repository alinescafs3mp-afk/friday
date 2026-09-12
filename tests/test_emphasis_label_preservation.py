"""Explicit label/value composition preserves requested bytes and boundaries."""

import pytest

from friday.text_shape import (
    exact_emphasis_label_literal_owned,
    explicit_emphasis_label_contract,
    repair_explicit_text_shape,
)


@pytest.mark.parametrize(
    "label,value", [("готово", "UNIT-52"), ("согласовано", "Заказ-17"), ("ready", "alpha_42")]
)
@pytest.mark.parametrize("style", ["*", "**", "_", "__"])
def test_missing_requested_word_survives_emphasis(label, value, style):
    question = f"Подчеркни словом {label} значение {value} в коротком ответе."
    actual = repair_explicit_text_shape(question, f"{style}{value}{style}")
    assert actual == f"{style}{label} {value}{style}"
    assert repair_explicit_text_shape(question, actual) == actual


def test_tracking_suffix_is_metadata_not_required_answer_text():
    question = "Выдели словом принято токен OWNED-7 в кратком ответе. Проверка CTRL-99."
    assert repair_explicit_text_shape(question, "**OWNED-7**") == "**принято OWNED-7**"


@pytest.mark.parametrize(
    "label,value",
    [
        ("такси", "заказала"),
        ("напоминание", "установлено"),
        ("файл", "создан"),
    ],
)
def test_closed_label_contract_owns_only_exact_request_bytes(label, value):
    question = f"Подчеркни словом {label} значение {value} в коротком ответе."
    contract = explicit_emphasis_label_contract(question)
    assert contract is not None
    assert (contract.label, contract.value) == (label, value)
    assert exact_emphasis_label_literal_owned(question, f"*{value}*", allow_value_only=True)
    assert exact_emphasis_label_literal_owned(question, f"*{label} {value}*")
    assert not exact_emphasis_label_literal_owned(question, f"*{value}*")
    assert not exact_emphasis_label_literal_owned(question, f"*{label.title()} {value}*")
    assert not exact_emphasis_label_literal_owned(question, f"*{label} {value}* *готово*")
    assert not exact_emphasis_label_literal_owned(question, f"Готово: *{label} {value}*")


@pytest.mark.parametrize(
    "question",
    [
        "Не подчёркивай словом готово значение OWNED-7 в коротком ответе.",
        "Переведи: «Подчеркни словом готово значение OWNED-7 в коротком ответе».",
        "Подчеркни словом готово значение OWNED-7 из файла.",
        "Подчеркни словом готово значение OWNED-7 в коротком ответе; отправь письмо.",
        "Подчеркни словом готово значение OWNED-7 в коротком ответе. Затем создай файл.",
        "Подчеркни словом готово значение OWNED-7 в коротком ответе. Проверка CTRL-99. Сделай заказ.",
        "Подчеркни словом готово или принято значение OWNED-7 в коротком ответе.",
        "Он попросил: Подчеркни словом готово значение OWNED-7 в коротком ответе.",
        "Подчеркни словом готово значение <script> в коротком ответе.",
    ],
)
def test_ambiguous_source_compound_and_noninstruction_requests_are_unchanged(question):
    assert explicit_emphasis_label_contract(question) is None
    assert not exact_emphasis_label_literal_owned(question, "*готово OWNED-7*")
    assert repair_explicit_text_shape(question, "*OWNED-7*") == "*OWNED-7*"


@pytest.mark.parametrize(
    "answer",
    [
        "Не могу подтвердить выполнение.",
        "*WRONG-7*",
        "*owned-7*",
        "*OWNED-7* *OWNED-7*",
        "Описание: *OWNED-7*",
        "*OWNED-7*\nСтрока с фактом.",
        "*готово OWNED-7*",
        "**готово** *OWNED-7*",
        "`*OWNED-7*`",
    ],
)
def test_refusal_prose_wrong_values_and_existing_labels_are_not_rewritten(answer):
    question = "Подчеркни словом готово значение OWNED-7 в коротком ответе."
    assert repair_explicit_text_shape(question, answer) == answer
