"""Bounded static archive parsing: bytes and plans only, never extraction/exec."""
from dataclasses import dataclass
import bz2
import hashlib
import io
import lzma
import stat
import struct
from types import MappingProxyType
import zipfile
import zlib
from canonical import ContractError, digest, exact_keys, validate_digest, validate_integer, validate_path


def normalize_link(path, target):
    # Import only when invoked, after the exact-spec closed module set is loaded.
    from manifest import normalize_link as normalize
    return normalize(path, target)

MEMBER_KEYS = ("path", "type", "size", "sha256", "target", "mode")
LIMIT_KEYS = ("archive_bytes", "expanded_bytes", "members", "member_bytes", "total_bytes",
              "depth", "path_bytes", "compression_ratio")
DEFAULT_LIMITS = dict(archive_bytes=64 << 20, expanded_bytes=128 << 20, members=10000,
                      member_bytes=64 << 20, total_bytes=128 << 20, depth=32,
                      path_bytes=4096, compression_ratio=200)
MAX_LIMITS = dict(archive_bytes=1 << 30, expanded_bytes=1 << 32, members=10000,
                  member_bytes=1 << 30, total_bytes=1 << 32, depth=32,
                  path_bytes=4096, compression_ratio=1000)


def _limits(limits):
    if limits is not None and not isinstance(limits, (dict, MappingProxyType)):
        raise ContractError("limit object required")
    result = dict(DEFAULT_LIMITS if limits is None else limits)
    exact_keys(result, LIMIT_KEYS)
    for key in LIMIT_KEYS:
        validate_integer(result[key], minimum=1, maximum=MAX_LIMITS[key])
    return result


@dataclass(frozen=True)
class ArchivePlan:
    members: tuple
    payloads: object
    source_sha256: str
    plan_sha256: str
    limits: object

    def projection(self):
        return {"members": [dict(member) for member in self.members], "source_sha256": self.source_sha256, "limits": dict(self.limits)}


def resolve_materialized_member(mapping, path, *, maximum_depth=32):
    """The single parser/assembler resolver; hardlinks never traverse symlinks."""
    seen = set()
    while True:
        if path in seen or len(seen) >= maximum_depth:
            raise ContractError("archive hardlink cycle/depth")
        seen.add(path)
        node = mapping.get(path)
        if node is None:
            raise ContractError("archive hardlink missing target")
        if node["type"] == "file":
            return node
        if node["type"] != "hardlink":
            raise ContractError("archive hardlink nonfile/symlink target")
        path = node["target"]


def materialized_payloads(plan):
    """Resolve/hash/account the entire output before the first filesystem effect."""
    mapping = {item["path"]: item for item in plan.members}
    result, total = {}, 0
    for member in plan.members:
        if member["type"] not in ("file", "hardlink"):
            continue
        target = resolve_materialized_member(mapping, member["path"], maximum_depth=plan.limits["depth"])
        raw = plan.payloads.get(target["path"])
        if type(raw) is not bytes or len(raw) != target["size"] or hashlib.sha256(raw).hexdigest() != target["sha256"]:
            raise ContractError("complete materialized payload required")
        total += len(raw)
        if total > plan.limits["total_bytes"]:
            raise ContractError("archive materialized-copy aggregate bomb")
        result[member["path"]] = raw
    return MappingProxyType(result), total


