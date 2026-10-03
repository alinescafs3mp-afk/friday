"""Bounded ZIP wheel parser. Member bytes are inventoried and are not executed."""

import base64
import binascii
import struct

from .bounds import HARD_MAX_METADATA_BYTES, admission_cause, charge, clock_cause, performing_requested, reserve
from .causes import pack
from .digests import content_sha256, crc32, record_digest
from .filename import parse_wheel_filename, pep503_name
from .guards import casefold_collision, duplicate_path, normalize_member_path, symlink_escapes
from .normalize import requires_python_correspondence

_LOCAL = 0x04034B50
_CENTRAL = 0x02014B50
_EOCD = 0x06054B50
_ZIP64_LOCATOR = 0x07064B50
_ZIP64_EOCD = 0x06064B50
_UTF8_FLAG = 1 << 11
_DESCRIPTOR_FLAG = 1 << 3
_ENCRYPT_FLAG = 1
# These framed ancillary owner/time records do not redefine names, compression,
# member type or ranges in this selected Source profile. All bytes are retained.
_IGNORABLE_EXTRA_IDS = frozenset((0x000A,0x5455,0x5855,0x7855,0x7875))


def _u16(data, pos):
    return struct.unpack("<H", data[pos:pos+2])[0]


def _u32(data, pos):
    return struct.unpack("<I", data[pos:pos+4])[0]


def _u64(data, pos):
    return struct.unpack("<Q", data[pos:pos+8])[0]


def _decode_name(raw, flags):
    if flags & _UTF8_FLAG:
        return raw.decode("utf-8")
    return raw.decode("cp437")


def _walk_extra(extra):
    pos = 0
    fields = []
    while pos < len(extra):
        if pos + 4 > len(extra):
            return None, "truncated_zip_header"
        ident = _u16(extra, pos)
        length = _u16(extra, pos + 2)
        pos += 4
        if pos + length > len(extra):
            return None, "truncated_zip_header"
        fields.append((ident, extra[pos : pos + length]))
        pos += length
    if pos != len(extra):
        return None, "truncated_zip_header"
    return fields, None


def _zip64_values(extra, need_size, need_csize, need_offset, need_disk=False):
    fields, cause = _walk_extra(extra)
    if cause is not None:
        return None, cause
    found = None
    for ident, chunk in fields:
        if ident != 0x0001:
            if ident not in _IGNORABLE_EXTRA_IDS:
                return None,"zip_extra_unsupported"
            continue
        if found is not None:
            return None, "truncated_zip_header"
        cursor = 0
        usize = csize = offset = disk = None
        if need_size:
            if cursor + 8 > len(chunk):
                return None, "zip64_extra_absent"
            usize = _u64(chunk, cursor)
            cursor += 8
        if need_csize:
            if cursor + 8 > len(chunk):
                return None, "zip64_extra_absent"
            csize = _u64(chunk, cursor)
            cursor += 8
        if need_offset:
            if cursor + 8 > len(chunk):
                return None, "zip64_extra_absent"
            offset = _u64(chunk, cursor)
            cursor += 8
        if need_disk:
            if cursor + 4 > len(chunk):
                return None, "zip64_extra_absent"
            disk = _u32(chunk, cursor)
            cursor += 4
        if cursor != len(chunk):
            return None, "truncated_zip_header"
        found = (usize, csize, offset, disk)
    if (need_size or need_csize or need_offset or need_disk) and found is None:
        return None, "zip64_extra_absent"
    return found, None


def _find_eocd(data):
    if len(data) < 22:
        return None
    start = max(0, len(data) - (65535 + 22))
    signature = b"PK\x05\x06"
    cursor = len(data)
    while True:
        found = data.rfind(signature, start, cursor)
        if found < 0 or found + 22 > len(data):
            return None
        comment_len = _u16(data, found + 20)
        if found + 22 + comment_len == len(data):
            return found
        cursor = found


def _read_sizes(csize, usize, offset, extra, disk=0):
    need_u = usize == 0xFFFFFFFF
    need_c = csize == 0xFFFFFFFF
    need_o = offset == 0xFFFFFFFF
    need_d = disk == 0xFFFF
    parsed, extra_cause = _zip64_values(extra, need_u, need_c, need_o, need_d)
    if extra_cause is not None:
        return None, None, None, None, extra_cause
    if not (need_u or need_c or need_o or need_d):
        return usize, csize, offset, disk, None
    if parsed is None:
        return None, None, None, None, "zip64_extra_absent"
    got_u, got_c, got_o, got_d = parsed
    if need_u:
        usize = got_u
    if need_c:
        csize = got_c
    if need_o:
        offset = got_o
    if need_d:
        if got_d != 0:
            return None, None, None, None, "zip_disk_count"
        disk = got_d
    return usize, csize, offset, disk, None


