"""Пересчёт векторов не должен оставлять поиск без индекса.

Вектор, посчитанный по неверному тексту, в базе неотличим от честного: хэш пишется
от полного текста, а посчитан вектор мог быть по укороченному. Замерено на корпусе
владельца 2026-07-29: 32 из 50 проверенных векторов описывали другой текст —
лечением отказа сервиса по длине было укорачивание всех текстов пачки вдвое.

Значит нужен способ сказать «пересчитай всё». Очевидная его форма — удалить строки
и дать индексатору посчитать заново — на корпусе в полторы тысячи документов
означает двадцать минут без плотного поиска. Поэтому пометка: старый вектор
остаётся на месте и отвечает на запросы, пока не придёт правильный.
"""

from __future__ import annotations

import hashlib

import pytest

from jericho.dedup import pack_vector
from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _make(storage, user_id: str, index: int) -> str:
    text = f"Документ номер {index} про поставки и сроки приёмки " * 20
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(f"{user_id}-{index}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=f"Документ {index}",
        summary="сводка",
    )
    storage.store_knowledge_object(knowledge)
    storage.upsert_knowledge_embeddings(
        [
            {
                "knowledge_object_id": knowledge.id,
                "user_id": user_id,
                "model": "test-embed",
                "dim": 2,
                "source_version": knowledge.version,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                "vector": pack_vector([0.5, 0.5]),
            }
        ]
    )
    return knowledge.id


def test_marking_stale_keeps_the_vector_answering_until_it_is_replaced(storage):
    """Главное свойство: помеченный вектор НЕ исчезает.

    Удаление было бы проще написать и хуже жить: на полутора тысячах документов
    плотный канал отключился бы на всё время пересчёта, а человек об этом узнал бы
    по ухудшившимся ответам, а не из сообщения.
    """
    storage.ensure_user("alice")
    ids = [_make(storage, "alice", index) for index in range(5)]

    before = storage.count_knowledge_embeddings("alice")
    marked = storage.mark_embeddings_stale(user_id="alice")

    assert marked == len(ids)
    assert storage.count_knowledge_embeddings("alice") == before, (
        "пометка удалила вектора — на время пересчёта корпус остался бы без плотного поиска"
    )
    # И при этом индексатор обязан увидеть их как требующие работы.
    assert storage.count_knowledge_missing_embedding("test-embed") == len(ids)


def test_marking_one_tenant_leaves_the_others_alone(storage):
    """Пересчёт одного арендатора не должен заставлять пересчитывать всех."""
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    [_make(storage, "alice", index) for index in range(3)]
    [_make(storage, "bob", index) for index in range(4)]

    assert storage.mark_embeddings_stale(user_id="alice") == 3
    assert storage.count_knowledge_missing_embedding("test-embed") == 3, (
        "помечены чужие вектора: команда с --user трогает только своего арендатора"
    )


def test_marking_everyone_covers_every_tenant(storage):
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    [_make(storage, "alice", index) for index in range(3)]
    [_make(storage, "bob", index) for index in range(4)]

    assert storage.mark_embeddings_stale() == 7
    assert storage.count_knowledge_missing_embedding("test-embed") == 7


def test_a_fresh_vector_written_after_the_mark_clears_it(storage):
    """Пометка снимается ТОЛЬКО записью нового вектора — иначе пересчёт был бы вечным."""
    storage.ensure_user("alice")
    object_id = _make(storage, "alice", 0)
    storage.mark_embeddings_stale()
    assert storage.count_knowledge_missing_embedding("test-embed") == 1

    record = storage.get_knowledge_object(object_id)
    assert record is not None
    storage.upsert_knowledge_embeddings(
        [
            {
                "knowledge_object_id": object_id,
                "user_id": "alice",
                "model": "test-embed",
                "dim": 2,
                "source_version": record["version"],
                "content_hash": hashlib.sha256(b"whatever").hexdigest(),
                "vector": pack_vector([0.1, 0.9]),
            }
        ]
    )
    assert storage.count_knowledge_missing_embedding("test-embed") == 0


@pytest.mark.parametrize("confirmed", [False, True])
def test_the_command_asks_before_it_makes_the_gpu_work(tmp_path, monkeypatch, confirmed):
    """Без --yes команда обязана ничего не делать: она заказывает работу на часы."""
    import argparse

    from jericho.cli import _reindex_embeddings

    calls: list[str | None] = []

    class _Storage:
        def mark_embeddings_stale(self, *, user_id=None):
            calls.append(user_id)
            return 7

        def record_event(self, *args, **kwargs): ...
        def close(self): ...

    monkeypatch.setattr("jericho.config.load_settings", lambda: object())
    monkeypatch.setattr("jericho.config.ensure_runtime_dirs", lambda settings: None)
    monkeypatch.setattr("jericho.storage.init_storage", lambda settings: _Storage())

    code = _reindex_embeddings(argparse.Namespace(user=None, yes=confirmed))

    if confirmed:
        assert code == 0 and calls == [None]
    else:
        assert code == 2 and calls == [], "команда пересчитала корпус без подтверждения"
