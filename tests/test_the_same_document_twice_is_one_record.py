"""Тот же ДОКУМЕНТ, пришедший другим файлом, — повтор, а не вторая запись.

Найдено при разборе очередей 2026-08-03. В живом архиве 200 конфликтов
«почти-дубликат» ждали разбора человеком, и разбор их состава показал:

    точные копии (текст совпал побайтово) ....  56
    версии (имя то же, текст другой) .........  54
    «разные» (и имя, и текст другие) .........  90  <- медиана похожести 0.99

То есть смысловых конфликтов там не было вовсе: один документ лежал в папке в
нескольких экземплярах, и все 200 пришли ОДНИМ импортом 29 июля.

Ни одна из 56 точных копий не совпадала по хешу файла: дедупликация сравнивала
байты, а тот же документ, пересохранённый из Word или положенный в две папки,
даёт другие байты при том же содержимом. Двести решений система создала себе
сама, и в 56 из них решать было нечего.

Порог похожести здесь по-прежнему не используется и использоваться не может —
`friday/dedup.py` замерил, что классы «дубликат» и «следующая заметка в серии»
перекрываются и никаким порогом не разделяются. Речь только о ТОЧНОМ совпадении
текста, где решать нечего по определению.
"""

from __future__ import annotations

import pytest

from friday.ingestion._base import _extracted_text_digest


def test_the_same_text_has_the_same_fingerprint() -> None:
    assert _extracted_text_digest("Приказ №214") == _extracted_text_digest("Приказ №214")


def test_line_breaks_are_not_a_difference_in_the_document() -> None:
    """Экспорт из Word и из PDF расставляет переносы по-разному."""
    assert _extracted_text_digest("Приказ №214\nо поверке") == _extracted_text_digest(
        "Приказ №214   о поверке"
    )
    assert _extracted_text_digest(" Приказ  №214 ") == _extracted_text_digest("Приказ №214")


def test_case_is_left_to_the_person() -> None:
    """«Приказ №214» и «ПРИКАЗ №214» — разные написания.

    Объявлять их одним документом здесь нельзя: для таких пар и существует
    очередь разбора, где решает человек.
    """
    assert _extracted_text_digest("Приказ №214") != _extracted_text_digest("ПРИКАЗ №214")


def test_no_text_means_no_fingerprint() -> None:
    """Мутация: вернуть хеш пустой строки — картинки склеятся в один документ.

    У картинки и у нечитаемого файла текста нет у всех сразу, и общий ключ
    объявил бы одним документом всё, что не разобралось.
    """
    assert _extracted_text_digest("") == ""
    assert _extracted_text_digest("   \n\t ") == ""
    assert _extracted_text_digest(None) == ""  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_a_resaved_document_does_not_become_a_second_record(settings, storage) -> None:
    """Мутация: убрать проверку по тексту — вернутся два объекта и конфликт.

    Два РАЗНЫХ файла (разные байты, разные имена) с одинаковым извлечённым
    текстом — это один документ. Ровно так выглядели 56 пар в живом архиве.
    """
    from friday.ingestion._base import _extracted_text_digest as digest
    from friday.storage.models import RawObject

    storage.ensure_user("alice", preset_key="admin")
    text = "Приказ №214 от 3 мая. О проведении поверки приборов."
    storage.store_raw_object(
        RawObject(
            id="raw-first",
            user_id="alice",
            source="upload",
            source_ref="upload:1",
            raw_content=text,
            content_type="file",
            metadata_json={"filename": "Приказ.docx", "sha256": "aaa", "text_sha256": digest(text)},
        )
    )

    # Тот же текст, набранный с другими переносами, — тот же документ.
    found = storage.find_file_by_extracted_text(
        "alice", digest("Приказ №214 от 3 мая.\nО проведении поверки приборов.")
    )

    assert found is not None, "тот же документ под другим файлом снова заведёт вторую запись"
    assert found["id"] == "raw-first"


def test_a_real_version_is_still_a_new_record(settings, storage) -> None:
    """Обратная сторона: изменённый документ обязан остаться отдельной записью.

    Иначе правка приказа молча слилась бы с его прежней редакцией — это потеря
    данных, а не дедупликация.
    """
    from friday.ingestion._base import _extracted_text_digest as digest
    from friday.storage.models import RawObject

    storage.ensure_user("alice", preset_key="admin")
    first = "Приказ №214. Поверку провести до 1 июня."
    storage.store_raw_object(
        RawObject(
            id="raw-v1",
            user_id="alice",
            source="upload",
            source_ref="upload:1",
            raw_content=first,
            content_type="file",
            metadata_json={"filename": "Приказ.docx", "text_sha256": digest(first)},
        )
    )

    changed = "Приказ №214. Поверку провести до 15 июня."
    assert storage.find_file_by_extracted_text("alice", digest(changed)) is None


