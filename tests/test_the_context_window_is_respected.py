"""Окно модели — 32 768 токенов, и это надо учитывать, а не надеяться.

Заказ владельца 2026-08-02: «у модели контекстное окно всего 32768 токенов, надо
высчитать возможные последствия и придумать как с этим бороться».

Посчитано на живой сборке. Тяжёлый ход: описания инструментов 4 650, результат
инструмента до 4 000, выдача поиска до 4 000, история, найденные документы,
системный промпт — около 21 000 токенов промпта плюс 2 048 на ответ. Один вызов
проходит, но в диалоге их до четырёх, в исследовании до двенадцати, и каждый
добавляет свой результат в ту же ленту: четыре — уже впритык, двенадцать —
переполнение.

Механизм ужимания существовал и работал, но МОЛЧАЛ: старые части разговора
выбрасывались без строки в журнал и без слова модели. Со стороны это выглядит как
«Пятница забыла, о чём мы говорили пять минут назад».

Здесь закреплены четыре меры: молчаливый обрез стал слышным, модель узнаёт о
потере, история считается по знакам, а описания инструментов сокращаются по виду
вопроса.
"""

from __future__ import annotations

import inspect

from friday.agent_runtime import _HISTORY_CHAR_BUDGET, _history_within_budget
from friday.agent_runtime.llm import _fit_messages_to_context


def _turn(role: str, size: int) -> dict[str, str]:
    return {"role": role, "content": "я" * size}


def test_history_is_measured_in_characters_not_in_turns() -> None:
    """Мутация: вернуть `[-10:]` — тест краснеет.

    Десять коротких реплик — шестьсот знаков, десять разборов документа —
    двадцать тысяч. Считать надо то, что дорого.
    """
    fat = [_turn("user" if index % 2 == 0 else "assistant", 3_000) for index in range(10)]
    selected = _history_within_budget(fat)
    spent = sum(len(item["content"]) for item in selected)
    assert spent <= _HISTORY_CHAR_BUDGET + 3_000, f"история заняла {spent} знаков"
    assert len(selected) < 10, "длинные ходы прошли все — бюджет не работает"


def test_short_turns_are_kept_generously() -> None:
    """Короткий разговор целиком помещается — сокращать нечего."""
    thin = [_turn("user" if index % 2 == 0 else "assistant", 40) for index in range(12)]
    assert len(_history_within_budget(thin)) == 12


def test_the_newest_turn_is_never_dropped() -> None:
    """Даже один огромный ход остаётся: на него человек и ссылается словом «это»."""
    huge = [_turn("user", 50_000)]
    assert len(_history_within_budget(huge)) == 1


def test_the_order_of_the_conversation_survives() -> None:
    history = [
        {"role": "user", "content": "первое"},
        {"role": "assistant", "content": "второе"},
        {"role": "user", "content": "третье"},
    ]
    assert [item["content"] for item in _history_within_budget(history)] == ["первое", "второе", "третье"]


def test_service_roles_are_not_counted_as_conversation() -> None:
    history = [{"role": "system", "content": "служебное"}, {"role": "user", "content": "вопрос"}]
    assert [item["role"] for item in _history_within_budget(history)] == ["user"]


def test_the_model_is_told_when_the_start_did_not_fit() -> None:
    """Мутация: убрать вставку — тест краснеет.

    Без этого модель отвечает так, будто помнит весь разговор, и человек получает
    уверенное «мы этого не обсуждали» про сказанное десять минут назад.
    """
    messages = [{"role": "system", "content": "правила"}]
    messages += [_turn("user" if index % 2 == 0 else "assistant", 20_000) for index in range(8)]

    fitted = _fit_messages_to_context(messages, max_model_len=32_768, max_output_tokens=2_048)

    notices = [
        item
        for item in fitted
        if item.get("role") == "system" and "не поместилось" in str(item.get("content"))
    ]
    assert notices, "модель не знает, что видит не весь разговор"
    assert fitted[0]["content"] == "правила", "системные правила перестали идти первыми"


def test_a_fitting_prompt_gets_no_notice() -> None:
    """Когда всё поместилось, лишнего сообщения быть не должно."""
    messages = [{"role": "system", "content": "правила"}, _turn("user", 100)]
    fitted = _fit_messages_to_context(messages, max_model_len=32_768, max_output_tokens=2_048)
    assert not any("не поместилось" in str(item.get("content")) for item in fitted)


def test_tool_descriptions_shrink_for_an_unrelated_topic() -> None:
    """Инструмент остаётся ДОСТУПНЫМ, сокращается только его описание."""
    from friday.execution_kernel import ToolSpec

    spec = ToolSpec(
        name="resolve_duplicates",
        description=(
            "Разобрать очередь предложенных слияний сущностей. Показывает пары, у которых "
            "совпали имена, и просит решить по каждой. Пара, помеченная «не дубликат», "
            "больше не предлагается."
        ),
        security_id="kg.merge",
        parameters={"type": "object", "properties": {}},
        risk="observe",
    )
    full = spec.to_openai()["function"]["description"]
    brief = spec.to_openai(brief=True)["function"]["description"]

    assert len(brief) < len(full) / 2, f"короткая форма не короче: {len(brief)} против {len(full)}"
    assert len(brief) <= 91
    assert spec.to_openai(brief=True)["function"]["name"] == "resolve_duplicates"


def test_the_turn_passes_the_topic_to_the_kernel() -> None:
    """Проверяется подключённое: вид вопроса доходит до выбора описаний."""
    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime.chat)
    assert "get_tool_definitions(actor, topic=topic)" in source
    assert "context.outward_verdict" in source
