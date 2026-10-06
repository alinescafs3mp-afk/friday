"""Strict Debian control, Release, clearsign, and checksum adapters."""

from resource_meter import HashlibProxy, reserve_allocation, debit_read, checkpoint
hashlib = HashlibProxy()
import re

from contract import (
    MAX_CLEARSIGN_BYTES,
    MAX_FIELD_VALUE,
    MAX_FIELDS,
    MAX_MEMBER_SIZE,
    MAX_PACKAGES_BYTES,
    MAX_STANZAS,
    ContractError,
    bounded_int,
    is_digest,
)

_FIELD_NAME = re.compile(rb"^[A-Za-z0-9][A-Za-z0-9+.-]{0,63}$")
_SHA256_ENTRY = re.compile(rb"^([0-9a-f]{64})[ ]+([0-9]+)[ ]+([A-Za-z0-9._+/~-]+)$")
_SHASUM_LINE = re.compile(rb"^([0-9a-f]{64})  ([A-Za-z0-9._+-]+)$")
_BASE64_LINE = re.compile(rb"^[A-Za-z0-9+/=]+$")
_CHECKSUM_LINE = re.compile(rb"^=[A-Za-z0-9+/]{4}$")
_ARMOR_HEADER = re.compile(rb"^[A-Za-z0-9-]+: [A-Za-z0-9 .+_-]+$")

_BEGIN = b"-----BEGIN PGP SIGNED MESSAGE-----"
_SIGNATURE = b"-----BEGIN PGP SIGNATURE-----"
_END = b"-----END PGP SIGNATURE-----"
_SUPPORTED_HASH = {b"SHA256": "sha256", b"SHA512": "sha512"}
RELEASE_MAX_BYTES = 2000000


def parse_deb822(data, max_bytes, max_stanzas=MAX_STANZAS, max_fields=MAX_FIELDS, max_field_value=MAX_FIELD_VALUE):
    # Compatibility collector; whole-vector selection uses the streaming API.
    stanzas=[]
    for stanza in iter_deb822(data,max_bytes,max_stanzas,max_fields,max_field_value):
        reserve_allocation(8)
        stanzas.append(stanza)
    return stanzas


def iter_deb822(data, max_bytes, max_stanzas=MAX_STANZAS, max_fields=MAX_FIELDS, max_field_value=MAX_FIELD_VALUE):
    if type(data) is not bytes:
        raise ContractError("deb822_type")
    debit_read(len(data)*3)
    if len(data) > max_bytes or b"\r" in data or b"\x00" in data:
        raise ContractError("deb822_bounds")
    count=0
    current = None
    last_name = None
    start=0
    while start<len(data):
        checkpoint()
        end=data.find(b'\n',start)
        if end<0:end=len(data)
        reserve_allocation(end-start)
        raw=data[start:end]
        start=end+1
        if raw == b"":
            if current is not None:
                if not current:
                    raise ContractError("empty_stanza")
                count+=1
                if count > max_stanzas:
                    raise ContractError("stanza_count")
                yield current
            current = None
            last_name = None
            continue
        if raw.startswith((b" ", b"\t")):
            if current is None or last_name is None:
                raise ContractError("orphan_continuation")
            size=len(current[last_name])+1+len(raw)-1
            if size > max_field_value:
                raise ContractError("field_length")
            reserve_allocation(size+len(raw))
            merged = current[last_name] + b"\n" + raw[1:]
            current[last_name] = merged
            continue
        if b":" not in raw:
            raise ContractError("missing_colon")
        reserve_allocation(len(raw)*2+80)
        name, value = raw.split(b":", 1)
        if _FIELD_NAME.fullmatch(name) is None:
            raise ContractError("field_name")
        if value.startswith(b" "):
            value = value[1:]
        elif value != b"":
            raise ContractError("field_space")
        if len(value) > max_field_value:
            raise ContractError("field_length")
        reserve_allocation(len(name)*4+64)
        key = name.decode("ascii")
        if current is None:
            current = {}
        if key in current:
            raise ContractError("duplicate_field")
        if len(current) >= max_fields:
            raise ContractError("field_count")
        current[key] = value
        reserve_allocation(len(key) + len(value) + 8)
        last_name = key
    if current is not None:
        if not current:
            raise ContractError("empty_stanza")
        count+=1
        if count>max_stanzas:raise ContractError('stanza_count')
        yield current
    if not count:
        raise ContractError("stanza_count")