def parse_zip_members(data, ceilings, capabilities, resources=None, meter=None):
    from .compression import decode_admitted
    from .causes import enter_phase
    phase = enter_phase(meter,"ARCHIVE_AND_MEMBER_CLOSURE","parse_zip_members",meter.get("expected_path") if isinstance(meter,dict) else None)
    if phase:
        return pack("REFUSED",phase,stage="ARCHIVE_AND_MEMBER_CLOSURE")

    if len(data) > ceilings["max_archive_bytes"]:
        return pack("REFUSED", "resource_ceiling_exceeded", detail={"limit": "max_archive_bytes"})
    probed, probe_cause = crc32(b"", resources, meter)
    if probe_cause is not None:
        status = "NOT_COVERED" if probe_cause == "runtime_zlib_module_absent" else "REFUSED"
        return pack(status, probe_cause)
    eocd = _find_eocd(data)
    if eocd is None:
        return pack("REFUSED", "truncated_zip_header")
    try:
        disk = _u16(data, eocd + 4)
        disk_start = _u16(data, eocd + 6)
        entries_here = _u16(data, eocd + 8)
        total = _u16(data, eocd + 10)
        cd_size = _u32(data, eocd + 12)
        cd_offset = _u32(data, eocd + 16)
    except struct.error as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,raw_origin)
        return pack("REFUSED", "truncated_zip_header")
    zip64 = (
        cd_offset == 0xFFFFFFFF
        or cd_size == 0xFFFFFFFF
        or total == 0xFFFF
        or entries_here == 0xFFFF
        or disk == 0xFFFF
        or disk_start == 0xFFFF
    )
    z64_off = None
    if zip64:
        if eocd < 20:
            return pack("REFUSED", "zip64_extra_absent")
        locator = eocd - 20
        if data[locator : locator + 4] != b"PK\x06\x07":
            return pack("REFUSED", "zip64_extra_absent")
        try:
            locator_disk = _u32(data, locator + 4)
            total_disks = _u32(data, locator + 16)
            if locator_disk != 0 or total_disks != 1:
                return pack("REFUSED", "zip_disk_count")
            z64 = _u64(data, locator + 8)
            if z64 + 56 > len(data) or _u32(data, z64) != _ZIP64_EOCD:
                return pack("REFUSED", "truncated_zip_header")
            record_size = _u64(data, z64 + 4)
            if record_size < 44:
                return pack("REFUSED", "truncated_zip_header")
            if record_size != 44:
                return pack("REFUSED", "zip_flag_unsupported")
            version_needed = _u16(data, z64 + 14)
            if z64 + 12 + record_size != locator:
                return pack("REFUSED", "truncated_zip_header")
            if version_needed != 45:
                return pack("REFUSED", "zip_flag_unsupported")
            z64_disk = _u32(data, z64 + 16)
            z64_disk_start = _u32(data, z64 + 20)
            z64_entries = _u64(data, z64 + 24)
            z64_total = _u64(data, z64 + 32)
            z64_cd_size = _u64(data, z64 + 40)
            z64_cd_offset = _u64(data, z64 + 48)

            def _adopt(current, sentinel, updated, adopt_cause):
                if current == sentinel:
                    return updated, None
                if current != updated:
                    return None, adopt_cause
                return current, None

            disk, adopt_cause = _adopt(disk, 0xFFFF, z64_disk, "zip_disk_count")
            if adopt_cause is not None:
                return pack("REFUSED", adopt_cause)
            disk_start, adopt_cause = _adopt(disk_start, 0xFFFF, z64_disk_start, "zip_disk_count")
            if adopt_cause is not None:
                return pack("REFUSED", adopt_cause)
            entries_here, adopt_cause = _adopt(entries_here, 0xFFFF, z64_entries, "zip_disk_count")
            if adopt_cause is not None:
                return pack("REFUSED", adopt_cause)
            total, adopt_cause = _adopt(total, 0xFFFF, z64_total, "zip_disk_count")
            if adopt_cause is not None:
                return pack("REFUSED", adopt_cause)
            cd_size, adopt_cause = _adopt(cd_size, 0xFFFFFFFF, z64_cd_size, "zip_central_range")
            if adopt_cause is not None:
                return pack("REFUSED", adopt_cause)
            cd_offset, adopt_cause = _adopt(cd_offset, 0xFFFFFFFF, z64_cd_offset, "zip_central_range")
            if adopt_cause is not None:
                return pack("REFUSED", adopt_cause)
            z64_off = z64
        except struct.error as raw_origin:
            from tools.native_support import retain_source_origin
            retain_source_origin(None,raw_origin)
            return pack("REFUSED", "truncated_zip_header")
    if disk != 0 or disk_start != 0 or entries_here != total:
        return pack("REFUSED", "zip_disk_count")
    if zip64:
        if z64_off is None or cd_offset + cd_size != z64_off:
            return pack("REFUSED", "zip_central_range")
    elif cd_offset + cd_size != eocd:
        return pack("REFUSED", "zip_central_range")
    if total > ceilings["max_members"]:
        return pack("REFUSED", "resource_ceiling_exceeded", detail={"limit": "max_members"})
    if cd_offset > len(data) or cd_size > len(data) - cd_offset:
        return pack("REFUSED", "truncated_zip_header")
    members = []
    ranges = []
    expanded = 0
    pos = cd_offset
    end = cd_offset + cd_size
    for _index in range(total):
        tick = charge(resources, meter, work_bytes=46, members=0)
        if tick is not None:
            return pack("REFUSED", tick)
        if pos + 46 > end:
            return pack("REFUSED", "truncated_zip_header")
        if _u32(data, pos) != _CENTRAL:
            return pack("REFUSED", "truncated_zip_header")
        central_header_offset = pos
        central_header = data[pos:pos+46]
        made_by = _u16(data, pos + 4)
        version_needed = _u16(data, pos + 6)
        flags = _u16(data, pos + 8)
        method = _u16(data, pos + 10)
        crc = _u32(data, pos + 16)
        csize = _u32(data, pos + 20)
        usize = _u32(data, pos + 24)
        central_desc_zip64 = csize == 0xFFFFFFFF or usize == 0xFFFFFFFF
        name_len = _u16(data, pos + 28)
        extra_len = _u16(data, pos + 30)
        comment_len = _u16(data, pos + 32)
        external = _u32(data, pos + 38)
        local_off = _u32(data, pos + 42)
        central_disk = _u16(data, pos + 34)
        allocation = reserve(resources,meter,work_bytes=(name_len+extra_len+comment_len)*8,
                             live_bytes=(name_len+extra_len+comment_len)*16+65536)
        if allocation:
            return pack("REFUSED",allocation,stage="ARCHIVE_AND_MEMBER_CLOSURE")
        if central_disk not in (0, 0xFFFF):
            return pack("REFUSED", "zip_disk_count")
        pos += 46
        if pos + name_len + extra_len + comment_len > end:
            return pack("REFUSED", "truncated_zip_header")
        name_raw = data[pos : pos + name_len]
        extra = data[pos + name_len : pos + name_len + extra_len]
        pos += name_len + extra_len + comment_len
        if flags & _ENCRYPT_FLAG:
            return pack("REFUSED", "zip_encryption_unsupported")
        if flags & ~(_UTF8_FLAG | _DESCRIPTOR_FLAG):
            return pack("REFUSED", "zip_flag_unsupported", detail={"flags": flags})
        sentinel_zip64 = (
            usize == 0xFFFFFFFF or csize == 0xFFFFFFFF or local_off == 0xFFFFFFFF or central_disk == 0xFFFF
        )
        usize, csize, local_off, disk_no, zip64_cause = _read_sizes(csize, usize, local_off, extra, central_disk)
        if zip64_cause is not None:
            return pack("REFUSED", zip64_cause)
        if disk_no != 0:
            return pack("REFUSED", "zip_disk_count")
        if usize > ceilings["max_expanded_bytes"] or csize > ceilings["max_archive_bytes"]:
            return pack("REFUSED", "resource_ceiling_exceeded", detail={"limit": "declared_member_size"})
        if expanded + usize > ceilings["max_expanded_bytes"]:
            return pack("REFUSED", "resource_ceiling_exceeded", detail={"limit": "max_expanded_bytes"})
        try:
            name = _decode_name(name_raw, flags)
        except UnicodeError as raw_origin:
            from tools.native_support import retain_source_origin
            retain_source_origin(None,raw_origin)
            return pack("REFUSED", "member_name_undecodable")
        normalized, path_cause = normalize_member_path(
            name, ceilings["max_member_name_bytes"], ceilings["max_depth"]
        )
        if path_cause is not None:
            return pack("REFUSED", path_cause, detail={"member": name})
        if local_off + 30 > len(data):
            return pack("REFUSED", "truncated_zip_header")
        if _u32(data, local_off) != _LOCAL:
            return pack("REFUSED", "zip_central_local_mismatch")
        local_version = _u16(data, local_off + 4)
        local_flags = _u16(data, local_off + 6)
        local_method = _u16(data, local_off + 8)
        local_crc = _u32(data, local_off + 14)
        local_csize = _u32(data, local_off + 18)
        local_usize = _u32(data, local_off + 22)
        local_name_len = _u16(data, local_off + 26)
        local_extra_len = _u16(data, local_off + 28)
        allocation = reserve(resources,meter,work_bytes=(local_name_len+local_extra_len)*8,
                             live_bytes=(local_name_len+local_extra_len)*16+65536)
        if allocation:
            return pack("REFUSED",allocation,stage="ARCHIVE_AND_MEMBER_CLOSURE")
        local_name = data[local_off + 30 : local_off + 30 + local_name_len]
        local_extra = data[local_off + 30 + local_name_len : local_off + 30 + local_name_len + local_extra_len]
        local_fields, local_extra_cause = _walk_extra(local_extra)
        if local_extra_cause is not None:
            return pack("REFUSED", local_extra_cause)
        if local_fields is None:
            return pack("REFUSED", "truncated_zip_header")
        local_desc_zip64 = local_csize == 0xFFFFFFFF or local_usize == 0xFFFFFFFF
        if local_name != name_raw or local_method != method or local_flags != flags or local_version != version_needed:
            return pack("REFUSED", "zip_central_local_mismatch")
        if version_needed not in (10, 20, 45, 63):
            return pack("REFUSED", "zip_flag_unsupported")
        if method == 8 and version_needed < 20:
            return pack("REFUSED", "zip_flag_unsupported")
        if (sentinel_zip64 or local_desc_zip64) and version_needed < 45:
            return pack("REFUSED", "zip_flag_unsupported")
        if method == 0 and version_needed < 10:
            return pack("REFUSED", "zip_flag_unsupported")
        local_usize, local_csize, _ignored, _local_disk, local_zip64 = _read_sizes(
            local_csize, local_usize, 0, local_extra, 0
        )
        if local_zip64 is not None:
            return pack("REFUSED", local_zip64)
        if not (flags & _DESCRIPTOR_FLAG):
            if local_crc != crc or local_csize != csize or local_usize != usize:
                return pack("REFUSED", "zip_central_local_mismatch")
        elif local_crc not in (0,crc) or local_csize not in (0,csize) or local_usize not in (0,usize):
            return pack("REFUSED","zip_central_local_mismatch")
        data_off = local_off + 30 + local_name_len + local_extra_len
        if data_off + csize > len(data):
            return pack("REFUSED", "truncated_zip_header")
        if data_off + csize > cd_offset:
            return pack("REFUSED", "zip_member_overlaps_central")
        streaming=performing_requested(resources)
        tick = reserve(resources, meter, members=1, work_bytes=csize, live_bytes=4096 if streaming else csize + usize)
        if tick is not None:
            return pack("REFUSED", tick)
        from .custody import HeldRange
        compressed = data.part(data_off,data_off+csize) if isinstance(data,HeldRange) else data[data_off:data_off+csize]
        range_end = data_off + csize
        descriptor_raw = b""
        if flags & _DESCRIPTOR_FLAG:
            desc = data_off + csize
            width = 20 if central_desc_zip64 or local_desc_zip64 else 12

            def _descriptor_end(start, signed):
                cursor = start + 4 if signed else start
                if signed and (start + 4 > len(data) or _u32(data, start) != 0x08074B50):
                    return None
                if cursor + width > cd_offset or cursor + width > len(data):
                    return None
                got_crc = _u32(data, cursor)
                if width == 12:
                    got_c = _u32(data, cursor + 4)
                    got_u = _u32(data, cursor + 8)
                else:
                    got_c = _u64(data, cursor + 4)
                    got_u = _u64(data, cursor + 12)
                if got_crc != crc or got_c != csize or got_u != usize:
                    return None
                return cursor + width

            unsigned_end = _descriptor_end(desc, False)
            signed_end = _descriptor_end(desc, True)
            if unsigned_end is not None and signed_end is not None:
                return pack("REFUSED", "zip_descriptor_mismatch", detail={"ambiguous": True})
            range_end = unsigned_end if unsigned_end is not None else signed_end
            if range_end is None:
                return pack("REFUSED", "zip_descriptor_mismatch")
            descriptor_raw = data[desc:range_end]
        ranges.append((local_off, range_end))
        stream_facts=None
        if streaming and method in (0,8):
            from .compression import stream_reader,BudgetStop
            if method==0 and csize!=usize:
                return pack("REFUSED","zip_stored_size_mismatch",detail={"member":normalized})
            keep=normalized.endswith((".dist-info/METADATA",".dist-info/WHEEL",".dist-info/RECORD")) or ((external>>16)&0o170000)==0o120000
            reader=stream_reader(None if method==0 else "deflate",compressed,capabilities,usize,resources,meter)
            try:
                raw,stream_facts=reader.observe(usize,keep,zlib=meter["codec_modules"]["deflate"]["module"])
                if reader._fill():
                    return pack("REFUSED","zip_inflated_size_mismatch",detail={"member":normalized})
            except BudgetStop as stop:
                from tools.native_support import retain_source_origin
                retain_source_origin(None,stop)
                from .public import exception_owned_result
                return exception_owned_result(meter,stop)
            finally:
                reader.close()
        elif method == 0:
            if csize != usize:
                return pack("REFUSED", "zip_stored_size_mismatch", detail={"member": normalized})
            tick = reserve(resources, meter, expanded_bytes=usize)
            if tick is not None:
                return pack("REFUSED", tick)
            raw = compressed
            if len(raw) > ceilings["max_expanded_bytes"]:
                return pack("REFUSED", "resource_ceiling_exceeded")
        elif method == 8:
            raw, inflate_cause = decode_admitted(
                "deflate", compressed, capabilities, usize, resources=resources, meter=meter
            )
            if inflate_cause is not None:
                if inflate_cause == "codec_memory_policy_absent":
                    inflate_status = "REFUSED"
                elif inflate_cause.endswith("absent") or inflate_cause.endswith("unsupported") or inflate_cause.endswith("refused") or inflate_cause.endswith("module_absent"):
                    inflate_status = "NOT_COVERED"
                else:
                    inflate_status = "REFUSED"
                return pack(inflate_status, inflate_cause, detail={"member": normalized})
            if len(raw) != usize:
                return pack("REFUSED", "zip_inflated_size_mismatch", detail={"member": normalized})
        else:
            return pack("NOT_COVERED", "zip_method_unsupported", detail={"method": method, "member": normalized})
        computed, crc_cause = (stream_facts["crc32"],None) if stream_facts is not None else crc32(raw, resources, meter)
        if crc_cause is not None:
            status = "NOT_COVERED" if crc_cause == "runtime_zlib_module_absent" else "REFUSED"
            return pack(status, crc_cause, detail={"member": normalized})
        if computed != crc:
            return pack("REFUSED", "zip_crc_mismatch", detail={"member": normalized})
        mode = None
        unix_mode = (external >> 16) & 0xFFFF
        creator_host = made_by >> 8
        if creator_host not in (0,3):
            return pack("NOT_COVERED","zip_creator_unsupported",stage="ARCHIVE_AND_MEMBER_CLOSURE")
        if not version_needed <= (made_by & 255) <= 63:
            return pack("NOT_COVERED","zip_creator_unsupported",stage="ARCHIVE_AND_MEMBER_CLOSURE")
        member_type = "regular"
        if creator_host == 3 and unix_mode != 0:
            mode = unix_mode
            kind = unix_mode & 0o170000
            if name.endswith("/") and kind not in (0,0o040000):
                return pack("REFUSED","zip_member_type_unsupported",detail={"member":normalized})
            if kind == 0o120000:
                member_type = "symlink"
            elif kind == 0o040000 or name.endswith("/"):
                member_type = "directory"
            elif kind in (0o100000, 0):
                member_type = "regular"
            else:
                return pack("REFUSED", "zip_member_type_unsupported", detail={"member": normalized, "mode": unix_mode})
        elif name.endswith("/") or (creator_host == 0 and external & 0x10):
            member_type = "directory"
        raw_size=stream_facts["size"] if stream_facts is not None else len(raw)
        if member_type == "directory" and raw_size != 0:
            return pack(
                "REFUSED",
                "zip_member_type_unsupported",
                detail={"member": normalized, "directory_bytes": raw_size},
            )
        link_target = None
        link_sha = None
        file_sha = None
        if member_type == "symlink":
            member_type = "symlink"
            try:
                link_target = raw.decode("utf-8")
            except UnicodeError as raw_origin:
                from tools.native_support import retain_source_origin
                retain_source_origin(None,raw_origin)
                return pack("REFUSED", "symlink_target_undecodable", detail={"member": normalized})
            if symlink_escapes(normalized, link_target):
                recipe = resources.get("install_recipe") if isinstance(resources, dict) else None
                if not performing_requested(resources) or not isinstance(recipe, dict) or recipe.get("absolute_root") != "admitted-final-root":
                    return pack("REFUSED", "symlink_escape", detail={"member": normalized})
            link_sha = stream_facts["sha256"] if stream_facts is not None else content_sha256(raw, resources, meter)
        else:
            file_sha = stream_facts["sha256"] if stream_facts is not None else content_sha256(raw, resources, meter)
        expanded += raw_size
        members.append(
            {
                "source_format": "zip",
                "name": name,
                "normalized_path": normalized,
                "type": member_type,
                "flags": flags,
                "compression_method": "stored" if method == 0 else "deflate",
                "crc32": crc,
                "size": raw_size if member_type == "regular" else 0,
                "content_sha256": file_sha,
                "link_target": link_target,
                "link_target_sha256": link_sha,
                "mode": mode,
                "uid": None,
                "gid": None,
                "executable": bool(mode is not None and (mode & 0o111) and member_type == "regular"),
                "package_script": False,
                "executed": False,
                "zip_envelope": {"local_version": local_version, "central_version": version_needed,
                    "flags": flags, "method": method, "creator_host": creator_host,
                    "local_extra_hex": local_extra.hex(), "central_extra_hex": extra.hex(),
                    "local_offset": local_off, "data_offset": data_off, "range_end": range_end,
                    "compressed_size": csize, "uncompressed_size": usize,
                    "local_header_hex":data[local_off:local_off+30].hex(),
                    "central_header_hex":central_header.hex(),"name_hex":name_raw.hex(),
                    "central_header_offset":central_header_offset,"descriptor_hex":descriptor_raw.hex(),
                    "external_attributes":external,"extra_policy":"framed-owner-time-ancillary-recorded-not-applied-v1"},
                "payload": raw if member_type == "regular" else b"",
                "_stream_record_digests":stream_facts["record_digests"] if stream_facts is not None else None,
            }
        )
    if pos != end:
        return pack("REFUSED", "zip_central_range")
    ranges.sort()
    if not ranges:
        if cd_offset != 0:
            return pack("REFUSED", "zip_central_range")
    else:
        if ranges[0][0] != 0 or ranges[-1][1] != cd_offset:
            return pack("REFUSED", "zip_central_range")
        for previous, current in zip(ranges, ranges[1:]):
            if current[0] != previous[1]:
                return pack("REFUSED", "zip_member_range_overlap")
    paths = [item["normalized_path"] for item in members]
    dup = duplicate_path(paths)
    if dup is not None:
        return pack("REFUSED", "duplicate_member", detail={"member": dup})
    folded = casefold_collision(paths)
    if folded is not None:
        return pack("REFUSED", "casefold_collision", detail={"member": folded})
    return pack("OBSERVED", None, observation={"members": members, "expanded_bytes": expanded,
        "container_envelope":{"eocd_hex":data[eocd:].hex(),"eocd_offset":eocd,
            "zip64":zip64,"zip64_eocd_hex":data[z64_off:z64_off+56].hex() if zip64 else None,
            "zip64_locator_hex":data[eocd-20:eocd].hex() if zip64 else None,
            "central_offset":cd_offset,"central_size":cd_size,"member_count":total}})


