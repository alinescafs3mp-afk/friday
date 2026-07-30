"""Клиент к отдельной модели-переранжировщику (cross-encoder).

ЗАЧЕМ ОТДЕЛЬНАЯ МОДЕЛЬ, а не та же чат-модель. Замерено на корпусе владельца
(1537 документов, 80 вопросов, судья с известной базой 12.2%):

    отбор кандидатов работает   — 40% выданных отвечают против базы 12.2%
    упорядочивание внутри — нет — точность плоская по глубине: 40.0 / 38.4 / 40.5%
                                  в пятёрке, десятке и двадцатке

Лучший различитель среди имеющихся сигналов — плотный скор, AUC 0.687 на разделении
«отвечает / не отвечает». Итоговое слияние даёт 0.640, то есть ХУЖЕ своего лучшего
канала. Для «двух-трёх верных» нужен различитель порядка 0.9, и перевесами он не
получается: покоординатный спуск по всем четырём весам не сдвинул метрику больше чем
на один вопрос из сорока.

Чат-модель эту роль замерена и не потянула: пакетная постановка вырождает её оценку
(на шести вопросах названо 0, 0, 10, 0, 1, 10), а точная конфигурация — рассуждение
плюс выдержки в 5000 знаков — стоит 15-25 секунд на запрос. Подробности в `_rerank.py`.

Cross-encoder делает ровно эту работу одним проходом, со скоростью эмбеддингов, а не
генерации, и обучен именно ей.

ЗАМЕРЕНО на `qwen3-reranker-0.6b`, 64 вопроса, приговоры того же судьи. Критерий
успеха был назван ДО первого числа: AUC > 0.85 и точность в пятёрке > 55% на
отложенной половине.

    сигнал            AUC внутри пула   AUC общий   точн@5   охват@5
    выдача как есть        0.512          0.495      41.9%     68.8%
    плотный канал          0.488          0.738      42.5%     65.6%
    cross-encoder          0.754          0.877      53.1%     68.8%

Главное здесь — первый столбец. Внутри пула кандидатов прежний порядок был
подбрасыванием монеты (0.512), и плотный канал тоже (0.488): косинус говорит, есть ли
в архиве похожее вообще, но не какой из отобранных отвечает. Cross-encoder — первый
сигнал, который различает ИМЕННО ЭТО.

По объявленному рубежу: AUC взят (0.877), точность в пятёрке — 53.1% при обещанных
55%. Недобор в 1.9 пункта это три места из 160 при стандартной ошибке 3.9 пункта, то
есть от рубежа неотличимо; против исходных 41.9% прибавка втрое больше ошибки.
В пятёрке теперь отвечают 2.7 документа вместо 2.1.

Охват (есть ли в пятёрке хоть один отвечающий) не изменился — он упирается не в
порядок, а в состав пула: там, где отвечающих нет вовсе, переставлять нечего.

КОНТРАКТ. Общий для vLLM (`--task score`), TEI, Infinity и Jina: POST на `{base}/rerank`
с `{"model", "query", "documents": [...]}`, ответ `{"results": [{"index", "relevance_score"}]}`.
База включает префикс версии, как у эмбеддингов (`.../v1`), чтобы настройки выглядели
одинаково.

ГРАНИЦЫ ТЕ ЖЕ, что у чат-переранжировщика, и по тем же причинам: только перестановка,
ничего не добавляет и не теряет, отказ сервиса не роняет поиск.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)

# Сколько текста уходит на пару. Подобрано на ВЫВОДЯЩЕЙ половине вопросов, качество
# упорядочивания внутри пула (AUC по вопросу) и точность в пятёрке:
#
#     1000 знаков   0.734   57.5%
#     2000 знаков   0.736   55.6%
#     4000 знаков   0.832   61.3%     <- выбрано
#     8000 знаков   0.817   61.3%
#     куски по 2000, максимум   0.849   60.0%   (вшестеро дороже)
#
# Ниже 4000 модель недополучает текста — тот же порог, что нашёлся у чат-судьи. Выше
# 4000 прибавки нет. Нарезка на куски с максимумом выигрывает 0.017 AUC — это шум на
# 32 вопросах, и он не стоит шестикратной платы.
DOCUMENT_CHARS = 4_000

# Предел ЗАПРОСА, а не пары: сервер считает токены по всем парам сразу, и вопрос
# входит в каждую. Замерено на этом сервисе (`qwen3-reranker-0.6b`, предел 16384):
#
#     пара с пустым текстом и вопросом в 56 знаков  —    94 токена
#     8 пар по 4000 знаков (30981 знак)             — 14010 токенов
#
# То есть 2.35 знака на токен и ~80 токенов шаблона на пару. Двадцать документов по
# 4000 знаков — около 36 тысяч токенов, вдвое сверх предела: БЕЗ деления запроса
# переранжирование на рабочей глубине не сработало бы НИ РАЗУ, а `scores` вернула бы
# None, и поиск молча остался бы в прежнем порядке.
_REQUEST_MAX_TOKENS = 15_000
_PAIR_OVERHEAD_TOKENS = 80
_CHARS_PER_TOKEN = 2.3


def _estimated_tokens(query: str, documents: list[str]) -> float:
    """Оценка размера запроса в токенах. Заведомо грубая — отсюда и запас до 16384."""
    per_pair = _PAIR_OVERHEAD_TOKENS + len(query) / _CHARS_PER_TOKEN
    return len(documents) * per_pair + sum(len(text) for text in documents) / _CHARS_PER_TOKEN


class RerankBackend:
    """HTTP-клиент переранжировщика. Выключен, пока не настроен адрес."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._base = str(getattr(settings, "rerank_base_url", "") or "").rstrip("/")
        self._model = str(getattr(settings, "rerank_model", "") or "")
        key = str(getattr(settings, "rerank_api_key", "") or "")
        self._headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._timeout = float(getattr(settings, "rerank_timeout_sec", 20.0))
        # Пауза после отказа: та же мысль, что у эмбеддингов, — не добивать сервис,
        # который уже не справляется. Проще, потому что переранжирование необязательно:
        # пока пауза держится, поиск просто идёт прежним порядком.
        self._cooldown_until = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self._base and self._model)

    @property
    def cooling_down(self) -> bool:
        return time.monotonic() < self._cooldown_until

    async def _halve(self, query: str, documents: list[str], deadline: float) -> list[float] | None:
        """Оценить половины по отдельности и склеить.

        Склейка встык верна ровно потому, что половины — соседние срезы входа, и
        каждая возвращает свои скоры в своём порядке. Cross-encoder оценивает пары
        независимо, поэтому деление не меняет ни одного числа; у чат-переранжировщика
        это было не так — там пакетная постановка вырождала оценку, и делить было
        нельзя.

        Половины наследуют СРОК, а не таймаут: иначе каждая начинала бы отсчёт заново
        и поиск ждал бы кратно дольше обещанного.
        """
        middle = len(documents) // 2
        first = await self.scores(query, documents[:middle], _deadline=deadline, _nested=True)
        if first is None:
            return None
        second = await self.scores(query, documents[middle:], _deadline=deadline, _nested=True)
        if second is None:
            return None
        return first + second

    async def scores(
        self,
        query: str,
        documents: list[str],
        *,
        _deadline: float | None = None,
        _nested: bool = False,
    ) -> list[float] | None:
        """Скор релевантности на каждый документ, в порядке входа. None — не вышло.

        Порядок ответа НЕ предполагается: сервис возвращает `index`, и скоры
        раскладываются по нему. Молчаливая перестановка здесь означала бы приписывание
        чужих оценок документам, а это ровно тот класс ошибок, который в индексаторе
        уже ловили учётом смещений.

        Слишком большой запрос делится пополам: сначала по оценке размера, чтобы не
        платить заведомо провальным обращением, и затем по отказу сервера — оценка
        грубая, и промах должен стоить лишнего запроса, а не тишины.
        """
        if not self.enabled or not documents or self.cooling_down:
            return None
        if _deadline is None:
            _deadline = time.monotonic() + self._timeout
        remaining = _deadline - time.monotonic()
        if remaining <= 0:
            return None
        trimmed = [text[:DOCUMENT_CHARS] for text in documents]
        if len(trimmed) > 1 and _estimated_tokens(query, trimmed) > _REQUEST_MAX_TOKENS:
            return await self._halve(query, trimmed, _deadline)
        payload = {"model": self._model, "query": query, "documents": trimmed}
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(remaining, connect=min(5.0, remaining)),
                trust_env=False,
                headers=self._headers,
            ) as client:
                response = await client.post(f"{self._base}/rerank", json=payload)
                if response.status_code >= 500 or response.status_code == 429:
                    self._cooldown_until = time.monotonic() + 60.0
                    LOGGER.warning("rerank backend overloaded (%d); pausing", response.status_code)
                    return None
                if response.status_code == 413 and len(trimmed) > 1:
                    LOGGER.warning(
                        "rerank request too large for %d documents (service said: %s); splitting",
                        len(trimmed),
                        " ".join(response.text.split())[:200],
                    )
                    return await self._halve(query, trimmed, _deadline)
                response.raise_for_status()
                body = response.json()
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("rerank backend unavailable: %s", exc)
            return None

        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list) or len(results) != len(documents):
            LOGGER.info(
                "rerank backend returned %s results for %d documents", type(results).__name__, len(documents)
            )
            return None
        scores = [0.0] * len(documents)
        seen: set[int] = set()
        for entry in results:
            if not isinstance(entry, dict):
                return None
            index = entry.get("index")
            value = entry.get("relevance_score", entry.get("score"))
            if not isinstance(index, int) or isinstance(index, bool):
                return None
            if not 0 <= index < len(documents) or index in seen:
                return None
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                # Скор строкой или отсутствующий — не повод угадывать: молчаливый ноль
                # опустил бы документ вниз, и это выглядело бы как решение модели.
                return None
            scores[index] = float(value)
            seen.add(index)
        if len(seen) != len(documents):
            return None
        return scores


def _document_text(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "").strip()
    body = " ".join(str(item.get("content") or "").split())
    return (f"{title}. " if title else "") + body


async def rerank_with_backend(
    backend: RerankBackend,
    query: str,
    items: list[dict[str, Any]],
    *,
    min_score: float | None = None,
) -> list[dict[str, Any]] | None:
    """Переставить по скору переранжировщика. None — оставить как было.

    `min_score` НЕ выбрасывает: документы ниже порога уходят вниз, а не исчезают.
    Гейт доказательств уже отработал до этого места, и второе решение о том, что
    человеку не показывать, здесь принимать нечем.
    """
    if len(items) < 2:
        return None
    scores = await backend.scores(query, [_document_text(item) for item in items])
    if scores is None:
        return None
    order = sorted(range(len(items)), key=lambda index: (-scores[index], index))
    if min_score is not None:
        above = [index for index in order if scores[index] >= min_score]
        below = [index for index in order if scores[index] < min_score]
        order = above + below
    reordered = []
    for index in order:
        item = dict(items[index])
        item["_rerank_score"] = round(scores[index], 6)
        reordered.append(item)
    return reordered
