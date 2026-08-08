"""Результат инструмента ужимается ПОСЛЕ того, как модель его прочитала.

Ужимание отработавших результатов появилось затем, чтобы разговор помещался в
окно: один результат доходит до четырёх тысяч знаков, а в режиме исследования
вызовов до двенадцати. Правило верное, а стояло не в том месте — ВНУТРИ цикла по
вызовам одного хода. При двух параллельных вызовах второй ужимал результат
первого до 900 знаков прежде, чем модель увидела его хоть раз.

Замерено на стенде до правки: c1 = 948 знаков, видна одна запись из десяти;
c2 = 8 212 знаков, все десять. И перезапросить инструмент модели нечем — предел
вызовов на ход к этому моменту уже потрачен, цикл обрывается.

Слово «параллельных» тут по смыслу, а не по способу исполнения: вызовы одного
ответа модели идут последовательно, но читает она их вместе, следующим ходом. У
них нет старшего — поэтому при нехватке места режется КАЖДЫЙ до равной доли, а не
первый в огрызок при целом последнем.

Проверяется то, что получает МОДЕЛЬ: снимок `messages` на каждом обращении к ней.
"""

from __future__ import annotations

import copy

import pytest

from friday.agent_runtime import _ROUND_TOOL_BUDGET_CHARS, _SPENT_TOOL_RESULT_CHARS

#: Опознавательная метка в КАЖДОЙ записи: по ней видно, докуда дочитано.
_ROWS = 10


def _bulky(tool: str, row_chars: int = 700) -> str:
    return "\n".join(f"[{tool}#{index}] " + "з" * row_chars for index in range(_ROWS))


class _TwoCallsThenAnswer:
    """Первый ход — два вызова разом, второй — итоговый ответ."""

    enabled = True
    total_budget_sec = 120.0

    def __init__(self, calls_in_round: int = 2) -> None:
        self.rounds = 0
        self.seen: list[list[dict]] = []
        self._calls_in_round = calls_in_round

    async def chat(self, messages, *, temperature=None, max_tokens=None, tools=None):
        self.seen.append(copy.deepcopy(messages))
        self.rounds += 1
        if self.rounds == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{index}",
                        "function": {"name": "memory_search", "arguments": '{"query": "x"}'},
                    }
                    for index in range(self._calls_in_round)
                ],
                "_queue_wait_sec": 0.0,
            }
        return {"content": "готово", "tool_calls": None, "_queue_wait_sec": 0.0}


class _BulkyKernel:
    """Ядро-заглушка: каждый вызов отдаёт объёмный результат с метками записей."""

    def __init__(self, row_chars: int = 700) -> None:
        self.calls = 0
        self._row_chars = row_chars

    async def execute(self, name, arguments, *, actor=None):
        from friday.execution_kernel import ToolResult

        self.calls += 1
        result = ToolResult(name, True, data={})
        body = _bulky(f"{name}{self.calls}", self._row_chars)
        result.to_llm_message = lambda body=body: body  # type: ignore[method-assign]
        return result


async def _run(settings, storage, llm, kernel):
    from friday.agent_runtime import AgentContext, AgentRuntime
    from friday.permissions import ActorContext

    storage.ensure_user("alice")
    agent = AgentRuntime(settings, storage, llm=llm, kernel=kernel)
    actor = ActorContext(user_id="alice", preset_key="owner", source="api")
    context = AgentContext(conversation_id="conv-test", user_id="alice", interaction_mode="dialogue")
    return await agent._agentic_loop(  # noqa: SLF001
        context,
        "найди по двум запросам",
        actor,
        tools=[
            {
                "type": "function",
                "function": {"name": "memory_search", "parameters": {"type": "object"}},
            }
        ],
        attachments=None,
    )


def _tool_messages(snapshot: list[dict]) -> list[str]:
    return [str(item.get("content") or "") for item in snapshot if item.get("role") == "tool"]


