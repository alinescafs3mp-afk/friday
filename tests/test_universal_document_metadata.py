from __future__ import annotations

import io
import json
import zipfile

import pytest

from friday.agent_runtime import _document_metadata_answer, _OwnedAttachment
from friday.documents import DocumentExtractor
from friday.ingestion._files import _document_metadata_projection

SECRET = "INSTANCE-METADATA-SECRET-1234567890"


def _extractor() -> DocumentExtractor:
    return DocumentExtractor(secret_values=(SECRET,))


def _ooxml_package(*, custom_count: int = 70) -> bytes:
    custom = []
    for index in range(custom_count):
        value = SECRET if index == 0 else f"Значение {index}"
        custom.append(
            f'<property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" '
            f'pid="{index + 2}" name="Поле {index}"><vt:lpwstr>{value}</vt:lpwstr></property>'
        )
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "docProps/core.xml",
            """<cp:coreProperties
 xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/">
 <dc:title>План развёртывания</dc:title><dc:creator>Иван Петров</dc:creator>
 <cp:lastModifiedBy>Мария Сидорова</cp:lastModifiedBy>
 <cp:keywords>план; связь</cp:keywords>
 <dcterms:created>2013-01-02T03:04:05Z</dcterms:created>
 <dcterms:modified>2026-08-11T05:06:07Z</dcterms:modified>
</cp:coreProperties>""",
        )
        archive.writestr(
            "docProps/app.xml",
            """<Properties
 xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
 <Application>Friday Office</Application><AppVersion>1.2</AppVersion>
 <Company>Организация 7</Company><Manager>Руководитель</Manager>
 <Pages>4</Pages><Words>420</Words><SharedDoc>true</SharedDoc>
</Properties>""",
        )
        archive.writestr(
            "docProps/custom.xml",
            """<Properties
 xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">"""
            + "".join(custom)
            + "</Properties>",
        )
    return payload.getvalue()


@pytest.mark.parametrize("filename", ["план.docx", "план.xlsx", "план.pptx"])
def test_ooxml_core_app_custom_are_bounded_redacted_and_renderable(filename: str) -> None:
    metadata = _extractor().extract_document_metadata(_ooxml_package(), filename)

    assert metadata["format"] == filename.rsplit(".", 1)[-1]
    assert metadata["title"] == "План развёртывания"
    assert metadata["creator"] == "Иван Петров"
    assert metadata["application"] == "Friday Office"
    assert metadata["page_count"] == 4
    assert metadata["shared_document"] is True
    assert metadata["document_date"] == "2013-01-02"
    assert metadata["stored_properties_total"] > 70
    assert metadata["stored_properties_shown"] == 64
    assert metadata["technical_metadata_incomplete"] is True
    assert metadata["metadata_parse_status"] == "partial"
    assert SECRET not in json.dumps(metadata, ensure_ascii=False)

    projected = _document_metadata_projection(metadata)
    assert projected["metadata_schema_version"] == 4
    attachment = _OwnedAttachment(
        {
            "_safe_document_metadata": {
                "filename": filename,
                "mime_type": "application/octet-stream",
                "size_bytes": 123,
                **projected,
            }
        }
    )
    rendered = _document_metadata_answer([attachment])
    assert "Технические свойства файла (как сохранено)" in rendered
    assert "Дата в свойствах контейнера: 2013-01-02" in rendered
    assert "Сохранённые свойства, всего" in rendered
    assert "технические метаданные показаны частично" in rendered.casefold()
    assert SECRET not in rendered


def test_ooxml_metadata_rejects_dtd_and_never_expands_entities() -> None:
    payload = io.BytesIO()
    xml = """<?xml version="1.0" encoding="UTF-16"?>
<!DOCTYPE properties [<!ENTITY leaked SYSTEM "file:///etc/passwd">]>
<cp:coreProperties
 xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <dc:title>&leaked;</dc:title></cp:coreProperties>"""
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("docProps/core.xml", xml.encode("utf-16"))

    metadata = _extractor().extract_document_metadata(payload.getvalue(), "unsafe.docx")

    assert metadata["metadata_parse_status"] == "partial"
    assert metadata["technical_metadata_incomplete"] is True
    assert "title" not in metadata
    assert "root:" not in json.dumps(metadata, ensure_ascii=False)


