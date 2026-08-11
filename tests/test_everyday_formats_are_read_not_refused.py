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


def test_opendocument_carries_only_bounded_standard_metadata() -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr("content.xml", "<office><text>Тело документа</text></office>")
        archive.writestr(
            "meta.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<office:document-meta
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:xlink="http://www.w3.org/1999/xlink">
 <office:meta>
  <dc:title>План учений</dc:title>
  <dc:subject>Подготовка</dc:subject>
  <meta:initial-creator>Иван Петров</meta:initial-creator>
  <dc:creator>Мария Сидорова</dc:creator>
  <meta:printed-by>Сергей Крылов</meta:printed-by>
  <meta:keyword>учения</meta:keyword>
  <meta:creation-date>2024-03-04T12:30:00Z</meta:creation-date>
  <dc:date>2024-03-05T08:00:00+03:00</dc:date>
  <meta:print-date>2024-03-06T09:00:00Z</meta:print-date>
  <meta:template meta:title="Служебный документ" meta:date="2024-01-02T03:04:05Z"
      xlink:href="templates/service.ott"/>
  <meta:auto-reload xlink:href="https://example.test/source" meta:delay="PT30M"/>
  <meta:hyperlink-behaviour office:target-frame-name="_blank" xlink:show="new"/>
  <meta:editing-cycles>7</meta:editing-cycles>
  <meta:editing-duration>PT12M3S</meta:editing-duration>
  <meta:document-statistic meta:page-count="3" meta:word-count="420"
      meta:character-count="2100" meta:table-count="2" meta:image-count="1"
      meta:object-count="0" meta:paragraph-count="18"
      meta:non-whitespace-character-count="1700"/>
  <meta:user-defined meta:name="Подразделение" meta:value-type="string">Отдел 7</meta:user-defined>
 </office:meta>
</office:document-meta>""",
        )
        archive.writestr(
            "META-INF/documentsignatures.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<dsig:document-signatures
 xmlns:dsig="urn:oasis:names:tc:opendocument:xmlns:digitalsignature:1.0"
 xmlns:ds="http://www.w3.org/2000/09/xmldsig#"
 xmlns:xades="http://uri.etsi.org/01903/v1.3.2#">
 <ds:Signature Id="signature-1">
  <ds:KeyInfo><ds:X509Data><ds:X509SubjectName>CN=Иван Иванов</ds:X509SubjectName></ds:X509Data></ds:KeyInfo>
  <ds:Object><xades:QualifyingProperties><xades:SignedProperties>
   <xades:SignedSignatureProperties><xades:SigningTime>2024-03-05T08:01:00Z</xades:SigningTime></xades:SignedSignatureProperties>
  </xades:SignedProperties></xades:QualifyingProperties></ds:Object>
 </ds:Signature>
</dsig:document-signatures>""",
        )

    extractor = _extractor()
    result = extractor.extract(payload.getvalue(), "план.odt")
    header_only = extractor.extract_document_metadata(payload.getvalue(), "план.odt")

    assert result.success, result.error
    assert result.metadata["title"] == "План учений"
    assert result.metadata["creator"] == "Мария Сидорова"
    assert result.metadata["initial_creator"] == "Иван Петров"
    assert result.metadata["keywords"] == ["учения"]
    assert result.metadata["creation_date"] == "2024-03-04T12:30:00Z"
    assert result.metadata["document_date"] == "2024-03-04"
    assert result.metadata["editing_cycles"] == 7
    assert result.metadata["editing_duration"] == "PT12M3S"
    assert result.metadata["page_count"] == 3
    assert result.metadata["word_count"] == 420
    assert result.metadata["printed_by"] == "Сергей Крылов"
    assert result.metadata["print_date"] == "2024-03-06T09:00:00Z"
    assert result.metadata["template"] == {
        "title": "Служебный документ",
        "date": "2024-01-02T03:04:05Z",
        "href": "templates/service.ott",
    }
    assert result.metadata["auto_reload"] == {
        "href": "https://example.test/source",
        "delay": "PT30M",
    }
    assert result.metadata["hyperlink_behaviour"] == {
        "target_frame_name": "_blank",
        "show": "new",
    }
    assert result.metadata["paragraph_count"] == 18
    assert result.metadata["non_whitespace_character_count"] == 1700
    assert result.metadata["user_defined"] == [
        {"name": "Подразделение", "value_type": "string", "value": "Отдел 7"}
    ]
    assert result.metadata["signature_members"] == ["META-INF/documentsignatures.xml"]
    assert result.metadata["signature_count"] == 1
    assert result.metadata["signature_ids"] == ["signature-1"]
    assert result.metadata["signature_subjects"] == ["CN=Иван Иванов"]
    assert result.metadata["signature_times"] == ["2024-03-05T08:01:00Z"]
    assert result.metadata["signature_validity"] == "not_checked"
    assert header_only["title"] == "План учений"
    assert header_only["document_date"] == "2024-03-04"
    assert "input_bytes" not in header_only
    assert header_only["signature_validity"] == "not_checked"


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
