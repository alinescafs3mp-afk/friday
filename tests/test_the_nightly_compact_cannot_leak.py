"""Ночная сводка не может протечь — это свойство схемы, а не фильтра.

Заказ владельца 2026-08-04, проект — `artifacts/compactor_design.md`. Исходный
бриф предлагал отдать сырые диалоги локальной модели и попросить её обезличить
пересказ. Здесь этого нет, и шесть проверок ниже — про то, почему.

Обезличивание не поручено модели потому, что за двое суток ПЯТЬ раз замерено: её
поведение промптом не задаётся. Поле в конверте данных, служебная строка в
промпте, та же строка вплотную к реплике, «локальная персональная Knowledge OS»
первой строкой, «подтверди коротко» — ни одно не сработало как механизм. Корпус
при этом содержит фамилии, звания и названия подразделений.

Вместо фильтра — построение: в сводке нет ПОЛЯ, куда текст мог бы попасть. Коды
инцидентов и целые числа, человеческая формулировка живёт в коде программы.
"""

from __future__ import annotations

import json

import pytest

from friday.organs.compactor import (
    _ALLOWED_FIELDS,
    _marks,
    compact_a_day,
    incident_text,
    incidents_of_a_turn,
    patterns_across_days,
)

# Ход, в котором есть всё, чего в сводке быть не должно: фамилия, звание,
# название подразделения, номер, сырая реплика и имена документов.
A_SENSITIVE_TURN = {
    "search_query": "что там у майора Нестеренко по войсковой части 12345",
    "retrieval_trace": [
        {"id": "ko_1", "title": "штатка_назначение_Нестеренко.xlsx", "score": 0.4},
    ],
    "knowledge_citations": {"K1": "ko_1"},
    "knowledge_object_ids": ["ko_1"],
    "grounding_warning": "⚠️ сказано, что ответ взят из вашего архива, — это не так",
    "answer_mode": "personal_knowledge_missing",
    "verification_status": "skipped",
    "answer_grounded": False,
    "knowledge_hits": 0,
    "tools_used": [],
    "structural": {
        "verdict_kind": "архив",
        "answer_present": False,
        "model_spoke": True,
        "remainder_known": False,
        "rule_learned": False,
        "rule_forgotten": False,
        "rule_refused": False,
        "correction_learned": False,
        "self_description_replaced": False,
        "llm_failed": False,
    },
}

SECRETS = ("Нестеренко", "майор", "12345", "штатка", "войсковой")


