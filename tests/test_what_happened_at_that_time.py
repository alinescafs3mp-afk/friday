"""Вопрос о МОМЕНТЕ, а не о словах.

Требование владельца 2026-08-01: вся информация в чате и все файлы фиксируются по
дате и времени, и на вопрос «что было 26 июля в 15 часов» Пятница обязана
отвечать уверенно. Уточнение: это пример, а не единственный сценарий — временные
операции нужны любые.

Обычный поиск на такой вопрос не отвечает в принципе: он ищет СЛОВА. Ключевые
слова здесь — «26 июля» и «15 часов», и по ним найдутся документы, где эти даты
УПОМЯНУТЫ, а не то, что появилось в тот час. Поэтому отдельный инструмент и
отдельная лента.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from friday.execution_kernel import _moment_bounds, _spoken_day


@pytest.mark.parametrize(
    "text,expected",
    [
        ("26 июля 2026", "2026-07-26"),
        ("1 января 2020", "2020-01-01"),
        ("31 декабря 2025", "2025-12-31"),
        # Падеж не должен решать: разбор идёт по префиксу месяца.
        ("26 июле 2026", "2026-07-26"),
        ("5 марта 2026", "2026-03-05"),
    ],
)
def test_a_day_spoken_the_way_people_speak(text, expected):
    assert _spoken_day(text, today=date(2026, 8, 1)) == expected


def test_a_year_left_unsaid_means_this_one():
    assert _spoken_day("26 июля", today=date(2026, 8, 1)) == "2026-07-26"


@pytest.mark.parametrize(
    "text,days_back",
    [("сегодня", 0), ("вчера", 1), ("позавчера", 2), ("3 дня назад", 3), ("10 суток назад", 10)],
)
def test_relative_days(text, days_back):
    today = date(2026, 8, 1)
    assert _spoken_day(text, today=today) == (today - timedelta(days=days_back)).isoformat()


@pytest.mark.parametrize("text", ["весной", "прошлым летом", "как-нибудь", "32 июля 2026", ""])
def test_what_cannot_be_known_is_not_guessed(text):
    """Придумать период — тот же класс ошибки, что придумать дату документа."""
    assert _spoken_day(text, today=date(2026, 8, 1)) is None


def test_a_named_hour_means_that_hour():
    """«в 15 часов» — это промежуток 15:00–15:59, а не мгновение."""
    start, bad = _moment_bounds("26 июля 2026 в 15 часов", edge="since")
    end, bad_end = _moment_bounds("26 июля 2026 в 15 часов", edge="until", widen=True)
    assert not bad and not bad_end
    assert start == "2026-07-26T15:00:00"
    assert end == "2026-07-26T15:59:59"


def test_a_named_day_means_the_whole_day():
    start, _ = _moment_bounds("26 июля 2026", edge="since")
    end, _ = _moment_bounds("26 июля 2026", edge="until")
    assert start == "2026-07-26T00:00:00"
    assert end == "2026-07-26T23:59:59"


def test_an_exact_minute_stays_exact_when_the_end_is_given():
    """Мутация: расширять минуту всегда — тест краснеет.

    Когда конец промежутка назван явно, «с 15:30 до 16:30» обязано значить
    ровно это, иначе интервалы перестают работать.
    """
    end, _ = _moment_bounds("2026-07-26 16:30", edge="until", widen=False)
    assert end == "2026-07-26T16:30:59"


def test_an_unparsable_moment_is_refused_not_widened():
    """Непонятая граница — отказ, а не «показать всё».

    Молча снятый фильтр выдаёт чужое время за спрошенное, и человек об этом не
    узнаёт.
    """
    value, bad = _moment_bounds("когда-нибудь в мае", edge="since")
    assert value is None
    assert bad


def _seed(storage, user_id="alice"):
    storage.ensure_user(user_id)
    conversation = storage.create_conversation(user_id, "Разговор")
    return conversation["id"]


def test_the_window_finds_messages_and_documents_together(storage):
    """Лента — это и разговор, и поступления: «что было» значит и то, и другое."""
    conversation_id = _seed(storage)
    storage.store_message(conversation_id, "alice", "user", "вопрос в этот час")

    now = datetime.now(UTC)
    since = (now - timedelta(hours=1)).isoformat()
    until = (now + timedelta(hours=1)).isoformat()

    events = storage.what_happened("alice", since=since, until=until)
    assert [event["kind"] for event in events] == ["message"]
    assert events[0]["text"] == "вопрос в этот час"

    totals = storage.count_what_happened("alice", since=since, until=until)
    assert totals == {"messages": 1, "documents": 0, "total": 1}


def test_a_neighbouring_hour_is_not_the_asked_one(storage):
    """Мутация: убрать границы из запроса — тест краснеет."""
    conversation_id = _seed(storage)
    storage.store_message(conversation_id, "alice", "user", "сказано сейчас")

    long_ago = datetime.now(UTC) - timedelta(days=30)
    events = storage.what_happened(
        "alice",
        since=long_ago.isoformat(),
        until=(long_ago + timedelta(hours=1)).isoformat(),
    )
    assert events == [], "показано событие из другого времени"


def test_the_count_is_not_the_length_of_the_page(storage):
    """Длина страницы — не факт о промежутке.

    Сказать «за этот час было 5 событий», показав ровно свои пять, значит выдать
    размер собственного запроса за свойство архива.
    """
    conversation_id = _seed(storage)
    for index in range(7):
        storage.store_message(conversation_id, "alice", "user", f"сообщение {index}")

    now = datetime.now(UTC)
    since = (now - timedelta(hours=1)).isoformat()
    until = (now + timedelta(hours=1)).isoformat()

    shown = storage.what_happened("alice", since=since, until=until, limit=3)
    assert len(shown) == 3
    assert storage.count_what_happened("alice", since=since, until=until)["messages"] == 7


def test_another_persons_hour_is_not_visible(storage):
    """Граница арендатора — та же, что везде."""
    _seed(storage, "alice")
    bob_conversation = _seed(storage, "bob")
    storage.store_message(bob_conversation, "bob", "user", "чужое сообщение")

    now = datetime.now(UTC)
    events = storage.what_happened(
        "alice",
        since=(now - timedelta(hours=1)).isoformat(),
        until=(now + timedelta(hours=1)).isoformat(),
    )
    assert events == []


def test_the_model_is_told_what_day_it_is():
    """Мутация: убрать `_today_line()` из сборки промпта — тест краснеет.

    Замерено на живом экземпляре 2026-08-01: на «что происходило вчера?» модель
    вызвала инструмент с датой **25 июля** и уверенно ответила, что вчера ничего
    не было. Настоящее «вчера» — 31 июля, и события там были. Модель не знает
    текущую дату и знать не может; без этой строки любой относительный вопрос
    отвечается мимо, причём уверенно.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._build_initial_messages)  # noqa: SLF001
    assert "_today_line()" in source, "промпт собирается без текущей даты"

    line = AgentRuntime._today_line.__doc__ or ""
    assert "вчера" in line