def validate_members(members, *, limits=None, payloads=None, source_sha256=None):
    bounds = _limits(limits)
    if type(members) not in (list, tuple) or not members or len(members) > bounds["members"]:
        raise ContractError("archive member count")
    paths = []
    total = 0
    normalized = []
    for original in members:
        if not isinstance(original, (dict, MappingProxyType)):
            raise ContractError("archive member object required")
        member = exact_keys(dict(original), MEMBER_KEYS)
        path = validate_path(member["path"])
        if len(path) > bounds["path_bytes"] or len(path.split("/")) > bounds["depth"]:
            raise ContractError("archive path/depth limit")
        paths.append(path)
        kind = member["type"]
        if kind not in ("file", "directory", "symlink", "hardlink"):
            raise ContractError("archive special/sparse/pax/unknown member")
        validate_integer(member["size"], maximum=bounds["member_bytes"])
        validate_integer(member["mode"], maximum=0o7777)
        if member["mode"] & 0o7000:
            raise ContractError("archive special permission bits")
        if kind == "file":
            validate_digest(member["sha256"])
            if member["target"] is not None:
                raise ContractError("file link target forbidden")
            total += member["size"]
        else:
            if member["size"] or member["sha256"] is not None:
                raise ContractError("non-file payload forbidden")
            if kind == "directory" and member["target"] is not None:
                raise ContractError("directory target forbidden")
            if kind == "symlink":
                if type(member["target"]) is not str or member["target"].startswith("/"):
                    raise ContractError("archive absolute symlink")
                normalize_link(path, member["target"])
            if kind == "hardlink":
                validate_path(member["target"])
        normalized.append(member)
    if len(paths) != len(set(paths)) or len(paths) != len({path.casefold() for path in paths}):
        raise ContractError("archive duplicate/case collision")
    if total > bounds["total_bytes"]:
        raise ContractError("archive aggregate bomb")
    mapping = {member["path"]: member for member in normalized}
    materialized_total = 0
    for member in normalized:
        parts = member["path"].split("/")
        for index in range(1, len(parts)):
            parent = mapping.get("/".join(parts[:index]))
            if parent is None or parent["type"] != "directory":
                raise ContractError("unlisted/nondirectory archive parent")
        if member["type"] in ("file", "hardlink"):
            materialized_total += resolve_materialized_member(mapping, member["path"], maximum_depth=bounds["depth"])["size"]
            if materialized_total > bounds["total_bytes"]:
                raise ContractError("archive materialized-copy aggregate bomb")
        if member["type"] == "symlink":
            target = normalize_link(member["path"], member["target"]) if member["type"] == "symlink" else member["target"]
            seen = {member["path"]}
            while True:
                node = mapping.get(target)
                if node is None or target in seen or len(seen) > bounds["depth"]:
                    raise ContractError("archive dangling/looping link")
                seen.add(target)
                if node["type"] not in ("symlink", "hardlink"):
                    if member["type"] == "hardlink" and node["type"] != "file":
                        raise ContractError("archive hardlink nonfile")
                    break
                target = normalize_link(target, node["target"]) if node["type"] == "symlink" else node["target"]
    files = {member["path"]: member for member in normalized if member["type"] == "file"}
    if payloads is None:
        payloads = {}
    if type(payloads) is not dict or (payloads and set(payloads) != set(files)):
        raise ContractError("archive payload inventory")
    for path, raw in payloads.items():
        if type(raw) is not bytes or len(raw) != files[path]["size"] or hashlib.sha256(raw).hexdigest() != files[path]["sha256"]:
            raise ContractError("archive payload bytes mismatch")
    normalized.sort(key=lambda item: item["path"].encode("ascii"))
    source = digest(normalized) if source_sha256 is None else source_sha256
    validate_digest(source)
    projection = {"members": normalized, "source_sha256": source, "limits": bounds}
    return ArchivePlan(tuple(MappingProxyType(item) for item in normalized), MappingProxyType(dict(payloads)), source, digest(projection), MappingProxyType(bounds))


def _expand(raw, bounds):
    if type(raw) is not bytes or len(raw) > bounds["archive_bytes"]:
        raise ContractError("archive compressed byte bound")
    if raw.startswith(b"\x1f\x8b"):
        decoder = zlib.decompressobj(31)
        expanded = decoder.decompress(raw, bounds["expanded_bytes"] + 1)
        done = decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail
    elif raw.startswith(b"BZh"):
        decoder = bz2.BZ2Decompressor()
        expanded = decoder.decompress(raw, max_length=bounds["expanded_bytes"] + 1)
        done = decoder.eof and not decoder.unused_data
    elif raw.startswith(b"\xfd7zXZ\x00"):
        decoder = lzma.LZMADecompressor(memlimit=128 << 20)
        expanded = decoder.decompress(raw, max_length=bounds["expanded_bytes"] + 1)
        done = decoder.eof and not decoder.unused_data
    elif raw.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            from compression import zstd
        except ImportError as exc:
            raise ContractError("bounded zstd capability unavailable") from exc
        try:
            decoder = zstd.ZstdDecompressor(options={zstd.DecompressionParameter.window_log_max: 27})
            expanded = decoder.decompress(raw, max_length=bounds["expanded_bytes"] + 1)
            done = decoder.eof and not decoder.unused_data
        except (ValueError, zstd.ZstdError) as exc:
            raise ContractError("bounded zstd capability unavailable/invalid") from exc
    else:
        expanded, done = raw, True
    if not done or len(expanded) > bounds["expanded_bytes"] or len(expanded) > max(1, len(raw)) * bounds["compression_ratio"]:
        raise ContractError("archive decompression/trailing/ratio bomb")
    return expanded


