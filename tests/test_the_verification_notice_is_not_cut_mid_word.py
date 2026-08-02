"""Предупреждение автопроверки не обрывается на полуслове.

Найдено владельцем 2026-08-02: «сообщения обрезаются в последнем блоке, где
автопроверка нашла возможные несоответствия с вашими данными».

Прежняя редакция склеивала замечания через «; » и рубила строку на двухсотом
знаке — посреди слова, без единого признака, что дальше было ещё. Человек читал
оборванную претензию к собственным данным и не мог узнать, в чём она состояла.

Это уже пятое попадание одного класса за двое суток (голос на 2000-м знаке,
разбор по сроку, картинка без предела, документ 3.75 млн → 2 млн): предел сам по
себе законен, молчание о нём — нет.
"""

from __future__ import annotations

from friday.agent_runtime import (
    VERDICT_FAILED,
    VERDICT_PASSED,
    VERDICT_UNKNOWN,
    _verification_caution,
)

LONG = (
    "В ответе указано, что срок поверки составляет четыре года, тогда как в вашем "
    "документе форма No 1вп описан иной порядок и другие сроки"
)


def test_every_issue_is_shown_whole() -> None:
    """Мутация: вернуть склейку с обрезом `[:200]` — тест краснеет."""
    text = _verification_caution(VERDICT_FAILED, [LONG, "Второе замечание"])
    assert LONG in text, "замечание пришло обрезанным"
    assert "Второе замечание" in text


def test_each_issue_is_its_own_line() -> None:
    text = _verification_caution(VERDICT_FAILED, ["первое", "второе", "третье"])
    assert text.count("•") == 3, text


def test_what_did_not_fit_is_counted_out_loud() -> None:
    """Предел остаётся, но человек видит, что за ним что-то есть."""
    many = [f"{LONG} — замечание номер {index}" for index in range(9)]
    text = _verification_caution(VERDICT_FAILED, many)
    assert "и ещё" in text, "часть замечаний исчезла молча"
    hidden = 9 - text.count("•")
    assert f"и ещё {hidden}" in text, text


def test_a_single_giant_issue_is_marked_as_shortened() -> None:
    """Одно длинное замечание сокращается, но с многоточием, а не в никуда."""
    text = _verification_caution(VERDICT_FAILED, ["ц" * 400])
    assert "…" in text, "обрез снова молчаливый"


def test_the_first_issue_is_never_dropped() -> None:
    """Даже если оно одно и длиннее лимита — показать надо.

    Проверяется граничный случай счётчика: `budget` уходит в минус на первом же
    замечании, и наивная проверка «влезает ли» выбросила бы ВСЕ.
    """
    text = _verification_caution(VERDICT_FAILED, ["я" * 900, "второе"])
    assert text.count("•") >= 1
    assert "перепроверьте факты" in text


def test_a_passing_verdict_says_nothing() -> None:
    assert _verification_caution(VERDICT_PASSED, []) == ""


def test_an_unknown_verdict_warns_without_diagnostics() -> None:
    text = _verification_caution(VERDICT_UNKNOWN, ["verifier unavailable"])
    assert "осторожно" in text
    assert "verifier" not in text, "внутренняя диагностика ушла человеку"
