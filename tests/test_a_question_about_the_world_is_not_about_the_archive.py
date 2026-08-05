"""Вопрос о внешнем мире не отвечается личным архивом, как бы тот ни совпал.

Замерено на живой базе владельца 2026-08-03: из 617 ответов, помеченных режимом
«личные знания», 374 — 61% — не опирались НИ НА ОДНУ запись архива. Режим
назначался напрасно в трёх случаях из пяти, и в этих ходах модель отвечала из
собственной памяти, а подпись говорила человеку, что ответ взят из его
документов. Восемьдесят таких ответов вдобавок несли пометку «проверено».

Разобранный случай из живой переписки: человек спросил об общеизвестной дате.
Архив дал десять совпадений с уверенностью 0.851 — просто потому, что тема
лексически пересекается с его служебными бумагами. Ответа в них нет и быть не
может; модель ответила из головы, дату назвала неверно и сослалась на «базу
знаний». Человек поправил её сам.

Порогом это не лечится, и в этом суть. Уверенность переранжировщика мерит
ПОХОЖЕСТЬ текста на документы, а не отношение вопроса к ним: чем плотнее архив
человека набит его профессией, тем увереннее он совпадёт с любым вопросом из этой
же области. Отношение знает только арбитр — и он уже сказал «интернет» или
«знание», то есть «ответ живёт снаружи».

Тот же принцип, по которому вердикт оказался сильнее шаблона времени.

Проверяется ПОВЕДЕНИЕМ собранного контекста, а не текстом исходника: соседние
тесты этого файла смотрят на текст `_prepare_context`, и такая проверка переживает
любую правку, сохранившую слова.
"""

from __future__ import annotations

import asyncio

import pytest

from friday.agent_runtime import AgentRuntime


class _Searcher:
    """Поиск, который всегда что-то находит — как на настоящем корпусе."""

    def __init__(self, score: float = 0.85) -> None:
        self.score = score
        self.calls = 0

    async def search(self, user_id, query, **kwargs):  # noqa: ANN001, ARG002
        self.calls += 1
        return {
            "results": [
                {
                    "id": f"ko_{index}",
                    "title": f"Служебный документ {index}",
                    "content": "порядок и сроки проведения мероприятий",
                    "_score": self.score,
                    "_rerank_score": self.score,
                }
                for index in range(3)
            ],
            "entity_matches": [],
            "strategy": "hybrid",
            "trace": [],
        }


class _LLM:
    def __init__(self, verdict: str) -> None:
        self.enabled = True
        self.verdict = verdict
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        self.calls += 1
        return {"content": self.verdict}


def _context_for(settings, storage, message: str, verdict: str, *, score: float = 0.85):
    storage.ensure_user("alice")
    agent = AgentRuntime(settings, storage)
    agent.llm = _LLM(verdict)
    searcher = _Searcher(score)
    return asyncio.run(
        agent._prepare_context(
            "alice",
            message,
            "conv",
            prior_history=[],
            searcher=searcher,
        )
    ), searcher


OUTWARD = '{"вид": "интернет", "запрос": "день морской пехоты дата", "кто": "", "дни": []}'
KNOWLEDGE = '{"вид": "знание", "запрос": "", "кто": "", "дни": []}'
ARCHIVE = '{"вид": "архив", "запрос": "", "кто": "", "дни": []}'


def test_a_world_question_drops_documents_however_well_they_match(settings, storage) -> None:
    """Мутация: убрать `looks_outward` из условия — документы возвращаются.

    Счёт 0.85 намеренно высок: он выше замеренной границы 0.5, то есть прежнее
    правило («архив отвечает уверенно — значит вопрос про архив») здесь сработало
    бы и назначило режим личных знаний.
    """
    context, searcher = _context_for(settings, storage, "когда день морской пехоты", OUTWARD, score=0.85)

    assert searcher.calls == 1, "поиск не отработал — проверка ничего не значит"
    assert context.knowledge_hits == [], "документы уехали в ответ на вопрос о внешнем мире"
    assert context.answer_mode != "personal_knowledge"
    assert context.retrieval_confidence == 0.0


def test_a_general_knowledge_question_too(settings, storage) -> None:
    """«Чем отличается лизинг от аренды» — тоже не про его документы."""
    context, _ = _context_for(settings, storage, "чем отличается лизинг от аренды", KNOWLEDGE, score=0.9)

    assert context.knowledge_hits == []
    assert context.answer_mode != "personal_knowledge"


def test_an_archive_question_keeps_its_documents(settings, storage) -> None:
    """Обратная сторона. Без неё правка ломает то, ради чего система существует."""
    context, _ = _context_for(settings, storage, "что там по поверке приборов", ARCHIVE, score=0.85)

    assert context.knowledge_hits, "прямой вопрос к архиву остался без документов"
    assert context.answer_mode == "personal_knowledge"


def test_my_own_stuff_survives_an_outward_verdict(settings, storage) -> None:
    """«Что там по МОИМ приборам» — слова человека сильнее вердикта.

    Арбитр может ошибиться и назвать это вопросом наружу; прямое указание на свои
    материалы должно спасать документы. Иначе правка кренит систему в другую
    сторону — ошибка, за которой на этом проекте уже приходилось возвращаться.
    """
    context, _ = _context_for(settings, storage, "что там по моим приборам", OUTWARD, score=0.85)

    assert context.knowledge_hits, "свои материалы выброшены по ошибке арбитра"


def test_the_arbiter_is_told_about_the_status_question_form() -> None:
    """«Что там по поверке приборов» — вопрос о СВОЁМ деле, а не о мире.

    Проверяется содержимое промпта, и это тот случай, когда осмотр исходника
    уместен: промпт и ЕСТЬ изделие, поведение модели проверяется живым прогоном.

    Замерено 2026-08-03 до правки: эта форма уходила в интернет два раза из трёх,
    «как там с поверкой» — три из трёх. Притяжательного в такой фразе нет вовсе,
    и арбитр читал её как вопрос о мире. Правка перевела 5 архивных форм из 5 в
    «архив», не тронув 6 сетевых из 6.

    Соседняя пара форм обязана остаться снаружи, поэтому в промпте она названа
    прямо: без этого правило перетянуло бы «что нового в мире» в архив.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._web_query_by_arbiter)
    assert "что там по…" in source, "форма вопроса о своём деле пропала из промпта"
    assert "как там с…" in source
    assert "состоянии своего дела" in source, "не объяснено, ПОЧЕМУ это архив"
    assert "что нового в мире" in source, "не отделено от вопроса о внешнем мире"


@pytest.mark.parametrize("verdict", [OUTWARD, KNOWLEDGE])
def test_the_weak_match_rule_still_applies_underneath(settings, storage, verdict: str) -> None:
    """Прежнее правило никуда не делось: слабое совпадение отбрасывается и так."""
    context, _ = _context_for(settings, storage, "что-нибудь расскажи", verdict, score=0.1)

    assert context.knowledge_hits == []
