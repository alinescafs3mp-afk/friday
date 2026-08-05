"""Решение об источнике одно, а не пять независимых.

Задача владельца: «научить надёжно отличать намерение пользователя, что он хочет
прямо сейчас и куда за этим лезть. Собственная память, интернет, архив».

Замер по матрице из 26 запросов (пять направлений, три прогона на каждый) нашёл
корень непредсказуемости. «Устал сегодня» устойчиво — ТРИ РАЗА ИЗ ТРЁХ —
поднимало ленту событий архива, хотя основной арбитр на эту фразу отвечает
«быт», тоже три раза из трёх.

Механизм: слово «сегодня» даёт время, дальше спрашивается ОТДЕЛЬНЫЙ маленький
арбитр «это вопрос о ленте?», и он говорит «да». Каждое решение по отдельности
разумно, а вместе они противоречат друг другу — потому что принимаются
независимо и ничего не знают о вердикте.

Здесь они связаны: вердикт основного арбитра сильнее шаблона времени.

Замер до/после (26 запросов × 3 прогона):
    было   23/26 «туда», 2 устойчиво не туда, 1 плавает
    стало  см. коммит — «устал сегодня» перестало ходить в архив
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime


class _Kernel:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, tool: str, params: dict, actor=None):  # noqa: ANN001, ARG002
        self.calls.append(tool)

        class _Result:
            success = True
            data: dict = {}

            def to_llm_message(self) -> str:
                return "лента"

        return _Result()


def _run(message: str, kind: str) -> list[str]:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    runtime.llm = None
    context = AgentContext(conversation_id="c", user_id="u", outward_verdict=(kind, None))
    bound = AgentRuntime._prefetch_the_timeline_if_asked.__get__(runtime, AgentRuntime)
    asyncio.run(
        bound(
            message,
            None,
            [{"function": {"name": "what_happened"}}],
            [],
            [],
            [],
            context,
        )
    )
    return runtime.kernel.calls


@pytest.mark.parametrize("kind", ["быт", "действие", "интернет"])
def test_a_settled_verdict_stops_the_timeline(kind: str) -> None:
    """Мутация: убрать проверку вердикта — «устал сегодня» снова идёт в архив."""
    assert _run("устал сегодня", kind) == [], f"вид «{kind}» всё ещё поднимает ленту"


def test_a_real_timeline_question_still_works() -> None:
    """Обратная сторона: «что было 26 июля» обязано поднимать ленту.

    Замерено раньше: без предварительного вызова модель рассказывала про 26 июля
    ДРУГОГО года по документу, где эта дата упомянута.
    """
    assert _run("что было 26 июля", "архив") == ["what_happened"]


def test_a_question_without_a_verdict_behaves_as_before() -> None:
    """Старые вызовы без контекста не должны сломаться."""
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    bound = AgentRuntime._prefetch_the_timeline_if_asked.__get__(runtime, AgentRuntime)
    asyncio.run(bound("что было 26 июля", None, [{"function": {"name": "what_happened"}}], [], [], []))
    assert runtime.kernel.calls == ["what_happened"]


def test_the_verdict_is_checked_before_anything_else() -> None:
    """Порядок и есть смысл: проверка стоит до разбора времени.

    Иначе на бытовую фразу всё равно тратился бы вызов маленького арбитра
    «это вопрос о ленте?» — лишняя секунда там, где ответ уже известен.
    """
    source = inspect.getsource(AgentRuntime._prefetch_the_timeline_if_asked)
    assert source.index('kind.startswith(("быт"') < source.index("period_from_question")


def test_the_context_reaches_the_prefetch() -> None:
    """Проверяется подключённое: боевой цикл передаёт вердикт, а не зовёт вслепую."""
    loop = inspect.getsource(AgentRuntime._agentic_loop)
    at = loop.index("_prefetch_the_timeline_if_asked(")
    assert "context" in loop[at : at + 200], "вердикт до ленты не доезжает"
