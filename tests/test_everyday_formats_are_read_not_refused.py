"""Обиходные форматы читаются, а не отвергаются как незнакомые.

Диспетчер `extract` принимал `.odt` и молча отвергал `.ods` и `.odp` — при том
что текст у всей семьи OpenDocument лежит в одном и том же `content.xml`, и
разборщик для него уже написан. Это не решение, а недосмотр: таблица и
презентация в свободном офисе — обиходные документы, а человек получал
«формат не поддерживается».

`.eml` отвергался тоже, хотя почта — обычный текст с заголовками, и стандартная
библиотека разбирает её без единой зависимости; разбирал письма только
орган-импортёр почтового ящика, то есть присланный файл читать было нечем.
`.epub` — обычный zip с XHTML внутри, и очиститель разметки в проекте уже есть.

Что здесь НЕ делается и почему: `.xls` (старый двоичный Excel) требует отдельной
зависимости, `.msg` — разбора OLE-контейнера Outlook. Оба названы в предложении
как открытые, а не сделаны наполовину.
"""

from __future__ import annotations

import io
import zipfile

from friday.documents import DocumentExtractor


def _opendocument(mime: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", mime)
        archive.writestr(
            "content.xml",
            "<office><table><cell>Смета на март</cell><cell>1 200 000</cell></table></office>",
        )
    return buffer.getvalue()


def _epub() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("OEBPS/ch1.xhtml", "<html><body><h1>Глава первая</h1><p>Начало.</p></body></html>")
        archive.writestr("OEBPS/ch2.xhtml", "<html><body><p>Продолжение истории.</p></body></html>")
    return buffer.getvalue()


EMAIL = (
    "From: Ivan <ivan@example.ru>\r\n"
    "To: Petr <petr@example.ru>\r\n"
    "Date: Wed, 12 Apr 2023 10:00:00 +0300\r\n"
    "Subject: =?utf-8?B?0KHQvNC10YLQsA==?=\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n\r\n"
    "Смета согласована, отправляю в работу.\r\n"
).encode()


def _extractor() -> DocumentExtractor:
    return DocumentExtractor(secret_values=())


def test_a_spreadsheet_from_the_free_office_is_read() -> None:
    result = _extractor().extract(
        _opendocument("application/vnd.oasis.opendocument.spreadsheet"),
        "смета.ods",
        "application/vnd.oasis.opendocument.spreadsheet",
    )
    assert result.success, result.error
    assert "Смета на март" in result.text
    assert "1 200 000" in result.text


def test_a_presentation_from_the_free_office_is_read() -> None:
    result = _extractor().extract(
        _opendocument("application/vnd.oasis.opendocument.presentation"),
        "доклад.odp",
        "application/vnd.oasis.opendocument.presentation",
    )
    assert result.success, result.error
    assert "Смета на март" in result.text


def test_a_letter_is_read_with_the_headers_a_person_reads() -> None:
    result = _extractor().extract(EMAIL, "письмо.eml", "message/rfc822")
    assert result.success, result.error
    assert "Смета согласована" in result.text
    # Заголовки — часть содержания письма: без «от кого» и «когда» текст письма
    # теряет половину смысла и не находится поиском по отправителю.
    assert "ivan@example.ru" in result.text
    assert "Тема: Смета" in result.text


def test_a_letter_carries_its_own_date() -> None:
    """Дата письма — его собственная, а не день, когда файл попал в архив."""
    result = _extractor().extract(EMAIL, "письмо.eml", "message/rfc822")
    assert result.metadata.get("document_date") == "2023-04-12", result.metadata


def test_a_book_is_read_chapter_by_chapter() -> None:
    result = _extractor().extract(_epub(), "книга.epub", "application/epub+zip")
    assert result.success, result.error
    assert "Глава первая" in result.text
    assert "Продолжение истории" in result.text
    assert result.metadata.get("chapters_read") == 2, result.metadata


def test_the_extension_alone_is_enough() -> None:
    """Тип приходит не всегда: у файла с диска его может не быть вовсе."""
    result = _extractor().extract(
        _opendocument("application/vnd.oasis.opendocument.spreadsheet"), "смета.ods", ""
    )
    assert result.success, result.error
    assert "Смета на март" in result.text


def test_a_truly_unknown_format_is_still_refused() -> None:
    """Расширение списка не должно превратиться в «принимаем всё».

    Отказ — тоже ответ, и он честнее, чем мусор из двоичных байтов, выданный за
    текст документа.
    """
    result = _extractor().extract(b"\x00\x01\x02binary", "нечто.bin", "application/octet-stream")
    assert not result.success
    assert result.error == "unsupported_document_format"