def normalized_body(body_lines):
    reserve_allocation(sum(len(line)+2 for line in body_lines)*2+len(body_lines)*40)
    chunks = []
    for line in body_lines:
        if line.startswith(b"- "):
            line = line[2:]
        chunks.append(line + b"\r\n")
    return b"".join(chunks)


def parse_clearsign(data, max_bytes=MAX_CLEARSIGN_BYTES):
    if type(data) is not bytes or not data.endswith(b"\n"):
        raise ContractError("clearsign_type")
    if not 32 <= len(data) <= max_bytes or b"\r" in data or b"\x00" in data:
        raise ContractError("clearsign_bounds")
    debit_read(len(data))
    reserve_allocation(len(data)*2+(len(data)+1)*40)
    lines = data.split(b"\n")[:-1]
    if not lines or lines[0] != _BEGIN:
        raise ContractError("clearsign_begin")
    index = 1
    headers = []
    while index < len(lines) and lines[index] != b"":
        if not lines[index].startswith(b"Hash: "):
            raise ContractError("clearsign_header")
        headers.append(lines[index][6:])
        index += 1
    if len(headers) != 1 or index >= len(lines) or lines[index] != b"":
        raise ContractError("clearsign_separator")
    index += 1
    body = []
    while index < len(lines) and lines[index] != _SIGNATURE:
        body.append(lines[index])
        index += 1
    if index >= len(lines) or lines[index] != _SIGNATURE:
        raise ContractError("clearsign_signature_begin")
    signature_at = index
    index += 1
    while index < len(lines) and _ARMOR_HEADER.fullmatch(lines[index]) is not None:
        index += 1
    if index >= len(lines) or lines[index] != b"":
        raise ContractError("clearsign_armor_header")
    index += 1
    base64_count = 0
    while index < len(lines) and _BASE64_LINE.fullmatch(lines[index]) is not None and not lines[index].startswith(b"="):
        base64_count += 1
        index += 1
    if base64_count < 1 or index >= len(lines) or _CHECKSUM_LINE.fullmatch(lines[index]) is None:
        raise ContractError("clearsign_armor_body")
    index += 1
    if index != len(lines) - 1 or lines[index] != _END:
        raise ContractError("clearsign_trailing_material")
    body_bytes = normalized_body(body)
    reserve_allocation(sum(len(line)+1 for line in body)*2+len(body)*40)
    clear_lines = [line[2:] if line.startswith(b"- ") else line for line in body]
    cleartext = b"\n".join(clear_lines) + (b"\n" if clear_lines else b"")
    signed_body_sha256 = hashlib.sha256(body_bytes).hexdigest()
    algorithm = _SUPPORTED_HASH.get(headers[0])
    if algorithm is None:
        raise ContractError("unsupported_hash")
    hash_header_digest = hashlib.new(algorithm, body_bytes).hexdigest()
    if algorithm == "sha256" and hash_header_digest != signed_body_sha256:
        raise ContractError("hash_correspondence")
    if algorithm == "sha512" and len(hash_header_digest) != 128:
        raise ContractError("hash_correspondence")
    if len(signed_body_sha256) != 64:
        raise ContractError("hash_correspondence")
    reserve_allocation(sum(len(line)+1 for line in lines[signature_at:])*2+len(lines)*8)
    armor = b"\n".join(lines[signature_at:]) + b"\n"
    return {
        "raw_document_sha256": hashlib.sha256(data).hexdigest(),
        "normalized_body": body_bytes,
        "cleartext": cleartext,
        "signature_armor": armor,
        "signed_body_sha256": signed_body_sha256,
        "hash_header": headers[0].decode("ascii"),
        "hash_header_digest": hash_header_digest,
        "signature_armor_sha256": hashlib.sha256(armor).hexdigest(),
        "algorithm_status": "SUPPORTED",
        "publisher_proof": False,
    }


def _decode(value, cause):
    try:
        return value.decode("utf-8")
    except UnicodeError as exc:
        raise ContractError(cause) from exc


def _ascii_bytes(value, cause):
    if type(value) is not str:
        raise ContractError(cause)
    try:
        return value.encode("ascii")
    except UnicodeError as exc:
        raise ContractError(cause) from exc