def test_todays_line_names_the_date_and_the_hour(settings):
    """Строка обязана называть и дату, и час: «сегодня в час ночи» без времени —
    это угадывание."""
    from friday.agent_runtime import AgentRuntime

    runtime = object.__new__(AgentRuntime)
    runtime.settings = settings
    line = runtime._today_line()  # noqa: SLF001

    today = datetime.now().astimezone()
    assert today.strftime("%Y-%m-%d") in line
    assert ":" in line, "час не назван"
    assert "не полагайся на свою память" in line


@pytest.mark.parametrize(
    "question,expected",
    [
        ("что было 29 июля?", "29 июля"),
        ("что происходило вчера?", "вчера"),
        ("что было сегодня в час ночи?", "сегодня 01:00"),
        ("что было 26 июля в 15 часов?", "26 июля 15:00"),
        ("что было позавчера в 10:30?", "позавчера 10:30"),
        ("что было вчера в полдень?", "вчера 12:00"),
        ("что было 3 дня назад?", "3 дня назад"),
        ("что было 2026-07-29?", "2026-07-29"),
    ],
)
def test_the_moment_is_taken_from_the_question_as_said(question, expected):
    """Год не дописывается: на живом прогоне модель превратила «29 июля» в
    «29 июля 2024» и ответила про пустоту там, где было 1555 событий."""
    from friday.agent_runtime import moment_from_question

    assert moment_from_question(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "что нового по проекту?",
        "что было решено по штатному расписанию?",
        "какие документы есть про АК-12?",
    ],
)
def test_a_question_without_a_moment_is_not_a_timeline_question(question):
    from friday.agent_runtime import moment_from_question

    assert moment_from_question(question) is None


