"""Тег, стоящий почти на всём, — не ось навигации, и показывать его нельзя.

Замерено на архиве владельца (1537 объектов): теги `document` и `application` стоят на
1524 из них — 99%, то есть выбор такого тега не сужает НИЧЕГО. Приписывались они
каждому загруженному файлу без анализа содержимого: литерал плюс первая часть
mime-типа, которая для docx/doc/xlsx/pdf всегда «application». Литерал при этом
дублировал `knowledge_kind` (он и так `document` у 1531 объекта).

Показ сортирует по убыванию частоты, поэтому на экран попадала строго худшая часть
распределения: и чипы в админке, и `/tags` в Telegram возглавляли ровно эти два.
Обещание в справке бота — «теги базы знаний с количеством записей» — выполнялось
буквально и было бесполезным.

Полезное при этом в базе есть и не показывалось: 903 тега из 1693 стоят на 2-77
объектах, то есть сужают до пяти процентов и меньше.
"""

from __future__ import annotations

import hashlib

from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _make(storage, user_id: str, index: int, tags: list[str]) -> None:
    text = f"Документ {index}. Тело про сроки и приёмку. " * 5
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
    storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id=user_id,
            raw_object_id=raw.id,
            content=text,
            content_type="text",
            title=f"Документ {index}",
            tags_json=tags,
        )
    )


def test_a_tag_on_almost_everything_is_not_offered(storage):
    """Ровно случай владельца: служебный тег на 99% записей и полезный на 6%."""
    storage.ensure_user("alice")
    for index in range(50):
        tags = ["document", "application"]
        if index < 3:
            tags.append("поверка")
        _make(storage, "alice", index, tags)

    offered = {item["tag"] for item in storage.list_knowledge_tags("alice")}
    assert "поверка" in offered, "редкий и полезный тег пропал"
    assert "document" not in offered, "тег на всех записях всё ещё предлагается как ось"
    assert "application" not in offered


def test_a_tag_on_a_quarter_of_the_corpus_survives(storage):
    """Порог — половина, а не пятая часть: тег на четверти сужает вчетверо.

    На корпусе владельца это разделяет `document` (99%) и `рядовой` (22%): первый
    бесполезен, второй осмыслен.
    """
    storage.ensure_user("alice")
    for index in range(40):
        _make(storage, "alice", index, ["рядовой"] if index < 10 else ["прочее"])

    offered = {item["tag"] for item in storage.list_knowledge_tags("alice")}
    assert "рядовой" in offered


def test_a_small_archive_keeps_every_tag(storage):
    """На десяти записях любой тег покроет заметную долю, а листать десять можно и так."""
    storage.ensure_user("alice")
    for index in range(6):
        _make(storage, "alice", index, ["document", "заметка"])

    offered = {item["tag"] for item in storage.list_knowledge_tags("alice")}
    assert offered == {"document", "заметка"}, "правило сработало на крошечном архиве"


def test_the_page_stays_the_requested_size_after_filtering(storage):
    """Отсев не должен укорачивать страницу: иначе `limit=25` вернёт десять."""
    storage.ensure_user("alice")
    for index in range(60):
        _make(storage, "alice", index, ["document", "application", f"тема{index % 30}"])

    page = storage.list_knowledge_tags("alice", limit=10)
    assert len(page) == 10, f"после отсева страница сократилась до {len(page)}"
    assert all(item["tag"].startswith("тема") for item in page)


def test_an_imported_file_no_longer_gets_the_two_useless_tags(settings, storage):
    """Показ их отбрасывает, но плодить мусор в базе всё равно незачем."""
    import asyncio

    from jericho.ingestion import IngestionPipeline
    from jericho.knowledge_graph import KnowledgeGraph

    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)
    storage.ensure_user("alice")
    body = "Приказ о поверке весового оборудования на складе. " * 20

    asyncio.run(pipeline.ingest_file("alice", None, body.encode("utf-8"), filename="приказ.txt"))
    tags = {tag for item in storage.list_knowledge_objects("alice") for tag in (item["tags_json"] or "")}
    inbox_tags = {tag for item in pipeline.list_inbox("alice") for tag in (item.get("suggested_tags") or [])}
    assert "application" not in tags and "application" not in inbox_tags
    assert "document" not in tags and "document" not in inbox_tags