_SINGLETONS = {
    "metadata-version",
    "name",
    "version",
    "requires-python",
    "wheel-version",
    "root-is-purelib",
    "generator",
}


def _header_map(text):
    """Headers end at the first blank line. The remainder is the body.

    Singleton identity is casefolded. Requires-Dist and Tag stay repeatable.
    """
    headers = {}
    fold_owner = {}
    body_lines = []
    current = None
    in_body = False
    for line in text.splitlines():
        if in_body:
            body_lines.append(line)
            continue
        if line == "":
            in_body = True
            current = None
            continue
        if line.startswith(" ") or line.startswith("\t"):
            if current is None:
                return None
            headers[current][-1] = headers[current][-1] + " " + line.strip()
            continue
        if ":" not in line:
            return None
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        folded = key.casefold()
        if folded in _SINGLETONS and folded in fold_owner:
            return None
        owner = fold_owner.get(folded)
        if owner is None:
            fold_owner[folded] = key
            headers[key] = [value]
            current = key
        else:
            headers[owner].append(value)
            current = owner
    headers["__body__"] = "\n".join(body_lines)
    return headers


def _folded_items(headers, key):
    folded = key.casefold()
    for name, items in headers.items():
        if isinstance(name, str) and name.casefold() == folded and isinstance(items, list):
            return name, items
    return None, None


