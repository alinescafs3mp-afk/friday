"""Read text out of a legacy Word document (``.doc``) using nothing but the stdlib.

Measured on a real 3.19 GB folder of working documents: **201 files** in this format,
130 MB, and the extractor read exactly none of them — every one became an Inbox item
whose entire content was ``[File: NAME.doc; …]``. Legacy Word is not a historical
curiosity in a Russian office; it is where the older half of the archive lives.

Two formats stack here and both are parsed from bytes:

* **OLE2 / Compound File Binary** — a FAT-like filesystem inside one file. Sectors,
  a FAT chain per stream, a directory of named streams, and a second "mini" tier for
  streams below 4096 bytes.
* **Word 97 (nFib ≥ 105)** — the ``WordDocument`` stream opens with the FIB, which
  points into ``0Table``/``1Table`` for the *piece table*: the text is not stored
  contiguously, it is a list of pieces each either UTF-16 or 8-bit, in document
  order. Reading the stream linearly and hoping produces exactly the kind of garbage
  that looks like success.

Everything is bounds-checked and every chain walk is cycle-guarded: this parser runs
on files a person was handed, and a malformed FAT that loops is the cheapest possible
denial of service.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF
_MAX_CHAIN = 1 << 20  # a 512-byte sector each: far past any real document
_DIR_ENTRY_SIZE = 128
_WORD_MAGIC = 0xA5EC

# Control characters Word stores inline. The field ones matter most: between 0x13 and
# 0x14 sits the field INSTRUCTION (`HYPERLINK "http://…"`, `PAGE`, `TOC \\o "1-3"`),
# and between 0x14 and 0x15 the result a reader actually sees. Keeping both turns
# every table of contents into noise that then gets indexed and embedded.
_FIELD_BEGIN = "\x13"
_FIELD_SEPARATOR = "\x14"
_FIELD_END = "\x15"
_PARAGRAPH_MARKS = {"\r", "\x0b", "\x0c", "\x07"}
_DROPPED = {"\x01", "\x02", "\x03", "\x04", "\x05", "\x06", "\x08", "\x1e", "\x1f", "\x00"}


class OleError(ValueError):
    """The bytes are not a compound file we can read."""


@dataclass(frozen=True)
class _DirEntry:
    name: str
    kind: int
    start: int
    size: int


class OleFile:
    """Minimal read-only compound-file reader: named streams, nothing else."""

    def __init__(self, data: bytes) -> None:
        if len(data) < 512 or not data.startswith(OLE_SIGNATURE):
            raise OleError("not an OLE2 compound file")
        self._data = data
        sector_shift = struct.unpack_from("<H", data, 0x1E)[0]
        mini_shift = struct.unpack_from("<H", data, 0x20)[0]
        if not 6 <= sector_shift <= 12 or not 2 <= mini_shift <= sector_shift:
            raise OleError("implausible sector sizes")
        self._sector_size = 1 << sector_shift
        self._mini_size = 1 << mini_shift
        self._mini_cutoff = struct.unpack_from("<I", data, 0x38)[0]
        self._fat = self._read_fat()
        self._directory = self._read_directory(struct.unpack_from("<I", data, 0x30)[0])
        self._mini_fat = self._read_mini_fat()
        root = next((entry for entry in self._directory if entry.kind == 5), None)
        self._mini_stream = (
            self._read_chain(root.start, root.size, self._fat, self._sector_size) if root else b""
        )

    def _sector(self, index: int) -> bytes:
        start = 512 + index * self._sector_size
        end = start + self._sector_size
        if start < 512 or end > len(self._data):
            raise OleError("sector past end of file")
        return self._data[start:end]

    def _read_fat(self) -> list[int]:
        difat = list(struct.unpack_from("<109I", self._data, 0x4C))
        next_difat = struct.unpack_from("<I", self._data, 0x44)[0]
        seen: set[int] = set()
        while next_difat not in (_ENDOFCHAIN, _FREESECT) and next_difat not in seen:
            seen.add(next_difat)
            if len(seen) > _MAX_CHAIN:
                raise OleError("DIFAT chain too long")
            block = self._sector(next_difat)
            entries = struct.unpack_from(f"<{self._sector_size // 4}I", block, 0)
            difat.extend(entries[:-1])
            next_difat = entries[-1]
        fat: list[int] = []
        for sector in difat:
            if sector in (_FREESECT, _ENDOFCHAIN):
                continue
            fat.extend(struct.unpack_from(f"<{self._sector_size // 4}I", self._sector(sector), 0))
        return fat

    def _read_mini_fat(self) -> list[int]:
        start = struct.unpack_from("<I", self._data, 0x3C)[0]
        count = struct.unpack_from("<I", self._data, 0x40)[0]
        if start in (_ENDOFCHAIN, _FREESECT) or not count:
            return []
        raw = self._read_chain(start, count * self._sector_size, self._fat, self._sector_size)
        return list(struct.unpack_from(f"<{len(raw) // 4}I", raw, 0))

    def _read_chain(self, start: int, size: int, fat: list[int], unit: int) -> bytes:
        """Follow a sector chain, refusing to loop and refusing to overrun.

        `collected < size` is what guarantees termination — a chain stops once it has
        the bytes it was asked for, cycle or no cycle. The visited set is a memory
        cap and an earlier exit, not the safety net; measured by removing it, which
        changed nothing about whether this returns.
        """
        chunks: list[bytes] = []
        sector = start
        seen: set[int] = set()
        collected = 0
        while sector not in (_ENDOFCHAIN, _FREESECT) and collected < size:
            if sector in seen or sector >= len(fat) or len(seen) > _MAX_CHAIN:
                break
            seen.add(sector)
            chunks.append(self._sector(sector) if unit == self._sector_size else self._mini(sector))
            collected += unit
            sector = fat[sector]
        return b"".join(chunks)[:size]

    def _mini(self, index: int) -> bytes:
        start = index * self._mini_size
        return self._mini_stream[start : start + self._mini_size]

    def _read_directory(self, first_sector: int) -> list[_DirEntry]:
        raw = self._read_chain(first_sector, 1 << 24, self._fat, self._sector_size)
        entries: list[_DirEntry] = []
        for offset in range(0, len(raw) - _DIR_ENTRY_SIZE + 1, _DIR_ENTRY_SIZE):
            block = raw[offset : offset + _DIR_ENTRY_SIZE]
            name_bytes = struct.unpack_from("<H", block, 0x40)[0]
            kind = block[0x42]
            if kind not in (1, 2, 5) or not 2 <= name_bytes <= 64:
                continue
            name = block[: name_bytes - 2].decode("utf-16-le", "replace")
            start = struct.unpack_from("<I", block, 0x74)[0]
            size = struct.unpack_from("<Q", block, 0x78)[0] & 0xFFFFFFFF
            entries.append(_DirEntry(name, kind, start, size))
        return entries

    def stream_names(self) -> list[str]:
        return [entry.name for entry in self._directory if entry.kind == 2]

    def read_stream(self, name: str) -> bytes:
        entry = next((item for item in self._directory if item.kind == 2 and item.name == name), None)
        if entry is None:
            raise OleError(f"no stream named {name!r}")
        if entry.size < self._mini_cutoff and self._mini_fat:
            return self._read_chain(entry.start, entry.size, self._mini_fat, self._mini_size)
        return self._read_chain(entry.start, entry.size, self._fat, self._sector_size)


def _decode_pieces(pieces: list[tuple[bool, bytes]]) -> str:
    """Decode 8-bit pieces with the codepage the document was actually written in.

    The format records "compressed" for 8-bit text but not WHICH single-byte codepage,
    and the answer is the ANSI codepage of the machine that saved it: cp1251 across
    this corpus, cp1252 for a Western document. Guessing wrong is not a subtle loss —
    it turns every Cyrillic word into mojibake that still looks like text, indexes
    cleanly and is never noticed.
    """
    eight_bit = b"".join(raw for compressed, raw in pieces if compressed)
    codepage = "cp1252"
    if eight_bit:
        cyrillic = sum(1 for byte in eight_bit if 0xC0 <= byte <= 0xFF)
        if cyrillic / len(eight_bit) > 0.15:
            codepage = "cp1251"
    parts: list[str] = []
    for compressed, raw in pieces:
        if compressed:
            parts.append(raw.decode(codepage, "replace"))
        else:
            parts.append(raw.decode("utf-16-le", "replace"))
    return "".join(parts)


def _clean(text: str) -> str:
    """Turn Word's inline control characters into plain text."""
    out: list[str] = []
    in_instruction = False
    for character in text:
        if character == _FIELD_BEGIN:
            in_instruction = True
            continue
        if character == _FIELD_SEPARATOR:
            in_instruction = False
            continue
        if character == _FIELD_END:
            in_instruction = False
            continue
        if in_instruction:
            continue
        if character in _PARAGRAPH_MARKS:
            out.append("\n")
        elif character in _DROPPED:
            continue
        else:
            out.append(character)
    lines = [" ".join(line.split()) for line in "".join(out).split("\n")]
    return "\n".join(line for line in lines if line)


