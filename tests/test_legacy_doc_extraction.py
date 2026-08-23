"""Reading a legacy Word document (`.doc`) with nothing but the stdlib.

Measured on a real 3.19 GB folder of working documents: 206 files in this format,
130 MB, and the extractor read none of them — each became an Inbox item whose whole
content was `[File: NAME.doc; …]`, nothing to review and nothing to retrieve. After:
197 of the 206 read, 4.4 million characters, all recognisably Russian, zero
replacement characters and zero leftover control characters.

The fixtures are BUILT HERE rather than committed as binary blobs. A compound file
is a small filesystem and a `.doc` is a piece table on top of it; a builder that
lays out both is the only fixture that says what the parser is supposed to survive,
and it keeps the owner's documents out of this repository entirely.
"""

from __future__ import annotations

import contextlib
import struct
import time

import pytest

from friday.documents import DocumentExtractor
from friday.documents._ole import OleError, OleFile, extract_doc_text, extract_msg_text

_SECTOR = 512
_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF


def _dir_entry(
    name: str,
    kind: int,
    start: int,
    size: int,
    *,
    left: int = _FREESECT,
    right: int = _FREESECT,
    child: int = _FREESECT,
) -> bytes:
    raw = name.encode("utf-16-le") + b"\x00\x00"
    entry = bytearray(128)
    entry[: len(raw)] = raw
    struct.pack_into("<H", entry, 0x40, len(raw))
    entry[0x42] = kind
    struct.pack_into("<I", entry, 0x44, left)
    struct.pack_into("<I", entry, 0x48, right)
    struct.pack_into("<I", entry, 0x4C, child)
    struct.pack_into("<I", entry, 0x74, start)
    struct.pack_into("<Q", entry, 0x78, size)
    return bytes(entry)


