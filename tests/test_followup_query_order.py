"""Уточняющий вопрос не должен терять собственные слова.

`_contextualize_query` приклеивает предыдущую реплику пользователя к короткому
follow-up — «а когда?» без контекста не ищется ни по чему. Но контекст ставился
ПЕРЕД вопросом, а `_fts_terms` тратит бюджет в порядке текста, и при длинном
предыдущем вопросе все двенадцать мест уходили на него.

Замерено: 35 содержательных токенов на входе, 13 термов доходят до FTS, и **ни один
не принадлежит тому, что человек только что спросил**.

Тот же порядок чинит вторую обрезку, уже отмеченную в `_is_mineable_eval_query`:
сохранённый запрос режется по 500 символов, и отваливался именно follow-up.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime import AgentRuntime
from friday.storage._knowledge import _FTS_TERM_BUDGET, _fts_terms

LONG_PREVIOUS = (
    "подскажи пожалуйста как именно в нашей организации правильно оформляется "
    "передача имущества между подразделениями при смене материально ответственного "
    "лица и какие документы для этого нужны собрать заранее"
)


def _contextualized(question: str) -> str:
    return AgentRuntime._contextualize_query(  # noqa: SLF001
        question, [{"role": "user", "content": LONG_PREVIOUS}]
    )


@pytest.mark.parametrize(
    "question,expected",
    [
        ("а когда дежурство?", {"когда", "дежурство"}),
        ("почему по накладной расхождение?", {"накладной", "расхождение"}),
        ("сколько экземпляров описи?", {"сколько", "экземпляров", "описи"}),
    ],
)
def test_the_words_the_person_just_typed_reach_the_index(question, expected):
    terms = set(_fts_terms(_contextualized(question)))
    assert expected <= terms, f"потеряно: {sorted(expected - terms)}"


def test_the_context_still_contributes_what_fits():
    """Контекст не выброшен — он просто уступает очередь."""
    terms = _fts_terms(_contextualized("а когда дежурство?"))
    from_context = [term for term in terms if term in LONG_PREVIOUS]
    assert from_context, "контекст исчез полностью"
    assert len(terms) <= _FTS_TERM_BUDGET


def test_the_question_comes_first_in_the_stored_query():
    """Сохранённый запрос режется по 500 символов; вопрос обязан пережить обрезку."""
    joined = _contextualized("а когда дежурство?")
    assert joined.startswith("а когда дежурство?")
    assert "\n" in joined, "перенос строки — то, по чему follow-up отличают от обычного запроса"
    assert joined[:500].startswith("а когда дежурство?")


def test_a_question_that_is_not_a_followup_is_left_alone():
    # Не начинается с вопросительного слова из списка: «как…» до 90 символов
    # считается уточнением и контекст получает — это отдельное решение,
    # существовавшее до этой правки.
    plain = "оформление передачи имущества между подразделениями"
    assert AgentRuntime._contextualize_query(plain, [{"role": "user", "content": LONG_PREVIOUS}]) == plain  # noqa: SLF001


def test_no_label_is_added_because_a_label_costs_a_term():
    """Любое слово-разделитель само становится термом и тратит бюджет, который защищает."""
    joined = _contextualized("а когда дежурство?")
    assert "follow" not in joined.casefold()
    assert "контекст" not in joined.casefold()


def test_a_long_question_keeps_the_word_that_names_the_answer():
    """Бюджет поднят с 12 до 24, и вот случай, ради которого.

    Шестнадцать общих слов плюс редкий терм последним: при двенадцати он не доходил
    до индекса вовсе. Замер на 342 настоящих документах — цель в топ-10 9 раз из 60
    против 12 из 60. Выигрыш маленький и записан таким; потолок 12/60 даже при
    неограниченном бюджете говорит, что главная беда не здесь, а в том, что
    шестнадцать общих слов топят одно точное в bm25.
    """
    question = (
        "подскажи пожалуйста как именно в нашей организации правильно оформить этот "
        "вопрос сегодня утром через канцелярию сотрудникам подразделения согласно "
        "установленному регламенту делопроизводства инвентаризационный"
    )
    assert "инвентаризационный" in _fts_terms(question)


def test_the_budget_is_still_a_ceiling():
    """Поднят — не снят: неограниченный список термов это другой дефект."""
    question = " ".join(f"слово{index}" for index in range(200))
    assert len(_fts_terms(question)) <= _FTS_TERM_BUDGET


def test_a_possessive_followup_gets_the_previous_subject():
    """«а его брат?» после вопроса о человеке — самый частый хвост разговора.

    Притяжательных не было в списке признаков: вопрос уходил в поиск без
    контекста предыдущего хода и находил чужое или ничего.
    """
    for question in ("а его брат?", "её телефон?", "а у него какое звание?"):
        joined = AgentRuntime._contextualize_query(  # noqa: SLF001
            question, [{"role": "user", "content": LONG_PREVIOUS}]
        )
        assert "\n" in joined, f"{question!r} не распознан как продолжение"
        assert joined.startswith(question)


def test_a_short_a_noun_question_is_a_followup_only_with_a_question_mark():
    """«а брат?» — продолжение; «а Иван пришёл» — утверждение, и склеивать его
    с прошлым ходом нельзя: вопросительный знак и есть различие."""
    history = [{"role": "user", "content": LONG_PREVIOUS}]
    followed = AgentRuntime._contextualize_query("а брат?", history)  # noqa: SLF001
    assert "\n" in followed

    statement = AgentRuntime._contextualize_query("а Иван пришёл", history)  # noqa: SLF001
    assert statement == "а Иван пришёл"