def _tar_string(raw):
    value, separator, trailing = raw.partition(b"\x00")
    if separator and trailing.strip(b"\x00"):
        raise ContractError("tar hidden NUL suffix")
    try:
        return value.decode("ascii")
    except UnicodeError as exc:
        raise ContractError("tar non-ASCII path") from exc


def _octal(raw):
    value = raw.strip(b" \x00")
    if not value or any(byte not in b"01234567" for byte in value):
        raise ContractError("tar noncanonical numeric/base256 field")
    return int(value, 8)


def parse_tar(raw, *, limits=None, _debian_root_prefix=False):
    bounds = _limits(limits)
    try:
        stream = _expand(raw, bounds)
        if len(stream) % 512:
            raise ContractError("tar block alignment")
        offset = 0
        members, payloads = [], {}
        root_seen = False
        while offset + 512 <= len(stream):
            header = stream[offset:offset + 512]
            if header == bytes(512):
                if stream[offset:offset + 1024] != bytes(1024) or any(stream[offset:]):
                    raise ContractError("tar trailing/partial terminator")
                return validate_members(members, limits=bounds, payloads=payloads, source_sha256=hashlib.sha256(raw).hexdigest())
            if len(members) >= bounds["members"]:
                raise ContractError("tar member count")
            if _octal(header[148:156]) != sum(header[:148]) + 8 * 32 + sum(header[156:]):
                raise ContractError("tar header checksum")
            magic = header[257:263]
            if magic not in (b"ustar\x00", bytes(6)):
                raise ContractError("tar extension/GNU format")
            name = _tar_string(header[:100])
            prefix = _tar_string(header[345:500]) if magic == b"ustar\x00" else ""
            path = (prefix + "/" if prefix else "") + name
            size, mode = _octal(header[124:136]), _octal(header[100:108])
            kind = {b"0": "file", b"\x00": "file", b"5": "directory", b"2": "symlink", b"1": "hardlink"}.get(header[156:157])
            if kind is None:
                raise ContractError("tar special/sparse/pax member")
            if kind == "directory" and path.endswith("/"):
                path = path[:-1]
            if _debian_root_prefix and path == "." and kind == "directory" and size == 0 and not root_seen and not members:
                if mode & 0o7000:
                    raise ContractError("deb root special permissions")
                root_seen = True
                offset += 512
                continue
            if _debian_root_prefix and path.startswith("./"):
                path = path[2:]
            validate_path(path)
            if size > bounds["member_bytes"] or offset + 512 + size > len(stream):
                raise ContractError("tar member size/truncation")
            payload = stream[offset + 512:offset + 512 + size]
            target = _tar_string(header[157:257]) if kind in ("symlink", "hardlink") else None
            member = dict(path=path, type=kind, size=size if kind == "file" else 0,
                          sha256=hashlib.sha256(payload).hexdigest() if kind == "file" else None,
                          target=target, mode=mode)
            if kind != "file" and size:
                raise ContractError("tar nonfile data")
            members.append(member)
            if kind == "file":
                payloads[path] = payload
            offset += 512 + ((size + 511) // 512) * 512
        raise ContractError("tar missing terminator")
    except (OSError, EOFError, ValueError, zlib.error, lzma.LZMAError) as exc:
        raise ContractError("invalid bounded tar stream") from exc


def parse_deb(raw, *, limits=None):
    """Strict ar/deb2.0 parser; control scripts validated as bytes, never run.

    Only data.tar contents become an assembly plan. Debian's one lexical './'
    prefix is normalized in this format only; resulting inventory is canonical.
    """
    bounds = _limits(limits)
    if type(raw) is not bytes or len(raw) > bounds["archive_bytes"] or not raw.startswith(b"!<arch>\n"):
        raise ContractError("deb ar header/byte bound")
    offset, entries = 8, []
    while offset < len(raw):
        if len(entries) >= 3 or offset + 60 > len(raw):
            raise ContractError("deb ar count/truncation")
        header = raw[offset:offset + 60]
        if header[58:] != b"`\n":
            raise ContractError("deb ar header terminator")
        try:
            name = header[:16].decode("ascii").rstrip(" ")
            size_field = header[48:58].decode("ascii").rstrip(" ")
        except UnicodeError as exc:
            raise ContractError("deb ar ASCII header") from exc
        if name.endswith("/"):
            name = name[:-1]
        if not size_field.isdigit() or len(size_field) > 10:
            raise ContractError("deb ar numeric size")
        size = int(size_field)
        if size > bounds["archive_bytes"] or offset + 60 + size > len(raw):
            raise ContractError("deb ar size/truncation")
        payload = raw[offset + 60:offset + 60 + size]
        if size & 1 and raw[offset + 60 + size:offset + 61 + size] != b"\n":
            raise ContractError("deb ar padding")
        entries.append((name, payload))
        offset += 60 + size + (size & 1)
    if len(entries) != 3 or entries[0] != ("debian-binary", b"2.0\n"):
        raise ContractError("deb format/version/order")
    accepted = (".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")
    if not any(entries[1][0] == "control" + suffix for suffix in accepted) or not any(entries[2][0] == "data" + suffix for suffix in accepted):
        raise ContractError("deb fixed control/data member names")
    parse_tar(entries[1][1], limits=bounds, _debian_root_prefix=True)
    data = parse_tar(entries[2][1], limits=bounds, _debian_root_prefix=True)
    return validate_members([dict(item) for item in data.members], payloads=dict(data.payloads),
                            limits=bounds, source_sha256=hashlib.sha256(raw).hexdigest())


def parse_zip(raw, *, limits=None):
    bounds = _limits(limits)
    if type(raw) is not bytes or len(raw) > bounds["archive_bytes"]:
        raise ContractError("zip compressed byte bound")
    end = raw.rfind(b"PK\x05\x06", max(0, len(raw) - 65557))
    if end < 0 or end + 22 > len(raw):
        raise ContractError("zip end record missing")
    fields = struct.unpack("<4s4H2IH", raw[end:end + 22])
    _, disk, directory_disk, disk_count, count, directory_size, directory_offset, comment = fields
    if disk or directory_disk or disk_count != count or not count or count > bounds["members"] or count == 65535 or comment or end + 22 != len(raw) or directory_offset + directory_size != end:
        raise ContractError("zip disk/zip64/comment/count/trailing contour")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) != count:
                raise ContractError("zip central count drift")
            members, payloads = [], {}
            expanded = 0
            for info in infos:
                if info.orig_filename != info.filename:
                    raise ContractError("zip NUL/truncated filename")
                if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or info.extra or info.comment:
                    raise ContractError("zip encryption/extension/method")
                if info.file_size > bounds["member_bytes"] or info.file_size > max(1, info.compress_size) * bounds["compression_ratio"]:
                    raise ContractError("zip member/ratio bomb")
                expanded += info.file_size
                if expanded > bounds["expanded_bytes"] or expanded > bounds["total_bytes"]:
                    raise ContractError("zip aggregate bomb")
                path = info.filename[:-1] if info.is_dir() else info.filename
                validate_path(path)
                mode = info.external_attr >> 16
                kind = "directory" if info.is_dir() else "symlink" if stat.S_ISLNK(mode) else "file"
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK):
                    raise ContractError("zip special member")
                with archive.open(info) as source:
                    payload = source.read(bounds["member_bytes"] + 1)
                if len(payload) != info.file_size:
                    raise ContractError("zip member size mismatch")
                target = payload.decode("ascii") if kind == "symlink" else None
                if kind == "directory" and payload:
                    raise ContractError("zip directory data")
                member = dict(path=path, type=kind, size=len(payload) if kind == "file" else 0,
                              sha256=hashlib.sha256(payload).hexdigest() if kind == "file" else None,
                              target=target, mode=mode & 0o7777)
                members.append(member)
                if kind == "file":
                    payloads[path] = payload
            return validate_members(members, limits=bounds, payloads=payloads, source_sha256=hashlib.sha256(raw).hexdigest())
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile, EOFError, NotImplementedError) as exc:
        raise ContractError("invalid bounded zip stream") from exc
