"""Колонтитулы, сноски, примечания и надписи docx читаются, а не помечаются.

Раньше их наличие только ОТНИМАЛО у документа полноту: `coverage.reasons`
получал `header_footer` или `unsupported_body_content`, а человек не получал из
них ни строчки. На бланках именно там стоят «Согласовано», «Исполнитель», номер и
дата — то есть ровно то, что ищут; в служебной записке сноска несёт оговорку, без
которой смысл абзаца меняется.

Текст уходит В ТОТ ЖЕ построитель, что и тело документа, и это не деталь: индекс
офисной структуры привязан к тексту отпечатком и проверяется на покрытие без
дыр. Индекс, посчитанный по одному куску и приложенный к другому, был бы отброшен
проверкой МОЛЧА — вместе с точным путём по таблицам, ради которого он и заведён.

Каждый кусок получает метку («[Колонтитул] …»): без неё «Согласовано: Петров» из
нижнего поля страницы читалось бы как фраза из текста приказа.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from friday.documents import DocumentExtractor, validate_office_structure_index

Document = pytest.importorskip("docx").Document
nsdecls = pytest.importorskip("docx.oxml.ns").nsdecls
parse_xml = pytest.importorskip("docx.oxml").parse_xml


def _bytes(document) -> bytes:
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def _extract(data: bytes):
    return DocumentExtractor(secret_values=()).extract(data, "документ.docx", "")


def test_a_header_and_a_footer_reach_the_text() -> None:
    document = Document()
    document.add_paragraph("Приказ по основной деятельности.")
    document.sections[0].header.paragraphs[0].text = "Согласовано: Петров И.И."
    document.sections[0].footer.paragraphs[0].text = "Исполнитель: Сидоров, тел. 12-34"

    result = _extract(_bytes(document))

    assert "Согласовано: Петров И.И." in result.text
    assert "Исполнитель: Сидоров" in result.text
    assert "Приказ по основной деятельности." in result.text


def test_each_piece_says_where_it_came_from() -> None:
    """Без метки подпись из колонтитула читалась бы как фраза приказа."""
    document = Document()
    document.add_paragraph("Тело.")
    document.sections[0].header.paragraphs[0].text = "Согласовано"

    result = _extract(_bytes(document))

    assert "[Колонтитул] Согласовано" in result.text


def test_a_text_box_is_read_too() -> None:
    """Надпись внутри рисунка `python-docx` не отдаёт вовсе.

    `Paragraph.text` собирает только прямые прогоны абзаца, а надпись — отдельный
    контейнер. На схемах и бланках там стоят подписи и номера.
    """
    document = Document()
    paragraph = document.add_paragraph("Видимое тело.")
    paragraph._p.append(
        parse_xml(
            f"<w:r {nsdecls('w')}><w:pict><w:txbxContent><w:p><w:r>"
            "<w:t>Надпись на схеме</w:t></w:r></w:p></w:txbxContent></w:pict></w:r>"
        )
    )

    result = _extract(_bytes(document))

    assert "Надпись на схеме" in result.text


def test_a_footnote_is_read() -> None:
    document = Document()
    document.add_paragraph("Основной текст.")
    original = _bytes(document)
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(original)) as source,
        zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target,
    ):
        for item in source.infolist():
            target.writestr(item, source.read(item.filename))
        target.writestr(
            "word/footnotes.xml",
            (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:footnotes {nsdecls("w")}><w:footnote w:id="1"><w:p><w:r>'
                "<w:t>Оговорка из сноски</w:t></w:r></w:p></w:footnote></w:footnotes>"
            ),
        )

    result = _extract(output.getvalue())

    assert "Оговорка из сноски" in result.text


def test_the_structure_index_survives_the_addition() -> None:
    """Главное свойство: индекс не должен молча отвалиться.

    Он привязан к тексту отпечатком, а проверка требует, чтобы отрезки блоков
    покрывали текст встык. Служебный кусок без своей строки в индексе оставил бы
    дыру — и весь индекс, вместе с точным путём по таблицам, был бы отброшен.
    """
    document = Document()
    document.add_paragraph("Тело приказа.")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "ФИО"
    table.rows[0].cells[1].text = "Роль"
    document.sections[0].header.paragraphs[0].text = "Согласовано"

    result = _extract(_bytes(document))
    index = result.office_structure_index

    assert index is not None, "индекс офисной структуры пропал вместе с колонтитулом"
    assert validate_office_structure_index(index, result.text) == index
    assert index["complete"] is True


def test_the_index_still_carries_no_document_text() -> None:
    """Свойство, которое не менялось: индекс носит места, а не содержимое."""
    import json

    document = Document()
    document.add_paragraph("Тело.")
    document.sections[0].header.paragraphs[0].text = "СЕКРЕТНАЯ-ПОДПИСЬ"

    result = _extract(_bytes(document))
    encoded = json.dumps(result.office_structure_index, ensure_ascii=False)

    assert "СЕКРЕТНАЯ-ПОДПИСЬ" not in encoded


def test_a_document_without_auxiliary_parts_is_unchanged() -> None:
    """Ни лишней метки, ни лишнего перевода строки у обычного документа."""
    document = Document()
    document.add_paragraph("Просто текст.")

    result = _extract(_bytes(document))

    assert result.text == "Просто текст."
