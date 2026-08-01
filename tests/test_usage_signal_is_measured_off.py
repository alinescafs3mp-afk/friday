"""«Этот документ уже доставали» — не аргумент в ранжировании (замер 2026-08-01).

Сигнал `usage` поднимает то, что и так поднимается: на архиве владельца верх
занимают документы-концентраторы («Судимости.docx», «люди (35 человек).docx»),
похожие на всё сразу и потому извлекаемые чаще прочих. Каждый такой показ повышал
их вес на следующем запросе — самоподкрепляющаяся петля.

Замер на БОЕВОЙ сборке (с переранжировщиком), 78 эталонов:

    вес 0.028 → recall@10 0.7179, MRR 0.4508
    вес 0     → recall@10 0.7308, MRR 0.4345

Эффект пограничный и разнонаправленный: без сигнала на один эталон больше попадает
в десятку, с сигналом найденное стоит чуть выше. Выбран recall — «нашлось вообще»
важнее «нашлось строчкой выше».

⚠️ Первая редакция этого файла утверждала, что сигнал ВРЕДИТ с p = 0.0044 по MRR
(recall 0.4872 → 0.5256). То число получено на сборке БЕЗ переранжировщика:
`run_eval` собирал поисковик вручную и половину настроек не передавал. Вывод был
уверенным, воспроизводимым — и относился к поиску, которого у человека нет.
Записано здесь намеренно: замер на неверной конфигурации выглядит ровно так же
убедительно, как верный.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from friday.knowledge_graph import KnowledgeGraph
from friday.retrieval import _USAGE_WEIGHT, HybridSearcher
from friday.storage.models import KnowledgeObject, RawObject, new_id


def _store(storage, title: str, content: str) -> str:
    raw = storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="upload",
            source_ref=f"sha256:{new_id('x')}",
            raw_content=content,
            content_type="text/plain",
            content_hash=new_id("h") * 2,
            received_at=datetime.now(UTC).isoformat(),
        )
    )
    stored = storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=raw.id,
            title=title,
            summary=content[:100],
            content=content,
            knowledge_kind="note",
            importance=0.5,
        )
    )
    return stored.id


def test_the_usage_weight_is_zero_by_measurement():
    """Мутация: вернуть 0.028 — тест краснеет.

    Число проверяется прямо, потому что вернуть его «как было» проще всего именно
    не глядя: сигнал выглядит разумным, и без записанного замера возврат кажется
    починкой, а не откатом.
    """
    assert _USAGE_WEIGHT == 0.0, (
        f"вес usage вернули в {_USAGE_WEIGHT} — замер на боевой сборке говорит обратное: "
        "с ним recall@10 0.7179, без него 0.7308"
    )


def test_a_frequently_retrieved_document_does_not_outrank_a_better_match(settings, storage):
    """Часто извлекаемый документ не обгоняет более подходящий.

    Стенд повторяет форму боевого случая: широкий документ-список, который уже
    доставали много раз, и узкий документ, действительно отвечающий на вопрос.
    """
    storage.ensure_user("alice", source="upload")
    hub = _store(
        storage,
        "Судимости.docx",
        "Список лиц: Бутко, Иванов, Петров, Сидоров. Зарплата, выплаты, начисления, отпуска.",
    )
    precise = _store(
        storage,
        "Бутко Сергей_октябрь_2025.pdf",
        "Расчётный листок Бутко за октябрь 2025: начислено, удержано, к выплате.",
    )
    # Концентратор «нахожен» тридцать раз — ровно та история, которую сигнал
    # превращал в преимущество.
    for _ in range(30):
        storage.record_knowledge_usage("alice", [hub], retrieved=True)

    searcher = HybridSearcher(storage, record_usage=False)
    results = asyncio.run(
        searcher.search("alice", "зарплата Бутко октябрь 2025", limit=5, kg=KnowledgeGraph(storage))
    )["results"]

    assert results, "поиск ничего не вернул"
    assert results[0]["id"] == precise, (
        "часто извлекаемый список обогнал документ, который прямо отвечает на вопрос"
    )
