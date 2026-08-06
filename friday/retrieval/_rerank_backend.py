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

ГРАНИЦЫ. Сам клиент только ПЕРЕСТАВЛЯЕТ: ничего не добавляет, ничего не теряет, отказ
службы не роняет поиск — выдача просто остаётся в прежнем порядке. Отсев по порогу
живёт НЕ здесь, а в `HybridSearcher`, и это намеренно: решение «не показывать» должно
приниматься там же, где считаются остальные причины отказа (`insufficient_evidence`,
`identifier_mismatch`, `deprecated_weak`), и попадать в тот же explain-трейс. Порог и
его цена — ниже, у `CONFIDENT_MIN_DEFAULT`.
"""

from __future__ import annotations

import asyncio
import logging
import re
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
# 2.3 было замерено на гладкой прозе и для боевого корпуса оптимистично: реальные
# документы здесь — расчётные листки и таблицы, где цифры и разметка токенизируются
# дороже слов. Замер на НАСТОЯЩИХ документах архива (по 4000 знаков каждый):
#
#     8 документов = 32 000 знаков → проходит
#    10 документов = 40 000 знаков → «Tokenized request exceeds 16384 tokens»
#
# То есть не более ~2.0 знака на токен, а не 2.3. Из-за завышенной оценки пакет
# уходил целиком, сервис отказывал, клиент делил и слал заново — 212 таких кругов
# за час в живом журнале, каждый в тот момент, когда человек ждёт ответа.
#
# Занижать оценку безопаснее, чем завышать: лишнее деление стоит одного
# дополнительного запроса, а промах — провального круга ПЛЮС тех же делений.
_CHARS_PER_TOKEN = 2.0

# Скор cross-encoder ОТКАЛИБРОВАН: это не относительная величина внутри выдачи, как
# слитый скор, а близкая к вероятности оценка «этот документ отвечает». На живом
# архиве «график отпусков» даёт пять штук по 0.999, а вопрос, ответа на который в
# архиве нет, — весь пул ниже 0.008.
#
# Замерено на отложенной половине (32 вопроса; у 25 ответ в пуле есть, у 7 нет):
#
#     порог   точность показанного   показано   верное молчание   потеряно вопросов
#      нет           43.5%             19.5          0 из 7               0
#      0.10          78.6%              8.2          6 из 7           4 из 25
#      0.90          82.8%              5.6          6 из 7           9 из 25
#
# Порог почти удваивает долю отвечающих и заставляет молчать там, где ответа нет,
# ценой 16% вопросов, у которых ответ был. Размен ПРИНЯТ владельцем: порог отсекает.
#
# Соблазн показать «хотя бы лучшего из отсеянных» тоже замерен и отвергнут: среди
# вопросов, у которых порог срезал всё, лучший срезанный отвечает 1 раз из 8, а на
# отложенной половине 0 из 4. Утешительный документ почти всегда не о том — и выглядит
# как ответ, то есть делает ровно то, ради предотвращения чего порог и стоит.
#
# Значение принадлежит МОДЕЛИ, у другой шкала будет другой, поэтому настраиваемое.
# `0` означает «не отсеивать»: скор не бывает отрицательным.
CONFIDENT_MIN_DEFAULT = 0.10


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
        # Сколько документов служба принимает за раз. Неизвестно, пока она сама не
        # скажет: у разных сборок предел разный, и зашивать его числом значило бы
        # угадывать.
        #
        # Замерено на живом экземпляре 2026-08-03: `rerank_top=40`, служба отвечает
        # «At most 32 documents are accepted», и КАЖДЫЙ поиск платил лишним
        # обращением — сначала заведомо провальным на 40, потом двумя по 20.
        # Отказ приходил, запоминать его было некуда, и стена стояла на том же
        # месте при следующем вопросе.
        self._max_documents: int | None = None

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

    async def _in_batches(
        self, query: str, documents: list[str], size: int, deadline: float
    ) -> list[float] | None:
        """Оценить партиями объявленного размера и склеить встык.

        Склейка верна по той же причине, что и в `_halve`: cross-encoder
        оценивает пары независимо, партии — соседние срезы входа, и каждая
        возвращает свои скоры в своём порядке.

        Отличие от `_halve` в том, что размер здесь ИЗВЕСТЕН, а не подбирается
        делением: 40 документов при пределе 32 — это 32 + 8, а не 20 + 20, и без
        первого заведомо провального обращения.

        Партии идут ОДНОВРЕМЕННО. Замерено вычитанием на живом архиве 2026-08-03:
        переранжировщик стоит 1.77 с из 2.25 с поиска — 79%, — и почти всё это
        ожидание ответа по сети. Последовательные партии складывали два таких
        ожидания подряд. Оценки от порядка обращений не зависят: cross-encoder
        считает пары независимо, и `gather` сохраняет порядок задач, а значит и
        порядок скоров.
        """
        chunks = [documents[start : start + size] for start in range(0, len(documents), size)]
        parts = await asyncio.gather(
            *(self.scores(query, chunk, _deadline=deadline, _nested=True) for chunk in chunks)
        )
        if any(part is None for part in parts):
            return None
        out: list[float] = []
        for part in parts:
            out.extend(part or [])
        return out

    @staticmethod
    def _declared_limit(detail: str) -> int | None:
        """Предел, названный самой службой: «At most 32 documents are accepted».

        Читается из текста отказа, а не задаётся настройкой: настройку пришлось
        бы держать в согласии с чужой сборкой вручную, и разъехались бы они
        молча — ровно в тот момент, когда служба обновится.
        """
        match = re.search(r"at most\s+(\d+)\s+document", detail, re.IGNORECASE)
        if not match:
            return None
        try:
            limit = int(match.group(1))
        except ValueError:
            return None
        return limit if 1 <= limit <= 4096 else None

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
        # Предел, о котором служба уже сказала, соблюдается СРАЗУ: бить в ту же
        # стену на каждом поиске — чистая потеря обращения.
        if self._max_documents and len(trimmed) > self._max_documents:
            return await self._in_batches(query, trimmed, self._max_documents, _deadline)
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
                    detail = " ".join(response.text.split())[:200]
                    LOGGER.warning(
                        "rerank request too large for %d documents; splitting",
                        len(trimmed),
                    )
                    # Предел запоминается, если служба его назвала: следующий поиск
                    # пойдёт сразу партиями и лишнего обращения не сделает.
                    limit = self._declared_limit(detail)
                    if limit and limit < len(trimmed):
                        self._max_documents = limit
                        LOGGER.info("rerank: служба принимает не больше %d документов за раз", limit)
                        return await self._in_batches(query, trimmed, limit, _deadline)
                    return await self._halve(query, trimmed, _deadline)
                if response.status_code == 400 and len(trimmed) > 1:
                    # vLLM-совместимые сервисы отвечают на переполнение контекста не
                    # 413, а 400 с текстом про длину («This model's maximum context
                    # length is …»). Без этой ветки промах оценки размера падал в
                    # общий except → None → поиск молча оставался без переранжирования
                    # и без порога, а сервис при этом жив.
                    detail = " ".join(response.text.split())[:300]
                    folded = detail.casefold()
                    if any(
                        marker in folded
                        for marker in ("context length", "too long", "maximum context", "input length")
                    ):
                        LOGGER.warning(
                            "rerank request over the context limit for %d documents; splitting",
                            len(trimmed),
                        )
                        return await self._halve(query, trimmed, _deadline)
                response.raise_for_status()
                body = response.json()
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("rerank backend unavailable (%s)", type(exc).__name__)
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
) -> list[dict[str, Any]] | None:
    """Переставить по скору переранжировщика. None — оставить как было.

    Порядок меняется, состав — нет. Здесь был ещё параметр `min_score`, который
    «опускал вниз» документы ниже порога: список к тому моменту уже отсортирован по
    убыванию скора, поэтому разбиение на «выше порога» и «ниже» возвращало РОВНО тот
    же порядок. Параметр не делал ничего и при этом читался как решение — удалён.

    Отсечь по порогу по-настоящему было бы можно (скор откалиброван, см. ниже), но
    это решение принимает не эта функция: цена замерена и отдана человеку.

    Единственный элемент не переставляется, но ОЦЕНИВАЕТСЯ: вызывающий с включённым
    порогом решает по `_rerank_score`, показывать ли его вообще. Прежний гард
    `len < 2` оставлял единственного кандидата без оценки — там, где ложный ответ
    убедительнее всего.
    """
    if not items:
        return None
    scores = await backend.scores(query, [_document_text(item) for item in items])
    if scores is None:
        return None
    order = sorted(range(len(items)), key=lambda index: (-scores[index], index))
    reordered = []
    for index in order:
        item = dict(items[index])
        item["_rerank_score"] = round(scores[index], 6)
        reordered.append(item)
    return reordered
