"""Бытовая реплика не вытягивает личные документы, а короткий запрос — работает.

Найдено владельцем 2026-08-03 на живой переписке с новым человеком: короткая
бытовая фраза в три слова получила ответ режима `personal_knowledge`,
построенный на документе его архива. Человек говорил о своём, а Пятница отвечала
ему выдержкой из служебных бумаг.

Механизм: поиск идёт на КАЖДЫЙ ход, кроме явной болтовни, и на корпусе в полторы
тысячи объектов находит что-нибудь почти всегда. У короткой реплики счёт
совпадения ВЫШЕ, чем у настоящего вопроса (0.83 против 0.26) — короткое слово
совпадает с документами целиком.

Порогом это не лечится, и это ЗАМЕРЕНО: у бытового «чай или кофе» уверенность
0.743 — ровно как у настоящего архивного вопроса. Разделяет только смысл,
поэтому у арбитра появился вид «быт», и он отбрасывает документы независимо от
счёта.

Вторая половина той же работы — короткий запрос без вежливости. Владелец:
«некоторые будут её использовать как тупой поисковик». Переспрос по длине
превращал «курс доллара» и «цена 5090» во встречный вопрос: человек напечатал
ровно то, что искал, и услышал «уточните».

Замеры до/после:
    бытовое чисто        13/14 -> 14/14  (архив в быт больше не лезет)
    короткие запросы      9/17 -> 14/17  (сеть, архив и человек отвечают сразу)
"""

from __future__ import annotations

import inspect

import pytest

from friday.agent_runtime import _ARCHIVE_IS_SURE, AgentRuntime, _archive_is_weak


def test_the_arbiter_knows_what_household_talk_is() -> None:
    """Вида, которого нет в промпте, модель не вернёт."""
    source = inspect.getsource(AgentRuntime._web_query_by_arbiter)
    assert "|быт|" in source, "вид «быт» пропал из перечня"
    assert "про обычную жизнь" in source
    assert "не нужны ни документы, ни поиск" in source, "не сказано, что делать с бытом"


def test_household_drops_documents_regardless_of_score() -> None:
    """Мутация: убрать `household` из условия — «чай или кофе» снова из архива.

    Порог тут бессилен: замерено 0.743 у бытовой фразы против такой же
    уверенности у настоящего архивного вопроса.
    """
    source = inspect.getsource(AgentRuntime._prepare_context)
    at = source.index("household = ")
    guard = source[at : at + 900]
    assert "household or _archive_is_weak" in guard, "быт снова решается порогом"
    assert "context.knowledge_hits = []" in guard


def test_a_direct_archive_question_keeps_its_documents() -> None:
    """Обратная сторона: «покажи штатное расписание» обязано отвечать по архиву.

    Правка, которая глушит архив целиком, формально решает задачу и ломает
    систему.
    """
    source = inspect.getsource(AgentRuntime._prepare_context)
    at = source.index("about_own_archive = ")
    guard = source[at : at + 400]
    for kind in ("архив", "человек", "файл"):
        assert f'"{kind}"' in guard, f"вид «{kind}» перестал защищать документы"


def test_a_personal_cue_still_wins() -> None:
    """«В моей базе», «у меня» — человек прямо сказал, что спрашивает о своём."""
    source = inspect.getsource(AgentRuntime._prepare_context)
    at = source.index("household = ")
    assert "not personal_cue" in source[at : at + 700]


def test_the_measured_threshold_is_still_the_one_used() -> None:
    """Граница взята из замера, а не из головы: 0.719/0.953 у своих тем, 0.028 у чужой."""
    assert _ARCHIVE_IS_SURE == 0.5
    assert _archive_is_weak([{"_rerank_score": 0.3}]) is True
    assert _archive_is_weak([{"_rerank_score": 0.7}]) is False
    # Нет оценки вовсе — переранжировщик выключен: прежнее правило, архив силён.
    assert _archive_is_weak([{"_score": 0.9}]) is False


def test_the_follow_up_question_is_left_only_for_the_unclear() -> None:
    """Мутация: вернуть переспрос для понятых видов — «курс доллара» снова уточняют."""
    source = inspect.getsource(AgentRuntime._prepare_context)
    at = source.index("context.terse_request = False")
    guard = source[max(0, at - 500) : at]
    assert 'startswith("друг")' in guard, "переспрос снова шире, чем «не понял»"


def test_the_web_prefetch_follows_the_verdict_not_the_question_mark() -> None:
    """Короткий запрос без знака вопроса тоже уходит в сеть.

    Замерено: «курс доллара» и «цена 5090» до предварительного поиска не
    доходили вовсе, и решение оставалось за моделью — то есть срабатывало
    примерно раз из шести.
    """
    source = inspect.getsource(AgentRuntime._prefetch_the_web_if_asked)
    assert "looks_like_a_request" in source, "форма вопроса снова решает за понимание"
    at = source.index("looks_like_a_request = ")
    expression = source[at : source.index("\n        if not asked_outright", at)]
    # Именно ИЛИ: с «и» вердикт перестаёт быть самостоятельным основанием, и
    # короткий запрос без знака вопроса снова не доходит до поиска. Первая
    # редакция теста проверяла лишь наличие имени и мутацию «or → and» не ловила.
    assert " or str(" in expression, "вердикт перестал быть самостоятельным основанием"
    assert 'startswith("интернет")' in expression


@pytest.mark.parametrize("kind", ["архив", "человек", "файл", "действие", "быт", "интернет"])
def test_every_understood_kind_skips_the_follow_up(kind: str) -> None:
    """Понятый вид — значит переспрашивать не о чем, каким бы коротким ни был ввод."""
    assert not kind.startswith("друг")