def test_the_timeline_prefetch_is_wired_into_the_loop():
    """Мутация: убрать вызов из `_agentic_loop` — тест краснеет.

    Замерено: на «что было 29 июля?» модель НЕ звала инструмент и отвечала по
    документу, где эта дата лишь упомянута («29 июля 2024 года зафиксировано
    прибытие военнослужащих»). К этому моменту контекст уже собран поиском, и
    своё решение звать инструмент модель принимает против готового текста.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    loop_source = inspect.getsource(AgentRuntime._agentic_loop)  # noqa: SLF001
    assert "_prefetch_the_timeline_if_asked(" in loop_source, (
        "лента объявлена, но из агентского цикла не вызывается"
    )


@pytest.mark.parametrize(
    "question",
    [
        "что было вчера",
        # «расскажи про 29 июля» отсюда убрано намеренно: шаблон ловил вместе с
        # ним и «расскажи про приказ от 29 июля» — вопрос о документе. Такие
        # фразы решает арбитр, см. test_a_question_about_a_document_is_not_a_timeline_question.
        "покажи что происходило в понедельник",
        "чем занимались 29го",
        "что я делал позавчера",
        "какие события были 30 июля",
        "события за вчера",
        "что нового появилось вчера",
        "чем я занимался 3 дня назад",
        "что случилось 26 июля в 15 часов",
        "покажи ленту за вчера",
    ],
)
def test_the_same_question_asked_differently_is_still_recognised(question):
    """Замерено до правки: из двенадцати живых формулировок шаблон узнавал пять.

    Владелец сказал прямо: «другая формулировка или опечатка не должны быть
    препятствием». Перечислить все формы нельзя, поэтому шаблон покрывает частые,
    а остальное решает модель — но частые обязаны ловиться без её участия, иначе
    каждый второй вопрос стоит лишнего вызова.
    """
    from friday.agent_runtime import _ASKS_WHAT_HAPPENED, moment_from_question

    assert moment_from_question(question), f"время в вопросе не найдено: {question}"
    assert _ASKS_WHAT_HAPPENED.search(question), f"намерение не распознано: {question}"


@pytest.mark.parametrize(
    "weekday,expected",
    [
        ("понедельник", "2026-07-27"),
        ("среду", "2026-07-29"),
        ("пятницу", "2026-07-31"),
        # Названный день недели, совпавший с сегодняшним, — это сегодня, а не
        # неделю назад.
        ("субботу", "2026-08-01"),
        ("воскресенье", "2026-07-26"),
    ],
)
def test_a_weekday_means_the_last_one_that_happened(weekday, expected):
    assert _spoken_day(weekday, today=date(2026, 8, 1)) == expected


def test_the_intent_check_asks_the_model_when_the_pattern_is_silent():
    """Мутация: убрать `_is_a_timeline_question` из условия — тест краснеет.

    Шаблон не может знать всех формулировок; когда время в вопросе уже найдено,
    а фраза незнакомая, решать должен тот, кто понимает язык.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._prefetch_the_timeline_if_asked)  # noqa: SLF001
    assert "_is_a_timeline_question(message)" in source, (
        "незнакомая формулировка отбрасывается без попытки понять её"
    )
    assert source.index("moment_from_question") < source.index("_is_a_timeline_question"), (
        "модель спрашивается даже когда времени в вопросе нет — это лишний вызов на каждом сообщении"
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("29го", "2026-07-29"),
        ("29-го", "2026-07-29"),
        ("31го", "2026-07-31"),
        # Сегодняшнее число засчитывается как сегодня, а не как месяц назад.
        ("2го", "2026-08-02"),
        ("3-е", "2026-07-03"),
        # 31-е есть не в каждом месяце: разбор обязан отступить к тому, где оно есть.
        ("31-го", "2026-07-31"),
    ],
)
def test_an_ordinal_day_without_a_month(text, expected):
    """Мутация: убрать разбор `_ORDINAL_DAY_RE` — тест краснеет.

    Найдено состязательным ревью перед демо: «чем занимались 29го» —
    СЦЕНАРНАЯ СТРОКА из DEMO.md. Извлечение момента её понимало, ядро — нет, и
    человек получал «в тот момент в архиве ничего не появилось» при 1531
    документе и 24 сообщениях за 29 июля. Число без месяца означает ближайшее
    ПРОШЕДШЕЕ такое число: о будущем архив ничего сказать не может.
    """
    assert _spoken_day(text, today=date(2026, 8, 2)) == expected


def test_an_impossible_ordinal_day_is_refused():
    assert _spoken_day("32го", today=date(2026, 8, 2)) is None
    assert _spoken_day("0го", today=date(2026, 8, 2)) is None


