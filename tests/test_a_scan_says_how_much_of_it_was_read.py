"""Скан на сорок страниц не выдаёт себя за прочитанный целиком.

У скана нет текстового слоя, поэтому его читают ГЛАЗАМИ модели: несколько
страниц уходят картинками в запрос. Цена честная — каждая страница стоит места в
запросе, и класть туда весь том нельзя. Молчание о цене — нет: человек получал
документ и спрашивал по нему, будучи уверен, что прочитано всё.

Числа считаются отдельным дешёвым проходом (у PDF это оглавление), а не
выводятся из числа взятых картинок: у одной страницы их может быть несколько, и
«взято четыре» не значит «страниц четыре».

Поля ПЛОСКИЕ (`vision_pages_total`, `vision_pages_read`), а не вложенный словарь,
и это не стиль: публичная проекция пропускает наружу только перечисленные имена,
и вложенный `vision` до моста не доезжает вовсе. На этом уже обжигались с
`parse_pages_truncated` — правка доехала до базы и не доехала до человека, а
проба была зелёной, потому что звала потребителя с рукотворным словарём.
"""

from __future__ import annotations

import io

import pytest

from friday.api.projections import public_chat_ingestion
from friday.documents import DocumentExtractor
from friday.telegram_bridge._callbacks import _file_fate_line

pypdf = pytest.importorskip("pypdf")


def _scan(pages: int) -> bytes:
    """PDF на `pages` страниц без единого знака текста — то есть скан."""
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_the_extractor_counts_the_pages_it_did_not_read() -> None:
    """Опора: разборщик знает объём документа, а не только взятое."""
    extractor = DocumentExtractor(secret_values=())
    assert extractor.visual_source_pages(_scan(40), "скан.pdf", "application/pdf") == 40


def test_an_image_is_one_page_and_a_text_file_is_none() -> None:
    """Считается то, что действительно может уйти в зрение, а не всё подряд."""
    extractor = DocumentExtractor(secret_values=())
    assert extractor.visual_source_pages(b"\x89PNG\r\n\x1a\n", "снимок.png", "image/png") == 1
    assert extractor.visual_source_pages(b"just text", "note.txt", "text/plain") == 0


def test_the_person_is_told_how_much_was_actually_read() -> None:
    """Строка о судьбе файла называет и объём, и прочитанное."""
    line = _file_fate_line(
        {
            "queued_for_review": True,
            "extraction": {"success": True, "chars": 900, "vision_pages_total": 40, "vision_pages_read": 4},
        }
    )
    assert "4" in line and "40" in line, line
    assert "остальные" in line, line


def test_a_fully_read_document_says_nothing_extra() -> None:
    """Предупреждение только там, где есть о чём предупреждать."""
    line = _file_fate_line(
        {
            "promoted": True,
            "extraction": {"success": True, "chars": 900, "vision_pages_total": 2, "vision_pages_read": 2},
        }
    )
    assert "страниц из" not in line, line


def test_the_numbers_survive_the_public_projection() -> None:
    """Между разбором и человеком стоит проекция, и она пропускает по СПИСКУ.

    Мутация «убрать поля из `_EXTRACTION_COUNT_FIELDS`» должна ронять именно эту
    пробу: без неё числа считались бы и терялись по дороге, а человеку по-прежнему
    никто ничего не сказал бы.
    """
    # Форма ровно та, что уходит в чат: результат разбора файла лежит под
    # `file_ingestion`, и чистится он `public_ingestion_receipt(file=True)` —
    # именно там стоит белый список имён.
    published = public_chat_ingestion(
        {
            "message": "Готово.",
            "file_ingestion": {
                "action": "review",
                "queued_for_review": True,
                "extraction": {
                    "success": True,
                    "chars": 900,
                    "vision_pages_total": 40,
                    "vision_pages_read": 4,
                },
            },
        }
    )
    receipt = published.get("file_ingestion") or {}
    extraction = receipt.get("extraction") or {}
    assert extraction.get("vision_pages_total") == 40, published
    assert extraction.get("vision_pages_read") == 4, published
    assert "40" in _file_fate_line(receipt), published
