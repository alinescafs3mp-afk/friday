"""Bounded Debian ar and tar metadata parser. Package scripts are not executed."""

import struct

from .bounds import admission_cause, charge, clock_cause, performing_requested, reserve
from .causes import pack
from .digests import content_sha256
from .guards import casefold_collision, duplicate_path, normalize_member_path, symlink_escapes
from .zip_wheel import _hex64, _open_held, _publisher_cause

_PACKAGE_SCRIPTS = {"preinst", "postinst", "prerm", "postrm", "config"}
_PAX_KEYS = {"path", "linkpath", "size", "uid", "gid", "uname", "gname", "mtime"}
_CONTROL_NAMES = {
    "control.tar": None,
    "control.tar.gz": "gzip",
    "control.tar.xz": "xz",
    "control.tar.zst": "zstd",
}
_DATA_NAMES = {
    "data.tar": None,
    "data.tar.gz": "gzip",
    "data.tar.xz": "xz",
    "data.tar.zst": "zstd",
}
_UNSUPPORTED_SUFFIXES = (".bz2", ".lz", ".lzma", ".Z")
_AR_NAME = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._+-"


def _field_text(field):
    if not isinstance(field, (bytes, bytearray)):
        raise ValueError("field")
    if b"\x00" in field:
        head, tail = field.split(b"\x00", 1)
        if any(byte not in (0, 32) for byte in tail):
            raise ValueError("tail")
    else:
        head = bytes(field)
    return head.strip()


def _header_text(field):
    if b"\x00" in field:
        head, tail = field.split(b"\x00", 1)
        if any(byte not in (0, 32) for byte in tail):
            raise UnicodeError("tail")
    else:
        head = bytes(field)
    return head.decode("utf-8")


def _octal(field):
    text = _field_text(field)
    if text == b"":
        return 0
    if any(byte not in b"01234567" for byte in text):
        raise ValueError("octal")
    return int(text, 8)


def _ar_uint(field):
    if not isinstance(field, (bytes, bytearray)):
        return None
    if any(byte not in b"0123456789 " for byte in field):
        return None
    text = field.strip(b" ")
    if text == b"0":
        return 0
    if text == b"" or text[:1] == b"0" or len(text) > 10:
        return None
    if any(byte not in b"0123456789" for byte in text):
        return None
    try:
        return int(text, 10)
    except (ValueError, OverflowError) as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(None,raw_origin)
        return None


def _parse_ar(data, ceilings, resources=None, meter=None):
    from .causes import enter_phase
    phase = enter_phase(meter,"ARCHIVE_AND_MEMBER_CLOSURE","parse_ar",meter.get("expected_path") if isinstance(meter,dict) else None)
    if phase:
        return None,phase
    if len(data) > ceilings["max_archive_bytes"]:
        return None, "resource_ceiling_exceeded"
    if not data.startswith(b"!<arch>\n"):
        return None, "truncated_ar_header"
    pos = 8
    members = []
    while pos < len(data):
        tick = charge(resources, meter, work_bytes=60, members=1)
        if tick is not None:
            return None, tick
        if pos == len(data):
            break
        if pos + 60 > len(data):
            return None, "truncated_ar_header"
        header = data[pos : pos + 60]
        if header[58:60] != b"`\n":
            return None, "truncated_ar_header"
        size = _ar_uint(header[48:58])
        if size is None:
            return None, "truncated_ar_header"
        if size < 0 or pos + 60 + size > len(data):
            return None, "truncated_ar_member"
        from .custody import HeldRange
        streaming=isinstance(data,HeldRange)
        tick = reserve(resources, meter, live_bytes=4096 if streaming else size * 2 + 4096, work_bytes=size)
        if tick:
            return None, tick
        payload = data.part(pos+60,pos+60+size) if streaming else data[pos+60:pos+60+size]
        name_field = header[0:16]
        header_size = size
        dialect = "sysv"
        if name_field.startswith(b"#1/"):
            dialect = "bsd"
            name_len = _ar_uint(name_field[3:])
            if name_len is None:
                return None, "truncated_ar_header"
            if name_len < 1 or name_len > size or name_len > ceilings["max_member_name_bytes"]:
                return None, "resource_ceiling_exceeded"
            raw_name = payload[:name_len].rstrip(b"\x00")
            if raw_name == b"":
                return None, "truncated_ar_header"
            try:
                name = raw_name.decode("utf-8")
            except UnicodeError as raw_origin:
                from tools.native_support import retain_source_origin
                retain_source_origin(meter,raw_origin)
                return None, "member_name_undecodable"
            payload = payload.part(name_len,len(payload)) if streaming else payload[name_len:]
            content_size = header_size - name_len
            name_prefix_bytes = name_len
            raw_name_prefix = data[pos+60:pos+60+name_len]
        else:
            try:
                text = name_field.decode("ascii")
            except UnicodeError as raw_origin:
                from tools.native_support import retain_source_origin
                retain_source_origin(meter,raw_origin)
                return None, "member_name_undecodable"
            trimmed = text.rstrip(" ")
            slash = trimmed.endswith("/")
            body = trimmed[:-1] if slash else trimmed
            if slash and body.endswith("/"):
                return None, "truncated_ar_header"
            if body == "" or any(char not in _AR_NAME for char in body):
                return None, "truncated_ar_header"
            if text != (body + ("/" if slash else "")).ljust(16):
                return None, "truncated_ar_header"
            name = body
            content_size = header_size
            name_prefix_bytes = 0
            raw_name_prefix = b""
        try:
            mtime_text = header[16:28].strip() or b"0"
            uid_text = header[28:34].strip() or b"0"
            gid_text = header[34:40].strip() or b"0"
            mode_text = header[40:48].strip() or b"0"
            if any(byte not in b"0123456789" for byte in mtime_text + uid_text + gid_text):
                return None, "truncated_ar_header"
            if any(byte not in b"01234567" for byte in mode_text):
                return None, "truncated_ar_header"
            mtime = int(mtime_text, 10)
            uid = int(uid_text, 10)
            gid = int(gid_text, 10)
            mode = int(mode_text, 8)
        except ValueError as raw_origin:
            from tools.native_support import retain_source_origin
            retain_source_origin(meter,raw_origin)
            return None, "truncated_ar_header"
        if uid > 0xFFFFFFFF or gid > 0xFFFFFFFF or mode > 0o777777:
            return None, "truncated_ar_header"
        members.append(
            {
                "name": name,
                "payload": payload,
                "size": len(payload),
                "uid": uid,
                "gid": gid,
                "mode": mode,
                "mtime": mtime,
                "content_size": content_size,
                "dialect": dialect,
                "header_payload_size": size,
                "name_prefix_bytes": name_prefix_bytes,
                "raw_name_field": name_field.decode("latin-1"),
                "raw_size_text": header[48:58].decode("latin-1"),
                "raw_mtime_text": header[16:28].decode("latin-1"),
                "raw_uid_text": header[28:34].decode("latin-1"),
                "raw_gid_text": header[34:40].decode("latin-1"),
                "raw_mode_text": header[40:48].decode("latin-1"),
                "raw_header_hex": header.hex(),
                "raw_name_prefix_hex": raw_name_prefix.hex(),
                "header_offset": pos,
            }
        )
        if size != name_prefix_bytes + content_size:
            return None, "truncated_ar_header"
        if len(members) > ceilings["max_members"]:
            return None, "resource_ceiling_exceeded"
        pos += 60 + size
        if size % 2 == 1:
            if pos >= len(data):
                return None, "ar_padding_missing"
            if data[pos : pos + 1] != b"\n":
                return None, "ar_padding_invalid"
            pos += 1
    return members, None

