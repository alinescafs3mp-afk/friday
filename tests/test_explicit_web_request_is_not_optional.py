"""Попросили посмотреть в интернете — значит смотрим, а не решаем заново.

Замерено на живом экземпляре 2026-08-01. Один и тот же вопрос «найди в
интернете, какая сейчас ключевая ставка ЦБ»:

* в одном прогоне модель позвала `web_search` и `web_fetch`;
* в следующем не позвала НИЧЕГО (`tools_used: []`) и ответила из памяти —
  «21%, декабрь 2025», при настоящих **14,00% от 31.07.2026** на странице ЦБ,
  которую поиск в первом прогоне и вернул.

Вызов инструмента везде остаётся решением модели. Здесь — не остаётся: человек
попросил прямым текстом, решать нечего, а найти верный ответ и сказать неверный
хуже, чем не найти.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime import _ASKS_FOR_THE_WEB, AgentRuntime


@pytest.mark.parametrize(
    "message",
    [
        "найди в интернете, какая сейчас ключевая ставка ЦБ",
        "посмотри в интернете последние новости про Су-57",
        "погугли расписание поездов Москва — Казань",
        "глянь в сети, что за протокол MCP",
        "search the web for python 3.14 release notes",
        "проверь в яндексе цену на нефть",
    ],
)
def test_an_explicit_request_is_recognised(message):
    assert _ASKS_FOR_THE_WEB.search(message), message


@pytest.mark.parametrize(
    "message",
    [
        "найди приказ 214 в моих документах",
        "что известно про Хасанова Руслана Рашитовича?",
        "покажи расчётные листки за декабрь",
        "сколько у меня документов в базе знаний?",
        # «сеть» в другом значении — просьбы искать снаружи здесь нет.
        "какая топология у нашей локальной сети?",
    ],
)
def test_a_question_about_the_personal_archive_is_not_a_web_request(message):
    assert not _ASKS_FOR_THE_WEB.search(message), message


@pytest.mark.parametrize(
    "message,expected",
    [
        ("найди в интернете, какая сейчас ключевая ставка ЦБ", "какая сейчас ключевая ставка ЦБ"),
        ("погугли расписание поездов Москва Казань", "расписание поездов Москва Казань"),
        ("посмотри в интернете последние новости про Су-57", "последние новости про Су-57"),
    ],
)
def test_the_filler_words_do_not_reach_the_search_engine(message, expected):
    assert AgentRuntime.web_query_from(message) == expected


def test_a_request_that_is_only_filler_still_searches_for_something():
    """«погугли» без темы — запрос пустым не станет."""
    assert AgentRuntime.web_query_from("погугли").strip()


@pytest.mark.anyio
async def test_the_search_runs_before_the_model_gets_a_turn(monkeypatch):
    """Мутация: убрать вызов `_prefetch_the_web_if_asked` — тест краснеет.

    Проверяется не помощник, а то, что поиск реально произошёл и его выдача
    легла в сообщения ДО первого хода модели.
    """
    import friday.agent_runtime as runtime_module

    calls: list[tuple[str, dict]] = []

    class _Result:
        success = True
        attachment = None
        data = {"results": [{"url": "https://cbr.ru/", "title": "Ставка 14,00%"}]}

        def to_llm_message(self) -> str:
            return "Результат web_research:\nставка 14,00% на 31.07.2026"

    class _Kernel:
        async def execute(self, name, arguments, *, actor):
            calls.append((name, arguments))
            return _Result()

    class _NoLLM:
        """Модель недоступна — а прямая просьба всё равно должна быть исполнена.

        Не выдуманный случай: 1 августа модель отпадала посреди работы. Запрос
        тогда формулирует не арбитр, а вычёркивание вводных слов, и поиск идёт.
        """

        enabled = False

    runtime = object.__new__(runtime_module.AgentRuntime)
    runtime.kernel = _Kernel()
    runtime.llm = _NoLLM()
    messages: list[dict] = []
    used: list[str] = []
    evidence: list[dict] = []

    await runtime_module.AgentRuntime._prefetch_the_web_if_asked(  # noqa: SLF001
        runtime,
        "найди в интернете, какая сейчас ключевая ставка ЦБ",
        actor=None,
        tools=[{"function": {"name": "web_research"}}],
        messages=messages,
        tools_used=used,
        tool_evidence=evidence,
    )

    # Именно `web_research`, а не `web_search`: он не только ищет, но и читает
    # страницы. Замерено на пяти вопросах, ответ на которые — число (ставка,
    # погода, курс, нефть, население): `web_search` называет конкретное значение
    # в 3 случаях из 5 при медиане 5.2 с, `web_research` — в 5 из 5 при 6.0 с.
    # Услужливый гугл называет значение, а не список ссылок на него.
    assert calls and calls[0][0] == "web_research", "поиск не выполнен при прямой просьбе"
    assert calls[0][1]["query"] == "какая сейчас ключевая ставка ЦБ"
    assert used == ["web_research"]
    # Выдача и указания приходят РАЗНЫМИ сообщениями: содержимое чужого сайта не
    # должно лежать в системной роли, где модель ждёт инструкций (2026-08-04).
    # Здесь проверяется, что оба доехали, — а не в каком порядке они сложены.
    whole = " ".join(str(item.get("content") or "") for item in messages)
    assert messages and "14,00%" in whole, "выдача не дошла до модели"
    assert "не подменяй" in whole or "не выдумывай" in whole


def test_the_prefetch_is_wired_into_the_loop():
    """Мутация: убрать вызов из `_agentic_loop` — тест краснеет.

    Зелёный тест на самом помощнике не доказывает, что его кто-то зовёт:
    первая редакция этого файла мутацию «удалить вызов из цикла» не ловила.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    loop_source = inspect.getsource(AgentRuntime._agentic_loop)  # noqa: SLF001
    assert "_prefetch_the_web_if_asked(" in loop_source, (
        "предварительный поиск объявлен, но из агентского цикла не вызывается"
    )
    body = loop_source[loop_source.index("_prefetch_the_web_if_asked(") :]
    # Вызов многострочный: сообщение человека — первый аргумент, а не какая-то
    # переработанная строка.
    first_argument = body.split("(", 1)[1].lstrip().split(",", 1)[0].strip()
    assert first_argument == "message", (
        f"в предварительный поиск уходит не сообщение человека, а {first_argument!r}"
    )


