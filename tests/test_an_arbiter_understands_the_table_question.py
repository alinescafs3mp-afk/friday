"""Точный путь по таблице открывается пониманием, а не перечислением форм.

Четыре русские регулярки ловили 19 форм вопроса из 32 на корпусе, собранном ДО
того, как их покрытие было измерено. Пропущенное — не
экзотика: «посчитай людей», «кого он включает», «полный состав команды», «выведи
весь перечень». Пятая регулярка отодвинула бы границу и не убрала её.

Арбитр выбирает из ЗАКРЫТОГО списка готовых видов ответа и ничего не сочиняет:
сам ответ по-прежнему строится структурой таблицы, а не моделью. Это важно —
арбитр решает «о чём спросили», но не «что ответить».

Спрашивается он только там, где точный ответ вообще возможен: ровно одно офисное
вложение с полной структурой. Иначе вызов модели тратился бы на каждом ходе с
любым файлом.
"""

from __future__ import annotations

from typing import Any

import pytest

from friday.agent_runtime._office_attachments import (
    office_arbiter_applies,
    office_request_kind,
    parse_office_intent,
)


def _office_attachment(*, complete: bool = True) -> dict[str, Any]:
    return {
        "filename": "штат.xlsx",
        "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "_office_exact_view": {
            "index_complete": complete,
            "prompt_complete": complete,
            "records": {},
            "record_sets": [],
        },
    }


@pytest.mark.parametrize(
    "question",
    [
        "посчитай людей",
        "кого он включает",
        "полный состав команды",
        "выведи весь перечень",
        "распиши поимённо кто там",
    ],
)
def test_these_forms_are_exactly_the_ones_the_regexes_miss(question: str) -> None:
    """Опора всей работы: без неё «арбитр помог» нечем проверить.

    Если регулярка когда-нибудь начнёт ловить эти формы, проба покраснеет — и это
    правильно: тогда надо перемерить, нужен ли арбитр вообще.
    """
    assert office_request_kind(question) == "", f"регулярка уже ловит: {question}"


def test_the_arbiter_is_asked_only_where_an_exact_answer_is_possible() -> None:
    """Цена вызова ограничена ровно теми ходами, где он окупается."""
    assert office_arbiter_applies("посчитай людей", [_office_attachment()]) is True


def test_no_attachment_no_arbiter() -> None:
    assert office_arbiter_applies("посчитай людей", []) is False
    assert office_arbiter_applies("посчитай людей", None) is False


def test_an_incomplete_structure_does_not_deserve_a_call() -> None:
    """Неполная структура не даст точного ответа — спрашивать не о чем."""
    assert office_arbiter_applies("посчитай людей", [_office_attachment(complete=False)]) is False


def test_two_attachments_do_not_deserve_a_call() -> None:
    """Целевого файла нет: ответ по «всему файлу» при двух файлах неоднозначен."""
    assert office_arbiter_applies("посчитай людей", [_office_attachment(), {"filename": "акт.pdf"}]) is False


def test_a_question_the_regexes_already_catch_needs_no_call() -> None:
    """Вторая половина той же границы: не платить за то, что уже решено."""
    assert office_request_kind("сколько там человек") != ""
    assert office_arbiter_applies("сколько там человек", [_office_attachment()]) is False


@pytest.mark.parametrize("question", ["сколько всего в таблице?", "сколько в таблице?"])
def test_a_target_only_table_count_takes_the_closed_route_without_an_arbiter(
    question: str,
) -> None:
    """The explicit table target makes count_auto structural and model-independent."""

    assert office_request_kind(question) == "count_auto"
    assert office_arbiter_applies(question, [_office_attachment()]) is False


@pytest.mark.parametrize(
    "question",
    [
        "О чём речь в этом файле?",
        "Дай подробный обзор файла",
        "Что скажешь про этот документ?",
        "Проанализируй документ и укажи риски",
        "Какие выводы следуют из этой таблицы?",
    ],
)
def test_an_ordinary_file_review_never_pays_for_the_exact_table_arbiter(question: str) -> None:
    """A review is one synthesis task, not a count/list classifier plus review."""

    assert office_arbiter_applies(question, [_office_attachment()]) is False


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('{"kind": "count_people"}', "count_people"),
        ('Вот ответ: {"kind":"list_records"} — всё', "list_records"),
        ('{"kind": "none"}', ""),
        ('{"kind": "выдумка"}', ""),
        ("совсем не json", ""),
        ("", ""),
    ],
)
def test_only_a_known_kind_is_accepted(reply: str, expected: str) -> None:
    """Список видов ЗАКРЫТ: арбитр выбирает готовый ответ, а не сочиняет намерение.

    Непонятная реплика — пустая строка, то есть обычный путь. Догадка здесь
    означала бы, что модель открывает точный путь произвольной фразой.
    """
    assert parse_office_intent(reply) == expected


def test_the_arbiter_is_wired_into_the_turn() -> None:
    """Проба на ПОДКЛЮЧЕНИЕ: арбитр без вызова — потраченный замер.

    Проверяется исходник хода, а не сам арбитр: механизм у него уже есть, и
    прежние находки этого проекта учат, что теряется именно подключение.
    """
    import inspect

    from friday import agent_runtime

    source = inspect.getsource(agent_runtime)
    assert "office_arbiter_applies(clean_message, attachments)" in source
    assert "kind_override=arbitrated" in source
