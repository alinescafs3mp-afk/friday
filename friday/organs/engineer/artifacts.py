"""Static analysis and bounded mutation of owner-supplied artifacts.

Parsers are fail-soft: truncated or hostile bytes still return a closed
report instead of raising into the tool loop. Mutation never rewrites the
source Raw Object; callers persist a new generated file.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import stat
import struct
import time
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from .redaction import redact_text, redact_url

MAX_ANALYZE_BYTES = 32 * 1024 * 1024
MAX_STRINGS = 80
MIN_STRING_LEN = 5
MAX_STRING_LEN = 160
MAX_ZIP_ENTRIES = 256
MAX_ZIP_ENTRY_BYTES = 16 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 200.0
MAX_ZIP_METADATA_BYTES = 256 * 1024
ZIP_STREAM_CHUNK_BYTES = 256 * 1024
MAX_PATCH_OPS = 32
MAX_REPLACE_HITS = 64
MAX_PATCH_CHUNK_BYTES = 1024 * 1024
MAX_IMPORTS = 40
MAX_STRING_SCAN_BYTES = 4 * 1024 * 1024
MAX_PE_ENTROPY_BYTES = 512 * 1024
_HEX_COMPACT = re.compile(r"^[0-9a-fA-F]+$")
_PRINTABLE = re.compile(rb"[\x20-\x7e]{%d,%d}" % (MIN_STRING_LEN, MAX_STRING_LEN))
_SUSPICIOUS_IMPORTS = frozenset(
    {
        "virtualprotect",
        "virtualalloc",
        "writeprocessmemory",
        "createremotethread",
        "ntunmapviewofsection",
        "winexec",
        "shellexecute",
        "isdebuggerpresent",
        "checkremotedebuggerpresent",
        "loadlibrarya",
        "loadlibraryw",
        "getprocaddress",
    }
)
_PE_MACHINES = {
    0x14C: "i386",
    0x8664: "x64",
    0xAA64: "arm64",
    0x1C0: "arm",
    0x1C4: "armnt",
}
_ELF_MACHINES = {
    3: "i386",
    62: "x86_64",
    40: "arm",
    183: "aarch64",
}
_ELF_TYPES = {1: "relocatable", 2: "executable", 3: "shared", 4: "core"}


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("engineer artifact deadline expired")


def _sample_bytes(data: bytes, limit: int) -> bytes:
    if len(data) <= limit:
        return data
    head = limit // 2
    return data[:head] + data[-(limit - head) :]


def parse_hex(value: str) -> bytes:
    raw = str(value or "")
    if len(raw) > MAX_PATCH_CHUNK_BYTES * 3:
        raise ValueError("hex payload exceeds the patch chunk cap")
    compact = re.sub(r"[\s:_-]", "", raw)
    if not compact or len(compact) % 2 or not _HEX_COMPACT.fullmatch(compact):
        raise ValueError("hex payload is not even-length hexadecimal")
    if len(compact) // 2 > MAX_PATCH_CHUNK_BYTES:
        raise ValueError("hex payload exceeds the patch chunk cap")
    return bytes.fromhex(compact)


def _entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    total = float(len(data))
    score = 0.0
    for count in counts.values():
        if not count:
            continue
        ratio = count / total
        score -= ratio * math.log2(ratio)
    return round(score, 3)


def digest_bytes(data: bytes) -> dict[str, str]:
    return {
        "md5": hashlib.md5(data, usedforsecurity=False).hexdigest(),
        "sha1": hashlib.sha1(data, usedforsecurity=False).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _strings(data: bytes) -> list[str]:
    sampled = _sample_bytes(data, MAX_STRING_SCAN_BYTES)
    found: list[str] = []
    for match in _PRINTABLE.finditer(sampled):
        value = redact_text(
            match.group().decode("ascii", errors="ignore"),
            limit=MAX_STRING_LEN,
        )
        if value and value not in found:
            found.append(value)
        if len(found) >= MAX_STRINGS:
            return found
    # UTF-16LE runs, sampled independently so a packed ASCII-less PE still talks.
    utf16: list[str] = []
    current = bytearray()
    for index in range(0, len(sampled) - 1, 2):
        low, high = sampled[index], sampled[index + 1]
        if high == 0 and 32 <= low < 127:
            current.append(low)
            continue
        if len(current) >= MIN_STRING_LEN:
            utf16.append(redact_text(current.decode("ascii"), limit=MAX_STRING_LEN))
            if len(found) + len(utf16) >= MAX_STRINGS:
                break
        current = bytearray()
    if len(current) >= MIN_STRING_LEN and len(found) + len(utf16) < MAX_STRINGS:
        utf16.append(redact_text(current.decode("ascii"), limit=MAX_STRING_LEN))
    merged = found + [item for item in utf16 if item not in found]
    return merged[:MAX_STRINGS]


def _u16(data: bytes, offset: int, endian: str = "<") -> int | None:
    if offset < 0 or offset + 2 > len(data):
        return None
    return struct.unpack_from(endian + "H", data, offset)[0]


def _u32(data: bytes, offset: int, endian: str = "<") -> int | None:
    if offset < 0 or offset + 4 > len(data):
        return None
    return struct.unpack_from(endian + "I", data, offset)[0]


def _u64(data: bytes, offset: int, endian: str = "<") -> int | None:
    if offset < 0 or offset + 8 > len(data):
        return None
    return struct.unpack_from(endian + "Q", data, offset)[0]


def classify_kind(data: bytes, filename: str = "") -> str:
    name = str(filename or "").casefold()
    if data.startswith(b"MZ"):
        return "pe" if b"PE\x00\x00" in data[:1024] else "dos"
    if data.startswith(b"\x7fELF"):
        return "elf"
    if data[:4] in {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe"}:
        return "macho"
    if data[:4] in {b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}:
        return "macho_fat"
    if data.startswith(b"dex\n"):
        return "dex"
    if data[:2] == b"PK":
        lower_name = name
        if lower_name.endswith(".apk") or b"AndroidManifest.xml" in data[: 64 * 1024]:
            return "apk"
        if lower_name.endswith(".jar") or b"META-INF/MANIFEST.MF" in data[: 64 * 1024]:
            return "jar"
        return "zip"
    if name.endswith((".exe", ".dll", ".sys")):
        return "pe"
    if name.endswith((".so", ".elf", ".o")):
        return "elf"
    return "unknown"


def _pe_checksum(data: bytes, checksum_offset: int, *, deadline: float | None = None) -> int:
    if checksum_offset < 0 or checksum_offset + 4 > len(data):
        return 0
    padded = data if len(data) % 2 == 0 else data + b"\x00"
    total = 0
    index = 0
    limit = len(padded)
    while index < limit:
        if index % (1024 * 1024) == 0:
            _check_deadline(deadline)
        if index == checksum_offset:
            index += 4
            continue
        word = padded[index] | (padded[index + 1] << 8)
        total += word
        total = (total & 0xFFFF) + (total >> 16)
        index += 2
    total = (total & 0xFFFF) + (total >> 16)
    return (total + len(data)) & 0xFFFFFFFF


def _rva_to_offset(sections: Sequence[Mapping[str, int]], rva: int) -> int | None:
    for section in sections:
        start = int(section["virtual_address"])
        size = max(int(section["virtual_size"]), int(section["raw_size"]))
        if start <= rva < start + size:
            return int(section["raw_offset"]) + (rva - start)
    return None


def _read_cstr(data: bytes, offset: int, limit: int = 256) -> str:
    if offset < 0 or offset >= len(data):
        return ""
    end = data.find(b"\x00", offset, min(len(data), offset + limit))
    chunk = data[offset:end] if end >= 0 else data[offset : offset + limit]
    return chunk.decode("ascii", errors="replace")


def _analyze_pe(data: bytes, *, deadline: float | None = None) -> dict[str, Any]:
    _check_deadline(deadline)
    e_lfanew = _u32(data, 0x3C) or 0
    if e_lfanew + 24 > len(data) or data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
        return {"readable": False, "reason": "pe_signature_missing"}
    coff = e_lfanew + 4
    machine = _u16(data, coff) or 0
    section_count = _u16(data, coff + 2) or 0
    timestamp = _u32(data, coff + 4) or 0
    optional_size = _u16(data, coff + 16) or 0
    characteristics = _u16(data, coff + 18) or 0
    optional = coff + 20
    magic = _u16(data, optional) or 0
    pe32_plus = magic == 0x20B
    checksum_off = optional + 64
    stored_checksum = _u32(data, checksum_off) or 0
    subsystem = _u16(data, optional + 68) or 0
    directory_off = optional + (112 if pe32_plus else 96)
    dir_count = _u32(data, optional + (108 if pe32_plus else 92)) or 0
    sections: list[dict[str, Any]] = []
    section_table = optional + optional_size
    findings: list[dict[str, str]] = []
    for index in range(min(section_count, 96)):
        _check_deadline(deadline)
        start = section_table + index * 40
        if start + 40 > len(data):
            break
        name = data[start : start + 8].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        virtual_size = _u32(data, start + 8) or 0
        virtual_address = _u32(data, start + 12) or 0
        raw_size = _u32(data, start + 16) or 0
        raw_offset = _u32(data, start + 20) or 0
        chars = _u32(data, start + 36) or 0
        slice_end = min(len(data), raw_offset + raw_size) if raw_offset else raw_offset
        body = (
            data[raw_offset : min(slice_end, raw_offset + MAX_PE_ENTROPY_BYTES)]
            if 0 <= raw_offset < slice_end
            else b""
        )
        entropy = _entropy(body)
        executable = bool(chars & 0x20000000)
        writable = bool(chars & 0x80000000)
        sections.append(
            {
                "name": name,
                "virtual_address": virtual_address,
                "virtual_size": virtual_size,
                "raw_offset": raw_offset,
                "raw_size": raw_size,
                "characteristics": f"0x{chars:08x}",
                "entropy": entropy,
                "entropy_sample_bytes": len(body),
                "executable": executable,
                "writable": writable,
            }
        )
        if executable and writable:
            findings.append({"code": "rwx_section", "detail": name or f"section_{index}"})
        if entropy >= 7.2 and raw_size >= 256:
            findings.append({"code": "high_entropy_section", "detail": f"{name}:{entropy}"})
        if name.upper().startswith("UPX"):
            findings.append({"code": "packed_upx", "detail": name})
    headers_end = section_table + section_count * 40
    overlay = max(
        0, len(data) - max((item["raw_offset"] + item["raw_size"] for item in sections), default=headers_end)
    )
    if overlay >= 64:
        findings.append({"code": "overlay_present", "detail": str(overlay)})
    computed = _pe_checksum(data, checksum_off, deadline=deadline)
    if stored_checksum == 0:
        findings.append({"code": "missing_checksum", "detail": "optional_header.CheckSum=0"})
    elif stored_checksum != computed:
        findings.append({"code": "checksum_mismatch", "detail": f"{stored_checksum:08x}!={computed:08x}"})
    imports: list[str] = []
    if dir_count > 1 and directory_off + 8 <= len(data):
        import_rva = _u32(data, directory_off + 8) or 0
        file_off = _rva_to_offset(sections, import_rva) if import_rva else None
        cursor = file_off or -1
        while cursor >= 0 and len(imports) < MAX_IMPORTS:
            if cursor + 20 > len(data):
                break
            name_rva = _u32(data, cursor + 12) or 0
            first_thunk = _u32(data, cursor + 16) or 0
            if name_rva == 0 and first_thunk == 0:
                break
            name_off = _rva_to_offset(sections, name_rva)
            dll = _read_cstr(data, name_off or -1).casefold()
            if dll:
                imports.append(dll)
                if any(token in dll for token in ("ntdll", "kernel32", "advapi32")):
                    pass
            cursor += 20
    lowered_imports = {item.casefold() for item in imports}
    text_blob = _sample_bytes(data, MAX_STRING_SCAN_BYTES).lower()
    for name in _SUSPICIOUS_IMPORTS:
        if name.encode("ascii") in text_blob or name in lowered_imports:
            findings.append({"code": "suspicious_import", "detail": name})
    clr_rva = 0
    if dir_count > 14:
        clr_rva = _u32(data, directory_off + 14 * 8) or 0
    return {
        "readable": True,
        "machine": _PE_MACHINES.get(machine, f"0x{machine:x}"),
        "section_count": section_count,
        "timestamp": timestamp,
        "pe32_plus": pe32_plus,
        "subsystem": subsystem,
        "dll": bool(characteristics & 0x2000),
        "checksum_stored": f"{stored_checksum:08x}",
        "checksum_computed": f"{computed:08x}",
        "clr": bool(clr_rva),
        "overlay_bytes": overlay,
        "sections": sections,
        "imports": imports,
        "findings": findings,
    }


def _analyze_elf(data: bytes) -> dict[str, Any]:
    if len(data) < 64 or data[:4] != b"\x7fELF":
        return {"readable": False, "reason": "elf_ident_missing"}
    elf_class = data[4]
    endian_flag = data[5]
    endian = "<" if endian_flag == 1 else ">"
    is64 = elf_class == 2
    elf_type = _u16(data, 16, endian) or 0
    machine = _u16(data, 18, endian) or 0
    findings: list[dict[str, str]] = []
    needed: list[str] = []
    interpreter = ""
    for match in re.finditer(rb"lib[\w.-]+\.so(?:\.\d+)*", data[: min(len(data), 256 * 1024)]):
        item = match.group().decode("ascii", errors="ignore")
        if item not in needed:
            needed.append(item)
        if len(needed) >= 24:
            break
    interp = data.find(b"/lib")
    if interp >= 0:
        interpreter = _read_cstr(data, interp, 80)
    if b"GNU_STACK" not in data[: min(len(data), 64 * 1024)]:
        findings.append({"code": "gnu_stack_unseen", "detail": "PT_GNU_STACK not in first 64KiB"})
    return {
        "readable": True,
        "class": "elf64" if is64 else "elf32",
        "endian": "le" if endian == "<" else "be",
        "type": _ELF_TYPES.get(elf_type, str(elf_type)),
        "machine": _ELF_MACHINES.get(machine, str(machine)),
        "interpreter": interpreter,
        "needed": needed,
        "findings": findings,
    }


def _unsafe_zip_name(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name or name.startswith(("/", "~")):
        return True
    if re.match(r"^[A-Za-z]:", name):
        return True
    path = PurePosixPath(name)
    parts = name.split("/")
    if parts and parts[-1] == "":
        parts.pop()
    return path.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts)


def _zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (int(info.external_attr) >> 16) & 0xFFFF
    return bool(mode and stat.S_ISLNK(mode))


def _validated_zip_infos(
    archive: zipfile.ZipFile,
    *,
    deadline: float | None = None,
) -> tuple[list[zipfile.ZipInfo], int]:
    entry_cap = min(MAX_ZIP_ENTRY_BYTES, MAX_ANALYZE_BYTES)
    total_cap = min(MAX_ZIP_TOTAL_BYTES, MAX_ANALYZE_BYTES)
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ValueError(f"archive contains more than {MAX_ZIP_ENTRIES} entries")
    names: set[str] = set()
    total = 0
    metadata = len(archive.comment)
    for info in infos:
        _check_deadline(deadline)
        name = str(info.filename)
        if name in names:
            raise ValueError("archive contains duplicate entry names")
        names.add(name)
        if _unsafe_zip_name(name):
            raise ValueError("archive contains an unsafe entry path")
        if _zip_symlink(info):
            raise ValueError("archive contains a symbolic-link entry")
        if info.flag_bits & 0x1:
            raise ValueError("encrypted archive entries are not supported")
        if info.file_size < 0 or info.compress_size < 0:
            raise ValueError("archive entry sizes are invalid")
        if info.file_size > entry_cap:
            raise ValueError("archive entry exceeds the uncompressed entry cap")
        total += info.file_size
        if total > total_cap:
            raise ValueError("archive exceeds the total uncompressed cap")
        metadata += len(name.encode("utf-8", errors="replace")) + len(info.extra) + len(info.comment)
        if metadata > MAX_ZIP_METADATA_BYTES:
            raise ValueError("archive metadata exceeds the cap")
        ratio = info.file_size / max(1, info.compress_size)
        if ratio > MAX_ZIP_COMPRESSION_RATIO:
            raise ValueError("archive entry exceeds the compression-ratio cap")
    return infos, total


def _analyze_zip(
    data: bytes,
    kind: str,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos, total = _validated_zip_infos(archive, deadline=deadline)
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        ValueError,
        RuntimeError,
        NotImplementedError,
        OSError,
    ) as exc:
        return {
            "readable": False,
            "reason": "unsafe_archive" if isinstance(exc, ValueError) else "zip_unreadable",
            "findings": [{"code": "unsafe_archive", "detail": type(exc).__name__}],
        }
    raw_names = [info.filename for info in infos]
    signed = any(
        item.upper().startswith("META-INF/") and item.upper().endswith((".RSA", ".DSA", ".EC", ".SF"))
        for item in raw_names
    )
    manifest = "AndroidManifest.xml" in raw_names
    dex = any(item.casefold().endswith(".dex") for item in raw_names)
    native = [
        redact_text(item, limit=260)
        for item in raw_names
        if item.startswith("lib/") and item.casefold().endswith(".so")
    ][:20]
    names = [redact_text(item, limit=260) for item in raw_names]
    if kind == "apk" and not signed:
        findings.append({"code": "unsigned_apk", "detail": "no META-INF signature block"})
    if kind == "apk" and not manifest:
        findings.append({"code": "apk_manifest_missing", "detail": "AndroidManifest.xml"})
    return {
        "readable": True,
        "entries": names,
        "entry_count": len(infos),
        "uncompressed_bytes": total,
        "truncated": False,
        "signed": signed,
        "android_manifest": manifest,
        "dex": dex,
        "native_libs": native,
        "findings": findings,
    }


def analyze_bytes(
    data: bytes,
    filename: str = "",
    *,
    deadline: float | None = None,
    external_describe: bool = False,
) -> dict[str, Any]:
    if len(data) > MAX_ANALYZE_BYTES:
        return {
            "ok": False,
            "error": f"artifact larger than {MAX_ANALYZE_BYTES} bytes",
            "size_bytes": len(data),
        }
    try:
        _check_deadline(deadline)
    except TimeoutError as exc:
        return {"ok": False, "error": str(exc), "size_bytes": len(data)}
    kind = classify_kind(data, filename)
    entropy_sample = _sample_bytes(data, MAX_STRING_SCAN_BYTES)
    report: dict[str, Any] = {
        "ok": True,
        "filename": redact_text(str(filename or "artifact"), limit=180),
        "kind": kind,
        "size_bytes": len(data),
        "entropy": _entropy(entropy_sample),
        "entropy_sample_bytes": len(entropy_sample),
        "hashes": digest_bytes(data),
        "strings": _strings(data),
        "format": {},
        "findings": [],
    }
    try:
        if kind in {"pe", "dos"}:
            report["format"] = (
                _analyze_pe(data, deadline=deadline)
                if kind == "pe"
                else {"readable": False, "reason": "mz_only"}
            )
        elif kind == "elf":
            report["format"] = _analyze_elf(data)
        elif kind in {"macho", "macho_fat"}:
            magic = data[:4].hex()
            report["format"] = {
                "readable": True,
                "magic": magic,
                "fat": kind == "macho_fat",
            }
        elif kind == "dex":
            report["format"] = {
                "readable": True,
                "magic": data[:8].decode("ascii", errors="replace"),
            }
        elif kind in {"zip", "jar", "apk"}:
            report["format"] = _analyze_zip(data, kind, deadline=deadline)
    except TimeoutError as exc:
        report["ok"] = False
        report["error"] = str(exc)
        return report
    nested = list(report.get("format", {}).get("findings") or [])
    iocs = _interesting_strings(list(report.get("strings") or []))
    if iocs.get("urls") or iocs.get("paths"):
        nested.append(
            {"code": "embedded_iocs", "detail": ",".join(list(iocs.get("urls") or [])[:4]) or "paths"}
        )
    report["findings"] = sorted(
        (
            {
                "code": redact_text(item.get("code"), limit=64),
                "detail": redact_text(item.get("detail"), limit=240),
            }
            for item in nested
            if isinstance(item, Mapping) and item.get("code")
        ),
        key=lambda item: (item["code"], item["detail"]),
    )[:128]
    report["finding_codes"] = sorted(
        {str(item.get("code") or "") for item in report["findings"] if item.get("code")}
    )
    report["iocs"] = iocs
    try:
        _check_deadline(deadline)
    except TimeoutError as exc:
        report["ok"] = False
        report["error"] = str(exc)
        return report
    if external_describe:
        try:
            from . import local_binaries

            described = local_binaries.describe_bytes(data, deadline=deadline)
            if described.get("ok"):
                report["file_cmd"] = redact_text(
                    str(described.get("stdout") or "").strip(),
                    limit=400,
                )
        except (OSError, TimeoutError, ValueError):
            report["file_cmd"] = ""
    return report


_URL_IN_STRING = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_PATH_IN_STRING = re.compile(r"(?:[A-Za-z]:\\|/)(?:[\w.-]+[\\/]){1,8}[\w.-]+")


def _interesting_strings(items: Sequence[str]) -> dict[str, list[str]]:
    urls: list[str] = []
    paths: list[str] = []
    for item in items:
        for match in _URL_IN_STRING.finditer(item):
            value = redact_url(match.group(), limit=160)
            if value not in urls:
                urls.append(value)
        for match in _PATH_IN_STRING.finditer(item):
            value = redact_text(match.group(), limit=160)
            if value not in paths:
                paths.append(value)
    return {"urls": urls[:16], "paths": paths[:16]}


def apply_patches(
    data: bytes,
    operations: Sequence[Mapping[str, Any]],
    *,
    max_output_bytes: int = MAX_ANALYZE_BYTES,
    deadline: float | None = None,
) -> tuple[bytes, list[dict[str, Any]]]:
    output_cap = min(int(max_output_bytes), MAX_ANALYZE_BYTES)
    if output_cap < 1:
        raise ValueError("patch output cap is invalid")
    if len(data) > output_cap:
        raise ValueError("source artifact exceeds the patch output cap")
    if len(operations) > MAX_PATCH_OPS:
        raise ValueError(f"at most {MAX_PATCH_OPS} patch operations")
    current = data
    log: list[dict[str, Any]] = []
    for index, raw_op in enumerate(operations):
        _check_deadline(deadline)
        if not isinstance(raw_op, Mapping):
            raise ValueError(f"operation {index} is not an object")
        kind = str(raw_op.get("kind") or "").strip().casefold()
        if kind == "write_at":
            offset = int(raw_op.get("offset") or 0)
            chunk = parse_hex(str(raw_op.get("bytes") or ""))
            if offset < 0 or offset > len(current):
                raise ValueError(f"write_at offset {offset} is outside the file")
            end = offset + len(chunk)
            projected = max(len(current), end)
            if projected > output_cap:
                raise ValueError("write_at exceeds the patch output cap")
            current = current[:offset] + chunk + current[end:]
            log.append({"kind": kind, "offset": offset, "bytes": len(chunk)})
        elif kind == "replace_bytes":
            needle = parse_hex(str(raw_op.get("find") or ""))
            replacement = parse_hex(str(raw_op.get("replace") or ""))
            if not needle:
                raise ValueError("replace_bytes needs a non-empty find value")
            replace_all = bool(raw_op.get("all"))
            hits = current.count(needle)
            if hits == 0:
                raise ValueError("replace_bytes did not match")
            if replace_all and hits > MAX_REPLACE_HITS:
                raise ValueError("replace_bytes hit cap")
            applied = hits if replace_all else 1
            projected = len(current) + applied * (len(replacement) - len(needle))
            if projected > output_cap:
                raise ValueError("replace_bytes exceeds the patch output cap")
            if replace_all:
                current = current.replace(needle, replacement)
            else:
                current = current.replace(needle, replacement, 1)
            log.append(
                {"kind": kind, "hits": applied, "find_bytes": len(needle), "replace_bytes": len(replacement)}
            )
        elif kind == "zip_replace":
            name = str(raw_op.get("name") or "")
            chunk = parse_hex(str(raw_op.get("bytes") or ""))
            if not name:
                raise ValueError("zip_replace needs an entry name")
            current = _zip_replace(
                current,
                name,
                chunk,
                max_output_bytes=output_cap,
                deadline=deadline,
            )
            log.append(
                {
                    "kind": kind,
                    "name": redact_text(name, limit=260),
                    "bytes": len(chunk),
                    "signature_invalidated": True,
                }
            )
        else:
            raise ValueError(f"unknown patch kind {kind!r}")
        if len(current) > output_cap:
            raise ValueError("patched artifact exceeds the output cap")
    if current[:2] == b"MZ" and b"PE\x00\x00" in current[:1024]:
        _check_deadline(deadline)
        e_lfanew = _u32(current, 0x3C) or 0
        checksum_off = e_lfanew + 4 + 20 + 64
        if checksum_off + 4 <= len(current):
            digest = _pe_checksum(current, checksum_off, deadline=deadline)
            current = current[:checksum_off] + struct.pack("<I", digest) + current[checksum_off + 4 :]
            log.append({"kind": "pe_checksum", "value": f"{digest:08x}"})
    return current, log


def _clone_zip_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    clone.compress_type = info.compress_type
    clone.comment = info.comment
    clone.create_system = info.create_system
    clone.create_version = info.create_version
    clone.extract_version = info.extract_version
    clone.internal_attr = info.internal_attr
    clone.external_attr = info.external_attr
    clone.volume = info.volume
    # Do not replay attacker-controlled extra fields or encryption/data-descriptor flags.
    clone.extra = b""
    clone.flag_bits = info.flag_bits & 0x800
    return clone


def _write_zip_stream(
    destination: Any,
    chunks: Iterable[bytes],
    *,
    expected_bytes: int,
    output: io.BytesIO,
    output_cap: int,
    deadline: float | None,
) -> None:
    written = 0
    for chunk in chunks:
        _check_deadline(deadline)
        written += len(chunk)
        if written > expected_bytes or written > MAX_ZIP_ENTRY_BYTES:
            raise ValueError("archive entry expanded beyond its declared size")
        destination.write(chunk)
        if output.tell() > output_cap:
            raise ValueError("rewritten archive exceeds the output cap")
    if written != expected_bytes:
        raise ValueError("archive entry size differs from its central-directory declaration")


def _iter_file_chunks(handle: Any) -> Iterable[bytes]:
    while True:
        chunk = handle.read(ZIP_STREAM_CHUNK_BYTES)
        if not chunk:
            return
        yield chunk


def _zip_replace(
    data: bytes,
    name: str,
    payload: bytes,
    *,
    max_output_bytes: int,
    deadline: float | None,
) -> bytes:
    if _unsafe_zip_name(name):
        raise ValueError("zip replacement name is unsafe")
    entry_cap = min(MAX_ZIP_ENTRY_BYTES, MAX_ANALYZE_BYTES)
    total_cap = min(MAX_ZIP_TOTAL_BYTES, MAX_ANALYZE_BYTES)
    if len(payload) > entry_cap:
        raise ValueError("zip replacement exceeds the entry cap")
    output_cap = min(int(max_output_bytes), MAX_ANALYZE_BYTES)
    out = io.BytesIO()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as source:
            infos, total = _validated_zip_infos(source, deadline=deadline)
            matching = [info for info in infos if info.filename == name]
            if not matching:
                raise ValueError(f"zip entry {redact_text(name, limit=260)!r} is missing")
            if matching[0].is_dir():
                raise ValueError("zip replacement target is a directory")
            original_size = matching[0].file_size
            if total - original_size + len(payload) > total_cap:
                raise ValueError("replacement exceeds the total uncompressed archive cap")
            with zipfile.ZipFile(out, "w", allowZip64=True) as destination:
                destination.comment = source.comment
                for info in infos:
                    _check_deadline(deadline)
                    clone = _clone_zip_info(info)
                    expected = len(payload) if info.filename == name else info.file_size
                    with destination.open(clone, "w", force_zip64=True) as target:
                        if info.filename == name:
                            chunks: Iterable[bytes] = (
                                payload[index : index + ZIP_STREAM_CHUNK_BYTES]
                                for index in range(0, len(payload), ZIP_STREAM_CHUNK_BYTES)
                            )
                            _write_zip_stream(
                                target,
                                chunks,
                                expected_bytes=expected,
                                output=out,
                                output_cap=output_cap,
                                deadline=deadline,
                            )
                        else:
                            with source.open(info, "r") as source_entry:
                                _write_zip_stream(
                                    target,
                                    _iter_file_chunks(source_entry),
                                    expected_bytes=expected,
                                    output=out,
                                    output_cap=output_cap,
                                    deadline=deadline,
                                )
                    if out.tell() > output_cap:
                        raise ValueError("rewritten archive exceeds the output cap")
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, OSError) as exc:
        raise ValueError("not a safe zip/apk archive") from exc
    result = out.getvalue()
    if len(result) > output_cap:
        raise ValueError("rewritten archive exceeds the output cap")
    return result


def render_markdown(report: Mapping[str, Any]) -> str:
    if not report.get("ok"):
        return str(report.get("error") or "analysis failed")
    lines = [
        f"# {redact_text(report.get('filename') or 'artifact', limit=180)}",
        f"kind: `{report.get('kind')}`  size: {report.get('size_bytes')}  entropy: {report.get('entropy')}",
        "hashes: " + ", ".join(f"{key} `{value}`" for key, value in (report.get("hashes") or {}).items()),
    ]
    findings = list(report.get("findings") or [])
    if findings:
        lines.append("findings:")
        for item in findings:
            lines.append(
                f"- `{redact_text(item.get('code'), limit=64)}` {redact_text(item.get('detail'), limit=240)}"
            )
    else:
        lines.append("findings: none from the static heuristics")
    fmt = report.get("format") or {}
    if fmt.get("imports"):
        lines.append("imports: " + ", ".join(redact_text(item, limit=120) for item in fmt["imports"][:20]))
    if fmt.get("sections"):
        lines.append(
            "sections: "
            + ", ".join(redact_text(item.get("name") or "?", limit=32) for item in fmt["sections"][:16])
        )
    if fmt.get("needed"):
        lines.append("needed: " + ", ".join(redact_text(item, limit=120) for item in fmt["needed"][:16]))
    if fmt.get("native_libs"):
        lines.append("native: " + ", ".join(redact_text(item, limit=160) for item in fmt["native_libs"][:12]))
    string_count = len(list(report.get("strings") or []))
    if string_count:
        lines.append(f"redacted string evidence: {string_count} bounded samples")
    return "\n".join(lines)[:12_000]


def public_finding_payload(report: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded, secret-stripped projection for the optional secondary brain."""

    fmt = report.get("format") if isinstance(report.get("format"), Mapping) else {}
    return {
        "kind": report.get("kind"),
        "size_bytes": report.get("size_bytes"),
        "entropy": report.get("entropy"),
        "hashes": dict(report.get("hashes") or {}),
        "finding_codes": list(report.get("finding_codes") or []),
        "imports": list((fmt or {}).get("imports") or [])[:16],
        "sections": [
            {
                "name": item.get("name"),
                "entropy": item.get("entropy"),
                "executable": item.get("executable"),
                "writable": item.get("writable"),
            }
            for item in list((fmt or {}).get("sections") or [])[:16]
            if isinstance(item, Mapping)
        ],
        "needed": list((fmt or {}).get("needed") or [])[:16],
        "signed": (fmt or {}).get("signed"),
        "clr": (fmt or {}).get("clr"),
    }
