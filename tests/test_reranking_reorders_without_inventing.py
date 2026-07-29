"""Переранжирование обязано ПЕРЕСТАВЛЯТЬ и только.

Зачем шаг вообще нужен, замерено на корпусе владельца: точность выдачи плоская по
глубине — 35.9% в пятёрке и 35.2% в двадцатке при базе судьи 12.2%. Документ на
двадцатом месте отвечает примерно так же часто, как на первом, то есть отбор
кандидатов работает, а упорядочивание внутри — нет.

Но шаг, который зовёт модель и переписывает выдачу, опасен ровно тем, чем полезен.
Здесь закреплены его границы: не добавляет, не теряет, не очищает выдачу и не роняет
поиск, когда модель молчит или врёт.
"""

from __future__ import annotations

import pytest

from jericho.retrieval._rerank import parse_order, rerank


class _Model:
    def __init__(self, content: str | None = None, *, raises: Exception | None = None) -> None:
        self.content = content
        self.raises = raises
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return {"content": self.content, "finish_reason": "stop"}


def _items(count: int) -> list[dict]:
    return [
        {"id": f"ko-{index}", "title": f"Документ {index}", "content": f"тело {index} " * 50}
        for index in range(count)
    ]


# --- разбор ответа ------------------------------------------------------------


def test_the_order_is_read_from_the_last_array_not_the_first():
    """Рассуждающая модель кладёт монолог перед ответом, и в монологе бывают массивы.

    Первый массив в тексте — обычно черновик внутри рассуждения; годен последний.
    """
    text = "<think>сначала я подумал [3, 1], потом передумал</think>\n[2, 0]"
    assert parse_order(text, 4) == [2, 0]


def test_out_of_range_and_repeated_numbers_are_dropped():
    """Модель может назвать номер, которого нет, или назвать один дважды — это не повод
    уронить перестановку целиком, но и не повод пропустить мусор дальше."""
    assert parse_order("[0, 9, 1, 1, -2]", 3) == [0, 1]


def test_booleans_are_not_indices():
    """`True` в Python — это `1`, и без явной проверки булево значение стало бы номером."""
    assert parse_order("[true, 0]", 3) == [0]


def test_unparsable_answers_give_none_not_an_empty_order():
    """None и пустой массив значат РАЗНОЕ: «не понял» против «не отвечает ни один»."""
    assert parse_order("вообще без массива", 3) is None
    assert parse_order("[не json]", 3) is None
    assert parse_order("[]", 3) == []


# --- поведение шага -----------------------------------------------------------


@pytest.mark.asyncio
async def test_it_reorders_and_keeps_every_item():
    """Ни один объект не теряется: потеря выглядела бы как «поиск ничего не нашёл»."""
    items = _items(5)
    result = await rerank(_Model("[3, 1]"), "вопрос", items)

    assert result is not None
    assert [item["id"] for item in result[:2]] == ["ko-3", "ko-1"]
    assert sorted(item["id"] for item in result) == sorted(item["id"] for item in items)
    assert len(result) == len(items)


@pytest.mark.asyncio
async def test_it_cannot_add_anything_that_was_not_given():
    """Шаг получает уже прошедшее гейт доказательств. Возможность добавить кандидата
    сделала бы его четвёртым обходом review-gate — их тут закрывали трижды."""
    items = _items(3)
    result = await rerank(_Model("[0, 1, 2, 7, 42]"), "вопрос", items)

    assert result is not None
    assert {item["id"] for item in result} == {"ko-0", "ko-1", "ko-2"}


@pytest.mark.asyncio
async def test_an_empty_verdict_does_not_empty_the_results():
    """«Не отвечает ни один» — мнение модели, а не решение за человека."""
    items = _items(4)
    assert await rerank(_Model("[]"), "вопрос", items) is None


@pytest.mark.asyncio
async def test_a_broken_model_leaves_the_order_alone():
    """Нет ответа, не разобрался JSON, модель недоступна — прежний порядок.

    Сегодняшний случай с `tools` показал цену обратного: агент не пользовался
    инструментами с самого начала, а человек видел «LLM недоступна».
    """
    items = _items(4)
    assert await rerank(_Model(None), "вопрос", items) is None
    assert await rerank(_Model("я не понял вопроса"), "вопрос", items) is None
    assert await rerank(_Model(raises=RuntimeError("модель недоступна")), "вопрос", items) is None


@pytest.mark.asyncio
async def test_a_single_item_is_not_worth_a_model_call():
    """Восемь секунд на перестановку одного элемента — чистая потеря."""
    model = _Model("[0]")
    assert await rerank(model, "вопрос", _items(1)) is None
    assert model.calls == 0, "модель звалась ради списка, в котором нечего переставлять"


@pytest.mark.asyncio
async def test_the_excerpt_carries_the_title_and_is_bounded():
    """Замерено: 400 знаков ДОРОЖЕ 800 — по обрывку модель дольше рассуждает, а на
    двадцати фрагментах упирается в потолок токенов. Заголовок нужен потому, что без
    него однотипные служебные документы неразличимы."""
    from jericho.retrieval._rerank import EXCERPT_CHARS, _excerpt

    text = _excerpt({"title": "Протокол приёмки", "content": "тело " * 5000})
    assert text.startswith("Протокол приёмки")
    assert len(text) <= EXCERPT_CHARS