def _one(headers, key):
    _name, items = _folded_items(headers, key)
    if items:
        return items[-1]
    return None


def _present(headers, key):
    name, items = _folded_items(headers, key)
    return name is not None and bool(items)


def _values(headers, key):
    _name, items = _folded_items(headers, key)
    return list(items) if items else []


def _ascii_size(text, limit):
    if not isinstance(text, str):
        return None, "record_row_shape"
    if text == "0":
        return 0, None
    if text == "" or len(text) > 20 or text[0] == "0" or any(char not in "0123456789" for char in text):
        return None, "record_row_shape"
    try:
        value = int(text)
    except (ValueError, OverflowError) as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(None,raw_origin)
        return None, "record_row_shape"
    except MemoryError as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(None,raw_origin)
        return None, "resource_ceiling_exceeded"
    if value < 0 or (isinstance(limit, int) and not isinstance(limit, bool) and value > limit):
        return None, "resource_ceiling_exceeded"
    return value, None


def _strict_csv_rows(text, resources=None, meter=None):
    if not isinstance(text, str) or "\x00" in text:
        return None, "record_row_shape"
    tick = reserve(resources, meter, work_bytes=len(text), live_bytes=len(text) * 16)
    if tick:
        return None, tick
    rows = []
    fields = []
    buf = []
    quoted = False
    closed = False
    ending = None
    index = 0
    try:
        while index < len(text):
            if index % 1024 == 0:
                tick = charge(resources, meter, work_bytes=min(1024, len(text) - index))
                if tick:
                    return None, tick
            char = text[index]
            if quoted:
                if char == '"':
                    if index + 1 < len(text) and text[index + 1] == '"':
                        buf.append('"')
                        index += 2
                        continue
                    quoted = False
                    closed = True
                else:
                    buf.append(char)
            elif char == '"':
                if buf or closed:
                    return None, "record_row_shape"
                quoted = True
            elif char == ",":
                fields.append("".join(buf))
                buf = []
                closed = False
            elif char in ("\r", "\n"):
                current_ending = "crlf" if char == "\r" else "lf"
                if char == "\r":
                    if index + 1 == len(text) or text[index + 1] != "\n":
                        return None, "record_row_shape"
                    index += 1
                if ending not in (None, current_ending):
                    return None, "record_row_shape"
                ending = current_ending
                fields.append("".join(buf))
                if fields != [""]:
                    rows.append(fields)
                fields = []
                buf = []
                closed = False
            else:
                if closed:
                    return None, "record_row_shape"
                buf.append(char)
            index += 1
        if quoted:
            return None, "record_row_shape"
        if fields or buf or closed:
            fields.append("".join(buf))
            rows.append(fields)
    except MemoryError as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,raw_origin)
        return None, "resource_ceiling_exceeded"
    return rows, None


