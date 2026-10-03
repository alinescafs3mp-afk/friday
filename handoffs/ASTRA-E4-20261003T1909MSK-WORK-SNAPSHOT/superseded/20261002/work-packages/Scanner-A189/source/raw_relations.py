"""Future metered raw/effective Source consumers. No bodies or code are executed here."""
from .bounds import reserve
from .digests import content_sha256


def member_raw_cause(member,resources=None,meter=None):
    from .deb_archive import _NumericUnsupported
    import struct
    tick = reserve(resources,meter,work_bytes=16384,live_bytes=32768)
    if tick:
        return tick
    try:
        if member.get("source_format") == "tar":
            raw = member["raw_header"]
            if set(raw) != {"hex","offset","dialect","effective","application"}:
                return "schema_rejected"
            header = bytes.fromhex(raw["hex"])
            if len(header) != 512 or raw["offset"] < 0 or raw["offset"] % 512:
                return "schema_rejected"
            from .deb_archive import _numeric,_header_text,_octal,_tar_checksum,_apply_pax,_effective_pax,_parse_pax,_gnu_text
            if _octal(header[148:156]) != _tar_checksum(header):
                return "schema_rejected"
            dialect = "posix" if header[257:265] == b"ustar\x0000" else "gnu" if header[257:265] == b"ustar  \x00" else None
            if dialect != raw["dialect"] or dialect != member["header_dialect"]:
                return "schema_rejected"
            name = _header_text(header[:100])
            prefix = _header_text(header[345:500]) if dialect == "posix" else ""
            if prefix:
                name = prefix+"/"+name
            expected = {"name":name,"linkname":_header_text(header[157:257]),
                "mode":_numeric(header[100:108]),"uid":_numeric(header[108:116]),
                "gid":_numeric(header[116:124]),"size":_numeric(header[124:136]),
                "mtime":str(_numeric(header[136:148])),"uname":_header_text(header[265:297]) or None,
                "gname":_header_text(header[297:329]) or None}
            kinds = {0:"regular",48:"regular",49:"hardlink",50:"symlink",53:"directory"}
            if kinds.get(header[156]) != member["type"]:
                return "schema_rejected"
            global_state = {}
            local_state = {}
            gnu_name = gnu_link = None
            origins = []
            for origin in member["extension_records"]:
                if origin["kind"] not in ("pax","gnu_longname","gnu_longlink"):
                    if origin["kind"] in ("gnu_atime","gnu_ctime"):
                        field = header[345:357] if origin["kind"] == "gnu_atime" else header[357:369]
                        if dialect != "gnu" or origin["value"] != _numeric(field) or origin["header_offset"] != raw["offset"]:
                            return "schema_rejected"
                    continue
                origin_header = bytes.fromhex(origin["raw_header_hex"])
                if len(origin_header) != 512 or origin["header_offset"] >= raw["offset"] or origin["applied_to_header_offset"] != raw["offset"]:
                    return "schema_rejected"
                if _octal(origin_header[148:156]) != _tar_checksum(origin_header):
                    return "schema_rejected"
                origins.append(origin)
                if origin["kind"] == "pax":
                    tick = reserve(resources,meter,work_bytes=len(origin["raw_record_utf8"])*8,
                                   live_bytes=len(origin["raw_record_utf8"])*16)
                    if tick:
                        return tick
                    payload = origin["raw_record_utf8"].encode("utf-8")
                    parsed = _parse_pax(payload)
                    if parsed != [(origin["key"],origin["value"])] or content_sha256(payload,resources,meter) != origin["raw_record_sha256"]:
                        return "schema_rejected"
                    expected_flag = b"g" if origin["scope"] == "global" else b"x"
                    if origin_header[156:157] != expected_flag or origin["record_offset"]+len(payload) > _numeric(origin_header[124:136]):
                        return "schema_rejected"
                    state = global_state if origin["scope"] == "global" else local_state
                    state[origin["key"]] = origin["value"] or None
                else:
                    tick = reserve(resources,meter,work_bytes=len(origin["raw_payload_hex"]),live_bytes=len(origin["raw_payload_hex"])*2)
                    if tick:
                        return tick
                    payload = bytes.fromhex(origin["raw_payload_hex"])
                    value,cause = _gnu_text(payload)
                    if cause or value != origin["value"] or len(payload) != _numeric(origin_header[124:136]):
                        return "schema_rejected"
                    if origin["kind"] == "gnu_longname":
                        if origin_header[156:157] != b"L":
                            return "schema_rejected"
                        gnu_name = value
                    else:
                        if origin_header[156:157] != b"K":
                            return "schema_rejected"
                        gnu_link = value
            application = {"global":global_state,"local":local_state,"gnu_name":gnu_name,"gnu_link":gnu_link}
            if application != raw["application"]:
                return "schema_rejected"
            for origin in origins:
                same = [row for row in origins if row["kind"] == origin["kind"] and row["scope"] == origin["scope"] and
                        (origin["kind"] != "pax" or row["key"] == origin["key"]) and row["record_id"] > origin["record_id"]]
                disposition = "SUPERSEDED" if same else "DELETED" if origin["kind"] == "pax" and origin["value"] == "" else "ACTIVE"
                if origin["disposition"] != disposition:
                    return "schema_rejected"
            if gnu_name is not None:
                expected["name"] = gnu_name
            if gnu_link is not None:
                expected["linkname"] = gnu_link
            if _apply_pax(_effective_pax(global_state,local_state).items(),expected):
                return "schema_rejected"
            if raw["effective"] != {k:expected[k] for k in raw["effective"]}:
                return "schema_rejected"
            for key in ("name","mode","uid","gid","mtime","size","uname","gname"):
                if expected[key] != member[key]:
                    return "schema_rejected"
            if member["type"] == "symlink" and expected["linkname"] != member["link_target"]:
                return "schema_rejected"
            if member["type"] == "hardlink":
                from .guards import normalize_member_path
                normalized,cause = normalize_member_path(expected["linkname"],65535,256)
                if cause or normalized != member["link_target"]:
                    return "schema_rejected"
        elif member.get("source_format") == "zip":
            import struct
            envelope = member["zip_envelope"]
            local = bytes.fromhex(envelope["local_header_hex"])
            central = bytes.fromhex(envelope["central_header_hex"])
            name_raw = bytes.fromhex(envelope["name_hex"])
            descriptor = bytes.fromhex(envelope["descriptor_hex"])
            if len(local) != 30 or len(central) != 46 or local[:4] != b"PK\x03\x04" or central[:4] != b"PK\x01\x02":
                return "schema_rejected"
            u16 = lambda data,n:struct.unpack_from("<H",data,n)[0]
            u32 = lambda data,n:struct.unpack_from("<I",data,n)[0]
            from .zip_wheel import _read_sizes,_decode_name
            local_extra = bytes.fromhex(envelope["local_extra_hex"])
            central_extra = bytes.fromhex(envelope["central_extra_hex"])
            usize,csize,offset,disk,cause = _read_sizes(u32(central,20),u32(central,24),u32(central,42),central_extra,u16(central,34))
            if cause or (usize,csize,offset,disk) != (envelope["uncompressed_size"],envelope["compressed_size"],envelope["local_offset"],0):
                return "schema_rejected"
            local_usize,local_csize,_offset,_disk,cause = _read_sizes(u32(local,18),u32(local,22),0,local_extra)
            if cause:
                return "schema_rejected"
            if (u16(local,4),u16(central,6),u16(local,6),u16(central,8),u16(local,8),u16(central,10)) != (envelope["local_version"],envelope["central_version"],member["flags"],member["flags"],envelope["method"],envelope["method"]):
                return "schema_rejected"
            if u16(local,26) != len(name_raw) or u16(central,28) != len(name_raw) or u16(local,28) != len(local_extra) or u16(central,30) != len(central_extra):
                return "schema_rejected"
            if _decode_name(name_raw,member["flags"]) != member["name"] or u32(central,16) != member["crc32"] or u32(central,38) != envelope["external_attributes"]:
                return "schema_rejected"
            if envelope["data_offset"] != offset+30+len(name_raw)+len(local_extra):
                return "schema_rejected"
            if member["flags"] & 8:
                width = 20 if any(value == 0xFFFFFFFF for value in (u32(local,18),u32(local,22),u32(central,20),u32(central,24))) else 12
                signed = len(descriptor) == width+4
                if len(descriptor) not in (width,width+4) or signed and descriptor[:4] != b"PK\x07\x08":
                    return "schema_rejected"
                start = 4 if signed else 0
                crc = u32(descriptor,start)
                if width == 12:
                    got_c,got_u = u32(descriptor,start+4),u32(descriptor,start+8)
                else:
                    got_c,got_u = struct.unpack_from("<QQ",descriptor,start+4)
                if (crc,got_c,got_u) != (member["crc32"],csize,usize) or u32(local,14) not in (0,crc) or local_csize not in (0,csize) or local_usize not in (0,usize):
                    return "schema_rejected"
            elif descriptor or (u32(local,14),local_csize,local_usize) != (member["crc32"],csize,usize):
                return "schema_rejected"
            if envelope["range_end"] != envelope["data_offset"]+csize+len(descriptor):
                return "schema_rejected"
            if envelope["local_version"] != envelope["central_version"] or envelope["flags"] != member["flags"]:
                return "schema_rejected"
            if member["type"] == "regular" and envelope["uncompressed_size"] != member["size"]:
                return "schema_rejected"
            if not 0 <= envelope["local_offset"] < envelope["data_offset"] <= envelope["range_end"]:
                return "schema_rejected"
            if envelope["data_offset"]+envelope["compressed_size"] > envelope["range_end"]:
                return "schema_rejected"
        else:
            return "schema_rejected"
    except (ValueError,UnicodeError,KeyError,TypeError,IndexError,struct.error,_NumericUnsupported) as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,raw_origin)
        return "schema_rejected"
    return None


