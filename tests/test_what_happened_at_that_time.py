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