def _parse_record(text, limit, resources=None, meter=None):
    rows = []
    parsed, cause = _strict_csv_rows(text, resources, meter)
    if cause is not None:
        return None, cause
    for row in parsed:
        if not row or row == [""]:
            continue
        if len(row) != 3:
            return None, "record_row_shape"
        path = row[0]
        digest = row[1]
        if digest == "":
            if row[2] != "":
                return None, "record_row_shape"
            size = None
        else:
            size, size_cause = _ascii_size(row[2], limit)
            if size_cause is not None:
                return None, size_cause
        rows.append({"path": path, "digest": digest, "size": size})
    return rows, None


def _wheel_observation(members, expected, filename, ceilings, resources=None, meter=None):
    from .causes import enter_phase
    phase = enter_phase(meter,"RAW_METADATA_CORRESPONDENCE","wheel_metadata_RECORD",expected["relative_path"])
    if phase:
        return pack("REFUSED",phase,stage="RAW_METADATA_CORRESPONDENCE")
    meta = [item for item in members if item["normalized_path"].endswith(".dist-info/METADATA")]
    wheel = [item for item in members if item["normalized_path"].endswith(".dist-info/WHEEL")]
    record = [item for item in members if item["normalized_path"].endswith(".dist-info/RECORD")]
    if len(meta) != 1 or len(wheel) != 1 or len(record) != 1:
        return pack("REFUSED", "wheel_metadata_member_missing", detail={"metadata": len(meta), "wheel": len(wheel), "record": len(record)})
    meta_item, wheel_item, record_item = meta[0], wheel[0], record[0]
    for item in (meta_item, wheel_item, record_item):
        if item["size"] > HARD_MAX_METADATA_BYTES:
            return pack("REFUSED", "metadata_member_above_cap", detail={"member": item["normalized_path"]})
        if item["type"] != "regular":
            return pack("REFUSED", "metadata_member_not_regular", detail={"member": item["normalized_path"]})
    try:
        text_size = meta_item["size"] + wheel_item["size"] + record_item["size"]
        allocation = reserve(resources, meter, live_bytes=text_size * 64 + len(members) * 512,
                             work_bytes=text_size * 8 + len(members) * 512)
        if allocation:
            return pack("REFUSED", allocation, stage="RAW_METADATA_CORRESPONDENCE")
        meta_text = meta_item["payload"].decode("utf-8")
        wheel_text = wheel_item["payload"].decode("utf-8")
        record_text = record_item["payload"].decode("utf-8")
    except UnicodeError as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,raw_origin)
        return pack("REFUSED", "metadata_not_utf8")
    headers = _header_map(meta_text)
    wheel_headers = _header_map(wheel_text)
    if headers is None or wheel_headers is None:
        return pack("REFUSED", "metadata_header_malformed")
    from .pins import SUPPORTED_METADATA_VERSIONS

    meta_version = _one(headers, "Metadata-Version")
    if meta_version is None:
        return pack("REFUSED", "metadata_version_absent")
    if meta_version not in SUPPORTED_METADATA_VERSIONS:
        return pack("REFUSED", "metadata_version_unsupported")
    distinfo = meta_item["normalized_path"].rsplit("/", 1)[0]
    expected_distinfo = f"{filename['distribution']}-{filename['version']}.dist-info"
    wheel_dist = wheel_item["normalized_path"].rsplit("/", 1)[0]
    record_dist = record_item["normalized_path"].rsplit("/", 1)[0]
    if distinfo != expected_distinfo or wheel_dist != distinfo or record_dist != distinfo:
        return pack("REFUSED", "dist_info_name_mismatch", detail={"found": distinfo, "expected": expected_distinfo})
    if _one(wheel_headers, "Wheel-Version") != "1.0":
        return pack("REFUSED", "wheel_version_unsupported")
    if _one(wheel_headers, "Root-Is-Purelib") not in ("true", "false"):
        return pack("REFUSED", "wheel_purelib_invalid")
    content_pin = expected.get("metadata_content_sha256")
    if isinstance(content_pin, str) and content_pin != meta_item["content_sha256"]:
        return pack("REFUSED", "metadata_raw_pin_mismatch")
    publisher_pin = expected.get("metadata_raw_sha256")
    if publisher_pin is not None and not isinstance(publisher_pin, str):
        return pack("REFUSED", "expected_shape_invalid")
    name = _one(headers, "Name")
    version = _one(headers, "Version")
    if name is None or version is None:
        return pack("REFUSED", "metadata_name_version_missing")
    if pep503_name(name) != pep503_name(filename["distribution"]) or pep503_name(name) != pep503_name(expected["name"]):
        return pack("REFUSED", "metadata_filename_name_mismatch", detail={"metadata": name, "filename": filename["distribution"]})
    if version != filename["version"] or version != expected["version"]:
        return pack("REFUSED", "metadata_filename_version_mismatch", detail={"metadata": version, "filename": filename["version"]})
    if _present(headers, "Requires-Python"):
        metadata_raw = _one(headers, "Requires-Python")
    else:
        metadata_raw = None
    requires = requires_python_correspondence(expected.get("requires_python"), metadata_raw)
    tag_values = _values(wheel_headers, "Tag")
    expanded = set(filename["expanded_tags"])
    if set(tag_values) != expanded or not tag_values:
        return pack("REFUSED", "wheel_tag_filename_mismatch", detail={"wheel": tag_values, "filename": filename["expanded_tags"]})
    rows, record_cause = _parse_record(record_text, ceilings.get("max_expanded_bytes"), resources, meter)
    if record_cause is not None:
        return pack("REFUSED", record_cause)
    by_path = {item["normalized_path"]: item for item in members}
    seen = set()
    for row in rows:
        tick = reserve(resources, meter, work_bytes=len(row["path"]) + 256, live_bytes=len(row["path"]) * 8 + 256)
        if tick:
            return pack("REFUSED", tick, stage="RAW_METADATA_CORRESPONDENCE")
        normalized, path_cause = normalize_member_path(
            row["path"], ceilings["max_member_name_bytes"], ceilings["max_depth"]
        )
        if path_cause is not None:
            return pack("REFUSED", path_cause, detail={"record": row["path"]})
        row["normalized_path"] = normalized
        if normalized in seen:
            return pack("REFUSED", "duplicate_member", detail={"record": normalized})
        seen.add(normalized)
        if normalized == record_item["normalized_path"] and (row["digest"] != "" or row["size"] is not None):
            return pack("REFUSED", "record_row_shape", detail={"member": normalized})
        digest = row["digest"]
        if digest == "":
            signature = normalized in (f"{distinfo}/RECORD.jws", f"{distinfo}/RECORD.p7s")
            record_self = normalized == record_item["normalized_path"]
            if not (signature or record_self):
                return pack("REFUSED", "record_hash_missing", detail={"member": normalized})
            if by_path.get(normalized) is None:
                return pack("REFUSED", "record_path_missing_member", detail={"member": normalized})
            member = by_path[normalized]
            row["binding"] = {"role":"self" if record_self else "signature","member_path":normalized,
                "member_type":member["type"],"member_sha256":member["content_sha256"],
                "content_size":member["size"],"algorithm":None,"computed_digest":None}
            continue
        if "=" not in digest:
            return pack("REFUSED", "unsupported_record_digest", detail={"member": normalized})
        algorithm, encoded = digest.split("=", 1)
        member = by_path.get(normalized)
        if member is None:
            return pack("REFUSED", "record_path_missing_member", detail={"member": normalized})
        if member["type"] == "symlink":
            target = member.get("link_target")
            if not isinstance(target, str):
                return pack("REFUSED", "record_non_regular_member", detail={"member": normalized})
            target_bytes = target.encode("utf-8")
            expected_digest = record_digest(algorithm, target_bytes, resources, meter)
            compared_size = len(target_bytes)
        elif member["type"] != "regular":
            return pack("REFUSED", "record_non_regular_member", detail={"member": normalized})
        else:
            retained=member.get("_stream_record_digests")
            expected_digest = retained.get(algorithm) if isinstance(retained,dict) else record_digest(algorithm, member["payload"], resources, meter)
            compared_size = member["size"]
        if expected_digest is None:
            return pack("REFUSED", "unsupported_record_digest", detail={"algorithm": algorithm, "member": normalized})
        if expected_digest != encoded:
            return pack("REFUSED", "record_digest_mismatch", detail={"member": normalized})
        if row["size"] != compared_size:
            return pack("REFUSED", "record_size_mismatch", detail={"member": normalized})
        row["binding"] = {"role":"content","member_path":normalized,"member_type":member["type"],
            "member_sha256":member["link_target_sha256"] if member["type"] == "symlink" else member["content_sha256"],
            "content_size":compared_size,"algorithm":algorithm,"computed_digest":expected_digest}
    if record_item["normalized_path"] not in seen:
        return pack("REFUSED", "record_coverage_gap", detail={"member": record_item["normalized_path"]})
    for item in members:
        if item["type"] == "directory":
            continue
        if item["normalized_path"] in (f"{distinfo}/RECORD.jws", f"{distinfo}/RECORD.p7s"):
            continue
        if item["normalized_path"] not in seen:
            return pack("REFUSED", "record_coverage_gap", detail={"member": item["normalized_path"]})
    for signature_name in ("RECORD.jws", "RECORD.p7s"):
        signature = by_path.get(f"{distinfo}/{signature_name}")
        if signature is not None and signature.get("type") != "regular":
            return pack("REFUSED", "record_non_regular_member", detail={"member": signature["normalized_path"]})
    requires_dist = _values(headers, "Requires-Dist")
    public_members = []
    for item in members:
        copied = dict(item)
        copied.pop("payload", None)
        copied.pop("_stream_record_digests",None)
        copied["source_archive_sha256"] = expected["sha256"]
        copied["source_domain"] = "wheel"
        public_members.append(copied)
    observation = {
        "archive_class": "wheel",
        "filename": expected["filename"],
        "relative_path": expected["relative_path"],
        "whole_sha256": expected["sha256"],
        "whole_size": expected["size"],
        "members": public_members,
        "member_count": len(public_members),
        "metadata": {
            "name": name,
            "version": version,
            "metadata_version": meta_version,
            "requires_dist": requires_dist,
            "raw_sha256": meta_item["content_sha256"],
            "header_provenance": {
                "requires_python": next(
                    (name for name in headers if isinstance(name, str) and name.casefold() == "requires-python"),
                    None,
                ),
            },
        },
        "wheel": {
            "root_is_purelib": _one(wheel_headers, "Root-Is-Purelib"),
            "tags": tag_values,
            "generator": _one(wheel_headers, "Generator"),
            "raw_sha256": wheel_item["content_sha256"],
        },
        "requires_python": requires,
        "compatibility": {
            "requires_python_raw_bill": requires["bill_raw"],
            "requires_python_raw_metadata": requires["metadata_raw"],
            "requires_python_explicit_normalization_bill": requires["bill_normalized"],
            "requires_python_explicit_normalization_metadata": requires["metadata_normalized"],
            "empty_string_to_null_applied": requires["empty_string_to_null_applied"],
            "literal_difference": requires["literal_difference"],
            "requires_dist": requires_dist,
            "python_tags": filename["python_tag"].split("."),
            "abi_tags": filename["abi_tag"].split("."),
            "platform_tags": filename["platform_tag"].split("."),
            "expanded_tags": filename["expanded_tags"],
            "filename_tag_inference_is_not_compatibility": True,
            "environment_marker_evaluation": "NOT_RUN",
            "compatibility_approved": False,
        },
        "record_coverage": "COMPLETE",
        "record_rows":rows,
        "record_raw_utf8":record_text,
        "record_raw_sha256":record_item["content_sha256"],
        "metadata_binding":{"publisher_raw_sha256":publisher_pin,
            "publisher_record_ordinal":((meter if isinstance(meter,dict) else {}).get("content_companion",{}).get(expected["relative_path"],{})).get("publisher_record_ordinal"),
            "selected_metadata_content_sha256":content_pin,"observed_metadata_content_sha256":meta_item["content_sha256"],
            "selected_archive_sha256":expected["sha256"],"record_raw_sha256":record_item["content_sha256"],
            "installed_join":"RETURNED_SOURCE_JOINS"},
        "publisher_metadata_raw_sha256": publisher_pin if isinstance(publisher_pin, str) else None,
        "publisher_proof": False,
        "install_performed": False,
        "filename_fields": {
            "build_tag": filename.get("build_tag"),
            "field_count": 5 if filename.get("build_tag") is None else 6,
        },
    }
    return pack("OBSERVED", None, observation=observation, stage="RAW_METADATA_CORRESPONDENCE")