def wheel_raw_cause(observation,resources=None,meter=None):
    """Consume complete normal ZIP container/CSV/source-member companions."""
    import struct
    try:
        container = observation["zip_container"]
        eocd = bytes.fromhex(container["eocd_hex"])
        tick = reserve(resources,meter,work_bytes=len(eocd)*4,live_bytes=len(eocd)*8)
        if tick:
            return tick
        if len(eocd)<22 or eocd[:4] != b"PK\x05\x06" or struct.unpack_from("<H",eocd,20)[0]+22 != len(eocd) or container["eocd_offset"]+len(eocd) != observation["whole_size"]:
            return "schema_rejected"
        disk,start,here,total,size,offset = struct.unpack_from("<HHHHII",eocd,4)
        if container["zip64"]:
            z64 = bytes.fromhex(container["zip64_eocd_hex"])
            locator = bytes.fromhex(container["zip64_locator_hex"])
            if len(z64) != 56 or len(locator) != 20 or z64[:4] != b"PK\x06\x06" or locator[:4] != b"PK\x06\x07":
                return "schema_rejected"
            if struct.unpack_from("<Q",z64,4)[0] != 44 or struct.unpack_from("<I",locator,16)[0] != 1 or struct.unpack_from("<I",locator,4)[0] != 0:
                return "schema_rejected"
            z_disk,z_start,z_here,z_total,z_size,z_offset = struct.unpack_from("<IIQQQQ",z64,16)
            for actual,sentinel,effective in ((disk,65535,z_disk),(start,65535,z_start),(here,65535,z_here),(total,65535,z_total),(size,4294967295,z_size),(offset,4294967295,z_offset)):
                if actual != sentinel and actual != effective:
                    return "schema_rejected"
            disk,start,here,total,size,offset = z_disk,z_start,z_here,z_total,z_size,z_offset
            if struct.unpack_from("<Q",locator,8)[0] != offset+size or offset+size+76 != container["eocd_offset"]:
                return "schema_rejected"
        elif offset+size != container["eocd_offset"]:
            return "schema_rejected"
        if disk or start or here != total or (offset,size,total) != (container["central_offset"],container["central_size"],container["member_count"]) or total != len(observation["members"]):
            return "schema_rejected"
        ranges = sorted((row["zip_envelope"]["local_offset"],row["zip_envelope"]["range_end"]) for row in observation["members"])
        if ranges and (ranges[0][0] != 0 or ranges[-1][1] != offset or any(a[1] != b[0] for a,b in zip(ranges,ranges[1:]))):
            return "schema_rejected"
        from .zip_wheel import _parse_record
        text = observation["record_raw_utf8"]
        tick = reserve(resources,meter,work_bytes=len(text)*8,live_bytes=len(text)*32)
        if tick:
            return tick
        raw = text.encode("utf-8")
        if content_sha256(raw,resources,meter) != observation["record_raw_sha256"]:
            return "schema_rejected"
        parsed,cause = _parse_record(text,2147483648,resources,meter)
        if cause:
            return cause
        actual = [{k:row[k] for k in ("path","digest","size")} for row in observation["record_rows"]]
        if parsed != actual:
            return "schema_rejected"
        from .guards import normalize_member_path
        members = {row["normalized_path"]:row for row in observation["members"]}
        if len(members) != len(observation["members"]):
            return "schema_rejected"
        self_paths = [path for path in members if path.endswith(".dist-info/RECORD")]
        if len(self_paths) != 1:
            return "schema_rejected"
        self_path = self_paths[0]
        distinfo = self_path.rsplit("/",1)[0]
        signature_paths = {distinfo+"/RECORD.jws",distinfo+"/RECORD.p7s"}
        seen = set()
        for row in observation["record_rows"]:
            normalized,cause = normalize_member_path(row["path"],65535,256)
            if cause or normalized != row["normalized_path"] or normalized in seen or normalized not in members:
                return "schema_rejected"
            seen.add(normalized)
            member = members[normalized]
            binding = row["binding"]
            if binding["member_path"] != normalized or binding["member_type"] != member["type"]:
                return "schema_rejected"
            if row["digest"] == "":
                role = "self" if normalized == self_path else "signature" if normalized in signature_paths else None
                if role is None or member["type"] != "regular" or binding["role"] != role or binding["algorithm"] is not None or binding["computed_digest"] is not None:
                    return "schema_rejected"
                if normalized == self_path and row["size"] is not None:
                    return "schema_rejected"
                sha,size = member["content_sha256"],member["size"]
            else:
                algorithm,encoded = row["digest"].split("=",1)
                from .digests import SUPPORTED_RECORD_ALGORITHMS
                if algorithm not in SUPPORTED_RECORD_ALGORITHMS or binding["role"] != "content" or binding["algorithm"] != algorithm or binding["computed_digest"] != encoded:
                    return "schema_rejected"
                if member["type"] == "regular":
                    sha,size = member["content_sha256"],member["size"]
                elif member["type"] == "symlink":
                    target = member["link_target"]
                    tick = reserve(resources,meter,work_bytes=len(target)*4,live_bytes=len(target)*4)
                    if tick:
                        return tick
                    sha,size = member["link_target_sha256"],len(target.encode("utf-8"))
                else:
                    return "schema_rejected"
                if row["size"] != size:
                    return "schema_rejected"
                if algorithm == "sha256":
                    import base64
                    if base64.urlsafe_b64encode(bytes.fromhex(sha)).rstrip(b"=").decode("ascii") != encoded:
                        return "schema_rejected"
            if (binding["member_sha256"],binding["content_size"]) != (sha,size):
                return "schema_rejected"
        if self_path not in seen or any(path not in seen and row["type"] != "directory" and path not in signature_paths for path,row in members.items()):
            return "schema_rejected"
        companion = observation["metadata_binding"]
        if (companion["selected_archive_sha256"],companion["observed_metadata_content_sha256"],companion["record_raw_sha256"],companion["publisher_raw_sha256"]) != (observation["whole_sha256"],observation["metadata"]["raw_sha256"],observation["record_raw_sha256"],observation["publisher_metadata_raw_sha256"]):
            return "schema_rejected"
        selected = meter.get("content_companion",{}).get(observation["relative_path"]) if isinstance(meter,dict) else None
        if selected is not None and (companion["publisher_raw_sha256"],companion["publisher_record_ordinal"],companion["selected_metadata_content_sha256"]) != (selected["publisher_raw_sha256"],selected["publisher_record_ordinal"],selected["metadata_content_sha256"]):
            return "schema_rejected"
    except (ValueError,UnicodeError,KeyError,TypeError,IndexError,struct.error) as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,raw_origin)
        return "schema_rejected"
    return None


