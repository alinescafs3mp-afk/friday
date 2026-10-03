"""Map observations and separately selected facts onto exact A009 key sets.

Unknown facts stay null. These structural projections convey no release authority.
"""

from .canonical import canonical_sha256
from .causes import pack
from .pins import PROVENANCE_KEYS, PROVENANCE_SCHEMA, SNAPSHOT_KEYS, SNAPSHOT_SCHEMA

_REGULAR = (
    "path",
    "type",
    "uid",
    "gid",
    "mode",
    "nlink",
    "mount_domain",
    "size",
    "sha256",
    "executable_class",
)
_SYMLINK = (
    "path",
    "type",
    "uid",
    "gid",
    "mode",
    "nlink",
    "mount_domain",
    "link_target",
    "link_target_sha256",
)
_DIRECTORY = (
    "path",
    "type",
    "uid",
    "gid",
    "mode",
    "nlink",
    "mount_domain",
)
_HARDLINK = (
    "path",
    "type",
    "uid",
    "gid",
    "mode",
    "nlink",
    "mount_domain",
    "link_target",
)
_ARTIFACT = (
    "class",
    "filename",
    "format",
    "tag",
    "abi",
    "size",
    "sha256",
    "upstream_authority",
    "signed_index_sha256",
    "detached_signature_sha256",
    "accepted_signer_fingerprint",
    "verifier_sha256",
)


def _hex64(value):
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _member_document(member,resources=None,meter=None):
    if not isinstance(member, dict):
        return None, "member_provenance_incomplete"
    path = member.get("final_destination") or member.get("normalized_path")
    source = member.get("source_archive_sha256")
    kind = member.get("type")
    if not isinstance(path, str) or path == "" or not _hex64(source) or kind not in ("regular", "symlink", "directory", "hardlink"):
        return None, "member_provenance_incomplete"
    installed = member.get("installed_observation")
    nlink = None
    mount = None
    if "installed_observation" in member:
        from .schema_validate import validate_role
        cause = validate_role("installed",installed,resources,meter)
        if cause:
            return None,cause
        from .bounds import performing_requested
        if performing_requested(resources) and meter.get("installed_inventory",{}).get(path) != installed:
            return None,"member_provenance_incomplete"
        if not isinstance(installed, dict) or not _hex64(installed.get("observation_sha256")) or installed.get("path") != path:
            return None, "member_provenance_incomplete"
        admitted_nlink = installed.get("nlink")
        mount_value = installed.get("mount_domain")
        if any(key not in installed for key in ("nlink", "mount_domain", "uid", "gid")):
            return None, "member_provenance_incomplete"
        if not isinstance(admitted_nlink, int) or isinstance(admitted_nlink, bool) or admitted_nlink < 0:
            return None, "member_provenance_incomplete"
        if kind in ("regular", "hardlink") and admitted_nlink != 1:
            return None, "member_provenance_incomplete"
        if not isinstance(mount_value, str) or mount_value == "":
            return None, "member_provenance_incomplete"
        if not isinstance(installed.get("uid"), int) or isinstance(installed.get("uid"), bool):
            return None, "member_provenance_incomplete"
        if not isinstance(installed.get("gid"), int) or isinstance(installed.get("gid"), bool):
            return None, "member_provenance_incomplete"
        nlink = admitted_nlink
        mount = mount_value
        if not isinstance(installed.get("mode"), int) or isinstance(installed.get("mode"), bool):
            return None, "member_provenance_incomplete"
        member = dict(member)
        member["uid"] = installed.get("uid")
        member["gid"] = installed.get("gid")
        member["mode"] = installed.get("mode")
        if kind == "hardlink":
            resolved = member.get("resolved_link")
            if not isinstance(resolved, dict) or resolved.get("type") != "regular":
                return None, "member_provenance_incomplete"
            member["content_sha256"] = resolved["sha256"]
            member["size"] = resolved["size"]
            kind = "regular"
        if installed.get("type") != kind:
            return None, "member_provenance_incomplete"
        if kind == "regular" and (installed.get("size") != member.get("size") or installed.get("sha256") != member.get("content_sha256")):
            return None, "member_provenance_incomplete"
        if kind == "symlink" and (installed["link_target"] != member.get("link_target") or installed["link_target_sha256"] != member.get("link_target_sha256")):
            return None,"member_provenance_incomplete"
    base = {
        "path": path,
        "type": kind,
        "uid": member.get("uid"),
        "gid": member.get("gid"),
        "mode": member.get("mode"),
        "nlink": nlink,
        "mount_domain": mount,
    }
    if kind == "regular":
        if not _hex64(member.get("content_sha256")) or not isinstance(member.get("size"), int):
            return None, "member_provenance_incomplete"
        base.update(
            {
                "size": member["size"],
                "sha256": member["content_sha256"],
                "executable_class": installed["executable_class"] if isinstance(installed,dict) else "executable" if member.get("executable") else "non_executable",
            }
        )
        keys = _REGULAR
    elif kind == "symlink":
        target = member.get("link_target")
        target_sha = member.get("link_target_sha256")
        if not isinstance(target, str) or not _hex64(target_sha):
            return None, "member_provenance_incomplete"
        base.update({"link_target": target, "link_target_sha256": target_sha})
        keys = _SYMLINK
    elif kind == "hardlink":
        target = member.get("link_target")
        if not isinstance(target, str) or target == "":
            return None, "member_provenance_incomplete"
        base.update({"link_target": target})
        keys = _HARDLINK
    else:
        keys = _DIRECTORY
    if tuple(base) != keys and set(base) != set(keys):
        return None, "snapshot_member_key_drift"
    ordered = {key: base[key] for key in keys}
    return ordered, None