def test_odf_utf16_internal_entity_is_rejected_in_metadata_and_signature_xml() -> None:
    meta_xml = """<?xml version="1.0" encoding="UTF-16"?>
<!DOCTYPE office [<!ENTITY injected "DTD_ENTITY_EXPANDED">]>
<office:document-meta
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <office:meta><dc:title>&injected;</dc:title></office:meta></office:document-meta>"""
    signature_xml = """<?xml version="1.0" encoding="UTF-16"?>
<!DOCTYPE signatures [<!ENTITY injected "DTD_ENTITY_EXPANDED">]>
<dsig:document-signatures
 xmlns:dsig="urn:oasis:names:tc:opendocument:xmlns:digitalsignature:1.0"
 xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
 <ds:Signature Id="s"><ds:X509SubjectName>&injected;</ds:X509SubjectName></ds:Signature>
</dsig:document-signatures>"""
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr("content.xml", "<office/>")
        archive.writestr("meta.xml", meta_xml.encode("utf-16"))
        archive.writestr(
            "META-INF/documentsignatures.xml",
            signature_xml.encode("utf-16"),
        )

    metadata = _extractor().extract_document_metadata(payload.getvalue(), "unsafe.odt")

    assert metadata["metadata_parse_status"] == "unreadable"
    assert metadata["technical_metadata_incomplete"] is True
    assert metadata["signature_metadata_incomplete"] is True
    assert "DTD_ENTITY_EXPANDED" not in json.dumps(metadata, ensure_ascii=False)


def test_epub_utf16_internal_entity_is_rejected_before_opf_projection() -> None:
    opf = """<?xml version="1.0" encoding="UTF-16"?>
<!DOCTYPE package [<!ENTITY injected "DTD_ENTITY_EXPANDED">]>
<package xmlns="http://www.idpf.org/2007/opf"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <metadata><dc:title>&injected;</dc:title></metadata></package>"""
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            """<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
 <rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>""",
        )
        archive.writestr("OEBPS/content.opf", opf.encode("utf-16"))

    metadata = _extractor().extract_document_metadata(payload.getvalue(), "unsafe.epub")

    assert metadata["metadata_parse_status"] == "unreadable"
    assert metadata["technical_metadata_incomplete"] is True
    assert "DTD_ENTITY_EXPANDED" not in json.dumps(metadata, ensure_ascii=False)


def _pdf_with_xmp_and_signature() -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        ByteStringObject,
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
        TextStringObject,
    )

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata(
        {
            "/Title": "PDF-план",
            "/Author": "Автор PDF",
            "/CreationDate": "D:20130102030405+03'00'",
            "/CustomUnit": "Отдел 9",
        }
    )
    xmp = DecodedStreamObject()
    xmp.set_data(
        b"""<?xpacket begin='x'?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <rdf:RDF><rdf:Description><dc:description>Stored XMP fact</dc:description>
 </rdf:Description></rdf:RDF></x:xmpmeta>"""
    )
    writer.root_object[NameObject("/Metadata")] = writer._add_object(xmp)  # noqa: SLF001
    signature = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Sig"),
            NameObject("/Name"): TextStringObject("Сохранённое имя подписанта"),
            NameObject("/M"): TextStringObject("D:20260811050607+03'00'"),
            NameObject("/Reason"): TextStringObject("Согласование"),
            NameObject("/SubFilter"): NameObject("/adbe.pkcs7.detached"),
            NameObject("/ByteRange"): ArrayObject(
                [NumberObject(0), NumberObject(1), NumberObject(2), NumberObject(3)]
            ),
            NameObject("/Contents"): ByteStringObject(b"not-a-real-signature"),
        }
    )
    field = DictionaryObject(
        {
            NameObject("/FT"): NameObject("/Sig"),
            NameObject("/T"): TextStringObject("ApprovalSignature"),
            NameObject("/V"): writer._add_object(signature),  # noqa: SLF001
        }
    )
    acroform = DictionaryObject(
        {NameObject("/Fields"): ArrayObject([writer._add_object(field)])}  # noqa: SLF001
    )
    writer.root_object[NameObject("/AcroForm")] = writer._add_object(acroform)  # noqa: SLF001
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_info_xmp_and_signature_fields_are_stored_facts_not_validation() -> None:
    metadata = _extractor().extract_document_metadata(_pdf_with_xmp_and_signature(), "signed.pdf")

    assert metadata["title"] == "PDF-план"
    assert metadata["document_date"] == "2013-01-02"
    assert metadata["signature_fields_total"] == metadata["signature_fields_shown"] == 1
    assert metadata["signature_fields"][0]["field_name"] == "ApprovalSignature"
    assert metadata["signature_fields"][0]["signer_name"] == "Сохранённое имя подписанта"
    assert metadata["signature_fields"][0]["contents_present"] == "да"
    assert metadata["signature_validity"] == "not_checked"
    assert any(item["source"] == "PDF XMP" for item in metadata["stored_properties"])

    projected = _document_metadata_projection(metadata)
    rendered = _document_metadata_answer(
        [_OwnedAttachment({"_safe_document_metadata": {"filename": "signed.pdf", **projected}})]
    )
    assert "не проверялась" in rendered
    assert "Сохранённое поле подписи PDF (не проверено)" in rendered
    assert "ApprovalSignature" in rendered


