"""Три потери, о которых система знала и молчала.

Каждая — тот же класс «молчаливый обрез», который в проекте ловили уже пять раз.
Общее у них одно: код ЗНАЛ о потере и записывал её, а до человека она не
доезжала, потому что читателя у признака не было.

* **Архив разобран не весь.** TAR ставил `archive_budget_exhausted`, ZIP и RAR не
  ставили ничего — при том что ZIP на входе встречается чаще всех. Человек
  получал список из тридцати имён и содержимое двадцати четырёх, и отличить
  «прочитано всё» от «прочитана часть» было нечем.
* **Исходник обрезан ДО разбора.** `source_truncated_for_parse` писали пять
  разборщиков, читал ноль потребителей: обещание без механизма, у которого,
  как всегда, два конца.
* **Причина отказа известна коду.** Битый файл и незнакомый формат приходили к
  человеку одной фразой «текст извлечь не удалось» — а следующий шаг у них
  разный: один пересохранить, другой прислать в другом виде.

Поля ПЛОСКИЕ и проверяются через настоящую проекцию: вложенный словарь до моста
не доезжает, и на этом уже обжигались с `parse_pages_truncated`.
"""

from __future__ import annotations

import io
import zipfile

from friday.api.projections import public_chat_ingestion
from friday.documents import DocumentExtractor
from friday.telegram_bridge._callbacks import _file_fate_line


def _zip_of(count: int) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(count):
            archive.writestr(f"файл{index:02d}.txt", f"Содержимое {index}")
    return buffer.getvalue()


def test_a_zip_now_says_it_stopped_like_tar_always_did() -> None:
    extractor = DocumentExtractor(secret_values=())
    result = extractor.extract(_zip_of(30), "архив.zip", "application/zip")

    assert result.metadata.get("archive_budget_exhausted") is True, result.metadata
    assert result.metadata["files"] > result.metadata["previewed_files"]


def test_a_small_zip_says_nothing_extra() -> None:
    """Признак только там, где потеря есть: иначе он перестанет значить что-либо."""
    extractor = DocumentExtractor(secret_values=())
    result = extractor.extract(_zip_of(3), "архив.zip", "application/zip")

    assert "archive_budget_exhausted" not in result.metadata, result.metadata


def test_the_person_hears_about_the_unread_part_of_the_archive() -> None:
    line = _file_fate_line(
        {
            "promoted": True,
            "extraction": {
                "success": True,
                "archive_truncated": True,
                "archive_files": 30,
                "archive_files_read": 24,
            },
        }
    )
    assert "30" in line and "24" in line, line
    assert "остальные" in line, line


def test_one_partly_parsed_archive_member_is_not_reported_as_whole() -> None:
    """Preview count is not a completeness count for nested parsers."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # The child CSV reaches its row ceiling while remaining below the
        # archive member-byte ceiling.  It was decompressed, but not read whole.
        archive.writestr("many.csv", "\n" * 100_001)
    result = DocumentExtractor(secret_values=()).extract(
        buffer.getvalue(),
        "one-partial-member.zip",
        "application/zip",
    )
    assert result.metadata["archive_budget_exhausted"] is True
    assert result.metadata["files"] == result.metadata["previewed_files"] == 1

    line = _file_fate_line(
        {
            "promoted": True,
            "extraction": {
                "success": True,
                "archive_truncated": True,
                "archive_files": result.metadata["files"],
                "archive_files_read": result.metadata["previewed_files"],
            },
        }
    )
    assert "разобран не целиком" in line, line
    assert "прочитан только частично" in line, line
    assert line != "✅ Файл стал знанием — можно спрашивать."


def test_the_person_hears_that_only_the_beginning_was_parsed() -> None:
    line = _file_fate_line(
        {
            "promoted": True,
            "extraction": {"success": True, "source_truncated_for_parse": True},
        }
    )
    assert "начало" in line, line


def test_an_unknown_format_says_what_to_do_next() -> None:
    """Отличается от «файл нечитаем»: следующий шаг у них разный."""
    line = _file_fate_line(
        {
            "queued_for_review": True,
            "extraction": {"success": False, "unsupported_format": True},
        }
    )
    assert "формат" in line, line
    assert "PDF" in line, line
    assert "я вижу файл, но не его содержимое" not in line, line


def test_a_broken_file_still_says_it_is_unreadable() -> None:
    """Вторая половина той же границы: битый файл — не незнакомый формат."""
    line = _file_fate_line(
        {
            "queued_for_review": True,
            "extraction": {"success": False, "unsupported_format": False},
        }
    )
    assert "я вижу файл, но не его содержимое" in line, line
    assert "формат я пока не читаю" not in line, line


def test_the_three_signs_survive_the_public_projection() -> None:
    """Между разбором и человеком стоит проекция, и она пропускает по списку."""
    published = public_chat_ingestion(
        {
            "message": "Готово.",
            "file_ingestion": {
                "action": "review",
                "queued_for_review": True,
                "extraction": {
                    "success": True,
                    "archive_truncated": True,
                    "archive_files": 30,
                    "archive_files_read": 24,
                    "source_truncated_for_parse": True,
                    "unsupported_format": False,
                },
            },
        }
    )
    extraction = (published.get("file_ingestion") or {}).get("extraction") or {}
    assert extraction.get("archive_truncated") is True, published
    assert extraction.get("archive_files") == 30, published
    assert extraction.get("archive_files_read") == 24, published
    assert extraction.get("source_truncated_for_parse") is True, published


def test_transient_flat_reliability_facts_survive_the_public_projection() -> None:
    published = public_chat_ingestion(
        {
            "message": "Готово.",
            "file_ingestion": {
                "action": "transient",
                "promoted": False,
                "queued_for_review": False,
                "raw_object_id": None,
                "extraction_success": True,
                "empty_text": False,
                "text_truncated": True,
                "parse_deadline_reached": True,
                "archive_truncated": True,
                "archive_files": 30,
                "archive_files_read": 24,
                "source_truncated_for_parse": True,
                "unsupported_format": False,
            },
        }
    )

    receipt = published["file_ingestion"]
    assert receipt == {
        "promoted": False,
        "queued_for_review": False,
        "persisted": False,
        "action": "transient",
        "extraction_success": True,
        "empty_text": False,
        "text_truncated": True,
        "parse_deadline_reached": True,
        "archive_truncated": True,
        "source_truncated_for_parse": True,
        "unsupported_format": False,
        "archive_files": 30,
        "archive_files_read": 24,
    }


def test_a_photo_is_not_called_an_unknown_format(settings, storage) -> None:
    """Картинку и звук Friday читает ДРУГИМИ путями — зрением и расшифровкой.

    Разбор текста их не берёт, и без этой границы человек получил бы на
    присланную фотографию фразу «пришлите в PDF» — то есть ложь о собственных
    возможностях. Признак ставится только там, где формат не читает НИКТО.
    """
    import asyncio

    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph

    def _unsupported(payload: bytes, name: str, mime: str) -> bool:
        pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
        result = asyncio.run(
            pipeline.ingest_file(
                "alice", None, payload, filename=name, mime_type=mime, source_ref=f"test:{name}"
            )
        )
        return bool(result["extraction"].get("unsupported_format"))

    assert _unsupported(b"\x00\x01binary", "макет.pages", "application/vnd.apple.pages") is True
    assert _unsupported(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "снимок.png", "image/png") is False
