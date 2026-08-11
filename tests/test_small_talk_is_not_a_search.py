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
        # «ок», «ага», «понятно» и другие слова СОГЛАСИЯ отсюда убраны 2026-08-04:
        # закрытый список решал за них без модели и гасил весь блок понимания, а
        # «ок» после «Могу поискать — сделать?» означает «делай». Теперь они идут
        # к арбитру, который видит предыдущий ход, — см.
        # `test_a_short_consent_continues_the_previous_turn`.
        "проверка связи",
        "проверка",
        "приём",
        "ПРИЕМ!",
        "тест",
        "раз два три",
        "как дела?",
        "как у тебя дела?",
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
    # Проверяется СВЯЗЬ признака с обнулением, а не расстояние между строками:
    # первая редакция брала фиксированный срез в 2600 знаков после признака и
    # покраснела, когда между ними появился (совершенно законный) блок запуска
    # арбитра. Тест должен ловить возвращение поиска на болтовне, а не любую
    # правку по соседству.
    # Условие расширилось: поиск пропускается ещё и на вопросах о деятельности
    # участника — на них отвечает инструмент надзора, архив там ни при чём.
    guard = source.index("if context.small_talk or looking_outward")
    branch = source[guard : source.index("elif searcher:", guard)]
    assert "context.knowledge_hits = []" in branch, "поиск всё ещё выполняется на болтовне"
    assert source.index("_is_small_talk(message)") < guard, "признак болтовни считается позже проверки"


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
    assert "looking_outward = asks_for_the_web(message)" in source
    assert "context.small_talk or looking_outward" in source, (
        "явная просьба поискать в интернете по-прежнему обыскивает архив"
    )


def test_hits_with_no_relevance_at_all_are_not_material():
    """Мутация: убрать порог шума — тест краснеет.

    Замерено на живой переписке: короткое обращение приносило девять документов
    с ЛУЧШИМ счётом 0.000, режим ответа становился «личные знания», и модель
    разворачивала этот шум в килобайт текста — от 36 до 92 секунд на реплику,
    которую человек написал одним словом. После правки: ноль попаданий,
    разговорный режим, 389 знаков, 15.7 с.

    Порог не ранжирует, а отсекает пустоту: recall@10 на 78 эталонах не
    изменился (0.7436), MRR тоже.
    """
    import inspect

    from friday.agent_runtime import _NOISE_FLOOR, AgentRuntime

    assert 0 < _NOISE_FLOOR < 0.01, "порог должен отсекать ноль, а не ранжировать"

    source = inspect.getsource(AgentRuntime._prepare_context)  # noqa: SLF001
    assert "_NOISE_FLOOR" in source
    assert 'float(item["_score"]) > _NOISE_FLOOR' in source
    # `None` и `0.0` — разные вещи: первое значит «счёт не вычислялся»
    # (упрощённая сборка поиска), второе — «вычислен и равен нулю». Первая
    # редакция их не различала и выбрасывала законные совпадения.
    assert 'item.get("_score") is not None' in source
    # Достаточно ОДНОГО осмысленного попадания, чтобы список остался целым:
    # отбрасывается только выдача, где нет ни одного.
    assert "not any(" in source, "порог применяется к каждому попаданию вместо всей выдачи"


def test_a_single_real_hit_keeps_the_whole_list():
    """Контроль: слабые соседи полезны рядом с настоящим совпадением."""
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._prepare_context)  # noqa: SLF001
    marker = source.index("_NOISE_FLOOR")
    tail = source[marker : marker + 400]
    assert "found = []" in tail
    assert "context.knowledge_hits = found" in tail


def test_a_one_word_request_gets_a_question_back_not_a_dump():
    """Мутация: убрать `terse_request` — тест краснеет.

    Замерено на живой переписке: на слово из пяти букв приходило десять
    документов и ответ на килобайт. Порогом счёта это не лечится — у такой
    реплики совпадение оказалось ВЫШЕ, чем у настоящего вопроса (0.83 против
    0.26): слово короткое и совпадает с документами целиком.

    Помощник в таком случае переспрашивает, а не гадает по документам.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    prepare = inspect.getsource(AgentRuntime._prepare_context)  # noqa: SLF001
    assert "context.terse_request" in prepare
    assert ") <= 2" in prepare or "<= 2\n" in prepare, "признак не привязан к длине обращения"

    prompt = inspect.getsource(AgentRuntime._build_initial_messages)  # noqa: SLF001
    assert "переспроси" in prompt
    assert "пересказывай найденное списком" in prompt


def test_the_noise_floor_reads_the_field_that_exists():
    """Мутация: вернуть `item.get("score")` — тест краснеет.

    Поле называется `_score`, со служебным подчёркиванием. Первая редакция
    читала `score`, всегда получала None — и порог не применялся вовсе. Хуже:
    замер, которым я его проверяла, печатал `float(item.get("score") or 0)` и
    показывал ровные нули, то есть подтверждал несуществующий эффект.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._prepare_context)  # noqa: SLF001
    assert 'item.get("_score") is not None' in source
    assert 'float(item["_score"]) > _NOISE_FLOOR' in source
    # Проверяется ЧТЕНИЕ правильного поля, а не отсутствие строки: упоминание
    # старого имени осталось в пояснении к правке, и это правильно — из него
    # понятно, чем ошибка была.
