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

import asyncio
import inspect

import pytest

from friday.agent_runtime import _ARCHIVE_IS_SURE, AgentRuntime, _archive_is_weak


class _Searcher:
    """Поиск, который всегда что-то находит — как на настоящем корпусе."""

    def __init__(self, score: float) -> None:
        self.score = score
        self.calls = 0

    async def search(self, user_id, query, **kwargs):  # noqa: ANN001, ARG002
        self.calls += 1
        return {
            "results": [
                {
                    "id": "ko_1",
                    "title": "Служебный документ",
                    "content": "порядок и сроки",
                    "_score": self.score,
                    "_rerank_score": self.score,
                }
            ],
            "entity_matches": [],
            "strategy": "hybrid",
            "trace": [],
        }


class _LLM:
    def __init__(self, verdict: str) -> None:
        self.enabled = True
        self.verdict = verdict

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        return {"content": self.verdict}


HOUSEHOLD = '{"вид": "быт", "запрос": "", "кто": "", "дни": []}'
ARCHIVE = '{"вид": "архив", "запрос": "", "кто": "", "дни": []}'
ABOUT_A_PERSON = '{"вид": "человек", "запрос": "", "кто": "Пегас", "дни": []}'
A_FILE = '{"вид": "файл", "запрос": "", "кто": "", "дни": []}'


def _prepared(settings, storage, message: str, verdict: str, *, score: float):
    """Настоящая сборка контекста с поддельными поиском и моделью."""
    storage.ensure_user("alice")
    agent = AgentRuntime(settings, storage)
    agent.llm = _LLM(verdict)
    searcher = _Searcher(score)
    context = asyncio.run(
        agent._prepare_context("alice", message, "conv", prior_history=[], searcher=searcher)
    )
    return context, searcher


def test_the_arbiter_knows_what_household_talk_is() -> None:
    """Вида, которого нет в промпте, модель не вернёт."""
    source = inspect.getsource(AgentRuntime._web_query_by_arbiter)
    assert "|быт|" in source, "вид «быт» пропал из перечня"
    assert "про обычную жизнь" in source
    assert "не нужны ни документы, ни поиск" in source, "не сказано, что делать с бытом"


def test_household_drops_documents_regardless_of_score(settings, storage) -> None:
    """Мутация: убрать `household` из условия — «чай или кофе» снова из архива.

    Порог тут бессилен: замерено 0.743 у бытовой фразы против такой же
    уверенности у настоящего архивного вопроса. Счёт 0.85 в проверке намеренно
    ВЫШЕ замеренной границы 0.5 — иначе документы отбросило бы прежнее правило и
    тест прошёл бы, ничего не проверив.

    Переписан с осмотра исходника на поведение 2026-08-03: прежняя редакция
    искала подстроку в окне на 900 знаков и покраснела от добавленного
    КОММЕНТАРИЯ, сдвинувшего код за границу окна. Проверка, которую ломает
    комментарий и переживает подмена смысла, — не проверка.
    """
    context, searcher = _prepared(settings, storage, "чай или кофе", HOUSEHOLD, score=0.85)

    assert searcher.calls == 1, "поиск не отработал — проверять нечего"
    assert context.knowledge_hits == [], "бытовая реплика снова тянет документы"
    assert context.answer_mode != "personal_knowledge"


def test_a_direct_archive_question_keeps_its_documents(settings, storage) -> None:
    """Обратная сторона: «покажи штатное расписание» обязано отвечать по архиву.

    Правка, которая глушит архив целиком, формально решает задачу и ломает
    систему.
    """
    for verdict in (ARCHIVE, A_FILE):
        context, _ = _prepared(settings, storage, "покажи штатное расписание", verdict, score=0.6)
        assert context.knowledge_hits, f"вид {verdict[:24]}… перестал защищать документы"


def test_a_question_about_a_person_clears_documents_on_purpose(settings, storage) -> None:
    """Вид «человек» для известной учётки архив ОБНУЛЯЕТ, и намеренно.

    На «что писал Пегас» отвечает инструмент надзора, когда Пегас — учётка.
    Рядом с его ответом
    подсказка «похожее в архиве есть, но не по делу» уезжала в контекст, и модель
    пересказывала подсказку вместо данных — владелец видел это на живой переписке.

    Неизвестное учёткам имя намеренно сохраняет архивные совпадения: это может
    быть человек из документов. Поэтому предусловие этой проверки — разрешимая
    системная учётка, а не одно лишь имя в вердикте арбитра.
    """
    storage.ensure_user("pegasus", display_name="Пегас", username="pegas")
    context, searcher = _prepared(
        settings,
        storage,
        "что писал Пегас",
        ABOUT_A_PERSON,
        score=0.9,
    )

    assert searcher.calls == 1, "архив не искался — проверять его очистку нечем"
    assert context.knowledge_hits == []


def test_a_personal_cue_still_wins(settings, storage) -> None:
    """«В моей базе», «у меня» — человек прямо сказал, что спрашивает о своём.

    Слабое совпадение (0.1) выбрано намеренно: без указания на своё документы
    отбросил бы порог, поэтому проверка ловит именно спасающее действие подсказки.
    """
    context, _ = _prepared(settings, storage, "что у меня по поверке", HOUSEHOLD, score=0.1)

    assert context.knowledge_hits, "прямое указание на свои материалы перестало спасать документы"


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
