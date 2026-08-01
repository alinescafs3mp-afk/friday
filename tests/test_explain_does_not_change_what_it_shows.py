"""Диагностика не меняет наблюдаемое.

`/api/admin/retrieval/explain` объявлен в своём же докстринге «Read-only,
deterministic» — и ходил через ОБЩИЙ поисковик, созданный с записью счётчика
обращений. Этот счётчик `usage_signal` читает обратно в ранжирование, то есть
инструмент наблюдения двигал ту самую выдачу, которую показывает.

Замерено прямым опытом на живом экземпляре: один вызов explain поднял
`retrieval_count` с 1968 до 1970. Несколько диагностических прогонов по золотому
набору сдвинули recall@10 с 0.5 до 0.4872 — «ухудшение», которого никто не вносил
и которое я сначала приняла за шум измерения.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from friday.knowledge_graph import KnowledgeGraph
from friday.retrieval import HybridSearcher
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


def _usage_total(storage) -> int:
    row = storage.execute(
        "SELECT COALESCE(SUM(retrieval_count), 0) AS total FROM knowledge_usage"
    ).fetchone()
    return int(row["total"] if row else 0)


def test_a_search_that_serves_a_person_records_usage(settings, storage):
    """Обратная сторона: обычный поиск счётчик писать ОБЯЗАН.

    Иначе проверка ниже проходила бы просто потому, что запись сломана целиком.
    """
    storage.ensure_user("alice", source="upload")
    _store(storage, "Смета на кровлю.md", "Работы по кровле, смета согласована подрядчиком.")
    searcher = HybridSearcher(storage, record_usage=True)

    before = _usage_total(storage)
    asyncio.run(searcher.search("alice", "смета кровля", limit=5, kg=KnowledgeGraph(storage)))
    assert _usage_total(storage) > before, "обычный поиск перестал учитывать обращения"


def test_an_observing_search_leaves_the_counter_alone(settings, storage):
    """Мутация: убрать `record_usage=False` из explain-маршрута — тест краснеет.

    Проверяется перекрытие НА ОДИН ВЫЗОВ у того же самого экземпляра, что пишет
    счётчик: именно так устроен живой случай — поисковик в `app.state` общий, и
    отдельного «читающего» у диагностики нет.
    """
    storage.ensure_user("alice", source="upload")
    _store(storage, "Смета на кровлю.md", "Работы по кровле, смета согласована подрядчиком.")
    searcher = HybridSearcher(storage, record_usage=True)

    before = _usage_total(storage)
    asyncio.run(
        searcher.search("alice", "смета кровля", limit=5, kg=KnowledgeGraph(storage), record_usage=False)
    )
    assert _usage_total(storage) == before, (
        "наблюдение изменило наблюдаемое: счётчик обращений вырос от диагностического запроса"
    )


def test_the_explain_route_asks_for_the_read_only_mode(settings):
    """Маршрут диагностики обязан просить режим без записи ЯВНО.

    Проверяется исходный код: поисковик берётся из `app.state`, он общий и пишет
    по умолчанию, поэтому «read-only» здесь — не свойство объекта, а обязанность
    места вызова. Забыть её нельзя молча — от этого и появился дефект.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "friday" / "admin_api" / "_evaluation.py").read_text(
        encoding="utf-8"
    )
    marker = source.index("async def retrieval_explain")
    # Комментарии отбрасываются: в них это же слово стоит в объяснении, и первая
    # редакция теста из-за этого проходила при снятом аргументе — проверяла текст,
    # а не вызов.
    body = "\n".join(
        line for line in source[marker : marker + 2000].splitlines() if not line.strip().startswith("#")
    )
    assert "record_usage=False" in body, (
        "explain-маршрут снова ходит через поисковик, пишущий счётчик обращений"
    )
