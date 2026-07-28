"""Граф не должен слепнуть молча и не должен держать сервер пять минут.

Две находки одного разбора, обе замерены на 5000 сущностей.

**Слепота.** `list_entities` зажат потолком 5000 и обрезал БЕЗ ЕДИНОГО ПРИЗНАКА.
Проверено исполнением на 8001 сущности: прямой поиск по имени находит запись, а
`search_entities` и `match_mentions` возвращают ноль — они строят своё
представление из этого списка. Порядок `ORDER BY name`, поэтому отрезается всегда
один и тот же хвост алфавита: сущность лежит в базе и невидима графу навсегда.

**Пять минут.** `GET /kg/resolutions/pending` занимал 317.5 с, из них 99.6% — в
запасном пути `get_entity_knowledge`, сканировавшем весь корпус с `SELECT *`,
потому что индекса на `entity_id` не было. Плюс сам список кандидатур не имел
лимита вообще — ни параметра, ни клампа.

Числа, которые здесь закрепляются, — про ПЛАН и про честность счётчиков; замер
времени живёт в scratchpad, потому что в тесте он мерил бы машину, а не запрос.
"""

from __future__ import annotations

import hashlib

from jericho.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id


def _entity(storage, user_id: str, name: str) -> str:
    entity = Entity(id=new_id("ent"), user_id=user_id, name=name, entity_type=EntityType.CONCEPT)
    storage.create_entity(entity)
    return entity.id


def _knowledge(storage, user_id: str, title: str, entity_id: str | None = None) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=f"Тело документа {title}",
        content_type="text",
        content_hash=hashlib.sha256(title.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=f"Тело документа {title}",
        content_type="text",
        title=title,
        entity_id=entity_id,
    )
    storage.store_knowledge_object(ko)
    return ko.id


# --- план запроса: то, из-за чего было 317 секунд ---------------------------


def test_finding_an_entitys_knowledge_uses_the_index_not_a_corpus_scan(storage):
    """Индекс обязан покрывать И фильтр, И сортировку.

    Индекс только по (user_id, entity_id) не помогал ВООБЩЕ: SQLite продолжал
    брать `idx_knowledge_user_importance`, потому что тот обслуживает ORDER BY, и
    сканировал всего арендатора. Замерено — 51.7 мс против 39.2 мс, то есть новый
    индекс просто не использовался. Поэтому проверяется имя индекса в плане, а не
    факт его существования в схеме.
    """
    storage.ensure_user("alice")
    entity_id = _entity(storage, "alice", "Склад")
    _knowledge(storage, "alice", "Договор", entity_id)

    plan = [
        str(row["detail"])
        for row in storage.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM knowledge_objects WHERE user_id=? AND entity_id=? "
            "AND deleted_at IS NULL ORDER BY importance DESC, updated_at DESC LIMIT ?",
            ("alice", entity_id, 50),
        ).fetchall()
    ]
    joined = " ".join(plan)
    assert "idx_knowledge_user_entity" in joined, f"запрос не идёт по своему индексу: {plan}"
    assert "TEMP B-TREE" not in joined, plan


def test_resolution_candidates_are_paged(storage):
    """Лимита не было вовсе — вся таблица поднималась в память и уходила в ответ."""
    import inspect

    signature = inspect.signature(storage.list_resolution_candidates)
    assert "limit" in signature.parameters
    assert "offset" in signature.parameters
    assert storage.count_resolution_candidates("alice") == 0


# --- честные числа вместо длины обрезанной страницы -------------------------


def test_the_entity_count_does_not_saturate_at_the_listing_cap(storage):
    """`len(list_entities(limit=5000))` застывал на пяти тысячах и выглядел точным."""
    storage.ensure_user("alice")
    for index in range(7):
        _entity(storage, "alice", f"Сущность {index}")

    assert storage.count_entities("alice") == 7
    # Потолок ниже набора: счётчик обязан остаться полным.
    assert len(storage.list_entities("alice", limit=3)) == 3
    assert storage.count_entities("alice") == 7


def test_the_recent_window_counts_instead_of_measuring_a_page(storage):
    """«За 30 дней» насыщалось ровно на 200 у всякого, кто перешагнул этот рубеж."""
    storage.ensure_user("alice")
    for index in range(5):
        _knowledge(storage, "alice", f"Заметка {index}")

    since = "2000-01-01T00:00:00+00:00"
    assert storage.count_recent_knowledge("alice", since_iso=since) == 5
    assert len(storage.list_recent_knowledge("alice", since_iso=since, limit=2)) == 2
    assert storage.count_recent_knowledge("alice", since_iso=since) == 5