def _pinned_keys(envelope, expected,resources=None,meter=None):
    from .schema_validate import load_schemas

    found, cause = load_schemas(resources,meter)
    if cause is not None:
        return cause
    pin = found.get(envelope) if isinstance(found, dict) else None
    if not isinstance(pin, dict) or pin.get("weakened") is not False:
        return "schema_unknown"
    loaded = pin.get("exact_top_level_keys")
    if tuple(loaded) != tuple(expected):
        return "schema_unknown"
    return None


def _express_candidate(candidate):
    if candidate is None:
        return None, None
    if not isinstance(candidate, dict):
        return None, "member_provenance_incomplete"
    keys = ("id", "owner", "recipe_sha256", "commit", "tree", "root", "controller", "gate", "preflight", "broker")
    if set(candidate) != set(keys):
        return None, "member_provenance_incomplete"
    for key in keys:
        value = candidate.get(key)
        if value is None:
            continue
        if key == "root":
            if not isinstance(value, dict) or set(value) != {"device", "inode", "full_mode", "uid", "gid", "nlink", "size", "mtime_ns", "ctime_ns"}:
                return None, "member_provenance_incomplete"
            if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in value.values()):
                return None, "member_provenance_incomplete"
            continue
        if key == "controller":
            if not isinstance(value, dict) or set(value) != {"relative_path", "sha256"} or not _hex64(value["sha256"]):
                return None, "member_provenance_incomplete"
            path = value["relative_path"]
            if not isinstance(path, str) or path.startswith("/") or any(p in ("", ".", "..") for p in path.split("/")):
                return None, "member_provenance_incomplete"
            continue
        if key in ("gate", "preflight", "broker") and not _hex64(value):
            return None, "member_provenance_incomplete"
        if isinstance(value, bool) or not isinstance(value, str) or value == "":
            return None, "member_provenance_incomplete"
    if candidate.get("recipe_sha256") is not None and not _hex64(candidate.get("recipe_sha256")):
        return None, "member_provenance_incomplete"
    return {key: candidate[key] for key in keys}, None