def _tar_checksum(header):
    total = 0
    for index, byte in enumerate(header):
        if 148 <= index < 156:
            total += 32
        else:
            total += byte
    return total


class _NumericUnsupported(Exception):
    pass


def _numeric(field):
    if not field:
        return 0
    first = field[0]
    if first & 0x80:
        if first != 0x80:
            raise _NumericUnsupported()
        return int.from_bytes(bytes([first & 0x7F]) + field[1:], "big")
    text = _field_text(field)
    if text == b"":
        return 0
    if any(byte not in b"01234567" for byte in text):
        raise ValueError("octal")
    return int(text, 8)


def _pax_uint(value):
    if not isinstance(value, str) or value == "" or len(value) > 20:
        return None
    if value == "0":
        return 0
    if any(char not in "0123456789" for char in value) or value[0] == "0":
        return None
    try:
        number = int(value)
    except (ValueError, OverflowError) as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(None,raw_origin)
        return None
    if number > 9223372036854775807:
        return None
    return number


def _pax_mtime(value):
    if not isinstance(value, str) or value == "" or len(value) > 40:
        return None
    if value.count(".") > 1:
        return None
    left, sep, right = value.partition(".")
    if left == "" or any(char not in "0123456789" for char in left):
        return None
    if left != "0" and left[0] == "0":
        return None
    if sep and (right == "" or len(right) > 9 or any(char not in "0123456789" for char in right)):
        return None
    return value


def _effective_pax(global_pax, local_pax):
    merged = dict(global_pax)
    merged.update(local_pax)
    return {key: value for key, value in merged.items() if value is not None}


def _gnu_text(payload):
    if not payload or b"\x00" not in payload:
        return None, "tar_pax_malformed"
    head, tail = payload.split(b"\x00", 1)
    if head == b"" or any(byte != 0 for byte in tail):
        return None, "tar_pax_malformed"
    try:
        return head.decode("utf-8"), None
    except UnicodeError as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(None,raw_origin)
        return None, "member_name_undecodable"


def _merge_pax(state, records):
    for key, value in records:
        if value == "":
            state[key] = None
        else:
            state[key] = value


def _apply_pax(records, member):
    for key, value in records:
        if value is None:
            continue
        if key == "path":
            member["name"] = value
        elif key == "linkpath":
            member["linkname"] = value
        elif key in ("size", "uid", "gid"):
            number = _pax_uint(value)
            if number is None:
                return "tar_pax_malformed"
            member[key] = number
        elif key == "uname":
            member["uname"] = value
        elif key == "gname":
            member["gname"] = value
        elif key == "mtime":
            if _pax_mtime(value) is None:
                return "tar_pax_malformed"
            member["mtime"] = value
    return None


def _parse_pax(payload):
    records = []
    pos = 0
    while pos < len(payload):
        space = payload.find(b" ", pos)
        if space < 0:
            return None
        length_token = payload[pos:space]
        if length_token == b"" or len(length_token) > 20 or any(byte not in b"0123456789" for byte in length_token):
            return None
        if len(length_token) > 1 and length_token[:1] == b"0":
            return None
        try:
            length = int(length_token, 10)
        except ValueError as raw_origin:
            from tools.native_support import retain_source_origin
            retain_source_origin(None,raw_origin)
            return None
        if length < 5 or pos + length > len(payload):
            return None
        record = payload[pos : pos + length]
        if not record.endswith(b"\n"):
            return None
        body = record[len(length_token) + 1 : -1]
        if b"=" not in body:
            return None
        key, value = body.split(b"=", 1)
        try:
            key_text = key.decode("utf-8")
            value_text = value.decode("utf-8")
        except UnicodeError as raw_origin:
            from tools.native_support import retain_source_origin
            retain_source_origin(None,raw_origin)
            return None
        if not key_text or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._" for char in key_text):
            return None
        if key_text not in _PAX_KEYS:
            return "unsupported"
        if key_text in ("size", "uid", "gid") and value_text != "" and _pax_uint(value_text) is None:
            return None
        if key_text == "mtime" and value_text != "" and _pax_mtime(value_text) is None:
            return None
        records.append((key_text, value_text))
        if len(records) > 256:
            return None
        pos += length
    if pos != len(payload):
        return None
    return records