def test_another_tenant_does_not_see_it(settings, storage) -> None:
    """Дедупликация не должна становиться каналом чтения чужого архива."""
    from friday.ingestion._base import _extracted_text_digest as digest
    from friday.storage.models import RawObject

    storage.ensure_user("alice", preset_key="admin")
    storage.ensure_user("bob", preset_key="admin")
    text = "Секретная записка"
    storage.store_raw_object(
        RawObject(
            id="raw-alice",
            user_id="alice",
            source="upload",
            source_ref="upload:1",
            raw_content=text,
            content_type="file",
            metadata_json={"filename": "з.docx", "text_sha256": digest(text)},
        )
    )

    assert storage.find_file_by_extracted_text("bob", digest(text)) is None


def test_a_deleted_document_does_not_block_a_new_one(settings, storage) -> None:
    """Удалённое не должно мешать принять документ заново."""
    from friday.ingestion._base import _extracted_text_digest as digest
    from friday.storage.models import RawObject

    storage.ensure_user("alice", preset_key="admin")
    text = "Ведомость инструктажа"
    storage.store_raw_object(
        RawObject(
            id="raw-gone",
            user_id="alice",
            source="upload",
            source_ref="upload:1",
            raw_content=text,
            content_type="file",
            metadata_json={"filename": "в.docx", "text_sha256": digest(text)},
            deleted_at="2026-08-01T00:00:00+00:00",
        )
    )

    assert storage.find_file_by_extracted_text("alice", digest(text)) is None


def test_the_earliest_record_wins(settings, storage) -> None:
    """Повтор воспроизводит ПЕРВОЕ решение, а не последнее — как и у хеша файла."""
    from friday.ingestion._base import _extracted_text_digest as digest
    from friday.storage.models import RawObject

    storage.ensure_user("alice", preset_key="admin")
    text = "Строевая записка"
    for index, when in enumerate(["2026-07-29T10:00:00+00:00", "2026-07-29T12:00:00+00:00"]):
        storage.store_raw_object(
            RawObject(
                id=f"raw-{index}",
                user_id="alice",
                source="upload",
                source_ref=f"upload:{index}",
                raw_content=text,
                content_type="file",
                metadata_json={"filename": "с.docx", "text_sha256": digest(text)},
                received_at=when,
            )
        )

    assert storage.find_file_by_extracted_text("alice", digest(text))["id"] == "raw-0"


@pytest.mark.anyio
async def test_ingesting_the_same_document_twice_gives_one_record(settings, storage) -> None:
    """Мутация: отключить проверку в приёме — появятся два объекта, тест краснеет.

    Первая редакция этого теста читала исходник `ingest_file` и мутацию НЕ
    ловила: имя `find_file_by_extracted_text` оставалось в тексте, даже когда
    ветка вокруг него была выключена. Проверять надо приём целиком.

    Два файла: разные байты (разные переносы строк), разные имена — один текст.
    Ровно так выглядели 56 пар в живом архиве.
    """
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph

    storage.ensure_user("alice", preset_key="admin")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))

    first = await pipeline.ingest_file(
        "alice",
        None,
        b"\xd0\x9f\xd1\x80\xd0\xb8\xd0\xba\xd0\xb0\xd0\xb7 214\n\xd0\xbe \xd0\xbf\xd0\xbe\xd0\xb2\xd0\xb5\xd1\x80\xd0\xba\xd0\xb5",
        filename="prikaz.txt",
        mime_type="text/plain",
        source_ref="upload:one",
    )
    second = await pipeline.ingest_file(
        "alice",
        None,
        b"\xd0\x9f\xd1\x80\xd0\xb8\xd0\xba\xd0\xb0\xd0\xb7 214   \xd0\xbe \xd0\xbf\xd0\xbe\xd0\xb2\xd0\xb5\xd1\x80\xd0\xba\xd0\xb5",
        filename="prikaz-kopiya.txt",
        mime_type="text/plain",
        source_ref="upload:two",
    )

    assert second.get("idempotent_replay") is True, "тот же документ завёл вторую запись"
    assert second.get("raw_object_id") == first.get("raw_object_id")
    live = storage.execute(
        "SELECT COUNT(*) AS n FROM raw_objects WHERE user_id='alice' AND deleted_at IS NULL"
    ).fetchone()["n"]
    assert live == 1, f"в архиве {live} записи вместо одной"


@pytest.mark.anyio
async def test_a_changed_document_is_ingested_as_its_own_record(settings, storage) -> None:
    """Обратная сторона той же проверки — иначе её можно «выполнить», не приняв ничего."""
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph

    storage.ensure_user("alice", preset_key="admin")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))

    await pipeline.ingest_file(
        "alice",
        None,
        "Поверку провести до 1 июня".encode(),
        filename="a.txt",
        mime_type="text/plain",
        source_ref="upload:1",
    )
    changed = await pipeline.ingest_file(
        "alice",
        None,
        "Поверку провести до 15 июня".encode(),
        filename="b.txt",
        mime_type="text/plain",
        source_ref="upload:2",
    )

    assert not changed.get("idempotent_replay"), "изменённый документ молча слился с прежним"
    live = storage.execute(
        "SELECT COUNT(*) AS n FROM raw_objects WHERE user_id='alice' AND deleted_at IS NULL"
    ).fetchone()["n"]
    assert live == 2
