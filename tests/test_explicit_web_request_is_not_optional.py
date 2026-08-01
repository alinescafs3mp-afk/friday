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

    runtime = object.__new__(runtime_module.AgentRuntime)
    runtime.kernel = _Kernel()
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
    assert messages and "14,00%" in messages[0]["content"], "выдача не дошла до модели"
    assert "не подменяй" in messages[0]["content"] or "не выдумывай" in messages[0]["content"]


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
    assert body.startswith("_prefetch_the_web_if_asked(message"), (
        "в предварительный поиск уходит не сообщение человека"
    )


@pytest.mark.anyio
async def test_nothing_happens_without_the_tool(monkeypatch):
    """Права важнее удобства: нет инструмента — нет обхода."""
    import friday.agent_runtime as runtime_module

    class _Kernel:
        async def execute(self, name, arguments, *, actor):  # pragma: no cover - не должен зваться
            raise AssertionError("инструмент недоступен, а его всё равно позвали")

    runtime = object.__new__(runtime_module.AgentRuntime)
    runtime.kernel = _Kernel()
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