_WHEEL_EXPECTED = (
    "archive_class",
    "name",
    "version",
    "filename",
    "sha256",
    "size",
    "requires_python",
    "python_tag",
    "abi_tag",
    "platform_tag",
    "relative_path",
)


def _hex64(value):
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _expected_cause(expected):
    if not isinstance(expected, dict):
        return "expected_shape_invalid"
    for key in _WHEEL_EXPECTED:
        if key not in expected:
            return "expected_shape_invalid"
    if expected.get("archive_class") != "wheel":
        return "expected_shape_invalid"
    raw = expected.get("requires_python")
    if raw is not None and not isinstance(raw, str):
        return "expected_shape_invalid"
    if not _hex64(expected.get("sha256")):
        return "expected_shape_invalid"
    size = expected.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return "expected_shape_invalid"
    for key in ("name", "version", "filename", "python_tag", "abi_tag", "platform_tag", "relative_path"):
        if not isinstance(expected.get(key), str) or expected.get(key) == "":
            return "expected_shape_invalid"
    return None


def _publisher_cause(evidence, resources):
    if evidence is None:
        return None, {"status": "ABSENT", "verified": False, "self_approved": False}
    if not isinstance(evidence, dict):
        return "publisher_evidence_shape_invalid", None
    role = evidence.get("role")
    if role in ("caller_self", "scanner_self") or evidence.get("subject") == "caller_module":
        return "self_approved_caller", None
    caller = resources.get("caller_module_sha256") if isinstance(resources, dict) else None
    if caller and evidence.get("sha256") == caller:
        return "self_approved_caller", None
    if role == "prior_execution" or evidence.get("transfers_prior_execution") is True:
        return "transferred_execution_rejected", None
    return None, {"status": "RECORDED_UNVERIFIED", "verified": False, "self_approved": False, "role": role}