def parse_tar_members(data, ceilings, resources=None, meter=None, framing=None):
    from .causes import enter_phase
    phase = enter_phase(meter,"ARCHIVE_AND_MEMBER_CLOSURE","parse_tar_members",meter.get("expected_path") if isinstance(meter,dict) else None)
    if phase:
        return None,phase
    from .compression import DecodedReader,BudgetStop
    streaming=isinstance(data,DecodedReader)
    if not streaming and len(data) > ceilings["max_expanded_bytes"]:
        return None, "resource_ceiling_exceeded"
    members = []
    pos = 0
    expanded = 0
    pending_name = None
    pending_link = None
    global_pax = {}
    local_pax = {}
    pax_history = []
    pending_hardlinks = []
    zero_blocks = 0
    events=[]
    terminator_offset=None
    while streaming or pos + 512 <= len(data):
        tick = charge(resources, meter, work_bytes=512)
        if tick is not None:
            return None, tick
        header_offset = pos
        header = data.read_exact(512) if streaming else data[pos:pos+512]
        if len(header)!=512:
            return None,"tar_terminator_absent"
        pos += 512
        if header == b"\x00" * 512:
            terminator_offset=header_offset
            if not streaming and pos + 512 > len(data):
                return None, "tar_terminator_absent"
            following=data.read_exact(512) if streaming else data[pos:pos+512]
            if following != b"\x00" * 512:
                return None, "tar_padding_invalid"
            pos += 512
            zero_blocks = 2
            break
        tick = reserve(resources, meter, members=1)
        if tick:
            return None, tick
        try:
            stored = _octal(header[148:156])
        except ValueError as raw_origin:
            from tools.native_support import retain_source_origin
            retain_source_origin(meter,raw_origin)
            return None, "tar_checksum_mismatch"
        if stored != _tar_checksum(header):
            return None, "tar_checksum_mismatch"
        magic8 = header[257:265]
        if magic8 == b"ustar\x0000":
            dialect = "posix"
        elif magic8 == b"ustar  \x00":
            dialect = "gnu"
        else:
            return None, "tar_magic_unsupported"
        extension_records = []
        try:
            name = _header_text(header[0:100])
            linkname = _header_text(header[157:257])
            uname = _header_text(header[265:297])
            gname = _header_text(header[297:329])
            prefix = ""
            if dialect == "posix":
                prefix = _header_text(header[345:500])
        except UnicodeError as raw_origin:
            from tools.native_support import retain_source_origin
            retain_source_origin(meter,raw_origin)
            return None, "member_name_undecodable"
        if prefix:
            name = prefix + "/" + name
        try:
            mode = _numeric(header[100:108])
            uid = _numeric(header[108:116])
            gid = _numeric(header[116:124])
            size = _numeric(header[124:136])
            header_mtime = _numeric(header[136:148])
            if dialect == "gnu":
                gnu_atime = _numeric(header[345:357])
                gnu_ctime = _numeric(header[357:369])
                extension_records.append({"kind": "gnu_atime", "value": gnu_atime,"header_offset":header_offset})
                extension_records.append({"kind": "gnu_ctime", "value": gnu_ctime,"header_offset":header_offset})
        except _NumericUnsupported as raw_origin:
            from tools.native_support import retain_source_origin
            retain_source_origin(meter,raw_origin)
            return None, "tar_numeric_unsupported"
        except ValueError as raw_origin:
            from tools.native_support import retain_source_origin
            retain_source_origin(meter,raw_origin)
            return None, "truncated_tar_header"
        if uid < 0 or gid < 0 or mode < 0 or size < 0:
            return None, "tar_numeric_unsupported"
        typeflag = header[156:157]
        if typeflag not in (b"x", b"g", b"L", b"K"):
            extension_records.extend(dict(item) for item in pax_history if item.get("scope") == "global" or item.get("applies_to_next") is True)
            for item in pax_history:
                if item.get("scope") != "global":
                    item["applies_to_next"] = False
        raw_mode = mode
        raw_uid = uid
        raw_gid = gid
        raw_size = size
        raw_type = typeflag.decode("latin-1")
        if typeflag not in (b"x", b"g", b"L", b"K"):
            effective_now = _effective_pax(global_pax, local_pax)
            if "size" in effective_now:
                parsed_size = _pax_uint(effective_now["size"])
                if parsed_size is None:
                    return None, "tar_pax_malformed"
                size = parsed_size
        if typeflag in (b"\x00", b"0"):
            member_type = "regular"
        elif typeflag == b"5":
            member_type = "directory"
        elif typeflag == b"2":
            member_type = "symlink"
        elif typeflag == b"1":
            member_type = "hardlink"
        elif typeflag in (b"x", b"g", b"L", b"K"):
            member_type = "extension"
        else:
            return None, "tar_type_unsupported"
        if member_type in ("directory", "symlink", "hardlink") and size != 0:
            return None, "tar_type_unsupported"
        try:
            if _numeric(header[329:337]) != 0 or _numeric(header[337:345]) != 0:
                return None, "tar_header_fields_unsupported"
            if dialect == "gnu" and (_numeric(header[369:381]) != 0 or any(byte not in (0, 32) for byte in header[381:512])):
                return None, "tar_header_fields_unsupported"
            if dialect == "posix" and any(byte not in (0, 32) for byte in header[500:512]):
                return None, "tar_header_fields_unsupported"
        except (ValueError, _NumericUnsupported) as raw_origin:
            from tools.native_support import retain_source_origin
            retain_source_origin(meter,raw_origin)
            return None, "tar_header_fields_unsupported"
        if member_type == "extension" and size > 1048576:
            return None, "resource_ceiling_exceeded"
        if size > ceilings["max_expanded_bytes"] or expanded + size > ceilings["max_expanded_bytes"]:
            return None, "resource_ceiling_exceeded"
        pad = (512 - (size % 512)) % 512
        if not streaming and pos + size + pad > len(data):
            return None, "truncated_tar_member"
        if not streaming and data[pos + size : pos + size + pad] != b"\x00" * pad:
            return None, "tar_padding_invalid"
        tick = charge(resources, meter, work_bytes=size)
        if tick is not None:
            return None, tick
        tick = reserve(resources, meter, live_bytes=16384 if streaming else size + 16384, work_bytes=size)
        if tick:
            return None, tick
        payload_facts=None
        if streaming:
            effective_path=_effective_pax(global_pax,local_pax).get("path") or pending_name or name
            normalized_now,path_cause=normalize_member_path(effective_path,ceilings["max_member_name_bytes"],ceilings["max_depth"])
            if member_type!="extension" and path_cause:
                return None,path_cause
            keep=member_type=="extension" or member_type=="regular" and data.keep_control and normalized_now=="control"
            payload,payload_facts=data.observe(size,keep)
            if data.read_exact(pad)!=b"\x00"*pad:
                return None,"tar_padding_invalid"
        else:
            payload = data[pos:pos+size]
        pos += size + pad
        expanded += size
        tick=reserve(resources,meter,live_bytes=8192+(len(payload)*8 if member_type=='extension' else 0),
                     work_bytes=4096+(len(payload)*4 if member_type=='extension' else 0))
        if tick:return None,tick
        events.append({'offset':header_offset,'header_hex':header.hex(),'payload_size':size,
            'payload_sha256':payload_facts['sha256'] if streaming else content_sha256(payload,resources,meter),
            'extension_payload_hex':payload.hex() if member_type=='extension' else None,
            'padding_bytes':pad,'padding_zero':True,
            'member_index':None if member_type=='extension' else len(members)})
        if typeflag in (b"x", b"g", b"L", b"K"):
            allocation = reserve(resources,meter,live_bytes=len(payload)*32+262144,work_bytes=len(payload)*8)
            if allocation:
                return None,allocation
            if typeflag in (b"L", b"K"):
                gnu_name, gnu_cause = _gnu_text(payload)
                if gnu_cause is not None:
                    return None, gnu_cause
                if len(pax_history) >= 256:
                    return None, "resource_ceiling_exceeded"
                pax_history.append({
                    "kind": "gnu_longname" if typeflag == b"L" else "gnu_longlink",
                    "value": gnu_name,
                    "scope": "gnu",
                    "header_offset": header_offset,
                    "applies_to_next": True,
                    "record_id":len(pax_history),
                    "raw_header_hex":header.hex(),
                    "raw_payload_hex":payload.hex(),
                })
                if typeflag == b"L":
                    pending_name = gnu_name
                else:
                    pending_link = gnu_name
            else:
                records = _parse_pax(payload)
                if records == "unsupported":
                    return None, "pax_key_unsupported"
                if records is None:
                    return None, "tar_pax_malformed"
                scope = "global" if typeflag == b"g" else "local"
                record_offset = 0
                for key_text, value_text in records:
                    if len(pax_history) >= 256:
                        return None, "resource_ceiling_exceeded"
                    space = payload.find(b" ",record_offset)
                    record_length = int(payload[record_offset:space])
                    raw_record = payload[record_offset:record_offset+record_length]
                    pax_history.append({"kind":"pax","key":key_text,"value":value_text,"scope":scope,
                        "header_offset":header_offset,"applies_to_next":True,"record_id":len(pax_history),
                        "record_offset":record_offset,"raw_record_utf8":raw_record.decode("utf-8"),
                        "raw_record_sha256":content_sha256(raw_record,resources,meter),"raw_header_hex":header.hex()})
                    record_offset += record_length
                if typeflag == b"g":
                    _merge_pax(global_pax, records)
                else:
                    _merge_pax(local_pax, records)
            continue
        member = {
            "name": name,
            "linkname": linkname,
            "mode": mode,
            "uid": uid,
            "gid": gid,
            "size": size,
            "type": member_type,
            "payload": payload,
            "uname": uname or None,
            "gname": gname or None,
            "mtime": str(header_mtime),
        }
        application = {"global":dict(global_pax),"local":dict(local_pax),
                       "gnu_name":pending_name,"gnu_link":pending_link}
        for origin in extension_records:
            if origin.get("kind") in ("pax","gnu_longname","gnu_longlink"):
                origin["applied_to_header_offset"] = header_offset
                state = application["global"] if origin["scope"] == "global" else application["local"] if origin["scope"] == "local" else None
                if origin["kind"] == "pax":
                    current = state.get(origin["key"])
                    later = [r for r in extension_records if r.get("kind") == "pax" and r.get("scope") == origin["scope"] and r.get("key") == origin["key"] and r["record_id"] > origin["record_id"]]
                    origin["disposition"] = "SUPERSEDED" if later else "DELETED" if current is None else "ACTIVE"
                else:
                    current = application["gnu_name" if origin["kind"] == "gnu_longname" else "gnu_link"]
                    later = [r for r in extension_records if r.get("kind") == origin["kind"] and r["record_id"] > origin["record_id"]]
                    origin["disposition"] = "SUPERSEDED" if later else "ACTIVE"
        if pending_name is not None:
            member["name"] = pending_name
            pending_name = None
        if pending_link is not None:
            member["linkname"] = pending_link
            pending_link = None
        effective = dict(global_pax)
        effective.update(local_pax)
        if effective:
            pax_cause = _apply_pax(effective.items(), member)
            if pax_cause is not None:
                return None, pax_cause
        if member["uid"] > 0xFFFFFFFF or member["gid"] > 0xFFFFFFFF or member["mode"] > 0o777777:
            return None, "tar_numeric_unsupported"
        local_pax = {}
        normalized, path_cause = normalize_member_path(
            member["name"], ceilings["max_member_name_bytes"], ceilings["max_depth"]
        )
        if path_cause is not None:
            return None, path_cause
        if normalized == "." and member["type"] != "directory":
            return None, "path_escape"
        link_target = None
        link_sha = None
        file_sha = None
        if member["type"] == "symlink":
            link_target = member["linkname"]
            if symlink_escapes(normalized, link_target):
                target_text = link_target.replace("\\", "/") if isinstance(link_target, str) else ""
                absolute = target_text.startswith("/") or (len(target_text) >= 2 and target_text[1] == ":")
                recipe = resources.get("install_recipe") if isinstance(resources, dict) else None
                admitted_root = isinstance(recipe, dict) and recipe.get("absolute_root") == "admitted-final-root"
                if performing_requested(resources) and admitted_root:
                    link_target = target_text
                elif performing_requested(resources) and absolute:
                    return None, "symlink_recipe_unbound"
                else:
                    return None, "symlink_escape"
            allocation = reserve(resources,meter,live_bytes=len(link_target) * 4,work_bytes=len(link_target))
            if allocation:
                return None,allocation
            link_sha = content_sha256(link_target.encode("utf-8"), resources, meter)
        elif member["type"] == "hardlink":
            link_target = member["linkname"]
            normalized_link, link_cause = normalize_member_path(
                link_target, ceilings["max_member_name_bytes"], ceilings["max_depth"]
            )
            if link_cause is not None:
                return None, "hardlink_escape"
            tick = reserve(resources, meter, work_bytes=len(members) * 64, live_bytes=len(members) * 8)
            if tick:
                return None, tick
            prior = [item for item in members if item["normalized_path"] == normalized_link and item["type"] == "regular"]
            link_target = normalized_link
            if prior:
                link_sha = prior[-1]["content_sha256"]
            else:
                link_sha = None
                pending_hardlinks.append(len(members))
        elif member["type"] == "regular":
            file_sha = payload_facts["sha256"] if streaming else content_sha256(member["payload"], resources, meter)
        base = normalized.rsplit("/", 1)[-1]
        members.append(
            {
                "source_format": "tar",
                "name": member["name"],
                "normalized_path": normalized,
                "type": member["type"],
                "mode": member["mode"],
                "uid": member["uid"],
                "gid": member["gid"],
                "uname": member["uname"],
                "gname": member["gname"],
                "mtime": member.get("mtime"),
                "size": size if streaming and member["type"]=="regular" else len(member["payload"]) if member["type"]=="regular" else 0,
                "content_sha256": file_sha,
                "link_target": link_target,
                "link_target_sha256": link_sha,
                "executable": bool(member["mode"] & 0o111) and member["type"] == "regular",
                "package_script": base in _PACKAGE_SCRIPTS,
                "compression_method": "stored",
                "crc32": None,
                "header_dialect": dialect,
                "raw_header": {"hex": header.hex(), "offset": header_offset, "dialect": dialect,
                    "effective": {"name": member["name"], "linkname": member["linkname"], "mode": member["mode"],
                                   "uid": member["uid"], "gid": member["gid"], "size": member["size"], "mtime": member["mtime"]},
                    "application":application},
                "extension_records": extension_records + [
                    {
                        "kind": "raw_header",
                        "offset": pos - size - pad - 512,
                        "size": raw_size,
                        "mode": raw_mode,
                        "uid": raw_uid,
                        "gid": raw_gid,
                        "typeflag": raw_type,
                        "dialect": dialect,
                    }
                ],
                "payload": member["payload"] if member["type"] == "regular" else b"",
            }
        )
        if len(members) > ceilings["max_members"]:
            return None, "resource_ceiling_exceeded"
    if zero_blocks != 2:
        return None, "tar_terminator_absent"
    rest = None if streaming else data[pos:]
    if streaming and not data.zero_tail() or not streaming and rest and (len(rest)%512!=0 or any(byte!=0 for byte in rest)):
        return None, "tar_trailing_bytes"
    if framing is not None:
        framing.update({'schema':'friday.scanner.tar-archive-framing.v1','events':events,
            'terminator_offset':terminator_offset,'zero_terminator_blocks':2,
            'tail_zero_bytes':data.tail_zero_bytes if streaming else len(rest),
            'expanded_stream_size':pos+(data.tail_zero_bytes if streaming else len(rest)),
            'framing_complete':True})
    if pending_name is not None or pending_link is not None or local_pax:
        return None, "tar_pending_extension"
    def _terminal_regular(start_index):
        seen = set()
        current = start_index
        for _step in range(len(members) + 1):
            tick = charge(resources, meter, work_bytes=64)
            if tick is not None:
                return tick
            if current in seen:
                return None
            seen.add(current)
            item = members[current]
            if item["type"] == "regular":
                return item
            if item["type"] != "hardlink":
                return None
            target = item.get("link_target")
            current = None
            for found, candidate in enumerate(members):
                tick = charge(resources, meter, work_bytes=len(candidate.get("normalized_path") or ""))
                if tick is not None:
                    return tick
                if candidate["normalized_path"] == target:
                    current = found
                    break
            if current is None:
                return None
        return None

    for index, item in enumerate(members):
        if item["type"] != "hardlink":
            continue
        terminal = _terminal_regular(index)
        if isinstance(terminal, str):
            return None, terminal
        if terminal is None:
            return None, "hardlink_escape"
        item["link_target_sha256"] = terminal["content_sha256"]
        item["resolved_link"] = {"type": "regular", "path": terminal["normalized_path"],
                                 "size": terminal["size"], "sha256": terminal["content_sha256"]}
    paths = [item["normalized_path"] for item in members]
    dup = duplicate_path(paths)
    if dup is not None:
        return None, "duplicate_member"
    folded = casefold_collision(paths)
    if folded is not None:
        return None, "casefold_collision"
    return members, None


