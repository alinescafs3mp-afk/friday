"""Вопрос надо понять, а не сверить со списком заготовленных фраз.

Требование владельца дословно: «Пятница должна осознавать вопрос, а не только
реагировать на заранее заготовленные фразы. Другая формулировка или опечатка не
должны быть для неё препятствием».

Замерено до правки на двадцати двух живых формулировках: узнавалось пятнадцать.
Мимо шли не экзотические конструкции, а обычные — «пагугли», «сходи в интернет и
узнай», «что пишут в интернете про…», «поиск в интернете: …».

Здесь проверяются обе половины решения:

- шаблон как быстрый путь — он расширен и обязан по-прежнему молчать на
  присланном документе (иначе приказ уходит поисковой строкой наружу);
- арбитр как понимание — он вызывается там, где шаблон промолчал, и его вердикт
  ограничен по длине, чтобы цена ошибки оставалась низкой.
"""

from __future__ import annotations

import asyncio

import pytest

from friday.agent_runtime import (
    _ASKS_FOR_THE_WEB,
    AgentRuntime,
    _might_be_a_question,
)


@pytest.mark.parametrize(
    "message",
    [
        "погугли что такое CRSF протокол",
        "пагугли что за зверь SQLite WAL",  # опечатка в приставке
        "найди в интренете доклад про Rust",  # перестановка букв
        "а можешь глянуть в интернет, что там с курсом евро?",
        "сходи в интернет и узнай, когда вышел Django 5",
        "что пишут в интернете про Claude Code",
        "поиск в интернете: как работает RAFT",
        "давайте посмотрим в сети последние новости про Python 3.14",
    ],
)
def test_a_request_survives_rephrasing_and_typos(message):
    assert _ASKS_FOR_THE_WEB.search(message), "живая формулировка просьбы не узнана"


@pytest.mark.parametrize(
    "message",
    [
        # Тот самый текст, который уходил целиком в Яндекс, Brave и DuckDuckGo.
        "Приказ №214: с 1 августа доступ в интернете к порталу ограничить, "
        "ответственный Проскурин В.А.",
        "в интернете полно ерунды, запиши это",
        "запиши в блокнот: интернет отключат в среду",
        "сохрани заметку про интернет-магазин",
        "в гугле я уже смотрел, не нашёл",
        "Пятница, доступ в интернет для отдела кадров закрыт",
        "найди приказ 214 в базе",
    ],
)
def test_a_document_is_never_mistaken_for_a_request(message):
    """Мутация: убрать `\\b` перед списком глаголов — «запиши» станет просьбой.

    Расширение шаблона обязано не расширять утечку. «Запиши это» содержит «пиши»,
    и без границы слова сообщение на сохранение объявлялось командой поискать.
    """
    assert not _ASKS_FOR_THE_WEB.search(message), "присланный материал принят за просьбу поискать"


@pytest.mark.parametrize(
    "message,expected",
    [
        ("какая завтра погода в Питере?", True),
        ("кто такой Линус Торвальдс", True),
        ("сколько стоит билет на поезд Москва — Сочи?", True),
        # Документ до арбитра доходить не должен вовсе: длинный и не вопрос.
        (
            "Приказ №214: с 1 августа доступ в интернете к порталу ограничить, "
            "ответственный Проскурин В.А. Контроль исполнения возложить на "
            "заместителя начальника управления. Срок — до 15 августа 2026 года, "
            "о результатах доложить установленным порядком в письменном виде.",
            False,
        ),
        ("Совещание перенесено на четверг, всем участникам подтвердить участие", False),
    ],
)
def test_only_something_that_looks_like_a_question_reaches_the_arbiter(message, expected):
    """Дешёвый предфильтр: лишний вызов модели на каждое сообщение не нужен."""
    assert _might_be_a_question(message) is expected


class _FakeLLM:
    """Модель, отвечающая заранее заданной строкой."""

    enabled = True

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[list[dict[str, str]]] = []

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        self.calls.append(messages)
        return {"content": self.content}


