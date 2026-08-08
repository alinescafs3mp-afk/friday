"""Один заход на исправление — и ни одним больше.

Спека v3 §5: «A result can receive at most a bounded repair pass after failed
verification; the system must not loop until it can claim success.»

Существенное здесь не «починить», а «не крутиться». Система, переписывающая
ответ до тех пор, пока проверка не согласится, в конце концов получит согласие —
и это будет означать лишь то, что она подобрала формулировку, а не то, что ответ
стал верным.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime import (
    VERDICT_FAILED,
    VERDICT_PASSED,
    AgentContext,
    AgentRuntime,
)


class _Router:
    """Модель, которая считает свои вызовы."""

    enabled = True

    def __init__(self, reply: str = "Исправленный ответ, опирающийся на записи целиком.") -> None:
        self.calls = 0
        self.reply = reply

    async def chat(self, messages, tools=None):  # noqa: ANN001, ARG002
        self.calls += 1
        return {"content": self.reply}


def _context() -> AgentContext:
    return AgentContext(
        conversation_id="c1",
        user_id="alice",
        knowledge_hits=[{"title": "Приказ 214", "snippet": "О назначении Хасанова командиром расчёта"}],
    )


def _runtime(router: _Router) -> AgentRuntime:
    runtime = object.__new__(AgentRuntime)
    runtime.llm = router
    return runtime


@pytest.mark.anyio
async def test_a_failed_verification_gets_exactly_one_attempt():
    router = _Router()
    runtime = _runtime(router)

    fixed = await runtime._repair_once(  # noqa: SLF001
        "кто командир расчёта?",
        "Командиром расчёта назначен Иванов.",
        _context(),
        {"status": VERDICT_FAILED, "issues": ["записи называют другого человека"]},
    )

    assert fixed.startswith("Исправленный ответ")
    assert router.calls == 1, "починка стоит ровно одного обращения к модели"


@pytest.mark.anyio
async def test_without_named_issues_there_is_nothing_to_repair():
    """Починка без указания, что именно не так, — это переписывание наугад."""
    router = _Router()
    runtime = _runtime(router)

    assert (
        await runtime._repair_once(  # noqa: SLF001
            "вопрос", "ответ", _context(), {"status": VERDICT_FAILED, "issues": []}
        )
        == ""
    )
    assert router.calls == 0


@pytest.mark.anyio
async def test_a_truncated_repair_is_not_an_improvement():
    """Мутация: принимать любой непустой результат — тест краснеет.

    Обрубок вместо ответа хуже исходного текста с честным предупреждением:
    человек теряет содержание и не получает взамен ничего.
    """
    router = _Router(reply="Не могу.")
    runtime = _runtime(router)

    long_answer = "Командиром расчёта назначен Иванов. " * 10
    assert (
        await runtime._repair_once(  # noqa: SLF001
            "кто командир?", long_answer, _context(), {"status": VERDICT_FAILED, "issues": ["не тот человек"]}
        )
        == ""
    )


@pytest.mark.anyio
async def test_a_dead_model_leaves_the_answer_alone():
    class _Dead(_Router):
        async def chat(self, messages, tools=None):  # noqa: ANN001, ARG002
            self.calls += 1
            raise RuntimeError("endpoint down")

    router = _Dead()
    runtime = _runtime(router)
    assert (
        await runtime._repair_once(  # noqa: SLF001
            "вопрос",
            "достаточно длинный исходный ответ про записи",
            _context(),
            {"status": VERDICT_FAILED, "issues": ["что-то не так"]},
        )
        == ""
    )


def test_the_loop_cannot_run_twice():
    """Мутация: обернуть починку в цикл — тест краснеет.

    Проверяется ФОРМА кода, потому что именно она задаёт границу: в `chat`
    ремонт вызывается один раз и повторная проверка после него одна.
    """
    import inspect

    source = inspect.getsource(AgentRuntime.chat)
    assert source.count("_repair_once(") == 1, "починка вызывается больше одного раза"
    # Условие смотрится ПЕРЕД вызовом: первая редакция теста брала текст после
    # него и мутацию `if` → `while` не ловила.
    head = source[: source.index("_repair_once(")]
    guard_line = head.rstrip().splitlines()[-2:]
    assert not any("while" in line for line in guard_line), (
        "починка обёрнута в цикл — система будет крутиться, пока проверка не согласится"
    )
    repair_start = source.index("_repair_once(")
    repair_end = source.index("attachment_expected_count", repair_start)
    repair_block = source[repair_start:repair_end]
    assert repair_block.count("_verify_response(") == 1, (
        "после починки проверка повторяется больше одного раза"
    )


def test_a_passed_verification_is_left_alone():
    """Чинить нечего: проверка согласилась."""
    import inspect

    source = inspect.getsource(AgentRuntime.chat)
    guard = source[: source.index("_repair_once(")]
    assert f"== {VERDICT_FAILED!r}" in guard or "VERDICT_FAILED" in guard, (
        "починка запускается не только на провалившейся проверке"
    )
    assert VERDICT_PASSED in {"passed"}
