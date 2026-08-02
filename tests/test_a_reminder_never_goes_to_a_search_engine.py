"""Просьба напомнить — дело помощника, а не запрос в поисковик.

Замерено на недельном прогоне 2026-08-02, уже на быстрой модели: «Напомни мне в
среду созвон с подрядчиком» ушло в интернет строкой «созвон с подрядчиком среда».
Цена двойная — напоминание не поставлено, и дело человека вместе с днём недели
оказалось в чужом поисковике.

Арбитр счёл это вопросом о внешнем мире, и уговаривать его бессмысленно: решение
остаётся за моделью, а просьба человека однозначна. Здесь решает структура.

Рядом — вторая находка того же прогона. После «что там по поверке приборов» →
«а сроки какие?» → «кто этим занимается?» арбитр, видя лишь предыдущую реплику,
составил запрос «кто занимается сроками доставки»: тему он достроил сам, и не ту.
Реплика-продолжение так же безтемна, как и текущая, поэтому передаются ДВЕ.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime, _ASKS_FOR_A_REMINDER


@pytest.mark.parametrize(
    "message",
    [
        "Напомни мне в среду созвон с подрядчиком",
        "напомни завтра позвонить в автосервис",
        "не дай забыть про отчёт в пятницу",
        "разбуди меня в 7",
        "предупреди меня о встрече",
        "поставь напоминание на понедельник",
    ],
)
def test_a_reminder_request_is_recognised(message: str) -> None:
    assert _ASKS_FOR_A_REMINDER.search(message), message


@pytest.mark.parametrize(
    "message",
    ["что там по поверке", "найди в интернете курс евро", "какая завтра погода"],
)
def test_an_ordinary_question_is_not_mistaken_for_one(message: str) -> None:
    assert not _ASKS_FOR_A_REMINDER.search(message), message


class _Kernel:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        self.queries.append(str(params.get("query") or ""))
        raise AssertionError("до поиска дойти не должно")


class _Runtime:
    def __init__(self) -> None:
        self.kernel = _Kernel()
        self.arbiter_calls = 0

    async def _web_query_by_arbiter(self, message, *, previous_turn=""):  # noqa: ANN001, ARG002
        self.arbiter_calls += 1
        return ("интернет", "созвон с подрядчиком среда")

    async def _mentions_someone_from_the_archive(self, message, actor):  # noqa: ANN001, ARG002
        return False


def test_the_turn_never_searches_the_web_for_a_reminder() -> None:
    """Мутация: убрать защиту — тест краснеет, и дело человека уходит наружу."""
    runtime = _Runtime()
    context = AgentContext(conversation_id="conv", user_id="boss")
    context.outward_verdict = ("интернет", "созвон с подрядчиком среда")

    bound = AgentRuntime._prefetch_the_web_if_asked.__get__(runtime, AgentRuntime)
    asyncio.run(
        bound(
            "Напомни мне в среду созвон с подрядчиком",
            None,
            [{"function": {"name": "web_research"}}],
            [],
            [],
            [],
            notice=[],
            context=context,
        )
    )

    assert runtime.kernel.queries == [], "просьба о напоминании ушла в поисковик"


def test_the_arbiter_sees_two_turns_not_one() -> None:
    """Проверяется подключённое: тема живёт раньше по разговору."""
    source = inspect.getsource(AgentRuntime._prepare_context)
    window = source[source.index("spoken: list[str] = []") : source.index("arbiter = asyncio.create_task")]
    assert "len(spoken) == 2" in window, "арбитру снова передают одну реплику — темы в ней нет"
    assert "reversed(spoken)" in window, "порядок реплик перевёрнут — разговор читается задом наперёд"
