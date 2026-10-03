"""Cross-field consumers for source observations, separate from JSON type checks."""


def member_cause(member,resources=None,meter=None):
    if not isinstance(member, dict):
        return "schema_rejected"
    from .raw_relations import member_raw_cause
    checked = member_raw_cause(member,resources,meter)
    if checked:
        return checked
    if member.get("source_format") == "tar":
        raw = member.get("raw_header")
        if not isinstance(raw, dict) or set(raw) != {"hex", "offset", "dialect", "effective","application"}:
            return "schema_rejected"
        if not isinstance(raw["hex"], str) or len(raw["hex"]) != 1024 or any(c not in "0123456789abcdef" for c in raw["hex"]):
            return "schema_rejected"
        if raw["dialect"] != member.get("header_dialect") or raw["offset"] < 0 or raw["offset"] % 512:
            return "schema_rejected"
        effective = raw["effective"]
        for key in ("name", "mode", "uid", "gid", "mtime", "size"):
            if effective.get(key) != member.get(key):
                return "schema_rejected"
    elif member.get("source_format") == "zip":
        envelope = member.get("zip_envelope")
        if not isinstance(envelope, dict) or envelope.get("local_version") != envelope.get("central_version"):
            return "schema_rejected"
        if member.get("type") == "regular" and envelope.get("uncompressed_size") != member.get("size"):
            return "schema_rejected"
        if not 0 <= envelope["local_offset"] < envelope["data_offset"] <= envelope["range_end"]:
            return "schema_rejected"
    else:
        return "schema_rejected"
    resolved = member.get("resolved_link")
    if resolved is not None and (member.get("type") != "hardlink" or resolved.get("type") != "regular" or resolved.get("sha256") != member.get("link_target_sha256")):
        return "schema_rejected"
    return None


def deb_cause(observation,resources=None,meter=None):
    from .raw_relations import deb_raw_cause
    checked = deb_raw_cause(observation,resources,meter)
    if checked:
        return checked
    for item in observation.get("ar_members", []):
        raw = item.get("raw_header_hex")
        if not isinstance(raw, str) or len(raw) != 120 or any(c not in "0123456789abcdef" for c in raw):
            return "schema_rejected"
        header = bytes.fromhex(raw)
        if header[58:] != b"`\n" or item["header_payload_size"] != item["name_prefix_bytes"] + item["content_size"] or item["size"] != item["content_size"]:
            return "schema_rejected"
        for key, start, end in (("raw_name_field", 0, 16), ("raw_mtime_text", 16, 28), ("raw_uid_text", 28, 34), ("raw_gid_text", 34, 40), ("raw_mode_text", 40, 48), ("raw_size_text", 48, 58)):
            if item[key].encode("latin-1") != header[start:end]:
                return "schema_rejected"
    fields = observation.get("control_fields")
    if not isinstance(fields, dict):
        return "schema_rejected"
    order = fields["field_order"]
    if len(order) != len(set(name.casefold() for name in order)):
        return "schema_rejected"
    for key in ("raw_fields", "folded_fields", "field_kinds"):
        if set(fields[key]) != set(order):
            return "schema_rejected"
    if fields["all"] != fields["folded_fields"] or fields["unknown_field_policy"] != "simple_no_continuation":
        return "schema_rejected"
    for key, value in fields["multiline_fields"].items():
        if fields["field_kinds"].get(key) != "multiline" or "\n".join(value) != fields["folded_fields"][key]:
            return "schema_rejected"
    return None