def test_the_whole_chain_understands_an_ordinal_day():
    """Проверяется СВЯЗКА, а не звенья: извлечение из вопроса плюс разбор ядром.

    Прежний тест проверял только извлечение и оставался зелёным, пока ядро эту
    же форму отвергало.
    """
    from friday.agent_runtime import _ASKS_WHAT_HAPPENED, moment_from_question

    question = "чем занимались 29го"
    moment = moment_from_question(question)
    assert moment == "29го"
    assert _ASKS_WHAT_HAPPENED.search(question)
    since, bad = _moment_bounds(moment, edge="since")
    assert bad is None and since, "ядро не разобрало момент, который извлекли из вопроса"


def test_an_unparsed_moment_never_becomes_an_empty_timeline():
    """Мутация: убрать ветку `understood is False` — тест краснеет.

    «Не разобрал дату» и «в этот момент ничего не было» — разные вещи. Второе —
    утверждение о личном архиве человека, которого никто не проверял.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._prefetch_the_timeline_if_asked)  # noqa: SLF001
    assert 'get("understood") is False' in source, (
        "неразобранный момент подаётся модели как пустая лента"
    )
    branch = source[source.index('get("understood") is False') :][:900]
    assert "НЕ утверждай" in branch


@pytest.mark.parametrize(
    "question,expected",
    [
        ("что было вчера в 8 часов вечера", "вчера 20:00"),
        ("что было вчера в 3 часа дня", "вчера 15:00"),
        ("что было вчера в два часа ночи", "вчера 02:00"),
        ("что было сегодня в час ночи", "сегодня 01:00"),
        ("что было вчера в час дня", "вчера 13:00"),
        ("что было вчера в 9 утра", "вчера 09:00"),
        ("что было вчера в 12 ночи", "вчера 00:00"),
        ("что было вчера в полдень", "вчера 12:00"),
        # Полное время частью суток не сдвигается: «в 20:30 вечера» — это 20:30.
        ("что было вчера в 20:30", "вчера 20:30"),
    ],
)
def test_the_hour_matches_the_part_of_the_day(question, expected):
    """Мутация: убрать `_hour_with_part_of_day` — тест краснеет.

    Замерено ревью: «8 часов вечера» разбиралось как 08:00, «3 часа дня» как
    03:00, а «два часа ночи» — как 01:00 при любом числительном, потому что
    правило «час ночи» искалось подстрокой по всему тексту. Человек спрашивал
    про вечер и слышал «в тот час ничего не было».
    """
    from friday.agent_runtime import moment_from_question

    assert moment_from_question(question) == expected


@pytest.mark.parametrize(
    "question,expected",
    [
        ("что было с 29 по 31 июля", ("29 июля", "31 июля")),
        ("что было с 26 июля по 1 августа", ("26 июля", "1 августа")),
        ("что было между 26 и 29 июля", ("26 июля", "29 июля")),
    ],
)
def test_a_named_range_keeps_both_ends(question, expected):
    """Мутация: убрать `period_from_question` — тест краснеет.

    `until` не передавался НИКОГДА: «что было с 29 по 31 июля» показывало один
    день вместо трёх, причём последний — то есть ответ был про другое время, чем
    спросили.
    """
    from friday.agent_runtime import period_from_question

    assert period_from_question(question) == expected


@pytest.mark.parametrize("question", ["что было вчера", "что было 29 июля", "запиши: с Ивановым по срокам"])
def test_a_single_day_is_not_mistaken_for_a_range(question):
    from friday.agent_runtime import period_from_question

    assert period_from_question(question) is None


def test_the_feed_covers_the_whole_window_not_just_its_start(storage):
    """Мутация: вернуть `events[:window]` — тест краснеет.

    Лента отдавала первые N событий: на дне с 684 событиями человек видел первый
    час и не знал, что было дальше. Теперь выборка равномерна по промежутку, а
    последнее событие показывается всегда — чем всё кончилось, обычно и есть
    ответ на «что было».
    """
    from datetime import UTC, datetime, timedelta

    conversation_id = _seed(storage)
    base = datetime.now(UTC) - timedelta(hours=12)
    for index in range(60):
        storage.store_message(conversation_id, "alice", "user", f"сообщение {index}")

    since = (base - timedelta(hours=1)).isoformat()
    until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    events = storage.what_happened("alice", since=since, until=until, limit=10)

    assert len(events) == 10
    texts = [event["text"] for event in events]
    assert texts[-1] == "сообщение 59", "последнее событие промежутка не показано"
    assert texts[0] == "сообщение 0", "выборка не начинается с начала промежутка"
    # Между краями должны быть события из середины, а не подряд идущие первые.
    assert texts[1] != "сообщение 1", "показаны подряд идущие первые события"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3-го августа", "2026-08-03"),
        ("3-е августа 2026", "2026-08-03"),
        ("26 июля", "2026-07-26"),
        # Без месяца по-прежнему означает ближайшее прошедшее такое число.
        ("29го", "2026-07-29"),
    ],
)
def test_an_ordinal_day_with_a_month_keeps_the_month(text, expected):
    """Мутация: убрать порядковое окончание из `_DAY_MONTH_RE` — тест краснеет.

    Найдено ревью собственных правок: «3-го августа» разбиралось короткой веткой
    как «ближайшее прошедшее третье число», и месяц терялся — ответ был про
    другой месяц, чем спросили.
    """
    assert _spoken_day(text, today=date(2026, 8, 2)) == expected


def test_a_question_about_a_document_is_not_a_timeline_question():
    """Мутация: вернуть «расскажи про …» в шаблон намерения — тест краснеет.

    «Расскажи про приказ от 29 июля» — вопрос о ДОКУМЕНТЕ, а шаблон отправлял
    его в ленту событий того дня. Такие фразы теперь решает арбитр, который
    различает «что было 29 июля» и «что сказано в приказе от 29 июля».
    """
    from friday.agent_runtime import _ASKS_WHAT_HAPPENED

    assert not _ASKS_WHAT_HAPPENED.search("расскажи про приказ от 29 июля")
    assert _ASKS_WHAT_HAPPENED.search("что было 29 июля")
    assert _ASKS_WHAT_HAPPENED.search("что происходило вчера")


@pytest.mark.asyncio
async def test_the_timeline_shows_my_conversation_not_the_owners(settings, storage):
    """Лента показывает МОЮ переписку и ОБЩИЕ документы — это разные границы.

    Найдено большим ревью 2026-08-04 и воспроизведено: инструмент звал ленту с
    `actor.user_id`, а в общем архиве это арендатор. Участник, спросивший «что было
    вчера», получал реплики ВЛАДЕЛЬЦА дословно — с ролью и заголовком разговора, —
    а своих не видел ни одной. На живой базе под арендатором лежало 2862 сообщения
    владельца против 92/84/42/14/2 у участников.

    Простой заменой на `own_id` это не чинится, и в этом суть: один параметр
    обслуживал ДВЕ границы. Переписка личная, документы общие по прямой просьбе
    владельца — все 1562 знания живой базы лежат под арендатором, и замена увела
    бы их из ленты целиком. Поэтому параметра теперь два.

    Мутация: вернуть `person_id=actor.user_id` — краснеет утечка; убрать `user_id`
    из запроса документов — краснеет их пропажа.
    """
    from friday.execution_kernel import ExecutionKernel
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.permissions import LEGACY_OWNER_USER_ID, AuthorizationService
    from friday.storage.models import KnowledgeObject, RawObject, new_id, utc_now
    from friday.web_surfer import WebSurfer

    tenant = LEGACY_OWNER_USER_ID
    storage.ensure_user(tenant, preset_key="owner")
    storage.ensure_user("bob", preset_key="owner")
    for who, text in ((tenant, "надо заказать перегородки"), ("bob", "уточню смету у подрядчика")):
        conversation = storage.create_conversation(who, "рабочий")
        storage.store_message(conversation["id"], who, "user", text)
    raw = storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id=tenant,
            source="api",
            source_ref="",
            raw_content="итого 480 000",
            content_type="text",
            metadata_json={},
            content_hash=new_id("h"),
            version=1,
            received_at=utc_now(),
            created_at=utc_now(),
        )
    )
    storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("kn"),
            user_id=tenant,
            raw_object_id=raw.id,
            title="Смета за июль",
            content="итого 480 000",
            summary="сводка расходов",
            knowledge_kind="note",
            content_type="text/plain",
        )
    )

    auth = AuthorizationService(storage, shared_tenant=tenant)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))

    bob = auth.actor_for_user("bob", source="test")
    assert bob.user_id == tenant and bob.own_id == "bob", "стенд собран не как общий архив"

    result = await kernel.execute("what_happened", {"since": "сегодня"}, actor=bob)
    assert result.success, f"инструмент отказал: {result.error!r}"
    seen = str(result.data)
    assert "перегородки" not in seen, "участник читает переписку владельца"
    assert "подрядчика" in seen, "участник не видит собственной реплики"
    assert "Смета за июль" in seen, "общий документ пропал из ленты"

    owner = auth.actor_for_user(tenant, source="test")
    mine = str((await kernel.execute("what_happened", {"since": "сегодня"}, actor=owner)).data)
    assert "перегородки" in mine, "владелец потерял собственную переписку"
    assert "подрядчика" not in mine, "владельцу показали чужую реплику в ленте"