@pytest.mark.asyncio
async def test_both_parallel_results_reach_the_model_whole(settings, storage):
    """Мутация: вернуть ужимание внутрь цикла по вызовам — тест краснеет.

    Результаты подобраны так, чтобы ВМЕСТЕ они помещались в бюджет хода: здесь
    проверяется место правила, а не само правило.
    """
    llm = _TwoCallsThenAnswer()
    await _run(settings, storage, llm, _BulkyKernel(row_chars=200))

    assert len(llm.seen) >= 2, "второго обращения к модели не было"
    shown = _tool_messages(llm.seen[1])
    assert len(shown) == 2, f"модель увидела не оба результата: {len(shown)}"
    assert sum(len(body) for body in shown) < _ROUND_TOOL_BUDGET_CHARS, "стенд собран неверно"
    for index, body in enumerate(shown):
        assert f"#{_ROWS - 1}]" in body, f"результат {index} обрезан до первого показа модели"


@pytest.mark.asyncio
async def test_a_crowded_round_shares_the_budget_evenly(settings, storage):
    """Много вызовов в одном ходе не переполняют окно — и не жертвуют первым.

    Мутация: убрать бюджет раунда — сумма улетает за предел.
    """
    llm = _TwoCallsThenAnswer(calls_in_round=4)
    await _run(settings, storage, llm, _BulkyKernel())

    shown = _tool_messages(llm.seen[1])
    assert len(shown) == 4, "предел вызовов режима изменился — стенд надо пересобрать"
    assert sum(len(body) for body in shown) <= _ROUND_TOOL_BUDGET_CHARS + 4 * 120, "бюджет хода превышен"
    lengths = {len(body) for body in shown}
    assert len(lengths) == 1, f"результаты одного хода урезаны по-разному: {sorted(lengths)}"
    assert min(len(body) for body in shown) > _SPENT_TOOL_RESULT_CHARS, "доля меньше, чем у отработавших"


@pytest.mark.asyncio
async def test_the_model_is_told_which_calls_were_dropped(settings, storage):
    """Вызовы сверх предела режима отбрасывались молча.

    Модель просила восемь, исполнялись четыре, и дальше она отвечала так, будто
    спросила ровно то, что получила. Обрезано не содержимое, а намерение —
    поэтому по одним результатам заметить потерю нельзя.

    Мутация: убрать сообщение об отброшенных — тест краснеет.
    """
    llm = _TwoCallsThenAnswer(calls_in_round=8)
    await _run(settings, storage, llm, _BulkyKernel(row_chars=100))

    whole = "\n".join(str(item.get("content") or "") for item in llm.seen[1])
    assert "выполнено 4" in whole, f"модель не узнала об отброшенных вызовах: {whole[-400:]!r}"
    assert "(8)" in whole, "не сказано, сколько было запрошено"


@pytest.mark.asyncio
async def test_a_result_from_a_past_round_is_squeezed(settings, storage):
    """Обратная сторона: отработавшее ужимать НУЖНО, иначе окно кончится.

    Мутация: убрать ужимание прежних результатов вовсе — тест краснеет.
    """

    class _ThreeRounds(_TwoCallsThenAnswer):
        async def chat(self, messages, *, temperature=None, max_tokens=None, tools=None):
            self.seen.append(copy.deepcopy(messages))
            self.rounds += 1
            if self.rounds <= 2:
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call_r{self.rounds}",
                            "function": {"name": "memory_search", "arguments": '{"query": "x"}'},
                        }
                    ],
                    "_queue_wait_sec": 0.0,
                }
            return {"content": "готово", "tool_calls": None, "_queue_wait_sec": 0.0}

    llm = _ThreeRounds()
    await _run(settings, storage, llm, _BulkyKernel())

    последний_снимок = llm.seen[-1]
    bodies = _tool_messages(последний_снимок)
    assert len(bodies) == 2, "ожидались результаты двух ходов"
    assert len(bodies[0]) <= _SPENT_TOOL_RESULT_CHARS + 80, "отработавший результат остался целым"
    assert f"#{_ROWS - 1}]" in bodies[1], "свежий результат ужат вместе с отработавшим"
