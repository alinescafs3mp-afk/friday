"""Вопрос о внешнем мире не тонет в архиве только потому, что архив что-то нашёл.

Замерено на живой переписке 2026-08-02. Владелец спросил «Подскажи пожалуйста
характеристики 5090» и получил через 90 секунд ответ, начинающийся словами «По
запросу «характеристики 5090» в базе знаний…» — пересказ случайных документов.
Видеокарты в личном архиве нет и быть не может.

Виновато правило «свой архив вперёд чужого интернета». Оно смотрело на САМ ФАКТ
наличия совпадений, а поиск по корпусу в полторы тысячи объектов находит
что-нибудь почти на любой вопрос. Интернет оказывался закрыт навсегда.

Это третье за двое суток попадание одной и той же ошибки: соседство, совпадение и
наличие — не улики. Улика здесь — вердикт о ТЕМЕ вопроса, и его даёт арбитр.

Вторая половина правки — про время. Вердикт нужен раньше правила, а
последовательный вызов добавил бы свои секунды каждому вопросу; поэтому арбитр
считается ПАРАЛЛЕЛЬНО поиску по архиву и прячется за ним целиком.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime


def _context(**overrides) -> AgentContext:
    context = AgentContext(conversation_id="conv_test", user_id="boss")
    for key, value in overrides.items():
        setattr(context, key, value)
    return context


#: Сильное совпадение: свои документы по теме. Замерено на живом корпусе —
#: «что там по поверке приборов» даёт верх 0.719, «мои документы по подготовке»
#: 0.953.
HITS = [{"id": "ko_1", "title": "Приказ 214", "summary": "о порядке доступа", "_rerank_score": 0.72}]
#: Слабое: поиск что-то принёс, но мимо. «Что известно про приказ 214» на этом же
#: корпусе даёт 0.028 — приказа там нет, и уход в сеть верен.
WEAK_HITS = [{"id": "ko_2", "title": "Наставление", "summary": "не о том", "_rerank_score": 0.03}]


class _Result:
    success = True
    data: dict = {}

    def to_llm_message(self) -> str:
        return "Выдача: RTX 5090, 32 ГБ GDDR7."


class _Kernel:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def execute(self, tool: str, params: dict, actor=None):
        self.queries.append(str(params.get("query") or ""))
        return _Result()


class _Runtime:
    """Только та часть, что принимает решение: звать ли интернет."""

    def __init__(self, verdict=None):
        self.arbiter_calls = 0
        self._verdict = verdict
        self.kernel = _Kernel()

    async def _web_query_by_arbiter(self, message: str):
        self.arbiter_calls += 1
        return self._verdict or ("другое", None)

    async def _mentions_someone_from_the_archive(self, message, actor):
        return False

    def web_query_from(self, message: str) -> str:
        return message[:60]


def _decide(runtime, message: str, context, notice=None, messages=None):
    """Зовём НАСТОЯЩИЙ метод боевого хода, а не его пересказ."""
    notice = notice if notice is not None else []
    messages = messages if messages is not None else []
    bound = AgentRuntime._prefetch_the_web_if_asked.__get__(runtime, AgentRuntime)
    asyncio.run(
        bound(
            message,
            None,
            [{"function": {"name": "web_research"}}],
            messages,
            [],
            [],
            notice=notice,
            context=context,
        )
    )
    return runtime.kernel.queries


def test_the_signature_is_the_one_the_turn_uses() -> None:
    """Страховка от переименования: тест зовёт метод боевого хода."""
    params = set(inspect.signature(AgentRuntime._prefetch_the_web_if_asked).parameters)
    assert {"message", "actor", "tools", "messages", "notice", "context"} <= params


def test_a_question_about_the_world_still_goes_out_when_the_archive_matched() -> None:
    """Мутация: вернуть проверку только на `knowledge_hits` — тест краснеет."""
    runtime = _Runtime(verdict=("интернет", "характеристики RTX 5090"))
    context = _context(knowledge_hits=WEAK_HITS, outward_verdict=("интернет", "характеристики RTX 5090"))

    queries = _decide(runtime, "Подскажи пожалуйста характеристики 5090", context)

    assert queries == ["характеристики RTX 5090"], (
        "вопрос о видеокарте снова разобран по личному архиву вместо интернета"
    )


def test_a_strong_archive_match_beats_the_internet_verdict() -> None:
    """Обратная сторона той же границы, найденная недельным прогоном.

    «Подскажи, что там по поверке приборов» ушло в интернет и вернулось рассказом
    про счётчики воды за 34 секунды — при том что нужные документы лежат в архиве
    и находятся уверенно (верх 0.719). Вердикт «интернет» перебивает архив только
    там, где архив ответил случайно.
    """
    runtime = _Runtime(verdict=("интернет", "сроки поверки приборов учёта"))
    context = _context(knowledge_hits=HITS, outward_verdict=("интернет", "сроки поверки приборов учёта"))

    queries = _decide(runtime, "Подскажи, что там по поверке приборов", context)

    assert queries == [], "своя тема с уверенным совпадением ушла в поисковик"


def test_without_a_rerank_score_the_archive_keeps_priority() -> None:
    """Отказ переранжировщика не должен открывать дорогу наружу."""
    runtime = _Runtime(verdict=("интернет", "что угодно"))
    unscored = [{"id": "ko_3", "title": "Документ", "summary": "без оценки"}]
    context = _context(knowledge_hits=unscored, outward_verdict=("интернет", "что угодно"))

    assert _decide(runtime, "что там по поверке", context) == []


def test_a_question_about_my_own_papers_still_prefers_the_archive() -> None:
    """Защита, ради которой правило и вводилось, остаётся на месте.

    Замерено раньше: «что известно про приказ 214?» уходило в поисковик и
    возвращалось рассказом о разных нормативных актах с таким номером за 36 с,
    хотя нужный приказ лежал в базе.
    """
    runtime = _Runtime(verdict=("архив", None))
    context = _context(knowledge_hits=HITS, outward_verdict=("архив", None))

    queries = _decide(runtime, "что известно про приказ 214?", context)

    assert queries == [], "вопрос о своих документах ушёл в поисковик"


def test_a_settled_fact_is_answered_from_memory_even_with_archive_hits() -> None:
    """«Знание» тоже перебивает архив: иначе устоявшийся факт тонет так же."""
    runtime = _Runtime(verdict=("знание", None))
    context = _context(knowledge_hits=WEAK_HITS, outward_verdict=("знание", None))
    notice: list[str] = []

    queries = _decide(runtime, "чем отличается лизинг от аренды?", context, notice=notice)

    assert queries == [], "за объяснением всё же пошли в интернет"
    assert any("собственных знаний" in item for item in notice), notice


def test_a_strong_archive_match_beats_the_knowledge_verdict_too() -> None:
    """Свои документы важнее общего объяснения.

    Если человек спрашивает о том, что у него лежит в архиве, отвечать общими
    словами из головы модели — значит не заметить его собственный материал.
    """
    runtime = _Runtime(verdict=("знание", None))
    context = _context(knowledge_hits=HITS, outward_verdict=("знание", None))
    notice: list[str] = []

    _decide(runtime, "какой у нас порядок поверки?", context, notice=notice)

    assert notice == [], "уверенное совпадение в архиве подменили рассказом по памяти"


def test_the_ready_verdict_is_not_recomputed() -> None:
    """Вердикт посчитан параллельно поиску — второй раз модель не спрашиваем.

    Цена мутации — лишний обход к модели на каждом вопросе, ровно та задержка,
    ради устранения которой вердикт и считается заранее.
    """
    runtime = _Runtime(verdict=("интернет", "курс евро"))
    context = _context(knowledge_hits=[], outward_verdict=("интернет", "курс евро"))

    _decide(runtime, "какой сейчас курс евро?", context)

    assert runtime.arbiter_calls == 0, "арбитра спросили повторно — это удвоенное ожидание"


def test_without_a_ready_verdict_the_arbiter_is_still_asked() -> None:
    """Путь без параллельного расчёта не сломан: там вердикт берётся на месте."""
    runtime = _Runtime(verdict=("интернет", "курс евро"))
    context = _context(knowledge_hits=[], outward_verdict=None)

    _decide(runtime, "какой сейчас курс евро?", context)

    assert runtime.arbiter_calls == 1


def test_the_arbiter_runs_next_to_the_search_not_after_it() -> None:
    """Проверяется подключённое: задача арбитра создаётся до ожидания поиска."""
    source = inspect.getsource(AgentRuntime._prepare_context)
    # Проверка по смыслу, а не по одной строке: вызов законно переносится на
    # несколько строк, когда у арбитра появляются аргументы.
    assert "asyncio.create_task(" in source and "_web_query_by_arbiter" in source, (
        "арбитр снова считается последовательно — его секунды прибавятся к ответу"
    )
    created = source.index("asyncio.create_task(")
    awaited = source.index("await arbiter")
    searched = source.index("await searcher.search(")
    assert created < searched < awaited, "поиск и арбитр перестали идти одновременно"


@pytest.mark.parametrize(
    "message",
    [
        "Подскажи пожалуйста характеристики 5090",
        "Расскажи что-нибудь познавательное",
        "посмотри что там по договору аренды",
        "сравни две сметы",
        "проверь остаток по счёту",
    ],
)
def test_a_plain_request_counts_as_a_question(message: str) -> None:
    """Мутация: убрать ветку с глаголами обращения — тест краснеет.

    Прежняя редакция допускала «подскажи» только приставкой ПЕРЕД вопросительным
    словом. «Подскажи пожалуйста характеристики 5090» вопросом не считалось
    вовсе, и мысль об интернете не возникала — при том что это самая обычная
    форма просьбы у человека, который диктует голосом.
    """
    from friday.agent_runtime import _might_be_a_question

    assert _might_be_a_question(message), f"просьба не распознана как вопрос: {message!r}"


@pytest.mark.parametrize("message", ["привет", "Спасибо!", "Проверка связи", "ок, понял"])
def test_chatter_is_still_not_a_question(message: str) -> None:
    """Расширение не должно превратить болтовню в запрос — это чинили отдельно."""
    from friday.agent_runtime import _might_be_a_question

    assert not _might_be_a_question(message), f"реплика разговора принята за вопрос: {message!r}"


@pytest.mark.parametrize("verdict", [("материал", None), ("другое", None)])
def test_other_verdicts_keep_the_archive_first(verdict) -> None:
    """Присланный документ и разговор наружу не уходят — как и раньше."""
    runtime = _Runtime(verdict=verdict)
    context = _context(knowledge_hits=HITS, outward_verdict=verdict)

    assert _decide(runtime, "Приказ №214: доступ в интернете ограничить", context) == []