def _build_ole_streams(
    streams_by_name: dict[str, bytes],
    *,
    storage_names: tuple[str, ...] = (),
) -> bytes:
    """Build a bounded OLE container with a valid root sibling tree."""

    names_and_kinds = [(name, 2) for name in streams_by_name]
    names_and_kinds.extend((name, 1) for name in storage_names)
    streams = [payload.ljust(4096, b"\x00") for payload in streams_by_name.values()]
    sizes = [len(payload) for payload in streams_by_name.values()]
    sectors: list[bytes] = []
    starts: list[int] = []
    # Reserve enough contiguous sectors for the directory before stream starts.
    directory_bytes = (1 + len(names_and_kinds)) * 128
    directory_sector_count = max(1, (directory_bytes + _SECTOR - 1) // _SECTOR)
    data_start = directory_sector_count
    for stream in streams:
        starts.append(data_start + len(sectors))
        for offset in range(0, len(stream), _SECTOR):
            sectors.append(stream[offset : offset + _SECTOR])

    directory_parts = [
        _dir_entry(
            "Root Entry",
            5,
            _ENDOFCHAIN,
            0,
            child=1 if names_and_kinds else _FREESECT,
        )
    ]
    stream_index = 0
    for index, (name, kind) in enumerate(names_and_kinds, start=1):
        if kind == 2:
            start = starts[stream_index]
            size = sizes[stream_index]
            stream_index += 1
        else:
            start = _ENDOFCHAIN
            size = 0
        directory_parts.append(
            _dir_entry(
                name,
                kind,
                start,
                size,
                right=index + 1 if index < len(names_and_kinds) else _FREESECT,
            )
        )
    directory = b"".join(directory_parts).ljust(directory_sector_count * _SECTOR, b"\x00")
    directory_sectors = [
        directory[offset : offset + _SECTOR]
        for offset in range(0, len(directory), _SECTOR)
    ]

    body = list(directory_sectors) + sectors
    fat_sector_index = len(body)
    if fat_sector_index >= _SECTOR // 4:
        raise ValueError("synthetic OLE fixture exceeds its single FAT sector")
    fat = [_FREESECT] * (_SECTOR // 4)
    for index in range(len(directory_sectors)):
        fat[index] = index + 1 if index + 1 < len(directory_sectors) else _ENDOFCHAIN
    for position, start in enumerate(starts):
        count = len(streams[position]) // _SECTOR
        for step in range(count):
            sector = start + step
            fat[sector] = sector + 1 if step + 1 < count else _ENDOFCHAIN
    fat[fat_sector_index] = 0xFFFFFFFD
    body.append(struct.pack(f"<{_SECTOR // 4}I", *fat))

    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 0x1A, 0x003E)
    struct.pack_into("<H", header, 0x1C, 0xFFFE)
    struct.pack_into("<H", header, 0x1E, 9)
    struct.pack_into("<H", header, 0x20, 6)
    struct.pack_into("<I", header, 0x2C, 1)
    struct.pack_into("<I", header, 0x30, 0)
    struct.pack_into("<I", header, 0x38, 4096)
    struct.pack_into("<I", header, 0x3C, _ENDOFCHAIN)
    struct.pack_into("<I", header, 0x44, _ENDOFCHAIN)
    for index in range(109):
        struct.pack_into("<I", header, 0x4C + index * 4, _FREESECT)
    struct.pack_into("<I", header, 0x4C, fat_sector_index)
    return bytes(header) + b"".join(body)


def build_doc(text: str, *, compressed: bool = True, codepage: str = "cp1251") -> bytes:
    """A minimal but genuine Word 97 document: OLE2 container, FIB, one text piece."""
    payload = text.encode(codepage) if compressed else text.encode("utf-16-le")
    text_offset = 0x200

    word = bytearray(text_offset + len(payload))
    struct.pack_into("<H", word, 0x00, 0xA5EC)  # wIdent
    struct.pack_into("<H", word, 0x02, 193)  # nFib: Word 97
    struct.pack_into("<H", word, 0x0A, 0x0200)  # flags: the piece table is in 1Table
    word[text_offset : text_offset + len(payload)] = payload

    characters = len(text)
    plc = struct.pack("<II", 0, characters)
    fc = text_offset * 2 | 0x40000000 if compressed else text_offset
    plc += struct.pack("<HIH", 0, fc, 0)
    clx = bytes([0x02]) + struct.pack("<I", len(plc)) + plc
    struct.pack_into("<II", word, 0x01A2, 0, len(clx))  # fcClx / lcbClx
    table = bytearray(clx)

    return _build_ole_streams({"WordDocument": bytes(word), "1Table": bytes(table)})


RUSSIAN = "Ведомость на выдачу. Пункт первый: проверить наличие.\rПункт второй: доложить."


def test_a_russian_legacy_document_reads_as_russian():
    text, metadata = extract_doc_text(build_doc(RUSSIAN))
    assert "Ведомость на выдачу" in text
    assert "Пункт второй: доложить." in text
    assert "�" not in text, "codepage guessed wrong — mojibake still looks like text"
    assert metadata["format"] == "doc"


def test_a_paragraph_mark_becomes_a_line_break():
    text, _ = extract_doc_text(build_doc(RUSSIAN))
    assert text.splitlines() == [
        "Ведомость на выдачу. Пункт первый: проверить наличие.",
        "Пункт второй: доложить.",
    ]


def test_utf16_pieces_are_read_too():
    """Word stores a piece as 8-bit only when it can; anything else is UTF-16."""
    text, _ = extract_doc_text(build_doc("Приказ № 12 — ознакомить личный состав.", compressed=False))
    assert "Приказ № 12 — ознакомить личный состав." in text


def test_a_western_document_is_not_forced_into_cyrillic():
    """The codepage is inferred from the bytes, and inferring it wrong is silent."""
    text, _ = extract_doc_text(build_doc("Rapport annuel: résumé des activités.", codepage="cp1252"))
    assert "résumé des activités" in text


# An English technical note with a Russian header and signature — a shape this
# archive is full of. Cyrillic is under a tenth of the bytes.
MOSTLY_ENGLISH = (
    "Приложение к отчёту\r"
    "The deployment procedure is described below. Run the installer with the "
    "default profile, confirm the certificate fingerprint, and verify that the "
    "service answers on the management port before handing the host over to the "
    "operations team. Repeat the health check after the first scheduled restart, "
    "because the configuration is re-read only at startup and a stale value will "
    "otherwise survive unnoticed until the next maintenance window.\r"
    "Исполнитель: Петров"
)


def test_a_russian_header_survives_a_mostly_english_document():
    """The share of Cyrillic answers the wrong question.

    The guess used to be "are more than 15% of the bytes high?", which asks
    whether the document is Cyrillic-DOMINANT. A document with large English
    sections — or an English one with a Russian header and signature block — sits
    under any such threshold and decodes as cp1252, turning every Russian word
    into mojibake that still looks like text, indexes cleanly, embeds cleanly and
    is never noticed. Here Cyrillic is ~8% of the bytes.
    """
    document = build_doc(MOSTLY_ENGLISH)
    cyrillic_share = sum(1 for character in MOSTLY_ENGLISH if "А" <= character <= "я") / len(MOSTLY_ENGLISH)
    assert cyrillic_share < 0.15, "the premise: this document is not Cyrillic-dominant"

    text, _ = extract_doc_text(document)
    assert "Приложение к отчёту" in text
    assert "Исполнитель: Петров" in text
    assert "deployment procedure" in text  # and the English half is untouched


def test_field_instructions_are_dropped_and_results_kept():
    """`HYPERLINK "http://…"` is machinery; what the reader sees is the result.

    Keeping both turns every table of contents and every link into indexed noise.
    """
    document = build_doc('До \x13HYPERLINK "http://example.org"\x14ссылка\x15 после.')
    text, _ = extract_doc_text(document)
    assert "HYPERLINK" not in text and "example.org" not in text
    assert "До ссылка после." in text


def test_the_low_level_doc_parser_still_rejects_a_non_compound_file():
    with pytest.raises(OleError):
        extract_doc_text(b"{\\rtf1\\ansi this is really an RTF file}")


def test_an_rtf_exported_with_a_doc_suffix_is_read_by_its_own_magic():
    result = DocumentExtractor(secret_values=()).extract(
        b"{\\rtf1\\ansi Synthetic legacy body}",
        "legacy-export.doc",
        "application/msword",
    )

    assert result.success is True
    assert "Synthetic legacy body" in result.text
    assert result.metadata["format"] == "rtf"
    assert result.metadata["declared_format"] == "doc"


def test_an_outlook_msg_reads_root_headers_and_plain_body() -> None:
    def unicode_property(value: str) -> bytes:
        return value.encode("utf-16-le") + b"\x00\x00"

    message = _build_ole_streams(
        {
            "__substg1.0_0037001F": unicode_property("A"),
            "__substg1.0_0C1A001F": unicode_property("Иван Петров"),
            "__substg1.0_0E04001F": unicode_property("Мария Сидорова"),
            "__substg1.0_1000001F": unicode_property("Смета согласована."),
        },
        storage_names=("__attach_version1.0_#00000000",),
    )

    text, metadata = extract_msg_text(message)
    dispatched = DocumentExtractor(secret_values=()).extract(
        message,
        "message.bin",
        "application/vnd.ms-outlook",
    )

    assert "Тема: A" in text  # one ASCII UTF-16 character must survive its terminator
    assert "От: Иван Петров" in text
    assert "Кому: Мария Сидорова" in text
    assert text.endswith("Смета согласована.")
    assert metadata["body_format"] == "plain"
    assert metadata["attachment_streams"] == 1
    assert metadata["source_truncated_for_parse"] is True
    assert dispatched.success is True
    assert dispatched.text == text


def test_an_outlook_msg_uses_its_declared_ansi_codepage() -> None:
    message = _build_ole_streams(
        {
            "__substg1.0_3FDE0003": struct.pack("<I", 1251),
            "__substg1.0_0037001E": "Кириллическая тема".encode("cp1251") + b"\x00",
            "__substg1.0_1000001E": "Письмо прочитано верно.".encode("cp1251") + b"\x00",
        }
    )

    text, metadata = extract_msg_text(message)

    assert "Тема: Кириллическая тема" in text
    assert text.endswith("Письмо прочитано верно.")
    assert metadata["ansi_codepage"] == 1251
    assert metadata["body_format"] == "plain"
    assert "source_truncated_for_parse" not in metadata


def test_an_outlook_msg_reads_visible_html_and_drops_active_content() -> None:
    html_body = (
        "<html><body><h1>Отчёт</h1><p>Сумма &amp; статус согласованы.</p>"
        "<script>SECRET_SCRIPT_SENTINEL</script><style>.hidden{display:none}</style>"
        "</body></html>"
    ).encode("cp1251")
    message = _build_ole_streams(
        {
            "__substg1.0_3FDE0003": struct.pack("<I", 1251),
            "__substg1.0_10130102": html_body,
        }
    )

    text, metadata = extract_msg_text(message)

    assert "Отчёт" in text
    assert "Сумма & статус согласованы." in text
    assert "SECRET_SCRIPT_SENTINEL" not in text
    assert "display:none" not in text
    assert metadata["body_read"] is True
    assert metadata["body_format"] == "html"
    assert "source_truncated_for_parse" not in metadata


def _truncate_ole_stream_after_first_sector(content: bytes, stream_name: str) -> bytes:
    damaged = bytearray(content)
    encoded_name = stream_name.encode("utf-16-le")
    name_offset = damaged.find(encoded_name, 512)
    assert name_offset >= 512
    directory_entry = 512 + ((name_offset - 512) // 128) * 128
    stream_start = struct.unpack_from("<I", damaged, directory_entry + 0x74)[0]
    fat_sector = struct.unpack_from("<I", damaged, 0x4C)[0]
    fat_offset = 512 + fat_sector * _SECTOR + stream_start * 4
    struct.pack_into("<I", damaged, fat_offset, _ENDOFCHAIN)
    return bytes(damaged)


def test_a_truncated_msg_stream_returns_only_a_marked_partial_prefix() -> None:
    body_name = "__substg1.0_1000001F"
    message = _build_ole_streams(
        {
            body_name: ("Видимый префикс. " + "Продолжение " * 120).encode("utf-16-le")
            + b"\x00\x00",
        }
    )
    damaged = _truncate_ole_stream_after_first_sector(message, body_name)

    text, metadata = extract_msg_text(damaged)

    assert text.startswith("Видимый префикс.")
    assert metadata["body_read"] is True
    assert metadata["body_format"] == "plain"
    assert metadata["source_truncated_for_parse"] is True


def test_a_truncated_compound_file_does_not_raise_something_unexpected():
    document = build_doc(RUSSIAN)
    with pytest.raises(OleError):
        extract_doc_text(document[:600])


def test_a_looping_fat_chain_terminates():
    """A malformed FAT that points at itself must not spin.

    What actually terminates it is the declared size, not the visited set: mutation
    removed the cycle guard and this still finished, because every chain read stops
    once it has collected the bytes it was asked for. The visited set caps memory and
    stops sooner; saying otherwise in a comment would be a claim nobody checked.

    Mutation also showed the first version of this test proved nothing at all —
    `try/except OleError: pass` passes whether the parser refuses, succeeds, or hangs
    until the suite is killed. The wall clock is the assertion.
    """
    document = bytearray(build_doc(RUSSIAN))
    header_fat = struct.unpack_from("<I", document, 0x4C)[0]
    fat_offset = 512 + header_fat * _SECTOR
    struct.pack_into("<I", document, fat_offset, 0)  # directory sector 0 -> itself

    began = time.monotonic()
    with contextlib.suppress(OleError):
        extract_doc_text(bytes(document))
    elapsed = time.monotonic() - began
    assert elapsed < 2.0, f"a self-referencing FAT took {elapsed:.1f}s"


def test_the_production_extractor_dispatches_on_the_extension():
    result = DocumentExtractor().extract(build_doc(RUSSIAN), "приказ.doc", "application/msword")
    assert result.success
    assert "Ведомость на выдачу" in result.text
    assert result.metadata["format"] == "doc"


def test_stream_names_are_readable():
    ole = OleFile(build_doc(RUSSIAN))
    assert "WordDocument" in ole.stream_names()
    assert "1Table" in ole.stream_names()
