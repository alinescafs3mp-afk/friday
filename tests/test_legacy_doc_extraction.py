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

from jericho.documents import DocumentExtractor
from jericho.documents._ole import OleError, OleFile, extract_doc_text

_SECTOR = 512
_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF


def _dir_entry(name: str, kind: int, start: int, size: int) -> bytes:
    raw = name.encode("utf-16-le") + b"\x00\x00"
    entry = bytearray(128)
    entry[: len(raw)] = raw
    struct.pack_into("<H", entry, 0x40, len(raw))
    entry[0x42] = kind
    struct.pack_into("<I", entry, 0x44, _FREESECT)  # left sibling
    struct.pack_into("<I", entry, 0x48, _FREESECT)  # right sibling
    struct.pack_into("<I", entry, 0x4C, _FREESECT)  # child
    struct.pack_into("<I", entry, 0x74, start)
    struct.pack_into("<Q", entry, 0x78, size)
    return bytes(entry)


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

    # Both streams padded past the 4096-byte mini-stream cutoff, so every read goes
    # through the normal FAT and the fixture exercises the same path a real file does.
    streams = [bytes(word).ljust(8192, b"\x00"), bytes(table).ljust(8192, b"\x00")]
    sizes = [len(bytes(word)), len(bytes(table))]

    directory_sector = 0
    data_start = 1
    sectors: list[bytes] = []
    starts: list[int] = []
    for stream in streams:
        starts.append(data_start + len(sectors))
        for offset in range(0, len(stream), _SECTOR):
            sectors.append(stream[offset : offset + _SECTOR])

    directory = (
        _dir_entry("Root Entry", 5, _ENDOFCHAIN, 0)
        + _dir_entry("WordDocument", 2, starts[0], sizes[0])
        + _dir_entry("1Table", 2, starts[1], sizes[1])
        + bytes(128)
    )
    directory_sectors = [
        directory[offset : offset + _SECTOR].ljust(_SECTOR, b"\x00")
        for offset in range(0, len(directory), _SECTOR)
    ]

    body = list(directory_sectors) + sectors
    fat_sector_index = len(body)
    fat = [_FREESECT] * (_SECTOR // 4)
    for index in range(len(directory_sectors)):
        fat[index] = index + 1 if index + 1 < len(directory_sectors) else _ENDOFCHAIN
    for position, start in enumerate(starts):
        count = len(streams[position]) // _SECTOR
        for step in range(count):
            sector = start + step
            fat[sector] = sector + 1 if step + 1 < count else _ENDOFCHAIN
    fat[fat_sector_index] = 0xFFFFFFFD  # the FAT sector describes itself
    body.append(struct.pack(f"<{_SECTOR // 4}I", *fat))

    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 0x1A, 0x003E)  # minor/major version
    struct.pack_into("<H", header, 0x1C, 0xFFFE)  # little endian
    struct.pack_into("<H", header, 0x1E, 9)  # sector shift: 512
    struct.pack_into("<H", header, 0x20, 6)  # mini sector shift: 64
    struct.pack_into("<I", header, 0x2C, 1)  # FAT sector count
    struct.pack_into("<I", header, 0x30, directory_sector)
    struct.pack_into("<I", header, 0x38, 4096)  # mini stream cutoff
    struct.pack_into("<I", header, 0x3C, _ENDOFCHAIN)  # no mini FAT
    struct.pack_into("<I", header, 0x44, _ENDOFCHAIN)  # no extra DIFAT
    for index in range(109):
        struct.pack_into("<I", header, 0x4C + index * 4, _FREESECT)
    struct.pack_into("<I", header, 0x4C, fat_sector_index)
    return bytes(header) + b"".join(body)


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


def test_field_instructions_are_dropped_and_results_kept():
    """`HYPERLINK "http://…"` is machinery; what the reader sees is the result.

    Keeping both turns every table of contents and every link into indexed noise.
    """
    document = build_doc('До \x13HYPERLINK "http://example.org"\x14ссылка\x15 после.')
    text, _ = extract_doc_text(document)
    assert "HYPERLINK" not in text and "example.org" not in text
    assert "До ссылка после." in text


def test_a_file_that_is_not_a_compound_document_is_reported_as_unsupported():
    """Five of the owner's 206 `.doc` files are not compound files at all.

    "Jericho cannot read this format" and "this file is damaged" have to stay
    different sentences for whoever is looking at the Inbox.
    """
    with pytest.raises(OleError):
        extract_doc_text(b"{\\rtf1\\ansi this is really an RTF file}")


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
