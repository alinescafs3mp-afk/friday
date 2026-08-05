"""Про собственное устройство Пятница говорит правду, а не самоописание модели.

Найдено ВЛАДЕЛЬЦЕМ в живой переписке 2026-08-03. На «ты же знаешь, что является
твоим ядром? и крутится это на видеокарте стоимостью с автомобиль» пришло:

    «Моё ядро — это GPT-4o … обученная компанией OpenAI»
    «сервер на базе NVIDIA H100 или A100 … Одна такая карта стоит от $25,000»
    «Вся эта мощь крутится в дата-центрах»

Ни одно слово не верно. Ядро — qwen3.6-35b-a3b на vLLM, эндпойнт в локальной сети
владельца, железо — его собственная видеокарта. Метаданные хода: `knowledge_hits`
0, `tools_used` пуст, проверка пропущена — ни поиска, ни инструментов, ни судьи.
Ответ целиком из обучающих данных: семейство Qwen училось в том числе на текстах,
порождённых моделями OpenAI, и «помнит» себя как их продукт.

ПЯТЫЙ замеренный случай, когда уговоры не работают. Системный промпт ПЕРВОЙ
СТРОКОЙ говорит «локальная персональная Knowledge OS» — и это не помешало сказать
«в дата-центрах».

Цена ошибки не косметическая. Человеку, чья система построена вокруг «данные
никуда не уходят», сказали, что он общается с продуктом OpenAI в дата-центре.

ГРАНИЦА ПРАВДЫ здесь так же важна, как сама правда: говорится только то, что
система ЗНАЕТ. Имя модели и адрес эндпойнта лежат в настройках; модели видеокарты
там нет — и подставлять свою выдумку вместо чужой нельзя.
"""

from __future__ import annotations

import asyncio

import pytest

from friday.agent_runtime import AgentRuntime, _self_description
from friday.permissions import ActorContext

INVENTS_ITS_CORE = (
    "Моё ядро — это GPT-4o, обученная компанией OpenAI. Крутится в дата-центрах "
    "на NVIDIA H100 стоимостью от $25,000."
)


class _Hostile:
    """Модель, отвечающая ровно то, что ответила живая."""

    enabled = True
    total_budget_sec = 5.0

    def __init__(self, *, rest: str | None = "") -> None:
        self.rest = rest
        self.final_calls = 0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        asked = " ".join(str(m.get("content") or "") for m in messages)
        if "РАЗГОВОР или ЗАПРОС" in asked:
            return {"content": "ЗАПРОС"}
        if '"вид": "интернет' in asked:
            return {"content": '{"вид": "знание", "правило": "", "запрос": "", "кто": "", "дни": []}'}
        if '"остаток"' in asked and "уже решена" in asked:
            return {"content": '{"остаток": "%s"}' % ("" if self.rest is None else self.rest)}
        self.final_calls += 1
        return {"content": INVENTS_ITS_CORE}


def _answer(settings, storage, llm: _Hostile, message: str) -> str:
    storage.ensure_user("alice")
    agent = AgentRuntime(settings, storage)
    agent.llm = llm
    actor = ActorContext(user_id="alice", preset_key="user", source="test")
    result = asyncio.run(agent.chat("alice", message, actor=actor, enable_tools=False))
    return str(result.get("message") or "")


@pytest.mark.parametrize(
    "asked",
    [
        "ты же знаешь, что является твоим ядром?",
        "какая ты модель?",
        "на чём ты работаешь?",
        "кто тебя обучил?",
        "на каком железе ты крутишься?",
    ],
)
def test_it_does_not_call_itself_someone_elses_product(settings, storage, asked) -> None:
    """Мутация: отдать текст модели — «GPT-4o» и «дата-центры» возвращаются."""
    said = _answer(settings, storage, _Hostile(), asked)

    lowered = said.casefold()
    assert "gpt-4o" not in lowered, f"чужое имя выдано за своё: {said!r}"
    assert "openai" not in lowered, f"чужой владелец выдан за своего: {said!r}"
    assert "дата-центр" not in lowered, f"локальная система названа облачной: {said!r}"