def tar_archive_cause(envelope,members,resources=None,meter=None):
    """Replay the WHOLE normal TAR frame and extension state, not each leaf.

    Empty/global/deleted/superseded extensions and unused trailing globals are
    represented by physical events. Contiguous event ranges, two terminators,
    all-zero block-aligned tail and every member's effective state are joined.
    No archive parser is invoked by this Source-only construction task.
    """
    from .deb_archive import _numeric,_octal,_tar_checksum,_header_text,_parse_pax,_gnu_text,_merge_pax,_effective_pax,_NumericUnsupported
    try:
        required={'schema','events','terminator_offset','zero_terminator_blocks',
                  'tail_zero_bytes','expanded_stream_size','framing_complete'}
        if set(envelope)!=required or envelope['schema']!='friday.scanner.tar-archive-framing.v1' or envelope['framing_complete'] is not True:return 'schema_rejected'
        offset=0;index=0;global_state={};local_state={};gnu_name=gnu_link=None
        history=[];pending_start=0
        for event in envelope['events']:
            tick=reserve(resources,meter,work_bytes=16384+len(event['extension_payload_hex'] or '')*8,
                live_bytes=32768+len(event['extension_payload_hex'] or '')*16)
            if tick:return tick
            if set(event)!={'offset','header_hex','payload_size','payload_sha256','extension_payload_hex','padding_bytes','padding_zero','member_index'}:return 'schema_rejected'
            header=bytes.fromhex(event['header_hex'])
            if len(header)!=512 or event['offset']!=offset or _octal(header[148:156])!=_tar_checksum(header):return 'schema_rejected'
            dialect='posix' if header[257:265]==b'ustar\x0000' else 'gnu' if header[257:265]==b'ustar  \x00' else None
            if dialect is None:return 'schema_rejected'
            flag=header[156:157];raw_size=_numeric(header[124:136])
            extension=flag in (b'x',b'g',b'L',b'K')
            size=raw_size if extension else int(_effective_pax(global_state,local_state).get('size',raw_size))
            pad=(-size)%512
            if size<0 or event['payload_size']!=size or event['padding_bytes']!=pad or event['padding_zero'] is not True:return 'schema_rejected'
            if _numeric(header[329:337])!=0 or _numeric(header[337:345])!=0:return 'schema_rejected'
            if dialect=='gnu' and (_numeric(header[369:381])!=0 or any(value not in (0,32) for value in header[381:512])):return 'schema_rejected'
            if dialect=='posix' and any(value not in (0,32) for value in header[500:512]):return 'schema_rejected'
            if extension:
                if event['member_index'] is not None:return 'schema_rejected'
                payload=bytes.fromhex(event['extension_payload_hex'])
                if len(payload)!=size or content_sha256(payload,resources,meter)!=event['payload_sha256']:return 'schema_rejected'
                if flag in (b'L',b'K'):
                    value,cause=_gnu_text(payload)
                    if cause:return cause
                    history.append({'kind':'gnu_longname' if flag==b'L' else 'gnu_longlink','value':value,
                        'scope':'gnu','header_offset':offset,'applies_to_next':True,'record_id':len(history),
                        'raw_header_hex':event['header_hex'],'raw_payload_hex':event['extension_payload_hex']})
                    if flag==b'L':gnu_name=value
                    else:gnu_link=value
                else:
                    parsed=_parse_pax(payload)
                    if parsed is None or parsed=='unsupported':return 'schema_rejected'
                    scope='global' if flag==b'g' else 'local';record_offset=0
                    for key,value in parsed:
                        space=payload.find(b' ',record_offset);length=int(payload[record_offset:space])
                        raw=payload[record_offset:record_offset+length]
                        history.append({'kind':'pax','key':key,'value':value,'scope':scope,'header_offset':offset,
                            'applies_to_next':True,'record_id':len(history),'record_offset':record_offset,
                            'raw_record_utf8':raw.decode('utf-8'),'raw_record_sha256':content_sha256(raw,resources,meter),
                            'raw_header_hex':event['header_hex']})
                        record_offset+=length
                    _merge_pax(global_state if flag==b'g' else local_state,parsed)
            else:
                if event['extension_payload_hex'] is not None or event['member_index']!=index or index>=len(members):return 'schema_rejected'
                member=members[index];raw=member['raw_header']
                if raw['hex']!=event['header_hex'] or raw['offset']!=offset:return 'schema_rejected'
                application={'global':dict(global_state),'local':dict(local_state),'gnu_name':gnu_name,'gnu_link':gnu_link}
                if raw['application']!=application or raw['effective']['size']!=size:return 'schema_rejected'
                origins=[dict(value) for ordinal,value in enumerate(history) if value['scope']=='global' or ordinal>=pending_start]
                for origin in origins:
                    origin['applied_to_header_offset']=offset
                    later=[row for row in origins if row['kind']==origin['kind'] and row['scope']==origin['scope'] and row['record_id']>origin['record_id'] and (origin['kind']!='pax' or row['key']==origin['key'])]
                    current=application['global' if origin['scope']=='global' else 'local'].get(origin['key']) if origin['kind']=='pax' else True
                    origin['disposition']='SUPERSEDED' if later else 'DELETED' if current is None else 'ACTIVE'
                actual=[value for value in member['extension_records'] if value['kind'] in ('pax','gnu_longname','gnu_longlink')]
                if actual!=origins:return 'schema_rejected'
                if member['type']=='regular':
                    if event['payload_sha256']!=member['content_sha256'] or size!=member['size']:return 'schema_rejected'
                elif size!=0 or event['payload_sha256']!=content_sha256(b'',resources,meter):return 'schema_rejected'
                local_state={};gnu_name=gnu_link=None;pending_start=len(history);index+=1
            offset+=512+size+pad
        tail=envelope['tail_zero_bytes']
        if index!=len(members) or offset!=envelope['terminator_offset'] or envelope['zero_terminator_blocks']!=2 or type(tail) is not int or tail<0 or tail%512 or envelope['expanded_stream_size']!=offset+1024+tail:return 'schema_rejected'
        if local_state or gnu_name is not None or gnu_link is not None:return 'schema_rejected'
    except (ValueError,UnicodeError,KeyError,TypeError,IndexError,_NumericUnsupported) as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,raw_origin)
        return 'schema_rejected'
    return None