def _runtime_with(content: str) -> tuple[AgentRuntime, _FakeLLM]:
    from friday.config import load_settings

    runtime = AgentRuntime.__new__(AgentRuntime)
    llm = _FakeLLM(content)
    runtime.llm = llm
    # Настройки нужны по-настоящему: арбитру передаётся сегодняшняя дата (иначе
    # он составляет запросы с годом из своего обучения — замерено на недельном
    # прогоне, «drones ... 2024» в 2026 году).
    runtime.settings = load_settings()
    return runtime, llm


def test_the_arbiter_returns_a_query_only_for_the_outside_world():
    runtime, _ = _runtime_with('{"вид": "интернет", "запрос": "курс биткоина сегодня"}')
    # Возвращается пара «вид, запрос»: вид нужен, чтобы отличить «ответ надо
    # искать» от «ответ модель знает сама».
    assert asyncio.run(runtime._web_query_by_arbiter("а что там по биткоину?"))[1] == (  # noqa: SLF001
        "курс биткоина сегодня"
    )

    for verdict in ("архив", "материал", "другое"):
        runtime, _ = _runtime_with(f'{{"вид": "{verdict}", "запрос": "приказ 214"}}')
        assert asyncio.run(runtime._web_query_by_arbiter("найди приказ 214"))[1] is None  # noqa: SLF001


def test_the_query_is_capped_so_a_mistake_stays_cheap():
    """Мутация: снять потолок — тест краснеет.

    Потолок здесь не косметика. Если арбитр ошибётся и примет присланный документ
    за вопрос, наружу уйдёт десяток слов, а не весь текст: в аудите остаётся
    только хеш запроса, и восстановить «что именно ушло» иначе нельзя.
    """
    long_query = " ".join(f"слово{i}" for i in range(40))
    runtime, _ = _runtime_with(f'{{"вид": "интернет", "запрос": "{long_query}"}}')
    _, query = asyncio.run(runtime._web_query_by_arbiter("что это?"))  # noqa: SLF001
    assert query is not None
    assert len(query.split()) <= 14
    assert len(query) <= 140


def test_a_broken_verdict_does_not_send_anything_anywhere():
    for content in ("", "не знаю", "{сломанный json", '{"вид": "интернет", "запрос": ""}'):
        runtime, _ = _runtime_with(content)
        assert asyncio.run(runtime._web_query_by_arbiter("что это?"))[1] is None  # noqa: SLF001


def test_the_prefetch_actually_asks_the_arbiter():
    """Мутация: убрать вызов арбитра из предзапроса — тест краснеет.

    Проверяется не помощник, а то, что его ЗОВУТ: работающий арбитр, к которому
    никто не обращается, не отвечает ни на одну живую формулировку.
    """
    import inspect

    source = inspect.getsource(AgentRuntime._prefetch_the_web_if_asked)  # noqa: SLF001
    assert "_web_query_by_arbiter(message)" in source, (
        "предварительный поиск не спрашивает арбитра — понимания не будет"
    )
    assert "_might_be_a_question(message)" in source, "предфильтр не подключён"
    # Явная просьба остаётся исполненной, даже если арбитр её не признал.
    assert "web_query_from(message)" in source, (
        "при отказе арбитра прямая просьба человека теряется"
    )


def test_a_question_about_the_timeline_never_goes_to_a_search_engine():
    """Мутация: убрать структурную защиту — вопрос про 26 июля уйдёт в поисковик.

    Замерено на живой модели: на «что было 26 июля в 15 часов» арбитр отвечал
    «интернет» и предлагал запрос «что произошло 26 июля в 15:00». Это вопрос о
    собственной ленте, и время в нём названо прямо — здесь решает структура
    вопроса, а не модель.
    """
    import inspect

    source = inspect.getsource(AgentRuntime._prefetch_the_web_if_asked)  # noqa: SLF001
    guard = source[: source.index("_web_query_by_arbiter(message)")]
    assert "_ASKS_WHAT_HAPPENED.search(message)" in guard, (
        "вопрос о ленте доходит до арбитра раньше, чем его перехватит структура"
    )
    assert "moment_from_question(message)" in guard


