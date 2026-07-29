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


def test_the_mark_defeats_the_reuse_cache_or_it_recomputes_nothing(storage):
    """Пометка версии без стирания хэша — пересчёт, который ничего не пересчитывает.

    Замерено на живом пересчёте 2026-07-29: 1208 объектов из 1537 «обновились» за
    две минуты по нулю секунд на пачку. Переиспользование по хэшу текста вернуло те
    же самые негодные вектора — по своим правилам совершенно верно: хэш писался от
    полного текста, а вектор считался по укороченному, и различить их хранилище не
    может. Команда отчиталась об успехе, индекс остался прежним.

    Значит признак «этот вектор уже есть для такого текста» обязан исчезнуть вместе
    с пометкой, иначе вся затея — переписывание номера версии.
    """
    import hashlib as _hashlib

    storage.ensure_user("alice")
    object_id = _make(storage, "alice", 0)
    record = storage.get_knowledge_object(object_id)
    assert record is not None
    text_hash = _hashlib.sha256(str(record["content"]).encode()).hexdigest()

    # До пометки текст узнаётся по хэшу — на этом и держится дешёвый переимпорт.
    assert storage.get_vectors_by_content_hash([text_hash], "test-embed")

    storage.mark_embeddings_stale()

    assert not storage.get_vectors_by_content_hash([text_hash], "test-embed"), (
        "хэш пережил пометку — индексатор возьмёт из кэша тот же негодный вектор"
    )
    assert not storage.get_reusable_vectors([object_id], "test-embed").get(object_id), (
        "объект по-прежнему предъявляет свой прежний вектор как годный к переиспользованию"
    )
    # И при этом вектор никуда не делся: поиск продолжает отвечать.
    assert storage.count_knowledge_embeddings("alice") == 1


def test_passage_vectors_are_marked_with_their_object(storage):
    """Чанки укорачивались в тех же пачках, значит и пересчитываться должны вместе."""
    import hashlib as _hashlib

    from jericho.dedup import pack_vector

    storage.ensure_user("alice")
    object_id = _make(storage, "alice", 0)
    chunk_text = "кусок документа про сроки приёмки"
    chunk_hash = _hashlib.sha256(chunk_text.encode()).hexdigest()
    # Пассажи пишутся отдельным отображением, а не в общий список: они живут в своей
    # таблице, и запись их как объектных векторов молча перезаписала бы объектный.
    storage.upsert_knowledge_vectors(
        [],
        {
            object_id: [
                {
                    "knowledge_object_id": object_id,
                    "user_id": "alice",
                    "model": "test-embed",
                    "dim": 2,
                    "source_version": 1,
                    "content_hash": chunk_hash,
                    "chunk_scheme": "v2",
                    "chunk_index": 0,
                    "start_char": 0,
                    "end_char": len(chunk_text),
                    "vector": pack_vector([0.3, 0.7]),
                }
            ]
        },
    )
    assert storage.get_vectors_by_content_hash([chunk_hash], "test-embed")

    storage.mark_embeddings_stale()

    assert not storage.get_vectors_by_content_hash([chunk_hash], "test-embed"), (
        "вектор пассажа остался в кэше переиспользования и вернётся вместо пересчёта"
    )
    assert storage.count_knowledge_chunk_embeddings("alice") == 1, "пассаж удалён, а должен был остаться"
