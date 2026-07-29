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

# Сколько текста уходит на пару. Замерено на чат-модели: 800 знаков НЕДОСТАТОЧНО,
# чтобы судить — на десяти документах судья с 5000 знаков находит два отвечающих, с
# 800 ни одного. У cross-encoder свой предел длины (у bge-reranker-v2-m3 — 8192
# токена), и упереться в него дешевле, чем недодать текста.
DOCUMENT_CHARS = 4_000


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

    async def scores(self, query: str, documents: list[str]) -> list[float] | None:
        """Скор релевантности на каждый документ, в порядке входа. None — не вышло.

        Порядок ответа НЕ предполагается: сервис возвращает `index`, и скоры
        раскладываются по нему. Молчаливая перестановка здесь означала бы приписывание
        чужих оценок документам, а это ровно тот класс ошибок, который в индексаторе
        уже ловили учётом смещений.
        """
        if not self.enabled or not documents or self.cooling_down:
            return None
        payload = {
            "model": self._model,
            "query": query,
            "documents": [text[:DOCUMENT_CHARS] for text in documents],
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=5.0),
                trust_env=False,
                headers=self._headers,
            ) as client:
                response = await client.post(f"{self._base}/rerank", json=payload)
                if response.status_code >= 500 or response.status_code == 429:
                    self._cooldown_until = time.monotonic() + 60.0
                    LOGGER.warning("rerank backend overloaded (%d); pausing", response.status_code)
                    return None
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
