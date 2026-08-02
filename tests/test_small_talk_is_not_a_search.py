"""Реплика разговора — не запрос к архиву.

Замерено на живой переписке владельца 2 августа: «проверка связи» ушло в архив с
десятью попаданиями и уверенностью 0.888, и Пятница вывалила список документов
про подготовку и проверку средств связи вместо «связь есть».

Цена не только в смысле. Такой ход стоил **65 секунд** против 3.3 у обычной
реплики: модель перечисляет документы, проверка обоснованности их не
подтверждает, включается ремонт ответа (16.5 с сам по себе). После правки та же
фраза отвечается за 1.2 с.

Ошибиться здесь можно в две стороны, и они не равны: не узнать разговорную
фразу — потерять три секунды и получить лишние документы в контексте; принять за
болтовню настоящий вопрос — не ответить на него вовсе. Поэтому список короткий,
закрытый, и проверяется он с обеих сторон.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime import _is_small_talk


@pytest.mark.parametrize(
    "message",
    [
        "привет",
        "Привет, пятница!",
        "Пятница, привет",
        "здравствуй",
        "добрый день",
        "спасибо",
        "спс",
        "пока",
        "ок",
        "ага",
        "понятно",
        "проверка связи",
        "проверка",
        "тест",
        "раз два три",
        "как дела?",
        "ты тут?",
        "пятница, ты тут?",
        "на связи",
    ],
)
def test_a_conversational_line_is_recognised(message):
    assert _is_small_talk(message), f"реплика разговора не узнана: {message!r}"


@pytest.mark.parametrize(
    "message",
    [
        "Привет! Найди приказ 214",
        "что известно про Хасанова?",
        "проверка связи по объекту 12",
        "сколько документов в базе?",
        "спасибо, а теперь покажи рапорты за июль",
        "что было в пятницу?",
        "Пятница, что было 26 июля?",
        "тест на прочность бетона по ГОСТ",
        "как дела у Проскурина?",
    ],
)
def test_a_real_request_is_never_mistaken_for_chatter(message):
    """Мутация: снять ограничение длины или убрать проверку хвоста — краснеет.

    Здесь цена ошибки выше: принятый за болтовню вопрос остаётся без ответа
    вовсе. «Что было в пятницу?» отдельно — обращение по имени снимается перед
    проверкой, и день недели не должен попасть под это правило.
    """
    assert not _is_small_talk(message), f"настоящий вопрос принят за болтовню: {message!r}"


def test_small_talk_skips_retrieval_entirely():
    """Мутация: вернуть только `searcher = None` — тест краснеет.

    Обнулять поисковик было мало: запасная ветка всё равно шла в
    `search_knowledge`, и «проверка связи» по-прежнему приносила десять
    документов, о которых Пятница начинала рассказывать.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._prepare_context)  # noqa: SLF001
    marker = source.index("_is_small_talk(message)")
    tail = source[marker : marker + 1400]
    assert "context.knowledge_hits = []" in tail, "поиск всё ещё выполняется на болтовне"
    assert "elif searcher:" in tail, "запасная ветка поиска осталась достижимой"


def test_the_model_is_told_this_is_a_conversation():
    """Пустой контекст молчит о причине; модель должна знать, что искать нечего."""
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._build_initial_messages)  # noqa: SLF001
    assert "context.small_talk" in source
    assert "НЕ ищи ничего в базе знаний" in source


def test_the_archive_comes_before_the_internet():
    """Мутация: убрать проверку `context.knowledge_hits` — тест краснеет.

    Замерено: «что известно про приказ 214?» уходило в поисковик и возвращалось
    рассказом о разных нормативных актах с таким номером — за 36 секунд, — тогда
    как нужный приказ лежал в базе и находился обычным поиском. Прямая просьба
    «найди в интернете» это правило снимает.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._prefetch_the_web_if_asked)  # noqa: SLF001
    guard = source[: source.index("_web_query_by_arbiter(message)")]
    assert "context.knowledge_hits" in guard, "интернет спрашивается раньше собственного архива"
    assert "not asked_outright" in guard, "прямая просьба поискать в интернете должна исполняться"


def test_an_explicit_web_request_skips_the_archive_search():
    """Мутация: убрать `looking_outward` — тест краснеет.

    Замерено на боевом корпусе: сам поиск стоит 2.7 секунды, и на «найди в
    интернете курс евро» они тратятся впустую — ответ придёт из выдачи, а
    найденные документы в контекст даже не попадут. Проверка шаблонная, без
    обращения к модели.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._prepare_context)  # noqa: SLF001
    assert "_ASKS_FOR_THE_WEB.search(message)" in source
    assert "context.small_talk or looking_outward" in source, (
        "явная просьба поискать в интернете по-прежнему обыскивает архив"
    )