def test_a_failed_search_is_not_passed_off_as_results():
    """Мутация: убрать ветку `not result.success` — тест краснеет.

    Замерено при живом прогоне: когда поиск срывался, в контекст модели уходило
    «Поиск уже выполнен по запросу «…», вот выдача: Ошибка инструмента
    web_research: …» и следом «Отвечай по этой выдаче». То есть модели велели
    отвечать по тексту ошибки — а провайдер может лежать прямо на глазах у
    человека, и честное «не дотянулась» лучше пересказанной диагностики.
    """
    import asyncio
    import inspect

    class _Failed:
        success = False
        attachment = None
        data: dict = {}

        def to_llm_message(self) -> str:
            return "Ошибка инструмента web_research: no provider answered"

    class _Kernel:
        async def execute(self, name, arguments, *, actor):  # noqa: ANN001, ARG002
            return _Failed()

    runtime, _ = _runtime_with('{"вид": "интернет", "запрос": "курс евро"}')
    runtime.kernel = _Kernel()
    messages: list[dict] = []
    asyncio.run(
        AgentRuntime._prefetch_the_web_if_asked(  # noqa: SLF001
            runtime,
            "какой сейчас курс евро?",
            actor=None,
            tools=[{"function": {"name": "web_research"}}],
            messages=messages,
            tools_used=[],
            tool_evidence=[],
        )
    )
    assert messages, "о сорванном поиске модели не сказали вовсе"
    content = messages[0]["content"]
    assert "не удался" in content
    assert "Отвечай по этой выдаче" not in content, "текст ошибки выдан за выдачу поиска"
    # Проверяется СМЫСЛ, а не формулировка. Прежняя редакция требовала слова «не
    # выдумывай» — то есть прибивала к тесту приказ, положенный в поток
    # сообщений. Приказы модель пересказывает человеку дословно, и это отдельный
    # класс дефекта; сказано должно быть то же самое, но фактом: сведений из
    # интернета в этом ходе нет, и всё похожее на найденное найденным не
    # является.
    assert "нет" in content and "найденным не является" in content, (
        f"модели не сказано, что сведений из интернета нет: {content!r}"
    )
    for order in ("скажи человеку", "не выдумывай", "предложи"):
        assert order not in content.casefold(), f"служебная строка написана как приказ: {content!r}"

    source = inspect.getsource(AgentRuntime._prefetch_the_web_if_asked)  # noqa: SLF001
    assert "if not result.success:" in source


def test_a_name_from_the_archive_never_leaves_for_a_search_engine():
    """Мутация: убрать `_mentions_someone_from_the_archive` — тест краснеет.

    Найдено сквозным прогоном на копии живого архива: «что известно про
    Хасанова?» арбитр счёл вопросом о внешнем мире, и фамилия сотрудника ушла
    поисковой строкой в Яндекс. Ответ при этом пришёл ИЗ АРХИВА — то есть поход
    наружу не дал ничего, кроме утечки, а в аудите остаётся только хеш запроса.
    """
    import inspect

    source = inspect.getsource(AgentRuntime._prefetch_the_web_if_asked)  # noqa: SLF001
    guard = source[: source.index("_web_query_by_arbiter(message)")]
    assert "_mentions_someone_from_the_archive(message, actor)" in guard, (
        "имя человека из архива доходит до арбитра и уходит в поисковик"
    )
    assert "not asked_outright" in guard, (
        "прямая просьба «найди в интернете про X» должна остаться исполненной"
    )