def test_graph_stats_report_counts_not_page_lengths(storage, monkeypatch):
    """Потолок надо ЗАСТАВИТЬ сработать, иначе тест зеленеет на сломанном коде.

    Первая версия просто заводила шесть сущностей и сверяла число с шестью — а при
    потолке в 5000 длина выборки и настоящий счёт совпадают, и подмена счётчика
    обратно на `len(entities)` тест не роняла. Мутация это показала. Здесь выборка
    урезается принудительно, и утверждение становится про ИСТОЧНИК числа.
    """
    from jericho.knowledge_graph import KnowledgeGraph

    storage.ensure_user("alice")
    for index in range(6):
        _entity(storage, "alice", f"Узел {index}")

    real_list = storage.list_entities
    monkeypatch.setattr(
        storage, "list_entities", lambda *args, **kwargs: real_list(*args, **{**kwargs, "limit": 2})
    )
    stats = KnowledgeGraph(storage).get_stats("alice")

    assert stats["entity_count"] == 6, (
        f"число взято из обрезанной выборки, а не сосчитано: {stats['entity_count']}"
    )
    assert stats["pending_resolutions"] == 0


def test_a_truncated_entity_listing_says_so_in_the_log(storage, caplog):
    """Тихий обрез — худший способ не справиться: ответ выглядит полным."""
    import logging

    storage.ensure_user("alice")
    for index in range(6):
        _entity(storage, "alice", f"Сущность {index:03d}")

    with caplog.at_level(logging.WARNING, logger="jericho.storage"):
        rows = storage.list_entities("alice", limit=4)

    assert len(rows) == 4
    assert any("4 of 6 entities" in record.getMessage() for record in caplog.records), (
        f"обрез прошёл молча: {[r.getMessage() for r in caplog.records]}"
    )


def test_a_complete_listing_stays_quiet(storage, caplog):
    import logging

    storage.ensure_user("alice")
    for index in range(3):
        _entity(storage, "alice", f"Сущность {index}")

    with caplog.at_level(logging.WARNING, logger="jericho.storage"):
        rows = storage.list_entities("alice", limit=3)

    assert len(rows) == 3
    assert not [r for r in caplog.records if "entities" in r.getMessage()], (
        "полная выдача не должна жаловаться — иначе предупреждение станет фоном"
    )


def test_the_lifecycle_walk_does_not_carry_document_bodies(storage):
    """Обход честно неограничен — значит цена строки умножается на весь корпус.

    Замерено на 3000 объектах по 19 КБ (медиана настоящего архива владельца):
    `SELECT k.*` дал пик 37.7 МБ и 8.2 с, нужные колонки — 1.7 МБ и 3.0 с. Дашборд
    делает ДВА таких обхода на один рендер, оба на event loop.

    Вердикту тело не нужно вовсе, а интерфейс показывает 160 символов — тест
    закрепляет, что выборка не тянет документ целиком.
    """
    storage.ensure_user("alice")
    body = "тело документа " * 3000  # ~45 КБ
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="test",
        source_ref=new_id("src"),
        raw_content=body,
        content_type="text",
        content_hash=hashlib.sha256(b"big").hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id="alice",
        raw_object_id=raw.id,
        content=body,
        content_type="text",
        title="Большой",
        importance=0.05,
        quality_score=0.1,
        promotion_score=0.1,
    )
    storage.store_knowledge_object(ko)
    storage.execute("UPDATE knowledge_objects SET updated_at='2020-01-01T00:00:00Z' WHERE id=?", (ko.id,))
    storage.commit()

    candidates = storage.list_lifecycle_candidates("alice", days_threshold=1, limit=10)
    assert candidates, "объект не попал в кандидаты — стенд собран неверно"
    carried = candidates[0]["knowledge_object"]
    assert len(str(carried.get("content") or "")) <= 400, (
        f"обход тащит тело целиком: {len(str(carried.get('content')))} символов"
    )
    # То, на чём держится вердикт и что показывает интерфейс, — на месте.
    for field in ("id", "title", "importance", "quality_score", "content_type", "metadata_json"):
        assert field in carried, f"из выборки пропало поле {field}"
