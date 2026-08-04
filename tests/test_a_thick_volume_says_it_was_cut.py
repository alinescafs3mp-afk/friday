"""Том толще потолка не выдаёт себя за прочитанный целиком.

Разборщик PDF читает не больше 250 страниц (`itertools.islice(reader.pages, 250)`),
и делал это МОЛЧА: 251-я страница и дальше не попадали ни в текст, ни в признаки.
Человек, приславший том на 400 страниц, узнавал об этом, только не найдя в нём того,
что там есть, — и решал, что система не умеет искать.

«Молчаливый обрез» на этом проекте — отдельный класс, найденный за одни сутки
четырежды: голос на двухтысячном знаке, разбор по сроку, картинка без предела,
документ 3.75 млн знаков. Дважды признак усечения уже существовал и терялся по
дороге к человеку — поэтому здесь проверяется вся дорога целиком, от метаданных
разборщика до фразы, которую человек читает в Telegram.

Причина обрезки названа отдельно от «не уместилось по объёму»: там помогает вопрос о
начале документа, а здесь конца тома система не видела вовсе.
"""

from __future__ import annotations

import pytest


def _pdf_with(pages: int) -> bytes:
    """Настоящий PDF с заданным числом страниц, каждая со своим текстом."""
    pypdf = pytest.importorskip("pypdf")
    reportlab_canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    import io

    buffer = io.BytesIO()
    pdf = reportlab_canvas.Canvas(buffer)
    for number in range(pages):
        pdf.drawString(72, 720, f"stranica {number + 1} soderzhanie")
        pdf.showPage()
    pdf.save()
    data = buffer.getvalue()
    assert len(pypdf.PdfReader(io.BytesIO(data)).pages) == pages
    return data


def test_a_volume_over_the_cap_is_marked():
    """Мутация: убрать `pages_truncated` — тест краснеет.

    Проверяются ОБА числа: сколько прочитано и сколько было. Одно без другого
    не отвечает на вопрос «много ли потеряно».
    """
    from friday.documents import DocumentExtractor

    extractor = DocumentExtractor()
    result = extractor.extract(_pdf_with(260), "том.pdf", "application/pdf")

    assert result.success
    assert result.metadata.get("pages_truncated"), "251-я страница потеряна молча"
    assert result.metadata.get("total_pages") == 260
    assert int(result.metadata.get("pages_read") or 0) == 250
    assert result.metadata.get("extraction_truncated"), "общий признак усечения не поднят"


def test_a_document_within_the_cap_says_nothing():
    """Ошибка в другую сторону: предупреждение не по делу обесценивает те, что по делу."""
    from friday.documents import DocumentExtractor

    extractor = DocumentExtractor()
    result = extractor.extract(_pdf_with(3), "тонкий.pdf", "application/pdf")

    assert result.success
    assert not result.metadata.get("pages_truncated")
    assert result.metadata.get("total_pages") == 3


def test_the_person_is_told_how_much_was_read():
    """Признак доезжает до ФРАЗЫ, которую человек читает.

    Дважды на этом проекте признак усечения существовал и терялся по дороге —
    поэтому проверяется потребитель, а не наличие поля.
    """
    from friday.telegram_bridge._callbacks import _file_fate_line

    line = _file_fate_line(
        {
            "promoted": True,
            "extraction": {
                "parse_pages_truncated": True,
                "parse_pages_read": 250,
                "parse_total_pages": 400,
            },
        }
    )
    assert "400" in line and "250" in line, f"человеку не сказали, сколько потеряно: {line!r}"

    quiet = _file_fate_line(
        {"promoted": True, "extraction": {"parse_pages_read": 3, "parse_total_pages": 3}}
    )
    assert "страниц" not in quiet, f"предупреждение не по делу: {quiet!r}"