def deb_raw_cause(observation,resources=None,meter=None):
    tick = reserve(resources,meter,work_bytes=16384,live_bytes=32768)
    if tick:
        return tick
    try:
        if set(observation['tar_framing'])!={'control','data'}:return 'schema_rejected'
        for domain in ('control','data'):
            cause=tar_archive_cause(observation['tar_framing'][domain],observation[domain+'_members'],resources,meter)
            if cause:return cause
        next_offset = 8
        for item in observation["ar_members"]:
            header = bytes.fromhex(item["raw_header_hex"])
            if len(header) != 60 or header[58:] != b"`\n" or item["header_offset"] != next_offset:
                return "schema_rejected"
            from .deb_archive import _ar_uint
            if _ar_uint(header[48:58]) != item["header_payload_size"]:
                return "schema_rejected"
            if item["header_payload_size"] != item["name_prefix_bytes"]+item["content_size"] or item["size"] != item["content_size"]:
                return "schema_rejected"
            for key,start,end in (("raw_name_field",0,16),("raw_mtime_text",16,28),("raw_uid_text",28,34),("raw_gid_text",34,40),("raw_mode_text",40,48),("raw_size_text",48,58)):
                if item[key].encode("latin-1") != header[start:end]:
                    return "schema_rejected"
            for key,start,end,base,digits in (("mtime",16,28,10,b"0123456789"),("uid",28,34,10,b"0123456789"),("gid",34,40,10,b"0123456789"),("mode",40,48,8,b"01234567")):
                text = header[start:end].strip() or b"0"
                if any(c not in digits for c in text) or int(text,base) != item[key]:
                    return "schema_rejected"
            prefix = bytes.fromhex(item["raw_name_prefix_hex"])
            if item["dialect"] == "bsd":
                if not header[:16].startswith(b"#1/") or _ar_uint(header[3:16]) != len(prefix) or len(prefix) != item["name_prefix_bytes"] or prefix.rstrip(b"\x00").decode("utf-8") != item["name"]:
                    return "schema_rejected"
            elif item["dialect"] == "sysv":
                raw_name = header[:16].decode("ascii").rstrip(" ")
                if prefix or item["name_prefix_bytes"] != 0 or raw_name.removesuffix("/") != item["name"]:
                    return "schema_rejected"
            else:
                return "schema_rejected"
            next_offset += 60+item["header_payload_size"]+(item["header_payload_size"]%2)
        if next_offset != observation["whole_size"]:
            return "schema_rejected"
        fields = observation["control_fields"]
        text = fields["raw_utf8"]
        tick = reserve(resources,meter,work_bytes=len(text)*8,live_bytes=len(text)*16)
        if tick:
            return tick
        raw = text.encode("utf-8")
        if content_sha256(raw,resources,meter) != fields["raw_sha256"]:
            return "schema_rejected"
        from .deb_archive import _deb822,_case_field
        reconstructed = _deb822(raw,resources,meter)
        if reconstructed is None:
            return "schema_rejected"
        for key,value in reconstructed.items():
            if fields[key] != value:
                return "schema_rejected"
        for key,name in (("package","Package"),("version","Version"),("architecture","Architecture")):
            if fields[key] != _case_field(reconstructed["folded_fields"],name):
                return "schema_rejected"
    except (ValueError,UnicodeError,KeyError,TypeError,IndexError) as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,raw_origin)
        return "schema_rejected"
    return None