def _held_present(held):
    if not isinstance(held, dict) or held.get("present") is not True:
        return "held_bytes_absent"
    return None


def _decode_chunk(chunk):
    if isinstance(chunk, str):
        try:
            return base64.b64decode(chunk, validate=True), None
        except (ValueError, binascii.Error) as raw_origin:
            from tools.native_support import retain_source_origin
            retain_source_origin(None,raw_origin)
            return None, "held_bytes_absent"
    if isinstance(chunk, (bytes, bytearray)):
        return bytes(chunk), None
    return None, "held_bytes_absent"


def _held_bytes(held, resources=None, meter=None):
    if _held_present(held) is not None:
        return None, "held_bytes_absent"
    grant = resources.get("grant") if isinstance(resources, dict) else None
    filesystem_read = isinstance(grant, dict) and grant.get("filesystem_read") is True
    encoded = held.get("chunks_b64") if isinstance(held, dict) else None
    chunks = held.get("chunks") if isinstance(held, dict) else None
    if performing_requested(resources) and (encoded or chunks):
        return None, "held_bytes_rejected"
    if not encoded and not chunks and filesystem_read:
        from .custody import read_nofollow_file

        limit = held.get("declared_size")
        return read_nofollow_file(held.get("relative_path"), limit, resources, meter)
    selected = encoded if isinstance(encoded, list) else chunks
    if not isinstance(selected, list) or not selected:
        return None, "held_bytes_absent"
    if filesystem_read and (encoded or chunks):
        return None, "held_bytes_rejected"
    size = held.get("declared_size")
    if not isinstance(size,int) or isinstance(size,bool) or size < 0 or size > resources["ceilings"]["max_archive_bytes"]:
        return None, "resource_ceiling_exceeded"
    tick = reserve(resources,meter,live_bytes=size * 2 + 65536,work_bytes=size)
    if tick:
        return None,tick
    out = bytearray(size)
    offset = 0
    for chunk in selected:
        bound = (len(chunk) // 4 + 1) * 3 if isinstance(chunk,str) else len(chunk) if isinstance(chunk,(bytes,bytearray)) else -1
        if bound < 0 or offset + max(0,bound - 3) > size:
            return None,"held_bytes_absent"
        tick = reserve(resources,meter,read_bytes=bound,work_bytes=bound * 2,live_bytes=bound)
        if tick:
            return None,tick
        piece, cause = _decode_chunk(chunk)
        if cause is not None:
            return None, cause
        if offset + len(piece) > size:
            return None,"expected_size_mismatch"
        out[offset:offset + len(piece)] = piece
        offset += len(piece)
    if offset != size:
        return None,"expected_size_mismatch"
    return out, None


def _require_custody(held):
    from .custody import compare_custody, plan_nofollow_read

    if not isinstance(held, dict):
        return pack("REFUSED", "custody_record_invalid")
    plan = plan_nofollow_read(held.get("relative_path") or "")
    if plan.get("nofollow") is not True or plan.get("execute") is not False:
        return pack("REFUSED", "custody_follow_refused")
    opened = held.get("opened")
    if not isinstance(opened, list) or not opened or opened[0].get("nofollow") is not True:
        return pack("REFUSED", "custody_follow_refused")
    opened_path = opened[0].get("relative_path")
    if isinstance(opened_path, str) and opened_path != held.get("relative_path"):
        return pack("REFUSED", "custody_record_invalid")
    before = held.get("before")
    if isinstance(before, list) and before and isinstance(before[0], dict):
        if before[0].get("relative_path") not in (None, held.get("relative_path")):
            return pack("REFUSED", "custody_record_invalid")
        for field in ("device", "inode", "size", "mtime_ns", "ctime_ns", "uid", "gid", "full_mode", "nlink", "sha256"):
            if opened[0].get(field) != before[0].get(field):
                return pack("REFUSED", "custody_identity_changed")
    compared = compare_custody(held.get("before"), held.get("after"))
    if compared["status"] != "OBSERVED":
        return compared
    compared["observation"]["read_plan"] = plan["op"]
    compared["observation"]["filesystem_open_performed"] = False
    return compared


def _adopt_reader_custody(held, meter):
    if not isinstance(held, dict) or not isinstance(meter, dict):
        return
    learned = {
        "before": meter.get("custody_before"),
        "opened": meter.get("custody_opened"),
        "after": meter.get("custody_after"),
    }
    for key, fresh in learned.items():
        if not isinstance(fresh, dict):
            continue
        existing = held.get(key)
        if isinstance(existing, list) and existing:
            caller = existing[0] if isinstance(existing[0], dict) else None
            if isinstance(caller, dict):
                for field in ("device", "inode", "size", "mtime_ns", "ctime_ns", "uid", "gid", "full_mode", "nlink", "sha256"):
                    if field in caller and caller.get(field) != fresh.get(field):
                        meter["post_scan_cause"] = "custody_identity_changed"
                        return
            continue
        held[key] = [fresh]


def _open_held(held, resources, meter):
    grant = resources.get("grant") if isinstance(resources, dict) else None
    filesystem_read = isinstance(grant, dict) and grant.get("filesystem_read") is True
    if filesystem_read:
        data, held_cause = _held_bytes(held, resources, meter)
        if held_cause is not None:
            status = "REFUSED" if held_cause in ("resource_time_exceeded", "resource_clock_invalid", "resource_ceiling_exceeded", "custody_follow_refused", "held_bytes_rejected") else "NOT_PROVEN"
            return None, pack(status, held_cause)
        _adopt_reader_custody(held, meter)
        custody_result = _require_custody(held)
        if custody_result["status"] != "OBSERVED":
            return None, custody_result
        return data, None
    custody_result = _require_custody(held)
    if custody_result["status"] != "OBSERVED":
        return None, custody_result
    data, held_cause = _held_bytes(held, resources, meter)
    if held_cause is not None:
        status = "REFUSED" if held_cause in ("resource_time_exceeded", "resource_clock_invalid", "resource_ceiling_exceeded", "custody_follow_refused", "held_bytes_rejected") else "NOT_PROVEN"
        return None, pack(status, held_cause)
    return data, None


def _scan_wheel(expected, held, resources, publisher_evidence=None, meter=None):
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
    held_cause = _held_present(held)
    if held_cause is not None:
        return pack("NOT_PROVEN", held_cause)
    admitted = admission_cause(resources)
    if admitted is not None:
        return pack("NOT_PROVEN", admitted)
    from .schema_validate import MEMBER_SCHEMA, RESOURCE_SCHEMA, WHEEL_SCHEMA, validate_value

    schema_cause = validate_value(RESOURCE_SCHEMA, resources, resources, meter)
    if schema_cause is not None:
        return pack("REFUSED", schema_cause)
    timed = clock_cause(resources)
    if timed is not None:
        status = "REFUSED" if timed in ("resource_time_exceeded", "resource_clock_invalid") else "NOT_PROVEN"
        return pack(status, timed)
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
        filename, filename_cause = parse_wheel_filename(expected["filename"])
        if filename_cause is not None:
            return pack("REFUSED", filename_cause)
        if (
            filename["python_tag"] != expected["python_tag"]
            or filename["abi_tag"] != expected["abi_tag"]
            or filename["platform_tag"] != expected["platform_tag"]
            or pep503_name(filename["distribution"]) != pep503_name(expected["name"])
            or filename["version"] != expected["version"]
        ):
            return pack("REFUSED", "expected_filename_tag_inconsistent")
        capabilities = resources.get("compression_capabilities") or []
        parsed = parse_zip_members(data, resources["ceilings"], capabilities, resources, meter)
        if parsed["status"] != "OBSERVED":
            return parsed
        from .custody import reserve_public_members
        allocation = reserve_public_members(resources, meter, parsed["observation"]["members"])
        if allocation:
            return pack("REFUSED", allocation)
        result = _wheel_observation(parsed["observation"]["members"], expected, filename, resources["ceilings"], resources, meter)
        if result["status"] == "OBSERVED":
            result["observation"]["zip_container"] = parsed["observation"]["container_envelope"]
            result["observation"]["publisher_evidence"] = publisher_record
            result["observation"]["resource_permission_actual"] = "NOT_PROVEN"
            result["observation"]["fds_opened_by_scanner"] = meter.get("fds_peak", 0) if meter.get("filesystem_stat_performed") is True else 0
            result["observation"]["nested_archives_opened"] = 0
            result["observation"]["custody_changed"] = False
            result["observation"]["filesystem_stat_performed"] = meter.get("filesystem_stat_performed") is True
            from .custody import seal_archive_custody

            result["observation"]["per_archive_custody"] = seal_archive_custody(meter, data, expected.get("sha256"), resources)
            schema_cause = validate_value(WHEEL_SCHEMA, result["observation"], resources, meter)
            if schema_cause is not None:
                return pack("REFUSED", schema_cause)
            from .raw_relations import wheel_raw_cause
            raw_cause = wheel_raw_cause(result["observation"],resources,meter)
            if raw_cause:
                return pack("REFUSED",raw_cause,stage="RAW_METADATA_CORRESPONDENCE")
            for member in result["observation"]["members"]:
                from .semantics import member_cause
                semantic = member_cause(member,resources,meter)
                if semantic:
                    return pack("REFUSED", semantic, stage="ARCHIVE_AND_MEMBER_CLOSURE")
                schema_cause = validate_value(MEMBER_SCHEMA, member, resources, meter)
                if schema_cause is not None:
                    return pack("REFUSED", schema_cause)
        return result

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


def scan_wheel(expected, held, resources, publisher_evidence=None, meter=None):
    from .digests import DigestStop
    owned = not isinstance(meter,dict)
    if owned:
        from .bounds import new_meter
        meter = new_meter()
        meter["active_resources"] = resources
    result=None
    try:
        result = _scan_wheel(expected, held, resources, publisher_evidence, meter)
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
        # _scan_wheel's data/parsed/metadata frame has ended. Retained public
        # graphs stay charged; only now may its nonescaping scratch retire.
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