def _decode_named(name, payload, capabilities, ceilings, resources=None, meter=None):
    from .compression import decode_admitted

    if name in _CONTROL_NAMES:
        method = _CONTROL_NAMES[name]
    elif name in _DATA_NAMES:
        method = _DATA_NAMES[name]
    else:
        return None, "debian_member_unexpected"
    if performing_requested(resources):
        from .compression import stream_reader
        room=max(0,ceilings["max_expanded_bytes"]-meter.get("expanded_bytes",0))
        if room<1: return None,"resource_ceiling_exceeded"
        return stream_reader(method,payload,capabilities,room,resources,meter,keep_control=name in _CONTROL_NAMES),None
    if method is None:
        if len(payload) > ceilings["max_expanded_bytes"]:
            return None, "resource_ceiling_exceeded"
        tick = reserve(resources, meter, expanded_bytes=len(payload), work_bytes=len(payload))
        if tick is not None:
            return None, tick
        return payload, None
    used = meter.get("expanded_bytes", 0) if isinstance(meter, dict) else 0
    if not isinstance(used, int) or isinstance(used, bool) or used < 0:
        used = 0
    room = max(0, ceilings["max_expanded_bytes"] - used)
    if room < 1:
        return None, "resource_ceiling_exceeded"
    raw, cause = decode_admitted(
        method, payload, capabilities, room, resources=resources, meter=meter
    )
    if cause is not None or not isinstance(raw, (bytes, bytearray)) or len(raw) > room:
        if cause is not None:
            return None, cause
        return None, "resource_ceiling_exceeded"
    return raw, None