@pytest.mark.anyio
async def test_nothing_happens_without_the_tool(monkeypatch):
    """Права важнее удобства: нет инструмента — нет обхода."""
    import friday.agent_runtime as runtime_module

    class _Kernel:
        async def execute(self, name, arguments, *, actor):  # pragma: no cover - не должен зваться
            raise AssertionError("инструмент недоступен, а его всё равно позвали")

    class _NoLLM:
        """Модель недоступна — а прямая просьба всё равно должна быть исполнена.

        Не выдуманный случай: 1 августа модель отпадала посреди работы. Запрос
        тогда формулирует не арбитр, а вычёркивание вводных слов, и поиск идёт.
        """

        enabled = False

    runtime = object.__new__(runtime_module.AgentRuntime)
    runtime.kernel = _Kernel()
    runtime.llm = _NoLLM()
    messages: list[dict] = []

    await runtime_module.AgentRuntime._prefetch_the_web_if_asked(  # noqa: SLF001
        runtime,
        "найди в интернете ключевую ставку",
        actor=None,
        tools=[{"function": {"name": "memory_search"}}],
        messages=messages,
        tools_used=[],
        tool_evidence=[],
    )
    assert messages == []


def test_a_request_to_search_is_not_material_for_the_archive(settings):
    """Просьба поискать не должна оседать в Inbox как кандидат в знания.

    Замерено на живом экземпляре 2026-08-01: пятнадцать вопросов вида «найди в
    интернете курс доллара» дали пятнадцать записей во «Входящих». Именная
    группа без предиката классификатору неотличима от заголовка факта, и он
    честно отправляет её человеку на решение — но решать тут нечего.

    Найденное при этом сохраняется штатно, через `web_research`: в архив
    попадает ответ, а не вопрос.

    Мутация: убрать ветку `asks_for_the_web` из `/api/chat` — тест краснеет.
    """
    from fastapi.testclient import TestClient

    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        before = (
            len(app.state.storage.list_inbox_pending_for_tests())
            if hasattr(app.state.storage, "list_inbox_pending_for_tests")
            else len(
                [row for row in app.state.storage.execute("SELECT id FROM inbox WHERE status='pending'")]
            )
        )

        response = client.post(
            "/api/chat",
            json={"message": "найди в интернете курс доллара на сегодня"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        ingestion = response.json().get("ingestion") or {}
        assert ingestion.get("queued_for_review") is False
        assert ingestion.get("category") == "web_request", ingestion

        after = len([row for row in app.state.storage.execute("SELECT id FROM inbox WHERE status='pending'")])
        assert after == before, "просьба поискать всё-таки осела во «Входящих»"


def test_an_ordinary_message_still_reaches_the_archive(settings):
    """Обратная сторона: обычное сообщение по-прежнему проходит приём."""
    from fastapi.testclient import TestClient

    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        response = client.post(
            "/api/chat",
            json={"message": "Договорились перенести поверку приборов на 12 мая, отвечает Проскурин."},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        ingestion = response.json().get("ingestion") or {}
        assert ingestion.get("category") != "web_request", ingestion


@pytest.mark.anyio
async def test_an_encyclopedia_fallback_is_never_passed_off_as_fresh(monkeypatch):
    """Мутация: убрать оговорку про энциклопедию — тест краснеет.

    Wikipedia — последнее звено цепочки: она отвечает 10/10, когда бесплатные
    HTML-провайдеры при серии запросов дают 1-2 из 20. Но на «какая завтра
    погода» её статья не ответ, и выдавать справочник за свежую выдачу нельзя.
    """
    import friday.agent_runtime as runtime_module

    class _Result:
        success = True
        attachment = None
        data = {"sources": [{"url": "https://ru.wikipedia.org/wiki/Погода", "title": "Погода", "text": "…"}]}

        def to_llm_message(self) -> str:
            return "Результат web_research: статья «Погода»"

    class _Kernel:
        async def execute(self, name, arguments, *, actor):  # noqa: ANN001, ARG002
            return _Result()

    class _NoLLM:
        enabled = False

    runtime = object.__new__(runtime_module.AgentRuntime)
    runtime.kernel = _Kernel()
    runtime.llm = _NoLLM()
    messages: list[dict] = []
    await runtime_module.AgentRuntime._prefetch_the_web_if_asked(  # noqa: SLF001
        runtime,
        "найди в интернете, какая завтра погода",
        actor=None,
        tools=[{"function": {"name": "web_research"}}],
        messages=messages,
        tools_used=[],
        tool_evidence=[],
    )
    assert messages
    content = messages[0]["content"]
    assert "ЭНЦИКЛОПЕДИИ" in content, "справочная статья выдана за свежую выдачу"
    assert "свежих данных получить не удалось" in content


@pytest.mark.anyio
async def test_an_ordinary_result_carries_no_encyclopedia_caveat(monkeypatch):
    """Контроль: обычная выдача не обрастает оговоркой."""
    import friday.agent_runtime as runtime_module

    class _Result:
        success = True
        attachment = None
        data = {"sources": [{"url": "https://cbr.ru/", "title": "Ставка", "text": "14%"}]}

        def to_llm_message(self) -> str:
            return "Результат web_research: ставка 14%"

    class _Kernel:
        async def execute(self, name, arguments, *, actor):  # noqa: ANN001, ARG002
            return _Result()

    class _NoLLM:
        enabled = False

    runtime = object.__new__(runtime_module.AgentRuntime)
    runtime.kernel = _Kernel()
    runtime.llm = _NoLLM()
    messages: list[dict] = []
    await runtime_module.AgentRuntime._prefetch_the_web_if_asked(  # noqa: SLF001
        runtime,
        "найди в интернете ключевую ставку",
        actor=None,
        tools=[{"function": {"name": "web_research"}}],
        messages=messages,
        tools_used=[],
        tool_evidence=[],
    )
    assert messages and "ЭНЦИКЛОПЕДИИ" not in messages[0]["content"]


@pytest.mark.anyio
async def test_the_person_sees_what_went_out_to_the_search_engine(monkeypatch):
    """Мутация: убрать `web_query_notice` — тест краснеет.

    В неудаляемый журнал запрос класть нельзя: туда попадёт и «пароль от роутера
    …». Но хеш никого не спасает — когда детектор намерения ошибается (а он
    ошибался дважды за двое суток, утащив наружу пересланный приказ и фамилию
    сотрудника), владелец должен УВИДЕТЬ это рядом с ответом и возразить, а не
    узнать через месяц.
    """
    import friday.agent_runtime as runtime_module

    class _Result:
        success = True
        attachment = None
        data = {"results": [{"url": "https://cbr.ru/", "title": "Ставка"}]}

        def to_llm_message(self) -> str:
            return "Результат web_research: 14%"

    class _Kernel:
        async def execute(self, name, arguments, *, actor):  # noqa: ANN001, ARG002
            return _Result()

    class _NoLLM:
        enabled = False

    runtime = object.__new__(runtime_module.AgentRuntime)
    runtime.kernel = _Kernel()
    runtime.llm = _NoLLM()
    notice: list[str] = []
    await runtime_module.AgentRuntime._prefetch_the_web_if_asked(  # noqa: SLF001
        runtime,
        "найди в интернете ключевую ставку ЦБ",
        actor=None,
        tools=[{"function": {"name": "web_research"}}],
        messages=[],
        tools_used=[],
        tool_evidence=[],
        notice=notice,
    )
    assert notice, "человеку не сказали, что именно ушло наружу"
    assert "ключевую ставку ЦБ" in notice[0]
    assert notice[0].startswith("🔎")


def test_the_notice_reaches_the_answer_and_the_bridge():
    """Работающее уведомление, которое никто не показывает, — не уведомление."""
    import inspect

    from friday.agent_runtime import AgentRuntime
    from friday.telegram_bridge._callbacks import CallbacksMixin

    chat_source = inspect.getsource(AgentRuntime.chat)
    # Именно в ВОЗВРАЩАЕМОМ словаре, а не где-то в теле. Первая редакция этого
    # теста проверяла просто наличие строки — и осталась зелёной, когда ключ по
    # ошибке попал в метаданные сохраняемого сообщения: уведомление писалось в
    # базу и не доходило до человека ни одним из путей.
    last_return = chat_source.rindex("return {")
    assert '"web_query_notice": str(response.get("web_query_notice") or "")' in chat_source[last_return:], (
        "уведомление не попало в ответ, который получает человек"
    )

    bridge_source = inspect.getsource(CallbacksMixin._format_response_message)  # noqa: SLF001
    assert 'response.get("web_query_notice")' in bridge_source, (
        "мост не показывает человеку, что ушло в поисковик"
    )