def extract_doc_text(content: bytes) -> tuple[str, dict[str, object]]:
    """Return ``(text, metadata)`` for a Word 97-2003 document.

    Raises ``OleError`` when the bytes are not a compound file or carry no Word
    document inside — the caller reports that as an unsupported format rather than
    as an empty document, because the two mean different things to a reviewer.
    """
    ole = OleFile(content)
    names = set(ole.stream_names())
    if "WordDocument" not in names:
        raise OleError("compound file without a WordDocument stream")
    document = ole.read_stream("WordDocument")
    if len(document) < 0x200 or struct.unpack_from("<H", document, 0)[0] != _WORD_MAGIC:
        raise OleError("WordDocument stream without a Word FIB")

    flags = struct.unpack_from("<H", document, 0x0A)[0]
    table_name = "1Table" if flags & 0x0200 else "0Table"
    if table_name not in names:
        table_name = "0Table" if "0Table" in names else "1Table"
    if table_name not in names:
        raise OleError("Word document without a table stream")
    table = ole.read_stream(table_name)

    fc_clx, lcb_clx = struct.unpack_from("<II", document, 0x01A2)
    if lcb_clx == 0 or fc_clx + lcb_clx > len(table):
        raise OleError("Word document without a piece table")
    clx = table[fc_clx : fc_clx + lcb_clx]

    # Walk the Clx: any number of Prc blocks, then the one Pcdt holding the pieces.
    offset = 0
    plc: bytes | None = None
    while offset < len(clx):
        marker = clx[offset]
        if marker == 0x01:
            if offset + 3 > len(clx):
                break
            size = struct.unpack_from("<H", clx, offset + 1)[0]
            offset += 3 + size
        elif marker == 0x02:
            if offset + 5 > len(clx):
                break
            size = struct.unpack_from("<I", clx, offset + 1)[0]
            plc = clx[offset + 5 : offset + 5 + size]
            break
        else:
            break
    if not plc or len(plc) < 12:
        raise OleError("Word piece table is empty")

    count = (len(plc) - 4) // 12
    if count <= 0:
        raise OleError("Word piece table has no pieces")
    positions = struct.unpack_from(f"<{count + 1}I", plc, 0)
    pieces: list[tuple[bool, bytes]] = []
    for index in range(count):
        descriptor = 4 * (count + 1) + index * 8
        fc = struct.unpack_from("<I", plc, descriptor + 2)[0]
        compressed = bool(fc & 0x40000000)
        start = (fc & 0x3FFFFFFF) // 2 if compressed else fc & 0x3FFFFFFF
        characters = positions[index + 1] - positions[index]
        if characters <= 0:
            continue
        length = characters if compressed else characters * 2
        if start < 0 or start + length > len(document):
            continue
        pieces.append((compressed, document[start : start + length]))

    text = _clean(_decode_pieces(pieces))
    metadata: dict[str, object] = {
        "format": "doc",
        "pieces": len(pieces),
        "table_stream": table_name,
    }
    return text, metadata


__all__ = ["OleError", "OleFile", "extract_doc_text"]