def _classify(name):
    if name in _CONTROL_NAMES or name in _DATA_NAMES:
        return "known"
    for suffix in _UNSUPPORTED_SUFFIXES:
        if name.endswith(suffix) and (name.startswith("control.tar") or name.startswith("data.tar")):
            return "unsupported"
    return "other"


def _case_field(fields, name):
    folded = name.casefold()
    for key, value in fields.items():
        if isinstance(key, str) and key.casefold() == folded:
            return value
    return None


def _field_kind(key):
    folded = key.casefold()
    if folded == "description":
        return "multiline"
    if folded in ("md5sum", "sha1", "sha256", "sha512", "files", "checksums-sha1", "checksums-sha256", "checksums-sha512"):
        return "raw"
    if folded in (
        "depends",
        "pre-depends",
        "recommends",
        "suggests",
        "enhances",
        "breaks",
        "conflicts",
        "provides",
        "replaces",
        "built-using",
        "uploaders",
    ):
        return "folded"
    return "simple"


def _deb822(payload, resources=None, meter=None):
    from .causes import enter_phase
    phase = enter_phase(meter,"RAW_METADATA_CORRESPONDENCE","DEB822",meter.get("expected_path") if isinstance(meter,dict) else None)
    if phase:
        from .digests import DigestStop
        raise DigestStop(phase)
    if not isinstance(payload, (bytes, bytearray)):
        return None
    if len(payload) > 1048576:
        return None
    allocation = reserve(resources,meter,live_bytes=len(payload) * 96 + 8192,work_bytes=len(payload) * 8)
    if allocation:
        from .digests import DigestStop
        raise DigestStop(allocation)
    if b"\r" in payload.replace(b"\r\n", b""):
        return None
    if b"\r\n" in payload and b"\n" in payload.replace(b"\r\n", b""):
        return None
    if b"\r\n" in payload:
        ending = "crlf"
        parts = payload.split(b"\r\n")
    else:
        ending = "lf"
        parts = payload.split(b"\n")
    lines = []
    for part in parts:
        try:
            lines.append(part.decode("utf-8"))
        except UnicodeError as raw_origin:
            from tools.native_support import retain_source_origin
            retain_source_origin(meter,raw_origin)
            return None
    folded = {}
    raw = {}
    multiline = {}
    kinds = {}
    order = []
    current = None
    closed = False
    seen_field = False
    separator = "\r\n" if ending == "crlf" else "\n"
    for line in lines:
        if line == "":
            if seen_field:
                closed = True
                current = None
            continue
        if line.startswith(" ") or line.startswith("\t"):
            if current is None or closed:
                return None
            kind = kinds[current]
            raw[current] = raw[current] + separator + line
            if kind == "simple":
                return None
            if not (line.startswith(" ") or line.startswith("\t")):
                return None
            body = line[1:]
            if kind == "multiline":
                if body == ".":
                    multiline[current].append("")
                    folded[current] = folded[current] + "\n"
                else:
                    multiline[current].append(body)
                    folded[current] = folded[current] + "\n" + body
            elif kind == "raw":
                folded[current] = folded[current] + separator + line
            else:
                folded[current] = folded[current] + " " + line.strip()
            continue
        if closed:
            return None
        if ":" not in line:
            return None
        key, value = line.split(":", 1)
        if key == "" or key != key.strip():
            return None
        if any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-" for char in key):
            return None
        if any(isinstance(existing, str) and existing.casefold() == key.casefold() for existing in folded):
            return None
        current = key
        kinds[key] = _field_kind(key)
        folded[key] = value.strip()
        raw[key] = line
        order.append(key)
        if kinds[key] == "multiline":
            multiline[key] = [value.strip()]
        seen_field = True
    return {
        "folded_fields": folded,
        "raw_fields": raw,
        "multiline_fields": multiline,
        "all": folded,
        "line_ending": ending,
        "field_order": order,
        "field_kinds": kinds,
        "unknown_field_policy": "simple_no_continuation",
    }