def test_the_model_is_not_asked_about_itself(settings, storage) -> None:
    """Ход модели здесь — единственная дверь, через которую входит выдумка."""
    llm = _Hostile()

    _answer(settings, storage, llm, "что является твоим ядром?")

    assert llm.final_calls == 0, "модели дали слово о том, чего она о себе не знает"


def test_a_question_beside_it_is_still_answered(settings, storage) -> None:
    """Половина реплики решена структурой, половина — нет.

    Общий разбор остатка нужен именно здесь: собственного поля «остаток» у арбитра
    видов нет, а терять заданный рядом вопрос нельзя.
    """
    llm = _Hostile(rest="и что там по отчёту за июль")

    said = _answer(settings, storage, llm, "а какая ты модель? и что там по отчёту за июль")

    assert llm.final_calls == 1, "вопрос человека потерян МОЛЧА"
    assert "gpt-4o" not in said.casefold(), "выдумка вернулась вместе с остатком"


def test_an_unrecognised_question_is_caught_on_the_way_out(settings, storage) -> None:
    """Второй рубеж: спросили так, как список не знает, — ложь ловится в тексте.

    Список формулировок взял три варианта из пяти в первой редакции, и это
    свойство списков, замеренное здесь и раньше. Поэтому у промаха есть второй
    шанс: готовый ответ, объявляющий себя чужим продуктом, заменяется правдой.
    """
    said = _answer(settings, storage, _Hostile(), "слушай, а ты вообще что такое?")

    lowered = said.casefold()
    assert "gpt-4o" not in lowered and "openai" not in lowered, said
    assert "не знаю" in lowered, "подменили ложь, но не сказали правду"


def test_talking_about_other_models_is_left_alone(settings, storage) -> None:
    """Обратная сторона: рассказ О ЧУЖОЙ модели — законный ответ.

    Рубеж требует САМОССЫЛКИ. Без этого «расскажи, что такое GPT-4o» подменялось
    бы описанием собственного устройства — и лечение стало бы хуже болезни.
    """
    from friday.agent_runtime import _CALLS_ITSELF_SOMEONE_ELSE

    innocent = (
        "GPT-4o — это модель OpenAI, вышедшая в 2024 году. Работает в дата-центрах на ускорителях NVIDIA."
    )

    assert not _CALLS_ITSELF_SOMEONE_ELSE.search(innocent), "подменили бы законный ответ"


def test_it_says_only_what_it_knows(settings) -> None:
    """Своя выдумка вместо чужой — не лечение.

    Имя модели и адрес эндпойнта лежат в настройках. Модели видеокарты там нет, и
    называть её нельзя ни при каких обстоятельствах.
    """
    said = _self_description(settings, served_name="qwen3.6-35b-a3b")

    assert "qwen3.6-35b-a3b" in said, "настоящее имя модели не названо"
    for invented in ("h100", "a100", "rtx", "5090", "$"):
        assert invented not in said.casefold(), f"выдумано железо: {invented}"


def test_a_local_endpoint_promises_the_data_stays(settings) -> None:
    """Главное, что человеку нужно знать: данные не уходят наружу.

    Проверяется именно ОБЕЩАНИЕ, а не слово «локальный»: оно встречается в тексте
    дважды, и первая редакция теста проходила даже с выброшенной фразой про
    приватность. Показала мутация — ровно тот же промах, что был с датами архива.
    """
    said = _self_description(settings, served_name="qwen3.6-35b-a3b").casefold()

    assert "не уходят" in said, said


def test_an_outside_endpoint_gets_no_such_promise(settings) -> None:
    """Обратная сторона: обещать приватность там, где её нет, — та же ложь.

    Если сервер модели вынесут наружу, «наружу твои данные не уходят» станет
    неправдой — и неправдой самого дорогого сорта, потому что человек на неё
    полагается.
    """
    import dataclasses

    outside = dataclasses.replace(settings, llm_base_url="https://api.example.com/v1")

    said = _self_description(outside, served_name="qwen3.6-35b-a3b").casefold()

    assert "не уходят" not in said, said
    assert "api.example.com" in said, "человеку не сказали, куда именно ходит система"
