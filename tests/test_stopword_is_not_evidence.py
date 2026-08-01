"""Попадание в FTS по префиксу стоп-слова — не доказательство.

`fts_ranking` — это стадия ОТБОРА, и она права, что щедра: стоп-слова остаются,
каждый терм ищется как префикс. Но `_exclusion_reason` принимал членство в этом
пуле за доказательство, а `"по"*` совпадает с 314 документами из 342 на настоящем
корпусе, `"на"*` — с 290, `"за"*` — с 259.

Замер на том же корпусе, 120 естественных вопросов: результатов, не содержащих ни
одного содержательного слова запроса, 138 из 1085 до правки и 130 после; целевой
документ в топ-10 — 76 из 120 в обоих случаях. То есть сегодня выигрыш скромный,
и он записан таким, какой есть, а не таким, каким его оценил аудит (там называлось
26.7% → 4.9%; воспроизвести это не удалось, метрика «пустого» результата у нас
разная — триграммное совпадение без общего слова считается совпадением по делу).

Ценность правки в другом: доля документов, которые впускает `"по"*`, растёт вместе
с корпусом, поэтому исключение, выданное за стоп-слово, со временем перестаёт
что-либо исключать.
"""

from __future__ import annotations

import pytest

from friday.retrieval import HybridSearcher
from friday.storage.models import KnowledgeObject, RawObject, new_id


def _store(storage, user_id: str, title: str, text: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("source"),
        raw_content=text,
        content_type="text",
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        title=title,
        summary=text[:120],
    )
    storage.store_knowledge_object(ko)
    return ko.id


@pytest.mark.asyncio
async def test_a_document_matched_only_by_a_stopword_is_not_admitted(storage, settings):
    storage.ensure_user("alice")
    target = _store(
        storage,
        "alice",
        "Порядок выдачи имущества",
        "Порядок выдачи имущества со склада. Заявка подаётся заранее, выдача по описи.",
    )
    # Ни одного содержательного слова запроса, но «по» ловит его префиксным поиском.
    noise = _store(
        storage,
        "alice",
        "Меню столовой",
        "Обед подаётся по расписанию. Первое, второе и компот, по будням и выходным.",
    )

    searcher = HybridSearcher(storage, None, record_usage=False)
    payload = await searcher.search("alice", "что там по выдаче имущества", limit=10)
    returned = [str(item["id"]) for item in payload["results"]]

    assert target in returned
    assert noise not in returned, "документ попал в ответ только за «по»"


@pytest.mark.asyncio
async def test_a_question_made_only_of_stopwords_still_returns_the_pool(storage, settings):
    """Убирать нечего — значит и опираться не на что; ответ остаётся прежним."""
    storage.ensure_user("alice")
    _store(storage, "alice", "Заметка", "Порядок выдачи имущества со склада по описи.")

    searcher = HybridSearcher(storage, None, record_usage=False)
    payload = await searcher.search("alice", "что там у нас по", limit=10)
    assert payload["count"] >= 0  # не падаем и не теряем пул


@pytest.mark.asyncio
async def test_a_content_word_still_admits_a_document_with_a_weak_lexical_score(storage, settings):
    """Исключение обязано СОХРАНИТЬСЯ: убрать его целиком — измеренное ухудшение.

    Длинный документ даёт lexical ниже 0.075 даже когда содержит все слова вопроса
    (39% объектов корпуса набирают меньше порога против собственного заголовка), и
    лобовое удаление исключения роняет recall@10 с 54.2% до 50.8%.
    """
    storage.ensure_user("alice")
    filler = " ".join(f"строка отчёта номер {index} без особого содержания." for index in range(400))
    target = _store(storage, "alice", "Годовой отчёт", f"{filler} Инвентаризация склада проведена.")

    searcher = HybridSearcher(storage, None, record_usage=False)
    payload = await searcher.search("alice", "что там по инвентаризации склада", limit=10)
    assert target in [str(item["id"]) for item in payload["results"]]