@pytest.mark.parametrize(("filename", "format_name"), [("mail.eml", "eml"), ("page.mhtml", "mhtml")])
def test_email_and_mhtml_headers_are_bounded_and_secret_redacted(
    filename: str,
    format_name: str,
) -> None:
    headers = [
        "From: Ivan <ivan@example.test>",
        "To: Petr <petr@example.test>",
        "Date: Wed, 12 Apr 2023 10:00:00 +0300",
        "Subject: Stored subject",
        f"X-Secret: {SECRET}",
        *(f"X-Property-{index}: value-{index}" for index in range(70)),
    ]
    payload = ("\r\n".join(headers) + "\r\n\r\nBODY MUST NOT BE PARSED").encode()

    metadata = _extractor().extract_document_metadata(payload, filename)

    assert metadata["format"] == format_name
    assert metadata["email_from"].startswith("Ivan")
    assert metadata["email_subject"] == "Stored subject"
    assert metadata["document_date"] == "2023-04-12"
    assert metadata["stored_properties_total"] == len(headers)
    assert metadata["stored_properties_shown"] == 64
    assert metadata["technical_metadata_incomplete"] is True
    assert "BODY MUST NOT BE PARSED" not in json.dumps(metadata, ensure_ascii=False)
    assert SECRET not in json.dumps(metadata, ensure_ascii=False)


def _epub_with_metadata() -> bytes:
    extra = "".join(f'<meta property="custom:{index}">value-{index}</meta>' for index in range(70))
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            """<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
 <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
 </rootfiles></container>""",
        )
        archive.writestr(
            "OEBPS/content.opf",
            """<package xmlns="http://www.idpf.org/2007/opf"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <metadata><dc:title>Синтетическая книга</dc:title><dc:creator>Автор книги</dc:creator>
 <dc:identifier>book-42</dc:identifier><dc:language>ru</dc:language>
 <dc:rights>"""
            + SECRET
            + "</dc:rights>"
            + extra
            + "</metadata></package>",
        )
        archive.writestr("OEBPS/chapter.xhtml", "<p>BODY MUST NOT BE PARSED</p>")
    return payload.getvalue()


def test_epub_opf_metadata_is_bounded_without_reading_chapters() -> None:
    metadata = _extractor().extract_document_metadata(_epub_with_metadata(), "book.epub")

    assert metadata["title"] == "Синтетическая книга"
    assert metadata["creator"] == "Автор книги"
    assert metadata["identifier"] == "book-42"
    assert metadata["stored_properties_total"] > 70
    assert metadata["stored_properties_shown"] == 64
    assert metadata["technical_metadata_incomplete"] is True
    encoded = json.dumps(metadata, ensure_ascii=False)
    assert SECRET not in encoded
    assert "BODY MUST NOT BE PARSED" not in encoded


def test_image_dimensions_and_exif_are_bounded_and_redacted() -> None:
    from PIL import Image

    image = Image.new("RGB", (17, 23), "white")
    exif = Image.Exif()
    exif[271] = "Synthetic Camera Co"
    exif[272] = "Model 42"
    exif[36867] = "2026:08:11 05:06:07"
    exif[37510] = SECRET.encode()
    payload = io.BytesIO()
    image.save(payload, format="JPEG", exif=exif)

    metadata = _extractor().extract_document_metadata(payload.getvalue(), "photo.jpg")

    assert metadata["format"] == "image"
    assert metadata["image_format"] == "JPEG"
    assert metadata["width_pixels"] == 17
    assert metadata["height_pixels"] == 23
    assert metadata["camera_make"] == "Synthetic Camera Co"
    assert metadata["camera_model"] == "Model 42"
    assert metadata["capture_date"] == "2026:08:11 05:06:07"
    assert metadata["stored_properties_total"] >= 4
    assert SECRET not in json.dumps(metadata, ensure_ascii=False)