def project_snapshot(members, materials_projection=None, created_utc=None, snapshot_id=None, candidate=None, facts=None, resources=None, meter=None, created_directories=None,supplemental_bindings=None):
    pin_cause = _pinned_keys(SNAPSHOT_SCHEMA, SNAPSHOT_KEYS,resources,meter)
    if pin_cause is not None:
        return pack("REFUSED", pin_cause)
    expressed, express_cause = _express_candidate(candidate)
    if express_cause is not None:
        return pack("REFUSED", express_cause)
    if not isinstance(members, list):
        return pack("REFUSED", "member_provenance_incomplete")
    if facts is not None:
        expected_facts = {"snapshot_id", "creation_tool_sha256", "created_utc", "platform", "runtime_contract_sha256", "root_merkle_sha256"}
        if not isinstance(facts, dict) or set(facts) != expected_facts:
            return pack("REFUSED", "member_provenance_incomplete")
        for key in ("creation_tool_sha256", "runtime_contract_sha256", "root_merkle_sha256"):
            if facts[key] is not None and not _hex64(facts[key]):
                return pack("REFUSED", "member_provenance_incomplete")
        platform = facts["platform"]
        if platform is not None:
            platform_keys = {"machine", "python_abi", "import_suffixes", "rootfs_identity", "kernel_contract_sha256", "loader_contract_sha256"}
            if not isinstance(platform, dict) or set(platform) != platform_keys or platform["machine"] != "x86_64":
                return pack("REFUSED", "member_provenance_incomplete")
            if not all(_hex64(platform[key]) for key in ("rootfs_identity", "kernel_contract_sha256", "loader_contract_sha256")):
                return pack("REFUSED", "member_provenance_incomplete")
            if not isinstance(platform["python_abi"], str) or not isinstance(platform["import_suffixes"], list) or any(not isinstance(x, str) for x in platform["import_suffixes"]):
                return pack("REFUSED", "member_provenance_incomplete")
    seen = {}
    documents = []
    for member in members:
        if isinstance(meter, dict):
            from .bounds import reserve
            tick = reserve(resources, meter, work_bytes=1024, live_bytes=4096)
            if tick:
                return pack("REFUSED", tick)
        source = member.get("source_archive_sha256") if isinstance(member, dict) else None
        path = member.get("normalized_path") if isinstance(member, dict) else None
        domain = member.get("source_domain") if isinstance(member, dict) else None
        final_dest = member.get("final_destination") if isinstance(member, dict) else None
        if isinstance(final_dest, str) and final_dest != "":
            key = ("final", final_dest)
        else:
            key = ("source", domain, path)
        if key in seen:
            previous = seen[key]
            compared = ("type", "uid", "gid", "mode", "mtime", "size", "content_sha256", "installed_observation")
            if not (isinstance(member, dict) and member.get("type") == "directory" and member.get("shared_directory") is True and all(previous.get(k) == member.get(k) for k in compared)):
                return pack("REFUSED", "duplicate_member_provenance", detail={"path": path})
            continue
        seen[key] = member
        document, cause = _member_document(member,resources,meter)
        if cause is not None:
            return pack("REFUSED", cause, detail={"path": path})
        documents.append(document)
    if supplemental_bindings is not None:
        from .schema_validate import validate_role
        from .bounds import reserve
        for binding in supplemental_bindings:
            allocation = reserve(resources,meter,work_bytes=8192,live_bytes=16384)
            if allocation:
                return pack("REFUSED",allocation)
            cause = validate_role("supplemental_binding",binding,resources,meter)
            if cause:
                return pack("REFUSED",cause)
            installed = binding["installed_observation"]
            path = binding["destination"]
            if path != installed["path"] or ("final",path) in seen or meter.get("installed_inventory",{}).get(path) != installed:
                return pack("REFUSED","member_provenance_incomplete")
            keys = {"regular":_REGULAR,"symlink":_SYMLINK,"directory":_DIRECTORY}[installed["type"]]
            documents.append({key:installed[key] for key in keys})
            seen[("final",path)] = installed
    if created_directories is not None:
        from .schema_validate import validate_role
        if not isinstance(created_directories,list):
            return pack("REFUSED","member_provenance_incomplete")
        for directory in created_directories:
            cause = validate_role("installed",directory,resources,meter)
            if cause:
                return pack("REFUSED",cause)
            path = directory["path"]
            if directory["type"] != "directory" or ("final",path) in seen:
                return pack("REFUSED","member_provenance_incomplete")
            if not isinstance(meter,dict) or meter.get("installed_inventory",{}).get(path) != directory:
                return pack("REFUSED","member_provenance_incomplete")
            documents.append({key:directory[key] for key in _DIRECTORY})
            seen[("final",path)] = directory
    documents.sort(key=lambda item: item["path"].encode("utf-8"))
    materials = None
    if materials_projection is not None:
        if isinstance(meter, dict):
            from .canonical import canonical_sha256_metered
            materials, cause = canonical_sha256_metered(materials_projection, resources, meter)
            if cause:
                return pack("REFUSED", cause)
        else:
            materials = canonical_sha256(materials_projection)
    doc = {
        "schema": SNAPSHOT_SCHEMA,
        "contract": "lab818-structural-projection-not-admission",
        "snapshot_id": snapshot_id,
        "creation_tool_sha256": None,
        "created_utc": created_utc,
        "candidate": expressed,
        "platform": None,
        "materials_sha256": materials,
        "runtime_contract_sha256": None,
        "members": documents,
        "root_merkle_sha256": None,
    }
    if isinstance(facts, dict):
        doc.update(facts)
    if tuple(doc) != SNAPSHOT_KEYS and set(doc) != set(SNAPSHOT_KEYS):
        return pack("REFUSED", "snapshot_key_drift")
    ordered = {key: doc[key] for key in SNAPSHOT_KEYS}
    from .schema_validate import validate_value
    validated = validate_value(SNAPSHOT_SCHEMA,ordered,resources,meter)
    if validated:
        return pack("REFUSED",validated)
    return pack("OBSERVED", None, observation=ordered)