def test_no_word_of_the_corpus_reaches_the_compact() -> None:
    """Главная проверка. Мутация: читать `search_query` — краснеет.

    Проверяется ВЕСЬ вывод целиком, а не отдельные поля: утечка ищет любую щель,
    и перечислять щели — то же самое, что запретительный список.
    """
    counters, incidents = compact_a_day([A_SENSITIVE_TURN])

    printed = json.dumps([counters, incidents], ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in printed, f"в сводку попало: {secret!r}"


def test_the_compact_holds_only_codes_and_numbers() -> None:
    """Гарантия по построению: поля, куда текст мог бы попасть, просто нет.

    Код инцидента — из закрытого набора, всё остальное целые числа. Тогда
    «не просочилось ли имя» перестаёт быть надеждой на обезличивание.
    """
    counters, incidents = compact_a_day([A_SENSITIVE_TURN])

    for name, value in counters.items():
        assert isinstance(value, int), f"счётчик {name} — не число"
    for incident in incidents:
        assert set(incident) == {"code", "severity", "count"}, incident
        assert incident_text(incident["code"]) != incident["code"], "код без формулировки"
        assert isinstance(incident["count"], int)
        assert incident["severity"] in {"high", "medium", "low"}


def test_the_field_list_is_an_allow_list() -> None:
    """Новое поле в метаданных не доходит до детекторов САМО.

    Проверяется НАПРЯМУЮ, на `_marks`, и это исправление после мутации. Первая
    редакция проверяла список через выход компакта — а он состоит из кодов и
    чисел, поэтому мутация «сделать список запретительным» его не меняла вовсе и
    ПЕРЕЖИЛА проверку.

    Вывод, который стоит держать в голове: разрешительный список здесь не
    несущий, а страховочный. Несёт гарантию форма выхода (коды и числа); список
    защищает будущее изменение, когда в сводку захотят положить что-то ещё. Но
    непроверяемый страховочный механизм гниёт, поэтому проверяется он сам.
    """
    for forbidden in ("search_query", "retrieval_trace", "knowledge_citations", "knowledge_object_ids"):
        assert forbidden not in _ALLOWED_FIELDS, f"{forbidden} читается компактором"

    passed = _marks({**A_SENSITIVE_TURN, "новое_поле_с_фамилией": "Нестеренко"})

    assert "новое_поле_с_фамилией" not in passed, "новое поле дошло до детекторов само"
    assert "search_query" not in passed, "сырая реплика человека дошла до детекторов"
    assert "retrieval_trace" not in passed, "имена документов дошли до детекторов"
    assert set(passed) <= _ALLOWED_FIELDS, sorted(set(passed) - _ALLOWED_FIELDS)
    # И обратная сторона: разрешённое пропускается, иначе детекторы ослепнут.
    assert "grounding_warning" in passed and "structural" in passed


def test_the_measured_defects_are_seen() -> None:
    """Проверка самого выбора: событийная сводка ловит то, ради чего затевалась.

    Все три дефекта, найденных владельцем вручную 2026-08-04, видны без чтения
    переписки. Если бы это было не так, отказ от модели был бы отказом от цели.
    """
    assert "claimed_archive_without_data" in incidents_of_a_turn(A_SENSITIVE_TURN)

    said_it_was_gpt = {"structural": {"self_description_replaced": True}}
    assert "called_itself_someone_else" in incidents_of_a_turn(said_it_was_gpt)

    order_dropped = {"tools_used": [], "structural": {"verdict_kind": "действие"}}
    assert "order_ignored" in incidents_of_a_turn(order_dropped)


def test_a_quiet_day_invents_nothing() -> None:
    """Обратная сторона: день без происшествий даёт пустой список.

    Сводка, всегда находящая инциденты, читается как разметка и перестаёт
    что-либо значить — тот же класс, что у предупреждений не по делу.
    """
    calm = {
        "answer_mode": "general_conversation",
        "verification_status": "skipped",
        "tools_used": [],
        "structural": {"verdict_kind": "быт", "model_spoke": True},
    }

    counters, incidents = compact_a_day([calm, calm])

    assert incidents == []
    assert counters["total_turns"] == 2
    assert counters["model_answers"] == 2


def test_counters_separate_the_structure_from_the_model() -> None:
    """Иначе «сколько ответов собрала структура» не значило бы ничего."""
    structural = {"structural": {"answer_present": True, "model_spoke": False}}
    ordinary = {"structural": {"answer_present": False, "model_spoke": True}}

    counters, _ = compact_a_day([structural, ordinary, ordinary])

    assert counters["structural_answers"] == 1
    assert counters["model_answers"] == 2
    assert counters["total_turns"] == 3


def _day(*codes: str) -> dict:
    return {"incidents": [{"code": code, "severity": "high", "count": 1} for code in codes]}


def test_three_days_in_a_row_are_a_pattern() -> None:
    """Три дня подряд — уже поведение системы, а не неудачные сутки.

    Ради этого сводки и складываются: один случай человек заметит глазами,
    привычку — только рядом лежащими сводками.
    """
    found = patterns_across_days([_day("model_silent"), _day("model_silent"), _day("model_silent")])

    assert found == [{"code": "model_silent", "days": 3}]


def test_scattered_cases_are_not_a_pattern() -> None:
    """Обратная сторона: три случая за месяц — это три случая.

    Разница видна человеку и меняет, что он станет чинить. Считать «сколько раз
    за неделю» вместо цепочки значило бы стирать её.
    """
    found = patterns_across_days(
        [_day("model_silent"), _day(), _day("model_silent"), _day(), _day("model_silent")]
    )

    assert found == []


def test_a_streak_that_ended_yesterday_is_not_a_pattern() -> None:
    """Код, пропавший вчера, сегодня уже не поведение.

    Цепочка тянется от САМОГО СВЕЖЕГО дня. Иначе давняя починенная беда
    показывалась бы человеку как привычка ещё неделю после починки.
    """
    found = patterns_across_days(
        [_day("verification_failed"), _day("model_silent"), _day("model_silent"), _day("model_silent")]
    )

    assert found == []


def test_a_short_history_claims_nothing() -> None:
    """Двух сводок мало, чтобы говорить о повторении, — и молчать тут честно."""
    assert patterns_across_days([_day("model_silent"), _day("model_silent")]) == []


@pytest.mark.parametrize(
    "code",
    [
        # `structural_softened` снят 2026-08-05: пути, которым он мог бы
        # сработать, в системе нет — утверждение структуры приписывается
        # дословно, модель его не видит, доставка режет текст, а не обрезает.
        "claimed_archive_without_data",
        "called_itself_someone_else",
        "order_ignored",
        "correction_not_applied",
        "model_silent",
        "verification_failed",
        "answer_ungrounded",
        "rights_demanded",
    ],
)
def test_every_code_has_a_human_wording(code) -> None:
    """Формулировка живёт в коде программы и в базу не попадает.

    Иначе она снова стала бы строкой, выведенной из переписки, — и весь выбор
    развалился бы обратно.
    """
    said = incident_text(code)

    assert said != code, f"код {code} без формулировки"
    assert said.endswith("."), said