def _public_members(members, archive_sha, domain):
    public = []
    for item in members:
        copied = dict(item)
        copied.pop("payload", None)
        copied["source_archive_sha256"] = archive_sha
        copied["source_domain"] = domain
        copied["executed"] = False
        public.append(copied)
    return public


_DEB_EXPECTED = (
    "archive_class",
    "name",
    "version",
    "architecture",
    "filename",
    "sha256",
    "size",
    "relative_path",
)


def _expected_cause(expected):
    if not isinstance(expected, dict):
        return "expected_shape_invalid"
    for key in _DEB_EXPECTED:
        if key not in expected:
            return "expected_shape_invalid"
    if expected.get("archive_class") != "deb":
        return "expected_shape_invalid"
    if not _hex64(expected.get("sha256")):
        return "expected_shape_invalid"
    size = expected.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return "expected_shape_invalid"
    for key in ("name", "version", "architecture", "filename", "relative_path"):
        if not isinstance(expected.get(key), str) or expected.get(key) == "":
            return "expected_shape_invalid"
    return None


def _scan_deb(expected, held, resources, publisher_evidence=None, meter=None):
    from .context import effects_cause
    effects = effects_cause(resources, meter)
    if effects:
        return pack("REFUSED", effects)
    shape = _expected_cause(expected)
    if shape is not None:
        return pack("REFUSED", shape)
    from .schema_validate import EXPECTATION_SCHEMA, validate_value

    schema_cause = validate_value(EXPECTATION_SCHEMA, expected, resources, meter)
    if schema_cause is not None:
        return pack("REFUSED", schema_cause)
    from .zip_wheel import _held_present

    held_cause = _held_present(held)
    if held_cause is not None:
        return pack("NOT_PROVEN", held_cause)
    admitted = admission_cause(resources)
    if admitted is not None:
        return pack("NOT_PROVEN", admitted)
    from .schema_validate import DEB_SCHEMA, MEMBER_SCHEMA, RESOURCE_SCHEMA, validate_value

    schema_cause = validate_value(RESOURCE_SCHEMA, resources, resources, meter)
    if schema_cause is not None:
        return pack("REFUSED", schema_cause)
    timed = clock_cause(resources)
    if timed is not None:
        return pack("REFUSED", timed)
    publisher_cause, publisher_record = _publisher_cause(publisher_evidence, resources)
    if publisher_cause is not None:
        return pack("REFUSED", publisher_cause)
    declared = held.get("declared_size", expected["size"]) if isinstance(held, dict) else expected["size"]
    if isinstance(declared, int) and not isinstance(declared, bool) and declared > resources["ceilings"]["max_archive_bytes"]:
        return pack("REFUSED", "resource_ceiling_exceeded", detail={"limit": "max_archive_bytes"})
    if not isinstance(meter, dict):
        from .bounds import new_meter

        meter = new_meter()
    if isinstance(meter, dict):
        from .custody import begin_archive
        begin_archive(meter, expected, held)
        meter["expected_sha256"] = expected.get("sha256")
        meter["expected_size"] = expected.get("size")
        meter["expected_path"] = expected.get("relative_path")
    data, opened = _open_held(held, resources, meter)
    if opened is not None:
        from .custody import release_retained_fd

        release_retained_fd(meter)
        return opened
    grant = resources.get("grant") if isinstance(resources, dict) else None
    if performing_requested(resources) and isinstance(grant, dict) and grant.get("filesystem_read") is True:
        if not isinstance(meter, dict) or "retained_fd" not in meter:
            return pack("REFUSED", "held_fd_unretained")

    try:
        if len(data) != expected["size"]:
            return pack("REFUSED", "expected_size_mismatch", detail={"held_size": len(data)})
        whole = content_sha256(data, resources, meter)
        if whole != expected["sha256"]:
            return pack("REFUSED", "expected_sha256_mismatch")
        ceilings = resources["ceilings"]
        capabilities = resources.get("compression_capabilities") or []
        ar_members, ar_cause = _parse_ar(data, ceilings, resources, meter)
        if ar_cause is not None:
            status = "REFUSED"
            if ar_cause == "resource_ceiling_exceeded":
                status = "REFUSED"
            return pack(status, ar_cause)
        names = [item["name"] for item in ar_members]
        if names.count("debian-binary") != 1:
            return pack("REFUSED", "debian_member_missing", detail={"member": "debian-binary"})
        unsupported = [item["name"] for item in ar_members if _classify(item["name"]) == "unsupported"]
        if unsupported:
            return pack("NOT_COVERED", "compression_method_unsupported", detail={"member": unsupported[0]})
        if len(ar_members) != 3 or ar_members[0]["name"] != "debian-binary":
            return pack("REFUSED", "debian_member_order")
        if ar_members[1]["name"] not in _CONTROL_NAMES or ar_members[2]["name"] not in _DATA_NAMES:
            return pack("REFUSED", "debian_member_order")
        control = [item for item in ar_members if item["name"] in _CONTROL_NAMES]
        data_members = [item for item in ar_members if item["name"] in _DATA_NAMES]
        if len(control) != 1 or len(data_members) != 1:
            return pack("REFUSED", "debian_member_missing", detail={"control": len(control), "data": len(data_members)})
        binary = next(item for item in ar_members if item["name"] == "debian-binary")
        from .custody import HeldRange
        binary_payload=binary["payload"][:4] if isinstance(binary["payload"],HeldRange) and len(binary["payload"])==4 else binary["payload"]
        if binary_payload != b"2.0\n":
            return pack("REFUSED", "debian_binary_version_unsupported")
        parsed_tars = {};tar_framing={}
        for item in (control[0], data_members[0]):
            raw, cause = _decode_named(item["name"], item["payload"], capabilities, ceilings, resources, meter)
            if cause is not None:
                covered = cause in (
                    "compression_capability_absent",
                    "compression_method_unsupported",
                    "compression_external_tool_refused",
                    "runtime_zlib_module_absent",
                    "runtime_lzma_module_absent",
                    "runtime_zstd_module_absent",
                )
                return pack("NOT_COVERED" if covered else "REFUSED", cause, detail={"member": item["name"]})
            from .compression import DecodedReader,BudgetStop
            try:
                envelope={}
                parsed,parse_cause=parse_tar_members(raw,ceilings,resources,meter,envelope)
                if parse_cause:
                    return pack("REFUSED",parse_cause,detail={"archive":item["name"]})
                parsed_tars[item["name"]]=parsed
                tar_framing[item['name']]=envelope
            except BudgetStop as stop:
                from tools.native_support import retain_source_origin
                retain_source_origin(meter,stop)
                from .public import exception_owned_result
                return exception_owned_result(meter,stop)
            finally:
                if isinstance(raw,DecodedReader): raw.close()
                raw=None
        control_members=parsed_tars[control[0]["name"]]
        data_tar_members=parsed_tars[data_members[0]["name"]]
        control_file = [item for item in control_members if item["normalized_path"] == "control"]
        control_fields = None
        if len(control_file) == 1 and control_file[0]["type"] == "regular":
            control_fields = _deb822(control_file[0]["payload"], resources, meter)
            if control_fields is None:
                return pack("REFUSED", "debian_control_malformed")
            folded_fields = control_fields.get("folded_fields") if isinstance(control_fields, dict) else None
            if not isinstance(folded_fields, dict):
                return pack("REFUSED", "debian_control_malformed")
            package = _case_field(folded_fields, "Package")
            version = _case_field(folded_fields, "Version")
            architecture = _case_field(folded_fields, "Architecture")
            for key, value in (("Package", package), ("Version", version), ("Architecture", architecture)):
                if value is None:
                    return pack("REFUSED", "debian_control_field_missing", detail={"field": key})
            if package != expected["name"] or version != expected["version"] or architecture != expected["architecture"]:
                return pack(
                    "REFUSED",
                    "debian_control_bill_mismatch",
                    detail={"package": package, "version": version},
                )
        elif control_file:
            return pack("REFUSED", "debian_control_malformed")
        else:
            return pack("REFUSED", "debian_control_field_missing", detail={"field": "control"})
        scripts = [item["normalized_path"] for item in control_members + data_tar_members if item["package_script"]]
        from .custody import reserve_public_members
        allocation = reserve_public_members(resources, meter, control_members + data_tar_members)
        if allocation:
            return pack("REFUSED", allocation)
        observation = {
            "archive_class": "deb",
            "filename": expected["filename"],
            "relative_path": expected["relative_path"],
            "whole_sha256": expected["sha256"],
            "whole_size": expected["size"],
            "ar_members": [
                {
                    "name": item["name"],
                    "size": item["size"],
                    "sha256": content_sha256(item["payload"], resources, meter),
                    "mtime": item.get("mtime"),
                    "uid": item.get("uid"),
                    "gid": item.get("gid"),
                    "mode": item.get("mode"),
                    "content_size": item.get("content_size"),
                    "dialect": item.get("dialect"),
                    "header_payload_size": item.get("header_payload_size"),
                    "name_prefix_bytes": item.get("name_prefix_bytes"),
                    "raw_name_field": item.get("raw_name_field"),
                    "raw_size_text": item.get("raw_size_text"),
                    "raw_mtime_text": item.get("raw_mtime_text"),
                    "raw_uid_text": item.get("raw_uid_text"),
                    "raw_gid_text": item.get("raw_gid_text"),
                    "raw_mode_text": item.get("raw_mode_text"),
                    "header_offset": item.get("header_offset"),
                    "raw_header_hex": item.get("raw_header_hex"),
                    "raw_name_prefix_hex":item["raw_name_prefix_hex"],
                }
                for item in ar_members
            ],
            "debian_binary": "2.0",
            "control_members": _public_members(control_members, expected["sha256"], "control"),
            "data_members": _public_members(data_tar_members, expected["sha256"], "data"),
            "tar_framing":{'control':tar_framing[control[0]['name']],'data':tar_framing[data_members[0]['name']]},
            "control_fields": {
                "package": package,
                "version": version,
                "architecture": architecture,
                "all": folded_fields,
                "raw_sha256": content_sha256(control_file[0]["payload"], resources, meter),
                "raw_utf8":control_file[0]["payload"].decode("utf-8"),
                "raw_fields": control_fields.get("raw_fields"),
                "folded_fields": folded_fields,
                "multiline_fields": control_fields.get("multiline_fields"),
                "line_ending": control_fields.get("line_ending"),
                "field_order": control_fields.get("field_order"),
                "field_kinds": control_fields.get("field_kinds"),
                "unknown_field_policy": control_fields.get("unknown_field_policy"),
            },
            "package_script_members": scripts,
            "package_scripts_executed": False,
            "publisher_evidence": publisher_record,
            "publisher_proof": False,
            "install_performed": False,
            "resource_permission_actual": "NOT_PROVEN",
            "fds_opened_by_scanner": meter.get("fds_peak", 0) if meter.get("filesystem_stat_performed") is True else 0,
            "nested_archives_opened": 0,
            "custody_changed": False,
            "filesystem_stat_performed": meter.get("filesystem_stat_performed") is True,
            "member_count": len(control_members) + len(data_tar_members),
        }
        from .custody import seal_archive_custody

        observation["per_archive_custody"] = seal_archive_custody(meter, data, expected.get("sha256"), resources)
        schema_cause = validate_value(DEB_SCHEMA, observation, resources, meter)
        if schema_cause is not None:
            return pack("REFUSED", schema_cause)
        from .semantics import deb_cause, member_cause
        semantic = deb_cause(observation,resources,meter)
        if semantic:
            return pack("REFUSED", semantic, stage="RAW_METADATA_CORRESPONDENCE")
        for member in observation["control_members"] + observation["data_members"]:
            semantic = member_cause(member,resources,meter)
            if semantic:
                return pack("REFUSED", semantic, stage="ARCHIVE_AND_MEMBER_CLOSURE")
            schema_cause = validate_value(MEMBER_SCHEMA, member, resources, meter)
            if schema_cause is not None:
                return pack("REFUSED", schema_cause)
        from .bounds import charge_output

        return pack("OBSERVED", None, observation=observation, stage="ARCHIVE_AND_MEMBER_CLOSURE")

    finally:
        from .custody import release_retained_fd

        ident = release_retained_fd(meter)
        after = meter.get("custody_after") if isinstance(meter, dict) else None
        if isinstance(ident, dict) and isinstance(meter, dict):
            if ident.get("cause") == "held_fd_unretained":
                meter["post_scan_cause"] = "held_fd_unretained"
            if isinstance(after, dict) and ident.get("cause") is None:
                for field in ("inode", "device", "size", "mtime_ns", "ctime_ns", "uid", "gid", "full_mode", "nlink"):
                    if ident.get(field) != after.get(field):
                        meter["post_scan_cause"] = "custody_identity_changed"
                        break