def test_the_graph_is_asked_only_about_words_that_could_be_a_name():
    """Служебные слова не должны дёргать граф на каждом вопросе."""
    import asyncio

    asked: list[str] = []

    class _Graph:
        def search_entities(self, user_id, query, *, limit=3):  # noqa: ANN001, ARG002
            asked.append(query)
            return []

    class _Kernel:
        kg = _Graph()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()

    class _Actor:
        user_id = "alice"

    asyncio.run(
        runtime._mentions_someone_from_the_archive(  # noqa: SLF001
            "что известно про Хасанова сегодня?", _Actor()
        )
    )
    assert "Хасанова" in asked
    for stop_word in ("что", "известно", "сегодня"):
        assert stop_word not in asked, f"граф спрошен про служебное слово {stop_word!r}"


def test_a_person_found_in_the_graph_stops_the_search():
    import asyncio

    class _Graph:
        def search_entities(self, user_id, query, *, limit=3):  # noqa: ANN001, ARG002
            if query.casefold().startswith("хасан"):
                return [{"name": "Хасанова Уркия Хадиевна", "entity_type": "person"}]
            return []

    class _Kernel:
        kg = _Graph()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()

    class _Actor:
        user_id = "alice"

    assert asyncio.run(
        runtime._mentions_someone_from_the_archive("расскажи про Хасанову", _Actor())  # noqa: SLF001
    )
    # Не человек — не повод отменять поиск: «Москва» в графе как локация не
    # должна запрещать вопрос о погоде в Москве.
    class _Places:
        def search_entities(self, user_id, query, *, limit=3):  # noqa: ANN001, ARG002
            return [{"name": "Москва", "entity_type": "location"}]

    runtime.kernel = type("K", (), {"kg": _Places()})()
    assert not asyncio.run(
        runtime._mentions_someone_from_the_archive("какая погода в Москве?", _Actor())  # noqa: SLF001
    )


def test_a_settled_fact_is_answered_from_memory_not_the_web():
    """Мутация: убрать вид «знание» — тест краснеет.

    Владелец: «кто был вторым президентом США» и «кто был первым президентом
    России» уходили в интернет, хотя модель это знает. Делить надо не по теме, а
    по одному признаку: изменился ли ответ с тех пор, как модель училась.

    Замерено после правки, 6/6: устойчивые факты отвечаются без интернета за
    6.6–13.9 с и верно (Джон Адамс, Ельцин, 1 сентября 1939, 29 дней), а погода
    и курс по-прежнему идут в сеть за 23–26 с.
    """
    runtime, _ = _runtime_with('{"вид": "знание", "запрос": ""}')
    kind, query = asyncio.run(runtime._web_query_by_arbiter("кто был вторым президентом США?"))  # noqa: SLF001
    assert kind.startswith("знание")
    assert query is None, "по устоявшемуся факту поиск не нужен"


def test_the_answer_from_memory_says_so_out_loud():
    """Мутация: убрать пометку — человек не отличит память от источника.

    У ответа из головы нет ссылки, которую можно открыть, и человек должен это
    видеть — как он видит оговорку о необоснованности ответа по архиву.
    """
    import inspect

    source = inspect.getsource(AgentRuntime._prefetch_the_web_if_asked)  # noqa: SLF001
    assert 'kind.startswith("знание")' in source
    assert "Отвечаю из собственных знаний" in source
    assert "НЕ уверена" in source, "модели не сказано сомневаться вслух"
    # Прямая просьба поискать перекрывает: сомневаешься — попроси проверить.
    assert 'kind.startswith("знание") and not asked_outright' in source


def test_a_changing_fact_still_goes_to_the_web():
    """Контроль: «сейчас», «сегодня», «сколько стоит» — только из сети."""
    runtime, _ = _runtime_with('{"вид": "интернет", "запрос": "курс доллара сегодня"}')
    kind, query = asyncio.run(runtime._web_query_by_arbiter("какой сейчас курс доллара?"))  # noqa: SLF001
    assert "интернет" in kind
    assert query == "курс доллара сегодня"
