"""Переранжирование выдачи локальной моделью: она читает выдержки и говорит, что отвечает.

ЗАЧЕМ. Замерено на корпусе владельца (1537 документов, 80 вопросов, судья с известной
базой 12.2% на случайном документе): точность выдачи ПЛОСКАЯ по глубине — 35.9% в
пятёрке и 35.2% в двадцатке. Документ на двадцатом месте отвечает примерно так же
часто, как на первом. То есть отбор кандидатов работает (35% против базы 12.2%), а
упорядочивание внутри — нет: отвечающие документы разбросаны по списку.

Ни один канал слияния не оценивает то, ради чего человек и пришёл, — отвечает ли
документ на вопрос. Лексика, поля, косинус и граф меряют похожесть, а похожесть и
ответ на однородном архиве расходятся. Этот шаг добавляет недостающий сигнал.

ЧТО ОН НЕ ДЕЛАЕТ, и это существеннее того, что делает:

* Не добавляет кандидатов. На вход приходит уже отобранное и прошедшее гейт
  доказательств; вернуть можно только перестановку. Иначе шаг стал бы четвёртым
  обходом review-gate — их в этом проекте закрывали трижды.
* Не решает за человека. Документ, который модель не назвала, не выбрасывается, а
  уходит вниз: ошибка модели должна стоить места в списке, а не исчезновения.
* Не роняет поиск. Нет ответа, не разобрался JSON, вышел бюджет, модель недоступна —
  возвращается прежний порядок. Сегодняшний случай с `tools` показал цену обратного:
  агент не пользовался инструментами с самого начала, а человек видел «LLM недоступна».

ЦЕНА, замерена на этой установке (пакетная оценка одним обращением):

    5 выдержек по 800 знаков —  7.6 с
    10 выдержек по 800 знаков — 8.5 с
    20 выдержек по 800 знаков — 13.9 с
    20 выдержек по 400 знаков — 28.5 с, упирается в потолок токенов

Короткие выдержки ДОРОЖЕ длинных: по обрывку модели труднее решать, и она дольше
рассуждает. Отсюда 800 знаков, а не 400.

Восемь секунд — это в пять раз дороже самого поиска (медиана 1.58 с). Поэтому шаг по
умолчанию выключен и включается настройкой: место ему там, где человек ждёт ОТВЕТ, а
не список.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

LOGGER = logging.getLogger(__name__)

# Длина выдержки. Замерено: 400 знаков дороже 800, потому что по обрывку модель дольше
# рассуждает, а на двадцати фрагментах ещё и упирается в потолок токенов.
EXCERPT_CHARS = 800

_PROMPT = """Вопрос: {question}

Ниже пронумерованные фрагменты документов из личного архива. Оцени каждый: отвечает ли \
он на вопрос — прямо или по существу.

{blocks}

Верни ТОЛЬКО JSON-массив номеров тех фрагментов, которые отвечают на вопрос, от самого \
подходящего к менее подходящему. Если не отвечает ни один — верни пустой массив. \
Без пояснений и без Markdown."""


class SupportsChat(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        priority: str = "foreground",
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...


def _excerpt(item: dict[str, Any]) -> str:
    """Заголовок плюс начало тела: по одному телу без заголовка модель хуже различает
    однотипные служебные документы, а они тут все однотипные."""
    title = str(item.get("title") or "").strip()
    body = " ".join(str(item.get("content") or "").split())
    head = f"{title}. " if title else ""
    return (head + body)[:EXCERPT_CHARS]


def parse_order(text: str, count: int) -> list[int] | None:
    """Вытащить массив номеров из ответа модели.

    Отдельной функцией, потому что это единственное место, где ответ модели становится
    решением, и оно должно быть проверяемо тестом без сети. Рассуждающая модель кладёт
    монолог перед ответом; годным считается ПОСЛЕДНИЙ массив в тексте, а не первый —
    первый обычно оказывается внутри рассуждения.
    """
    tail = re.sub(r"^.*</think>", "", text or "", flags=re.S)
    matches = re.findall(r"\[[^\[\]]*\]", tail)
    for candidate in reversed(matches):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, list):
            continue
        seen: set[int] = set()
        order: list[int] = []
        for value in parsed:
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            if 0 <= value < count and value not in seen:
                seen.add(value)
                order.append(value)
        return order
    return None


async def rerank(
    llm: SupportsChat,
    query: str,
    items: list[dict[str, Any]],
    *,
    budget_sec: float | None = None,
) -> list[dict[str, Any]] | None:
    """Переставить `items` по тому, отвечают ли они на `query`. None — оставить как было.

    Возвращается ПЕРЕСТАНОВКА тех же объектов: названные моделью в её порядке, затем
    остальные в прежнем. Ни один объект не теряется — это проверяется тестом, потому
    что потеря выглядела бы как «поиск ничего не нашёл», а не как сбой модели.
    """
    if len(items) < 2:
        return None
    blocks = "\n\n".join(f"[{index}] {_excerpt(item)}" for index, item in enumerate(items))
    try:
        response = await llm.chat(
            [{"role": "user", "content": _PROMPT.format(question=query, blocks=blocks)}],
            temperature=0.0,
            # Столько же, сколько совету по Inbox: замерено, что до JSON рассуждающая
            # модель думает 2516-3616 токенов, а на двадцати фрагментах доходит до 4096.
            max_tokens=4096,
            priority="foreground",
            tools=[],
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.info("rerank skipped: %s", exc)
        return None

    order = parse_order(str(response.get("content") or ""), len(items))
    if order is None:
        LOGGER.info("rerank skipped: model returned no usable order")
        return None
    if not order:
        # Модель говорит, что не отвечает ни один. Это НЕ повод очистить выдачу:
        # решение о полезности принимает человек, а гейт доказательств уже отработал.
        return None
    chosen = [items[index] for index in order]
    rest = [item for index, item in enumerate(items) if index not in set(order)]
    return chosen + rest