def scan_deb(expected, held, resources, publisher_evidence=None, meter=None):
    from .digests import DigestStop
    owned = not isinstance(meter,dict)
    if owned:
        from .bounds import new_meter
        meter = new_meter()
        meter["active_resources"] = resources
    result=None
    try:
        result = _scan_deb(expected, held, resources, publisher_evidence, meter)
    except DigestStop as stop:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,stop)
        from .public import exception_owned_result
        result = exception_owned_result(meter,stop)
    except (MemoryError,OSError,ValueError,UnicodeError,KeyError,TypeError,IndexError,RuntimeError) as exc:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,exc)
        from .public import exception_owned_result
        result=exception_owned_result(meter,exc)
    finally:
        # Both TAR/parser frames and their local control/raw aliases have ended.
        # The separately transferred full public graph is still charged.
        from .custody import release_archive_live,retain_public_observation
        try:
            if isinstance(result,dict) and "archive_live_start" in meter:
                meter["pending_terminal_result"]=result
                retained=retain_public_observation(resources,meter,result)
                if retained:
                    from .causes import causal_refusal
                    result=causal_refusal(result,retained,meter,
                        failure_detail={"reservation":"complete_leaf_result","capacity":"NOT_ADMITTED"})
        finally:
            release_archive_live(meter)
    post = meter.pop("post_scan_cause", None)
    if post:
        from .causes import causal_refusal
        result = causal_refusal(result,post,meter,cleanup_causes=[post])
    if owned:
        from .public import finish_owned
        return finish_owned(result,resources,meter)
    return result