def project_provenance(artifacts, manifest_sha256=None, recipe_sha256=None, facts=None,resources=None,meter=None):
    pin_cause = _pinned_keys(PROVENANCE_SCHEMA, PROVENANCE_KEYS,resources,meter)
    if pin_cause is not None:
        return pack("REFUSED", pin_cause)
    if not isinstance(artifacts, list):
        return pack("REFUSED", "member_provenance_incomplete")
    rows = []
    seen = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            return pack("REFUSED", "member_provenance_incomplete")
        filename = artifact.get("filename")
        digest = artifact.get("sha256")
        if not isinstance(filename, str) or filename == "" or not _hex64(digest):
            return pack("REFUSED", "member_provenance_incomplete")
        if filename in seen:
            return pack("REFUSED", "duplicate_member_provenance", detail={"filename": filename})
        seen.add(filename)
        row = {
            "class": artifact.get("class"),
            "filename": filename,
            "format": artifact.get("format"),
            "tag": artifact.get("tag"),
            "abi": artifact.get("abi"),
            "size": artifact.get("size"),
            "sha256": digest,
            "upstream_authority": artifact.get("upstream_authority"),
            "signed_index_sha256": artifact.get("signed_index_sha256"),
            "detached_signature_sha256": artifact.get("detached_signature_sha256"),
            "accepted_signer_fingerprint": artifact.get("accepted_signer_fingerprint"),
            "verifier_sha256": artifact.get("verifier_sha256"),
        }
        for key in ("signed_index_sha256", "detached_signature_sha256", "verifier_sha256"):
            if row[key] is not None and not _hex64(row[key]):
                return pack("REFUSED", "member_provenance_incomplete")
        for key in ("upstream_authority", "accepted_signer_fingerprint"):
            if row[key] is not None and (not isinstance(row[key], str) or row[key] == ""):
                return pack("REFUSED", "member_provenance_incomplete")
        rows.append({key: row[key] for key in _ARTIFACT})
    rows.sort(key=lambda item: item["filename"].encode("utf-8"))
    doc = {
        "schema": PROVENANCE_SCHEMA,
        "manifest_sha256": manifest_sha256,
        "assembly_recipe_sha256": recipe_sha256,
        "creation_tool_sha256": None,
        "approved_authorities": [],
        "artifacts": rows,
        "owner_approval": None,
    }
    if facts is not None:
        if not isinstance(facts, dict) or set(facts) != {"manifest_sha256", "creation_tool_sha256", "approved_authorities", "owner_approval"}:
            return pack("REFUSED", "member_provenance_incomplete")
        for key in ("manifest_sha256", "creation_tool_sha256"):
            if facts[key] is not None and not _hex64(facts[key]):
                return pack("REFUSED", "member_provenance_incomplete")
        if not isinstance(facts["approved_authorities"], list):
            return pack("REFUSED", "member_provenance_incomplete")
        if facts["owner_approval"] is not None and not isinstance(facts["owner_approval"], dict):
            return pack("REFUSED", "member_provenance_incomplete")
        doc.update(facts)
    ordered = {key: doc[key] for key in PROVENANCE_KEYS}
    from .schema_validate import validate_value
    validated = validate_value(PROVENANCE_SCHEMA,ordered,resources,meter)
    if validated:
        return pack("REFUSED",validated)
    return pack("OBSERVED", None, observation=ordered)