def parse_release(cleartext, expected, max_bytes=RELEASE_MAX_BYTES):
    if type(expected) is not dict:
        raise ContractError("release_expected")
    if type(cleartext) is not bytes:
        raise ContractError("release_type")
    if len(cleartext) > max_bytes:
        raise ContractError("release_bounds")
    stanzas = parse_deb822(cleartext, max_bytes=max_bytes, max_stanzas=1)
    if len(stanzas) != 1:
        raise ContractError("release_stanza_count")
    release = stanzas[0]
    for key in ("Origin", "Suite", "Components", "Architectures", "SHA256"):
        if key not in release:
            raise ContractError("release_identity_field")
    origin = _decode(release["Origin"], "release_origin")
    if origin != "Ubuntu":
        raise ContractError("release_origin")
    suite = _decode(release["Suite"], "suite")
    if suite != expected["suite"]:
        raise ContractError("suite_mismatch")
    components = release["Components"].split()
    component = _ascii_bytes(expected["component"], "component_mismatch")
    if component not in components:
        raise ContractError("component_mismatch")
    architectures = release["Architectures"].split()
    architecture = _ascii_bytes(expected["architecture"], "architecture_mismatch")
    if architecture not in architectures:
        raise ContractError("architecture_mismatch")
    member_name = component + b"/binary-" + architecture + b"/Packages"
    found = None
    for line in release["SHA256"].split(b"\n"):
        if line == b"":
            continue
        matched = _SHA256_ENTRY.fullmatch(line)
        if matched is None:
            raise ContractError("sha256_entry")
        digest, size_text, name = matched.groups()
        if name.startswith(b"/") or b".." in name.split(b"/"):
            raise ContractError("member_path")
        size = bounded_int(size_text, MAX_MEMBER_SIZE, "integer_text")
        if name == member_name:
            if found is not None:
                raise ContractError("duplicate_member")
            found = (digest.decode("ascii"), size, name.decode("ascii"))
    if found is None:
        raise ContractError("member_missing")
    if found[0] != expected["packages_sha256"] or found[1] != expected["size"]:
        raise ContractError("member_identity")
    return {
        "suite": suite,
        "component": expected["component"],
        "architecture": expected["architecture"],
        "member_name": found[2],
        "packages_sha256": found[0],
        "size": found[1],
        "publisher_proof": False,
    }


def select_package(data, expected, max_bytes=MAX_PACKAGES_BYTES):
    stanzas = iter_deb822(data, max_bytes=max_bytes)
    want = (expected["name"], expected["version"], expected["architecture"], expected["filename"])
    found = None
    for stanza in stanzas:
        identity = tuple(
            _decode(stanza[key], "package_text") if key in stanza else None
            for key in ("Package", "Version", "Architecture", "Filename")
        )
        if identity != want:
            continue
        if found is not None:
            raise ContractError("duplicate_package")
        for key in ("Package", "Version", "Architecture", "Filename", "Size", "SHA256"):
            if key not in stanza:
                raise ContractError("package_identity_field")
        size = bounded_int(stanza["Size"], MAX_MEMBER_SIZE, "integer_text")
        digest = _decode(stanza["SHA256"], "package_sha256")
        if not is_digest(digest) or size != expected["size"] or digest != expected["sha256"]:
            raise ContractError("package_archive_identity")
        if _decode(stanza["Filename"], "filename") != expected["filename"]:
            raise ContractError("filename")
        found = {
            "name": expected["name"],
            "version": expected["version"],
            "architecture": expected["architecture"],
            "filename": expected["filename"],
            "size": size,
            "sha256": digest,
            "unknown_fields": sorted(set(stanza) - {"Package", "Version", "Architecture", "Filename", "Size", "SHA256"}),
            "publisher_proof": False,
        }
    if found is None:
        raise ContractError("package_missing")
    return found


def select_shasum(cleartext, filename, authoritative_sha256):
    if type(cleartext) is not bytes or type(filename) is not str:
        raise ContractError("shasum_type")
    if len(cleartext) > MAX_CLEARSIGN_BYTES:
        raise ContractError("shasum_bounds")
    found = None
    lines = cleartext.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for line in lines:
        if line == b"":
            continue
        matched = _SHASUM_LINE.fullmatch(line)
        if matched is None:
            raise ContractError("shasum_line")
        digest, name = matched.groups()
        if name.decode("ascii") == filename:
            if found is not None:
                raise ContractError("duplicate_shasum")
            found = digest.decode("ascii")
    if found is None:
        raise ContractError("shasum_missing")
    if authoritative_sha256 is None:
        return {
            "filename": filename,
            "line_sha256": found,
            "status": "NOT_PROVEN",
            "cause": "authoritative_checksum_absent",
            "publisher_proof": False,
        }
    if type(authoritative_sha256) is not str or found != authoritative_sha256:
        raise ContractError("shasum_mismatch")
    return {
        "filename": filename,
        "line_sha256": found,
        "status": "STRUCTURALLY_BOUND",
        "cause": "checksum_row_bound",
        "publisher_proof": False,
    }
