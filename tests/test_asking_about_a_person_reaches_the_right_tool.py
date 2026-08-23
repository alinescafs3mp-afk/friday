"""«Что писал JBL?» — вопрос о человеке, а не о содержимом архива.

Найдено владельцем 2026-08-03 на живой переписке. У Пятницы есть `user_activity`
— «что один аккаунт писал и загружал, по имени, которым человек пользуется», — но
модель его не позвала: вопрос ушёл в поиск по архиву и вернулся ответом «похожее
есть, но не по делу». Владелец объяснил прямым текстом: «JBL это пользователь, как
и Пегас». Следующий вопрос — «Что писал Пегас?» — получил ровно тот же ответ.

Уговаривать модель бесполезно, это уже проверено на веб-поиске и на голосе:
решение звать инструмент остаётся её, и половину раз она его не принимает.
Лечится только выполнением ДО её хода.

Имя ищется среди УЧЁТОК: «что писал Иванов» про человека из документов останется
обычным поиском по архиву, как и было.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from friday.agent_runtime import _ASKS_WHAT_A_PERSON_WROTE, AgentRuntime


@pytest.mark.parametrize(
    "message",
    [
        "что писал JBL?",
        "Что писал Пегас?",
        "чем занимался Иванов на прошлой неделе",
        "что скидывал Пегас",
        "о чём спрашивал JBL",
        "активность Пегаса",
        "что загружал Yato вчера",
    ],
)
def test_a_question_about_a_person_is_recognised(message: str) -> None:
    assert _ASKS_WHAT_A_PERSON_WROTE.search(message), message


@pytest.mark.parametrize(
    "message",
    ["какая погода в Москве", "что там по поверке приборов", "напомни завтра позвонить"],
)
def test_other_questions_are_left_alone(message: str) -> None:
    assert not _ASKS_WHAT_A_PERSON_WROTE.search(message), message


class _Kernel:
    def __init__(self, rendered: str = "Активность: 3 документа, 12 сообщений.") -> None:
        self.calls: list[tuple[str, dict]] = []
        self._rendered = rendered

    async def execute(self, tool: str, params: dict, actor=None):  # noqa: ANN001, ARG002
        self.calls.append((tool, params))

        class _Result:
            success = True

            def to_llm_message(self_inner) -> str:  # noqa: N805
                return self._rendered

        return _Result()


class _Storage:
    def __init__(self, users: list[dict]) -> None:
        self._users = users

    def execute(self, sql: str, params: tuple[int, ...] = ()):  # noqa: ANN001
        """Serve the bounded directory query used by person resolution.

        The production resolver deliberately takes one sentinel row past its
        fuzzy-match ceiling from a single SQL snapshot.  This fake mirrors that
        contract instead of retaining the older ``list_users`` shortcut.
        """
        assert "FROM users" in sql
        assert params == (5001,)

        class _Rows:
            def fetchall(inner_self):  # noqa: ANN202, N805
                return list(self._users[: params[0]])

        return _Rows()


def _runtime(users: list[dict], rendered: str = "Активность: 3 документа."):
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel(rendered)
    runtime.storage = _Storage(users)
    return runtime


PEOPLE = [
    {"id": "telegram:telegram:2051783036", "display_name": "JBL", "username": "jbl", "status": "active"},
    {"id": "telegram:telegram:5344917795", "display_name": "Пегас", "username": "pegas", "status": "active"},
]


def _ask(runtime, message: str) -> list[dict]:
    messages: list[dict] = []
    bound = AgentRuntime._prefetch_person_activity.__get__(runtime, AgentRuntime)
    asyncio.run(
        bound(
            message,
            None,
            [{"function": {"name": "user_activity"}}],
            messages,
            [],
            [],
        )
    )
    return messages


def test_the_tool_runs_before_the_model_gets_the_turn() -> None:
    """Мутация: убрать предварительный вызов — тест краснеет."""
    runtime = _runtime(PEOPLE)

    messages = _ask(runtime, "что писал JBL?")

    assert runtime.kernel.calls, "инструмент не вызван — модель снова решает сама"
    assert runtime.kernel.calls[0][0] == "user_activity"
    assert runtime.kernel.calls[0][1]["person"] == "JBL"
    # Имя названо ЯВНО: на «а JBL что писал?» сразу после вопроса про Пегаса
    # модель повторяла прошлый ответ слово в слово — в контексте лежали и он, и
    # новые данные.
    said = str(messages[0]["content"])
    assert "JBL" in said, said
    assert "НЕ повторяй предыдущий ответ" in said, said


def test_the_second_name_works_too() -> None:
    """Владелец спросил про двоих подряд — сработать должно на обоих."""
    runtime = _runtime(PEOPLE)
    _ask(runtime, "Что писал Пегас?")
    assert runtime.kernel.calls[0][1]["person"] == "Пегас"


def test_an_unknown_name_changes_nothing() -> None:
    """«Что писал Иванов» про человека из документов — обычный поиск по архиву."""
    runtime = _runtime(PEOPLE)
    messages = _ask(runtime, "что писал Иванов")
    assert runtime.kernel.calls == []
    assert messages == []


def test_a_missing_tool_is_not_bypassed() -> None:
    """Нет права — нет вызова: предварительное выполнение прав не обходит."""
    runtime = _runtime(PEOPLE)
    messages: list[dict] = []
    bound = AgentRuntime._prefetch_person_activity.__get__(runtime, AgentRuntime)
    asyncio.run(bound("что писал JBL?", None, [], messages, [], []))
    assert runtime.kernel.calls == []


def test_the_prefetch_is_wired_into_the_loop() -> None:
    """Проверяется подключённое: вызов стоит в боевом цикле, а не рядом."""
    source = inspect.getsource(AgentRuntime._agentic_loop)
    assert "_prefetch_person_activity(" in source


def test_the_answer_carries_what_the_person_wrote() -> None:
    """Инструмент обязан нести СООБЩЕНИЯ, а не только загруженные файлы.

    Замерено на живом вопросе: «что писал JBL?» вернуло «сообщений 42 штуки, но
    сами записи не загрузились» — потому что надзор смотрел только
    `raw_objects`, а у человека, который просто переписывается, их ноль. Своё
    название инструмент выполнял наполовину.
    """
    import inspect

    from friday.execution_kernel import ExecutionKernel

    source = inspect.getsource(ExecutionKernel._user_activity)
    assert '"messages": storage.user_messages(' in source, "реплики человека снова не попадают в ответ"
    assert "include_content=include_content" in source, "глубина доступа перестала соблюдаться"


def test_a_participant_name_never_goes_to_a_search_engine() -> None:
    """Мутация: убрать защиту — «Что писал Пегас?» снова уйдёт наружу.

    Замерено: имя участника совпало с брендом, и Пятница рассказала про
    туроператора «Пегас Туристик», отправив имя человека из этой системы в чужой
    поисковик.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._prefetch_the_web_if_asked)
    guard = " ".join(source[: source.index("kind: str")].split())
    assert "self._mentions_someone_from_the_archive, web_message, actor," in guard, (
        "имя участника снова уходит в поиск"
    )
    assert "if local_person: return" in guard, "результат локальной проверки имени больше не закрывает поиск"


def test_a_person_question_wins_over_the_owners_own_timeline() -> None:
    """«Чем занимался Yato вчера?» — про Yato, а не про меня.

    Замерено: слово «вчера» поднимало ленту ВЛАДЕЛЬЦА, она приходила первой, и
    ответ получался про его собственную активность.
    """
    import inspect
    from datetime import date

    from friday.agent_runtime import (
        AgentRuntime,
        _closed_pure_past_timeline_intent,
        file_turn_authority,
    )

    message = "Чем занимался Yato вчера?"
    authority = file_turn_authority(message)
    assert authority.proved("person")
    assert _closed_pure_past_timeline_intent(message, today=date(2026, 8, 14)) is None

    source = inspect.getsource(AgentRuntime._agentic_loop)
    ordinary_lane = source[source.index("# Про ЧЕЛОВЕКА") :]
    person_at = ordinary_lane.index("_prefetch_person_activity(")
    timeline_at = ordinary_lane.index("_prefetch_the_timeline_if_asked(")
    assert person_at < timeline_at, "лента владельца снова отвечает раньше вопроса о человеке"
    assert "and not about_a_person" in " ".join(ordinary_lane.split()), (
        "лента поднимается даже когда вопрос был про человека"
    )
