"""Public held-archive scanner.

scan_retained is the only control consumer. It authenticates a canonical index,
requires NOFOLLOW before/opened/after custody, admits bounded same-FD ranges
before streaming content, then projects snapshot plus provenance from
the members that parse actually returned.
"""

from .bounds import HARD_MAX_ARCHIVE_BYTES, charge, new_meter, performing_requested, reserve
from .broker_map import project_provenance, project_snapshot
from .canonical import canonical_bytes, canonical_loads, canonical_sha256
from .causes import pack
from .deb_archive import scan_deb
from .pins import (
    RETAINED_INDEX_SHA256,
    RETAINED_INDEX_SIZE,
)
from .schema_validate import (
    ADMISSION_SCHEMA,
    HELD_SCHEMA,
    INDEX_SCHEMA,
    RESULT_SCHEMA,
    RETAINED_INDEX_SCHEMA,
    validate_document,
)
from .zip_wheel import scan_wheel

ASSERT_SNAPSHOT = "snapshot"
ASSERT_MATERIALS = "snapshot-materials"
ASSERT_PROVENANCE = "provenance"


def _row_expected(row):
    kind = row.get("archive_class") or row.get("class")
    if kind == "wheel":
        archive_class = "wheel"
    elif kind in ("deb", "ubuntu_minimum", "kernel_qualified"):
        archive_class = "deb"
    else:
        archive_class = kind
    expected = {
        "archive_class": archive_class,
        "name": row.get("name"),
        "version": row.get("version"),
        "filename": row.get("filename"),
        "sha256": row.get("sha256") or row.get("expected_sha256"),
        "size": row.get("size") if "size" in row else row.get("expected_size"),
        "relative_path": row.get("relative_path"),
        "requires_python": row.get("requires_python", row.get("requires_python_raw")),
        "python_tag": row.get("python_tag") or "",
        "abi_tag": row.get("abi_tag") or "",
        "platform_tag": row.get("platform_tag") or "",
        "architecture": row.get("architecture") or "amd64",
        "metadata_raw_sha256": row.get("metadata_raw_sha256"),
    }
    if isinstance(kind, str):
        expected["source_class"] = kind
    if "metadata_content_sha256" in row:
        expected["metadata_content_sha256"] = row.get("metadata_content_sha256")
    identity_keys = ("device", "inode", "mtime_ns", "ctime_ns", "uid", "gid", "full_mode", "nlink")
    nested = row.get("held_identity") if isinstance(row.get("held_identity"), dict) else None
    identity = {}
    for key in identity_keys:
        if key in row:
            identity[key] = row.get(key)
        elif isinstance(nested, dict) and key in nested:
            identity[key] = nested.get(key)
    if identity:
        expected["held_identity"] = identity
    return expected


def _held_from_document(entry):
    """Keep chunk text encoded. Bytes are joined only after admission."""
    return {
        "present": entry.get("present") is True,
        "declared_size": entry.get("declared_size"),
        "relative_path": entry.get("relative_path"),
        "descriptor": entry.get("descriptor"),
        "before": entry.get("before"),
        "opened": entry.get("opened"),
        "after": entry.get("after"),
        "chunks_b64": entry.get("chunks_b64"),
    }


def _project_members(observed):
    observation = observed.get("observation") or {}
    rows = []
    if observation.get("archive_class") == "wheel":
        for item in observation.get("members") or []:
            if isinstance(item, dict):
                copied = dict(item)
                copied["source_domain"] = copied.get("source_domain") or "wheel"
                copied["source_relative_path"] = observation.get("relative_path")
                rows.append(copied)
        return rows
    for domain, key in (("control", "control_members"), ("data", "data_members")):
        for item in observation.get(key) or []:
            if isinstance(item, dict):
                copied = dict(item)
                copied["source_domain"] = domain
                copied["source_relative_path"] = observation.get("relative_path")
                rows.append(copied)
    return rows


def _material_identity(expected_rows, recipe, resources=None, meter=None):
    authority = recipe.get("artifact_authorities") if isinstance(recipe, dict) else None
    supplemental = recipe.get("supplemental_artifacts",[]) if isinstance(recipe,dict) else []
    allocation = reserve(resources,meter,work_bytes=(len(expected_rows)+len(supplemental))*4096,
                         live_bytes=(len(expected_rows)+len(supplemental))*8192)
    if allocation:
        from .digests import DigestStop
        raise DigestStop(allocation)
    artifacts = []
    filenames = set()
    for row in expected_rows:
        item = {"class": row.get("source_class") or row["archive_class"], "filename": row["filename"],
                "format": "zip" if row["archive_class"] == "wheel" else "deb", "tag": row.get("platform_tag") or None,
                "abi": row.get("abi_tag") or None, "size": row["size"], "sha256": row["sha256"],
                "upstream_authority": None, "signed_index_sha256": None, "detached_signature_sha256": None,
                "accepted_signer_fingerprint": None, "verifier_sha256": None}
        if isinstance(authority, list):
            matches = [entry for entry in authority if isinstance(entry, dict) and entry.get("filename") == item["filename"]]
            if len(matches) != 1 or set(matches[0]) != set(item):
                return None
            if any(matches[0][key] != item[key] for key in ("class", "filename", "format", "tag", "abi", "size", "sha256")):
                return None
            item.update(matches[0])
        artifacts.append(item)
        if item["filename"] in filenames:
            return None
        filenames.add(item["filename"])
    for item in supplemental:
        if not isinstance(item,dict) or item.get("filename") in filenames:
            return None
        from .schema_validate import validate_role
        if validate_role("artifact",item,resources,meter):
            return None
        if not isinstance(authority,list) or item not in authority:
            return None
        artifacts.append(dict(item))
        filenames.add(item["filename"])
    if isinstance(authority,list) and (len(authority) != len(artifacts) or
        {item["filename"] for item in authority} != filenames):
        return None
    artifacts.sort(key=lambda item: item["filename"].encode("utf-8"))
    from .contracts import recipe_identity
    identity = recipe_identity(recipe or {})
    return {"artifacts": artifacts, "recipe": identity}


def _apply_assert(observed, expected_rows, mode, recipe_sha256=None, aggregate=None, resources=None, joins=None, meter=None):
    if observed.get("status") != "OBSERVED":
        return observed
    from .causes import enter_phase
    phase = enter_phase(meter,"AGGREGATE_PROVENANCE_AND_CANONICAL_CONSUMER","source_recipe_installed_A009")
    if phase:
        return pack("REFUSED",phase,stage="AGGREGATE_PROVENANCE_AND_CANONICAL_CONSUMER")
    count = sum(len((item.get("observation") or {}).get(k) or []) for item in (aggregate or [observed]) for k in ("members","control_members","data_members"))
    allocation = reserve(resources,meter,work_bytes=count * 4096,live_bytes=count * 32768)
    if allocation:
        return pack("REFUSED",allocation,stage="AGGREGATE_PROVENANCE_AND_CANONICAL_CONSUMER")
    members = _project_members(observed)
    if mode == "duplicate":
        return observed
    observed = dict(observed)
    if isinstance(observed.get("observation"), dict):
        observed["observation"] = dict(observed["observation"])
    materials = None
    if mode == ASSERT_MATERIALS and expected_rows:
        row = expected_rows[0]
        materials = {
            "artifacts": [
                {
                    "filename": row["filename"],
                    "sha256": row["sha256"],
                }
            ]
        }
    if isinstance(aggregate, list) and aggregate:
        members = []
        for item in aggregate:
            members.extend(_project_members(item))
    else:
        members = _project_members(observed)
    dests = {}
    if isinstance(joins, list):
        for join in joins:
            if not isinstance(join, dict):
                continue
            for item in join.get("members") or []:
                if isinstance(item, dict):
                    dests[(join.get("source_relative_path"), item.get("source_domain"), item.get("normalized_path") or item.get("member_path"))] = item.get("intended_destination")
    stamped = []
    recipe_now = resources.get("install_recipe") if isinstance(resources,dict) else None
    if performing_requested(resources):
        from .contracts import installed_inventory
        namespace,inventory_cause = installed_inventory(recipe_now,resources,meter)
        if inventory_cause:
            return pack("REFUSED",inventory_cause,stage="AGGREGATE_PROVENANCE_AND_CANONICAL_CONSUMER")
        meter["installed_inventory"] = namespace
    for member in members:
        if isinstance(member, dict):
            member = dict(member)
            key = (member.get("source_relative_path"), member.get("source_domain"), member.get("normalized_path"))
            if key in dests:
                member["final_destination"] = dests[key]
            recipe_now = resources.get("install_recipe") if isinstance(resources, dict) else None
            installed = recipe_now.get("installed_members") if isinstance(recipe_now, dict) else None
            if (isinstance(installed, list) and member.get("source_domain") != "control" and not
                (member.get("normalized_path") == "." and member.get("type") == "directory")):
                matches = [item for item in installed if isinstance(item, dict) and item.get("path") == member.get("final_destination")]
                if len(matches) != 1:
                    return pack("REFUSED", "member_provenance_incomplete")
                member["installed_observation"] = matches[0]
            if member.get("type") == "directory" and isinstance(recipe_now, dict) and recipe_now.get("unify_directories") is True:
                member["shared_directory"] = True
        stamped.append(member)
    members = [item for item in stamped if item.get("source_domain") != "control" and not
               (item.get("normalized_path") == "." and item.get("type") == "directory")]
    if performing_requested(resources):
        graph_cause = _final_link_graph(members, resources, meter)
        if graph_cause:
            return pack("REFUSED", graph_cause, stage="AGGREGATE_PROVENANCE_AND_CANONICAL_CONSUMER")
        for member in members:
            for join in (joins or []):
                if join["source_relative_path"] != member["source_relative_path"]:
                    continue
                for item in join["members"]:
                    if (item["source_domain"],item["normalized_path"]) == (member["source_domain"],member["normalized_path"]):
                        item["installed_observation"] = member["installed_observation"]
                        if "final_link_terminal" in member:
                            item["final_link_terminal"] = member["final_link_terminal"]
                        if "resolved_link" in member:
                            item["resolved_link"] = member["resolved_link"]
    candidate = resources.get("selected_candidate") if isinstance(resources, dict) else None
    recipe_now = resources.get("install_recipe") if isinstance(resources, dict) else None
    snapshot_facts = recipe_now.get("snapshot_facts") if isinstance(recipe_now, dict) else None
    if isinstance(recipe_now, dict):
        candidate = recipe_now.get("candidate", candidate)
    if performing_requested(resources):
        materials = _material_identity(expected_rows, recipe_now,resources,meter)
        if materials is None:
            return pack("REFUSED", "member_provenance_incomplete")
    created = recipe_now.get("created_directories") if isinstance(recipe_now,dict) and performing_requested(resources) else None
    supplemental = recipe_now.get("supplemental_bindings") if performing_requested(resources) else None
    snapshot = project_snapshot(members, materials_projection=materials, candidate=candidate, facts=snapshot_facts, resources=resources, meter=meter,created_directories=created,supplemental_bindings=supplemental)
    artifacts = []
    for row in expected_rows:
        artifacts.append(
            {
                "class": row.get("source_class") or row["archive_class"],
                "filename": row["filename"],
                "format": "zip" if row["archive_class"] == "wheel" else "deb",
                "tag": row.get("platform_tag") or None,
                "abi": row.get("abi_tag") or None,
                "size": row["size"],
                "sha256": row["sha256"],
            }
        )
    if performing_requested(resources):
        artifacts = materials["artifacts"]
    authority_rows = recipe_now.get("artifact_authorities") if isinstance(recipe_now, dict) else None
    if isinstance(authority_rows, list):
        for artifact in artifacts:
            matches = [item for item in authority_rows if isinstance(item, dict) and item.get("filename") == artifact["filename"] and item.get("sha256") == artifact["sha256"]]
            if len(matches) != 1:
                return pack("REFUSED", "member_provenance_incomplete")
            if any(matches[0].get(key) != artifact.get(key) for key in ("class", "filename", "format", "tag", "abi", "size", "sha256")):
                return pack("REFUSED", "member_provenance_incomplete")
            artifact.update(matches[0])
    provenance_facts = recipe_now.get("provenance_facts") if isinstance(recipe_now, dict) else None
    provenance = project_provenance(artifacts, recipe_sha256=recipe_sha256, facts=provenance_facts,resources=resources,meter=meter)
    if snapshot.get("status") != "OBSERVED":
        return snapshot
    if provenance.get("status") != "OBSERVED":
        return provenance
    if performing_requested(resources):
        from .contracts import provenance_relations
        relations = provenance_relations(snapshot["observation"],provenance["observation"],recipe_now,resources,meter)
        if relations:
            return pack("REFUSED",relations,stage="AGGREGATE_PROVENANCE_AND_CANONICAL_CONSUMER")
    observed["snapshot"] = snapshot.get("observation")
    observed["provenance"] = provenance.get("observation")
    if performing_requested(resources):
        expected_paths = set(meter["installed_inventory"])-{""}
        if {row["path"] for row in observed["snapshot"]["members"]} != expected_paths:
            return pack("REFUSED","member_provenance_incomplete",stage="AGGREGATE_PROVENANCE_AND_CANONICAL_CONSUMER")
        if meter["stock_context"]["roster"]["purpose"] == "retained-202-whole":
            if len(artifacts) != 350 or len(observed["snapshot"]["members"]) != 512:
                return pack("REFUSED","member_provenance_incomplete",stage="AGGREGATE_PROVENANCE_AND_CANONICAL_CONSUMER")
        observed["final_namespace"] = [meter["installed_inventory"][path] for path in sorted(meter["installed_inventory"],key=lambda x:x.encode("utf-8"))]
    observed["rootfs_nlink_actual"] = "NOT_PROVEN"
    if mode == ASSERT_SNAPSHOT or mode == ASSERT_MATERIALS:
        observed["observation"] = snapshot["observation"]
    elif mode == ASSERT_PROVENANCE:
        observed["observation"] = provenance["observation"]
    return observed


def _final_link_graph(members, resources, meter):
    """Resolve literal symlinks in the selected final namespace without filesystem effects."""
    nodes = {}
    selected = meter.get("installed_inventory",{})
    tick = reserve(resources, meter, work_bytes=(len(members)+len(selected)) * 512, live_bytes=(len(members)+len(selected)) * 1024)
    if tick:
        return tick
    for path,row in selected.items():
        nodes[path] = {"final_destination":path,"type":row["type"],"size":row["size"],
            "content_sha256":row["sha256"],"link_target":row["link_target"],"link_target_sha256":row["link_target_sha256"]}
    seen_source = {}
    for member in members:
        path = member.get("final_destination")
        if not isinstance(path, str) or not path:
            return "member_provenance_incomplete"
        prior = seen_source.get(path)
        if prior is not None and not (prior.get("type") == member.get("type") == "directory"):
            return "duplicate_member_provenance"
        seen_source[path] = member
        if path not in nodes:
            return "member_provenance_incomplete"
    for member in members:
        if member.get("type") == "hardlink":
            resolved = member.get("resolved_link")
            installed = nodes[member["final_destination"]]
            if not isinstance(resolved,dict) or installed["type"] != "regular" or (resolved["size"],resolved["sha256"]) != (installed["size"],installed["content_sha256"]):
                return "member_provenance_incomplete"
            member["final_link_terminal"] = {"path":member["final_destination"],"type":"regular",
                "size":installed["size"],"sha256":installed["content_sha256"]}
        if member.get("type") != "symlink":
            continue
        target = member.get("link_target")
        if not isinstance(target, str) or not target or "\x00" in target or "\\" in target:
            return "symlink_recipe_unbound"
        pending = target.split("/")
        parts = [] if target.startswith("/") else member["final_destination"].split("/")[:-1]
        seen = set()
        for _step in range((len(nodes) + 1) * 257):
            tick = reserve(resources, meter, work_bytes=1024, live_bytes=256)
            if tick:
                return tick
            if not pending:
                resolved = nodes.get("/".join(parts))
                if not isinstance(resolved, dict):
                    return "symlink_recipe_unbound"
                member["final_link_terminal"] = {"path": resolved["final_destination"], "type": resolved["type"],
                    "size": resolved.get("size"), "sha256": resolved.get("content_sha256") or resolved.get("link_target_sha256")}
                break
            part = pending.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    return "symlink_recipe_unbound"
                parts.pop()
                continue
            path = "/".join(parts + [part])
            resolved = nodes.get(path)
            if not isinstance(resolved, dict):
                return "symlink_recipe_unbound"
            if resolved.get("type") == "symlink":
                if path in seen:
                    return "symlink_recipe_unbound"
                seen.add(path)
                target = resolved.get("link_target")
                if not isinstance(target, str) or not target or "\x00" in target or "\\" in target:
                    return "symlink_recipe_unbound"
                tick = reserve(resources, meter, work_bytes=len(target), live_bytes=len(target) * 32)
                if tick:
                    return tick
                pending = target.split("/") + pending
                if target.startswith("/"):
                    parts = []
            else:
                if pending and resolved.get("type") != "directory":
                    return "symlink_recipe_unbound"
                parts.append(part)
        else:
            return "symlink_recipe_unbound"
    return None


def _class_missing(row):
    if not isinstance(row, dict):
        return True
    return row.get("archive_class") is None and row.get("class") is None


def _component(expected):
    kind = expected.get("source_class") or expected.get("archive_class")
    if kind == "wheel":
        return "python"
    if kind == "kernel_qualified":
        return "kernel"
    if kind == "ubuntu_minimum":
        return "ubuntu"
    return "debian"




def _json_preflight(raw, max_bytes, max_depth):
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > max_bytes:
        return "canonical_ingress_refused"
    depth = 0
    count = 0
    in_string = False
    escape = False
    for byte in raw:
        if in_string:
            if escape:
                escape = False
                continue
            if byte == 92:
                escape = True
                continue
            if byte == 34:
                in_string = False
            continue
        if byte == 34:
            in_string = True
            continue
        if byte in (123, 91):
            depth += 1
            count += 1
            if depth > max_depth or count > max_bytes:
                return "canonical_ingress_refused"
        elif byte in (125, 93):
            depth -= 1
            if depth < 0:
                return "canonical_ingress_refused"
    if in_string or depth != 0:
        return "canonical_ingress_refused"
    return None


def _scan_rows(rows, held_entries, resources, publisher_evidence=None, mode=None, meter=None):
    from .context import effects_cause
    effects = effects_cause(resources, meter)
    if effects:
        return pack("REFUSED", effects)
    if not isinstance(meter, dict):
        meter = new_meter()
    if not isinstance(rows, list) or not rows:
        return pack("REFUSED", "expected_shape_invalid")
    if _class_missing(rows[0]):
        return pack("REFUSED", "expected_shape_invalid")
    tick = reserve(resources, meter, work_bytes=len(rows) * 4096, live_bytes=len(rows) * 8192)
    if tick:
        return pack("REFUSED", tick)
    expected_rows = [_row_expected(row) for row in rows]
    companion = meter.get("content_companion") if isinstance(meter, dict) else None
    if isinstance(companion, dict):
        for expected in expected_rows:
            pin = companion.get(expected["relative_path"])
            if not isinstance(pin, dict):
                return pack("REFUSED", "ingress_context_unbound")
            expected["metadata_content_sha256"] = pin["metadata_content_sha256"]
            expected["metadata_raw_sha256"] = pin["publisher_raw_sha256"]
    first = expected_rows[0]
    if first.get("archive_class") not in ("wheel", "deb"):
        return pack("REFUSED", "unknown_archive_class", detail={"archive_class": first.get("archive_class")})
    seen_selected = set()
    for expected in expected_rows:
        path = expected.get("relative_path")
        if path in seen_selected:
            return pack("REFUSED", "custody_membership_changed", detail={"relative_path": path})
        seen_selected.add(path)
    seen_held = set()
    by_path = {}
    for entry in held_entries:
        if not isinstance(entry, dict):
            return pack("REFUSED", "custody_record_invalid")
        path = entry.get("relative_path")
        if path in seen_held:
            return pack("REFUSED", "custody_membership_changed", detail={"relative_path": path})
        seen_held.add(path)
        by_path[path] = entry
    for path in seen_held:
        if path not in seen_selected:
            return pack("REFUSED", "custody_membership_changed", detail={"relative_path": path})
    scanned = []
    joins = []
    last = None
    if not isinstance(meter, dict):
        meter = new_meter()
    tick = reserve(resources, meter, work_bytes=max(1, len(expected_rows)) * 4096)
    if tick is not None:
        return pack("REFUSED", tick)
    for expected in expected_rows:
        entry = by_path.get(expected.get("relative_path"))
        if entry is None:
            return pack("NOT_PROVEN", "held_bytes_absent", detail={"relative_path": expected.get("relative_path")})
        held = _held_from_document(entry)
        if expected["archive_class"] == "wheel":
            last = scan_wheel(expected, held, resources, publisher_evidence, meter)
        else:
            last = scan_deb(expected, held, resources, publisher_evidence, meter)
        post = meter.get("post_scan_cause") if isinstance(meter, dict) else None
        if isinstance(meter, dict):
            meter.pop("post_scan_cause", None)
        if post and last.get("status") == "OBSERVED":
            return pack("REFUSED", post)
        if last.get("status") != "OBSERVED":
            return last
        if performing_requested(resources):
            from .custody import complete_material_output
            receipt, receipt_cause = complete_material_output(last,expected,resources,meter)
            if receipt_cause:
                return pack("REFUSED",receipt_cause,stage="FINAL_SCHEMA_OUTPUT_AND_CUSTODY")
        scanned.append(last)
        observed = last.get("observation") or {}
        member_count = len(observed.get("members") or []) + len(observed.get("control_members") or []) + len(observed.get("data_members") or [])
        allocation = reserve(resources, meter, work_bytes=member_count * 1024, live_bytes=member_count * 16384)
        if allocation:
            return pack("REFUSED", allocation)
        member_rows = _project_members(last)
        component = _component(expected)
        joins.append(
            {
                "source_relative_path": expected.get("relative_path"),
                "archive_sha256": expected.get("sha256"),
                "archive_class": expected.get("archive_class"),
                "source_class": expected.get("source_class"),
                "source_component": component,
                "content_sha256": expected.get("sha256"),
                "size": expected.get("size"),
                "members": [
                    {
                        "member_path": item.get("member_path") or item.get("normalized_path"),
                        "normalized_path": item.get("normalized_path") or item.get("member_path"),
                        "member_type": item.get("type"),
                        "link_target": item.get("link_target"),
                        "content_sha256": item.get("content_sha256"),
                        "size": item.get("size"),
                        "uid": item.get("uid"),
                        "gid": item.get("gid"),
                        "mode": item.get("mode"),
                        "mtime": item.get("mtime"),
                        "source_component": component,
                        "intended_destination": None,
                        "source_domain": item.get("source_domain"),
                        "rootfs_nlink_actual": "NOT_PROVEN",
                    }
                    for item in member_rows
                    if isinstance(item, dict)
                ],
            }
        )
    recipe = resources.get("install_recipe") if isinstance(resources, dict) else None
    if performing_requested(resources):
        declared = {(item["source_relative_path"], item["source_domain"], item["source_path"]) for item in recipe["destinations"]}
        actual = {(join["source_relative_path"], item["source_domain"], item["normalized_path"]) for join in joins for item in join["members"]}
        if declared != actual:
            return pack("REFUSED", "member_provenance_incomplete")

    def _final_destination(join, member):
        if not isinstance(recipe, dict) or member.get("source_domain") not in ("data", "wheel", "control"):
            return None
        mapping = recipe.get("destinations")
        path = member.get("normalized_path") or member.get("member_path")
        domain = member.get("source_domain")
        if not isinstance(mapping, list) or not isinstance(path, str):
            return None
        matches = [item for item in mapping if isinstance(item, dict) and
                   item.get("source_relative_path") == join["source_relative_path"] and
                   item.get("source_domain") == domain and item.get("source_path") == path]
        if len(matches) != 1:
            return None
        selected = matches[0]
        if domain == "control":
            return None
        dest = selected.get("destination")
        if selected.get("action") != "install" or not isinstance(dest, str) or dest == "" or dest.startswith("/") or any(part in ("", ".", "..") for part in dest.split("/")):
            return None
        return dest

    if mode == "duplicate":
        seen = set()
        for item in scanned:
            for member in _project_members(item):
                key = (member.get("source_domain"), member.get("normalized_path"))
                if key in seen and member.get("type") != "directory":
                    return pack("REFUSED", "duplicate_member_provenance", detail={"path": key[1], "domain": key[0]})
                seen.add(key)
        return pack("REFUSED", "duplicate_member_not_reached")
    seen_dest = {}
    for join in joins:
        for member in join["members"]:
            dest = _final_destination(join, member)
            member["intended_destination"] = dest
            if dest is None:
                if (performing_requested(resources) and member.get("source_domain") != "control" and not
                    (member.get("normalized_path") == "." and member.get("member_type") == "directory")):
                    return pack("REFUSED", "member_provenance_incomplete")
                continue
            unify = isinstance(recipe, dict) and recipe.get("unify_directories") is True
            compatible = (member.get("member_type"), member.get("uid"), member.get("gid"), member.get("mode"), member.get("mtime"), member.get("size"), member.get("content_sha256"))
            if member.get("member_type") == "directory" and unify:
                if seen_dest.get(dest) not in (None, compatible):
                    return pack("REFUSED", "duplicate_member_provenance", detail={"path": dest})
                seen_dest[dest] = compatible
                continue
            if dest in seen_dest:
                return pack("REFUSED", "duplicate_member_provenance", detail={"path": dest})
            seen_dest[dest] = compatible
    recipe_sha = recipe.get("sha256") if isinstance(recipe, dict) else None
    result = _apply_assert(last, expected_rows, mode, recipe_sha, scanned, resources, joins, meter)
    if isinstance(result, dict) and result.get("status") == "OBSERVED":
        identities = [
            {
                "class": row.get("source_class") or row.get("archive_class"),
                "filename": row.get("filename"),
                "sha256": row.get("sha256"),
                "size": row.get("size"),
                "relative_path": row.get("relative_path"),
            }
            for row in expected_rows
        ]
        material_identity = _material_identity(expected_rows, recipe,resources,meter)
        if material_identity is None:
            return pack("REFUSED", "member_provenance_incomplete")
        from .canonical import canonical_sha256_metered
        material_sha, material_cause = canonical_sha256_metered(material_identity, resources, meter)
        if material_cause:
            return pack("REFUSED", material_cause)
        result["whole_materials_sha256"] = material_sha
        projection_sha, projection_cause = canonical_sha256_metered(
            {
                "artifacts": identities,
                "authority": "pending",
                "provenance": result.get("provenance"),
                "recipe_sha256": recipe_sha,
                "snapshot": result.get("snapshot"),
                "source_joins": joins,
                "context": {key: value for key, value in (meter.get("stock_context") or {}).items() if key not in ("output", "recipe")},
            }, resources, meter
        )
        if projection_cause:
            return pack("REFUSED", projection_cause)
        result["whole_projection_sha256"] = projection_sha
        result["stage"] = "AGGREGATE_PROVENANCE_AND_CANONICAL_CONSUMER"
        result["archive_observations"] = [
            {
                "relative_path": (item.get("observation") or {}).get("relative_path") if isinstance(item.get("observation"), dict) else None,
                "archive_sha256": (item.get("observation") or {}).get("whole_sha256") if isinstance(item.get("observation"), dict) else None,
                "status": item.get("status"),
                "cause": item.get("cause"),
                "stage": item.get("stage"),
                "observation": item.get("observation") if isinstance(item.get("observation"), dict) else None,
            }
            for item in scanned
        ]
        result["source_joins"] = joins
        if performing_requested(resources):
            result["material_output_receipts"] = meter["material_output_receipts"]
    return result


def scan_rows(rows, held_entries, resources, publisher_evidence=None, mode=None, meter=None):
    if isinstance(meter,dict):
        return _scan_rows(rows,held_entries,resources,publisher_evidence,mode,meter)
    owned = new_meter()
    owned["active_resources"] = resources
    from .digests import DigestStop
    from .compression import BudgetStop
    try:
        result = _scan_rows(rows,held_entries,resources,publisher_evidence,mode,owned)
    except (DigestStop,BudgetStop,MemoryError,OSError,ValueError,UnicodeError,KeyError,TypeError,IndexError,RuntimeError) as exc:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,exc)
        result = exception_owned_result(owned,exc)
    return finish_owned(result,resources,owned)


def _held_json_cap(admission):
    return 16777216


def _finish(result, resources=None, meter=None):
    from .canonical import canonical_bytes_bounded
    from .bounds import reserve, terminal_meter, TERMINAL_OUTPUT_CAP, release_live
    if meter is None:
        meter = new_meter()
    meter["pending_terminal_result"]=result
    from .causes import enter_phase
    previous_stage = meter.get("reached_stage")
    def refuse(cause,stage=None,detail=None):
        from .causes import causal_refusal
        return causal_refusal(result,cause,meter,stage,failure_detail=detail)
    tick = enter_phase(meter,"FINAL_SCHEMA_OUTPUT_AND_CUSTODY","final_schema_and_encoding")
    if tick:
        result = refuse(tick,previous_stage or "AUTHENTICATED_INGRESS")
    if result.get("status") != "OBSERVED" and previous_stage is not None:
        result["stage"] = previous_stage
    if result.get("status")!="OBSERVED": result["phase_trace"]=meter.get("phase_trace",[])
    from .bounds import reserve_terminal_causal
    capacity_cause = reserve_terminal_causal(resources,meter,result)
    if capacity_cause:
        result=refuse(capacity_cause,"FINAL_SCHEMA_OUTPUT_AND_CUSTODY",
            {"reservation":"complete_causal_terminal_graph","capacity":"NOT_ADMITTED"})
    if performing_requested(resources):
        if result.get("status") == "OBSERVED":
            from .contracts import material_receipt_relations
            receipt_cause = material_receipt_relations(result,resources,meter)
            if receipt_cause:
                result = refuse(receipt_cause,"FINAL_SCHEMA_OUTPUT_AND_CUSTODY")
        result["phase_trace"] = meter["phase_trace"]
        if result.get("status") == "OBSERVED":
            from .canonical import canonical_sha256_metered
            payload_sha,payload_cause = canonical_sha256_metered(result,resources,meter)
            if payload_cause:
                result = refuse(payload_cause,"FINAL_SCHEMA_OUTPUT_AND_CUSTODY")
            else:
                from .custody import RootHolderClient
                root_client=meter.get('root_holder_client')
                if isinstance(root_client,RootHolderClient):
                    prepared=root_client.prepare(payload_sha)
                    fields=('device','inode','full_mode','uid','gid','nlink','size','mtime_ns','ctime_ns')
                    archives=[{'relative_path':item['relative_path'],
                        'identity':{key:int(value) for key,value in zip(fields,item['identity9'])},
                        'sha256':item['sha256'],'named_path_checked':True}
                        for item in prepared['archives']]
                    receipts=meter.get('material_output_receipts',[])
                    expected_archives=[{'relative_path':item['relative_path'],'identity':item['identity'],
                        'sha256':item['archive_sha256'],'named_path_checked':True} for item in receipts]
                    if archives!=expected_archives:
                        from .digests import DigestStop
                        raise DigestStop('custody_identity_changed')
                    result['final_custody']={'payload_sha256':payload_sha,
                        'completion':'ROOT_ORIGINAL_GENERATIONS_HELD_THROUGH_EXTERNAL_COMMIT',
                        'archive_count':202,'archives':archives,
                        'root_pid':prepared['root_pid'],'generation':prepared['generation'],
                        'owner':'actual-root-native-tool','source_max_fds':16,'canonical_workers':4}
                else:
                    result["final_custody"] = {"payload_sha256":payload_sha,
                    "completion":"COMPOSED_PER_MATERIAL_RECEIPTS_NOT_SIMULTANEOUS_CUSTODY",
                    "archive_count":len(meter.get("material_output_receipts",[])),
                    "archives":[{"relative_path":receipt["relative_path"],"identity":receipt["identity"],
                        "sha256":receipt["archive_sha256"],"named_path_checked":True}
                        for receipt in meter.get("material_output_receipts",[])]}
    cause = validate_document(result, RESULT_SCHEMA, resources, meter)
    result=_with_origin_successor(result,meter)
    if cause is not None:
        from .causes import causal_refusal
        result=causal_refusal(result,cause,meter,"FINAL_SCHEMA_OUTPUT_AND_CUSTODY",
            failure_detail={"schema":RESULT_SCHEMA,"full_leaf_origins":meter.get('origin_failures',[])[:]})
        # The refusal is a new graph, not the previously validated graph.
        # Validate it on the fixed prepaid lane even after ordinary admission
        # has failed; an invalid successor is STOP_UNCONFIRMED, not wire DATA.
        if reserve_terminal_causal(resources,meter,result):
            raise RuntimeError('STOP_UNCONFIRMED: full schema-origin successor capacity unavailable')
        successor_lane=terminal_meter(meter)
        if successor_lane is None or validate_document(result,RESULT_SCHEMA,None,successor_lane) is not None:
            raise RuntimeError('STOP_UNCONFIRMED: strict schema successor invalid or unavailable')
    elif meter.get('terminal_origin_count',0):
        # A real metadata/schema origin may have been reached by validation
        # itself. Its full successor must be checked before its first encoding.
        if reserve_terminal_causal(resources,meter,result):
            raise RuntimeError('STOP_UNCONFIRMED: full origin successor capacity unavailable')
        successor_lane=terminal_meter(meter)
        if successor_lane is None or validate_document(result,RESULT_SCHEMA,None,successor_lane) is not None:
            raise RuntimeError('STOP_UNCONFIRMED: full origin successor schema unavailable')
    ceilings = resources.get("ceilings") if isinstance(resources, dict) else None
    cap = ceilings.get("max_output_bytes", 67108864) if isinstance(ceilings, dict) else 67108864
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 8192:
        cap = 8192
    room = max(0,cap - meter.get("output_bytes",0) - TERMINAL_OUTPUT_CAP)
    blob = canonical_bytes_bounded(result, room, resources, meter)
    stock = meter.get("stock_context") if isinstance(meter, dict) else None
    if blob is not None and result.get("status") == "OBSERVED" and isinstance(stock, dict):
        from .digests import content_sha256, DigestStop
        try:
            output_digest = content_sha256(blob,resources,meter)
        except DigestStop as stop:
            from tools.native_support import retain_source_origin
            retain_source_origin(None,stop)
            output_digest = None
            amount=len(blob); blob=None
            release_live(meter,amount)
            result = refuse(stop.cause,"FINAL_SCHEMA_OUTPUT_AND_CUSTODY")
        if blob is not None and output_digest != stock["output"]["expected_output_sha256"]:
            result = refuse("oracle_not_pinned","AGGREGATE_PROVENANCE_AND_CANONICAL_CONSUMER")
            amount=len(blob); blob=None
            release_live(meter,amount)
            successor_lane=terminal_meter(meter)
            if successor_lane is None or validate_document(result,RESULT_SCHEMA,None,successor_lane) is not None:
                raise RuntimeError('STOP_UNCONFIRMED: strict output-oracle successor invalid or unavailable')
            blob = canonical_bytes_bounded(result, room, resources, meter)
    if blob is not None:
        charged = reserve(resources,meter,output_bytes=len(blob))
        if charged:
            amount=len(blob); blob=None
            release_live(meter,amount)
            result = refuse(charged,"FINAL_SCHEMA_OUTPUT_AND_CUSTODY")
    if blob is not None and result.get('status')=='OBSERVED' and meter.get('root_holder_client') is not None:
        # Root's receipt is external, hashing these EXACT bytes while all202
        # original Root descriptors remain held. No Source metadata is a grant.
        meter['root_holder_client'].commit(blob)
    if blob is None:
        if result.get("status") == "OBSERVED":
            result = refuse("resource_ceiling_exceeded","FINAL_SCHEMA_OUTPUT_AND_CUSTODY")
        # Never erase an earlier detail/phase just to fit a terminal envelope.
        result["phase_trace"]=meter.get("phase_trace",[])
        # Capacity is for the exact reached chain, including any successor
        # formed by schema/hash/encoding/output admission. There is no fixed
        # 8192 projection and no deletion of earlier causal evidence.
        if reserve_terminal_causal(resources,meter,result):
            raise RuntimeError('STOP_UNCONFIRMED: full causal successor not admitted by original terminal reserve')
        lane = terminal_meter(meter)
        if lane is None:
            raise ValueError("terminal_reserve_already_consumed")
        strict=validate_document(result,RESULT_SCHEMA,None,lane)
        if strict is not None:
            raise RuntimeError('STOP_UNCONFIRMED: complete terminal successor schema: '+strict)
        blob = canonical_bytes_bounded(result,lane["terminal_limits"]["output_bytes"]-lane["output_bytes"],None,lane)
        if blob is None:
            raise ValueError("terminal_output_budget_unrepresentable")
        if reserve(None,lane,output_bytes=len(blob)):
            raise ValueError("terminal_output_budget_unrepresentable")
    previous = meter.pop("final_output_bytes",None)
    if previous is not None:
        amount=len(previous)
        owner=meter.get("terminal_lane_meter") if meter.get("final_output_in_terminal") else meter
        previous=None
        release_live(owner,amount)
    meter["final_output_bytes"] = blob
    meter["final_output_in_terminal"] = meter.get("terminal_spent") is True
    meter["returned_output_bytes"] = len(blob)
    meter["encoded_output_bytes"] = meter.get("encoded_output_bytes",0) + len(blob)
    return result


def _scan_retained(index_raw, held_raw, admission_raw, stock_context, meter):
    from .causes import enter_phase
    phase = enter_phase(meter,"AUTHENTICATED_INGRESS","canonical_ingress")
    if phase:
        return pack("REFUSED",phase,stage="AUTHENTICATED_INGRESS")
    from .bounds import bootstrap_reserve
    for raw in (admission_raw, index_raw, held_raw):
        if not isinstance(raw, (bytes, bytearray)):
            return pack("REFUSED", "canonical_ingress_refused")
        early = bootstrap_reserve(meter, len(raw))
        if early:
            return pack("REFUSED", early)
    if isinstance(admission_raw, (bytes, bytearray)):
        early = charge(None, meter, work_bytes=len(admission_raw))
        if early is not None:
            return pack("REFUSED", early)
    cause = _json_preflight(admission_raw, 1000000, 16)
    if cause is not None:
        return pack("REFUSED", cause)
    try:
        admission = canonical_loads(admission_raw, max_bytes=1000000, max_depth=16)
    except ValueError as exc:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,exc)
        return pack("REFUSED", "canonical_ingress_refused", detail={"cause": str(exc)})
    cause = validate_document(admission, ADMISSION_SCHEMA, meter=meter)
    if cause is not None:
        return pack("REFUSED", cause)
    held_cap = _held_json_cap(admission)
    cause = _json_preflight(index_raw, 2000000, 16)
    if cause is None:
        cause = _json_preflight(held_raw, held_cap, 16)
    if cause is not None:
        return pack("REFUSED", cause)
    resources = admission.get("resources") if isinstance(admission, dict) else None
    meter["active_resources"] = resources
    tick = charge(resources if isinstance(resources, dict) else None, meter)
    if tick is not None:
        return pack("REFUSED", tick)
    tick = reserve(resources, meter, read_bytes=len(admission_raw), work_bytes=len(index_raw) + len(held_raw) + len(admission_raw))
    if tick is not None:
        return pack("REFUSED", tick)
    try:
        index = canonical_loads(index_raw, max_bytes=2000000, max_depth=16)
        held = canonical_loads(held_raw, max_bytes=held_cap, max_depth=16)
    except ValueError as exc:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,exc)
        return pack("REFUSED", "canonical_ingress_refused", detail={"cause": str(exc)})
    from .digests import content_sha256
    raw_sha = content_sha256(index_raw,resources,meter)
    exact_retained = raw_sha == RETAINED_INDEX_SHA256 and len(index_raw) == RETAINED_INDEX_SIZE
    role = admission.get("index_role") if isinstance(admission, dict) else None
    if role == "retained-202" or exact_retained:
        if role != "retained-202":
            return pack("REFUSED", "ingress_context_unbound")
        from .canonical import canonical_sha256_metered
        canonical_index_sha,hash_cause = canonical_sha256_metered(index,resources,meter)
        if hash_cause:
            return pack("REFUSED",hash_cause)
        if not exact_retained or canonical_index_sha != RETAINED_INDEX_SHA256:
            return pack("REFUSED", "oracle_not_pinned")
        if isinstance(index, dict) and "oracle_id" in index:
            return pack("REFUSED", "schema_rejected")
        cause = validate_document(index, RETAINED_INDEX_SCHEMA, resources, meter)
        if cause is not None:
            return pack("REFUSED", cause)
    else:
        if role not in (None, "scan-index"):
            return pack("REFUSED", "ingress_context_unbound")
        cause = validate_document(index, INDEX_SCHEMA, resources, meter)
        if cause is not None:
            return pack("REFUSED", cause)
    cause = validate_document(held, HELD_SCHEMA, resources, meter)
    if cause is not None:
        return pack("REFUSED", cause)
    rows = index.get("rows") if isinstance(index, dict) else None
    entries = held.get("files") if isinstance(held, dict) else None
    evidence = admission.get("publisher_evidence") if isinstance(admission, dict) else None
    mode = admission.get("assert_document") if isinstance(admission, dict) else None
    from .context import bind_stock
    plan_cause = bind_stock(stock_context, admission, index_raw, rows or [], meter)
    if plan_cause is not None:
        return pack("REFUSED", plan_cause)
    if performing_requested(resources):
        from .contracts import bind_independent_output
        bound = bind_independent_output(meter["stock_context"]["output"],meter["input_binding"],
                                        index_raw,held_raw,admission_raw,resources,meter)
        if bound:
            return pack("REFUSED",bound,stage="AUTHENTICATED_INGRESS")
    if mode == "filename-census":
        return pack("OBSERVED", None, observation=census_wheel_filenames(rows or []), stage="AUTHENTICATED_INGRESS")
    return scan_rows(rows, entries or [], resources, evidence, mode, meter)


def scan_retained(index_raw, held_raw, admission_raw, stock_context=None):
    return _run_owned(index_raw,held_raw,admission_raw,stock_context)[0]


def scan_retained_bytes(index_raw, held_raw, admission_raw, stock_context=None):
    """Exact canonical bytes belong to the same finishing owner and budget."""
    return _run_owned(index_raw,held_raw,admission_raw,stock_context)[1]


def _run_owned(index_raw, held_raw, admission_raw, stock_context=None):
    """One entry, one shared meter and one custody completion for every route."""
    from tools.native_support import source_raw_owner,leave_source_scope
    raw_owner=source_raw_owner()
    meter=None
    try:
        from .custody import complete_custody
        meter = new_meter(raw_owner)
        result = None
        from .digests import DigestStop
        from .compression import BudgetStop
        try:
            result = _scan_retained(index_raw, held_raw, admission_raw, stock_context, meter)
        except (DigestStop,BudgetStop,MemoryError,OSError,ValueError,UnicodeError,KeyError,TypeError,IndexError,RuntimeError) as exc:
            from tools.native_support import retain_source_origin
            retain_source_origin(None,exc)
            result = exception_owned_result(meter,exc)
        resources = meter.get("active_resources")
        result = finish_owned(result,resources,meter)
        from tools.native_support import transfer_result
        return transfer_result(result,meter['final_output_bytes'],meter)

    except BaseException as exc:
        raw_owner.capture(exc)
        raise
    finally:
        if meter is not None:leave_source_scope(meter)


def exception_owned_result(meter,exc):
    from .digests import DigestStop
    from .compression import BudgetStop
    from .causes import causal_refusal,source_exception_detail
    from tools.native_support import retain_source_origin
    retain_source_origin(meter,exc)
    cause=exc.cause if isinstance(exc,(DigestStop,BudgetStop)) else 'resource_ceiling_exceeded' if isinstance(exc,MemoryError) else 'nofollow_open_failed' if isinstance(exc,OSError) else 'expected_shape_invalid'
    primary=meter.get('pending_terminal_result')
    if not isinstance(primary,dict): primary=pack('REFUSED',cause,stage=meter.get('reached_stage'))
    return causal_refusal(primary,cause,meter,exception_class=type(exc).__name__,
        failure_detail=source_exception_detail(meter,exc))


def _with_origin_successor(result,meter):
    """Consume first catches AND origins subsequently reached during cleanup.

    Records are joined before each final encoding. The actual exception objects
    remain in the meter's owned graph; schema evidence contains their full facts.
    """
    origins=meter.get('origin_failures',[])
    consumed=meter.get('terminal_origin_count',0)
    if len(origins)==consumed:return result
    from .causes import causal_refusal
    result=causal_refusal(result,result.get('cause') or 'held_fd_unretained',meter,
        failure_detail={'full_leaf_origins':origins[:]})
    meter['terminal_origin_count']=len(origins)
    return result


def finish_owned(result,resources,meter):
    """Uniform lease/schema/encoder/error cleanup owner for all public routes."""
    from .custody import complete_custody
    from tools.native_support import begin_terminal
    begin_terminal(meter)
    result=_with_origin_successor(result,meter)
    native=meter.get('native_owner')
    if native is not None:
        physical=native.snapshot()
        if physical['failed'] or physical['uncertain_fds']:
            from .causes import causal_refusal
            result=causal_refusal(result,'resource_ceiling_exceeded' if physical['failed'] else 'held_fd_unretained',meter,
                failure_detail={'native_owner':physical,'observation':'actual preinitialization owner failed; no observed credit'})
    # A whole aggregate has no archive leases: each complete observation already
    # finished its separately selected held generation.  Unexpected leftovers
    # are a refusal, never a simultaneous-custody success or a cap increase.
    unexpected = performing_requested(resources) and (meter.get("custody_leases") or "retained_fd" in meter)
    from .digests import DigestStop
    from .compression import BudgetStop
    try:
        from .bounds import reserve_terminal_causal
        capacity=reserve_terminal_causal(resources,meter,result)
        if capacity:
            from .causes import causal_refusal
            result=causal_refusal(result,capacity,meter,
                failure_detail={"reservation":"before_terminal_custody","capacity":"NOT_ADMITTED"})
        terminal = complete_custody(resources, meter, release=True)
        if unexpected: terminal=terminal or "held_fd_unretained"
        if terminal is not None:
            from .causes import causal_refusal
            result=causal_refusal(result,terminal,meter,"HELD_CUSTODY_AND_ADMISSION",cleanup_causes=[terminal])
        result=_finish(_with_origin_successor(result,meter),resources,meter)
        client=meter.get('root_holder_client')
        if client is not None:
            client.close()
            if meter.get('fd_close_uncertainties'):
                from .causes import causal_refusal
                result=_finish(_with_origin_successor(causal_refusal(result,'held_fd_unretained',meter,
                    cleanup_causes=['held_fd_unretained']),meter),resources,meter)
        from tools.native_support import own_map
        return own_map(result,meter)
    except (DigestStop,BudgetStop,MemoryError,OSError,ValueError,UnicodeError,KeyError,TypeError,IndexError,RuntimeError) as exc:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,exc)
        from tools.native_support import own_map
        return own_map(_finish_exception_refusal(meter.get("pending_terminal_result",result),resources,meter,exc),meter)


def _finish_exception_refusal(primary,resources,meter,exc):
    """Terminal exception route belongs to the SAME owner, never a second scan.
    Known failures are refusals, NOT evidence of the intended negative cause.
    A truly unavailable physical terminal allocation remains STOP_UNCONFIRMED;
    no Python Source can fabricate successful bytes after that host failure.
    """
    from tools.native_support import retain_source_origin
    retain_source_origin(meter,exc)
    from .custody import close_whole_custody,release_retained_fd,release_archive_live
    from .bounds import terminal_meter,release_live,reserve
    from .canonical import canonical_bytes_bounded
    from .digests import DigestStop
    from .compression import BudgetStop
    cause=exc.cause if isinstance(exc,(DigestStop,BudgetStop)) else "resource_ceiling_exceeded" if isinstance(exc,MemoryError) else "held_fd_unretained" if isinstance(exc,OSError) else "expected_shape_invalid"
    cleanup=[];cleanup_details=[]
    from .causes import source_exception_detail
    for closer in (close_whole_custody,release_retained_fd):
        try:
            value=closer(meter)
            problem=value.get("cause") if isinstance(value,dict) else value
            if problem: cleanup.append(problem)
        except (MemoryError,OSError,ValueError,KeyError,TypeError,RuntimeError) as cleanup_exc:
            from tools.native_support import retain_source_origin
            retain_source_origin(meter,cleanup_exc)
            cleanup.append("held_fd_unretained")
            retain_source_origin(meter,cleanup_exc)
            cleanup_details.append(source_exception_detail(meter,cleanup_exc))
    client=meter.get('root_holder_client')
    if client is not None:
        try:
            client.close()
            if meter.get('fd_close_uncertainties'): cleanup.append('held_fd_unretained')
        except (MemoryError,OSError,ValueError,KeyError,TypeError,RuntimeError) as cleanup_exc:
            from tools.native_support import retain_source_origin
            retain_source_origin(meter,cleanup_exc)
            cleanup.append('held_fd_unretained')
            retain_source_origin(meter,cleanup_exc)
            cleanup_details.append(source_exception_detail(meter,cleanup_exc))
    primary=primary if isinstance(primary,dict) else pack("REFUSED",cause)
    from .causes import causal_refusal
    result=causal_refusal(primary,cause,meter,exception_class=type(exc).__name__,cleanup_causes=cleanup,
        failure_detail={'origin':source_exception_detail(meter,exc),'cleanup_exceptions':cleanup_details,
            'full_leaf_origins':meter.get('origin_failures',[])[:]})
    previous=meter.pop("final_output_bytes",None)
    if previous is not None:
        amount=len(previous)
        owner=meter.get("terminal_lane_meter") if meter.get("final_output_in_terminal") else meter
        previous=None
        release_live(owner,amount)
    from .bounds import reserve_terminal_causal
    if reserve_terminal_causal(resources,meter,result):
        raise RuntimeError('STOP_UNCONFIRMED: full causal exception successor exceeds original terminal reserve')
    lane=terminal_meter(meter)
    if lane is None: raise RuntimeError("STOP_UNCONFIRMED: missing terminal owner capacity")
    strict=validate_document(result,RESULT_SCHEMA,None,lane)
    if strict is not None: raise RuntimeError('STOP_UNCONFIRMED: complete exception terminal schema: '+strict)
    raw=canonical_bytes_bounded(result,lane["terminal_limits"]["output_bytes"]-lane["output_bytes"],None,lane)
    if raw is None: raise RuntimeError("STOP_UNCONFIRMED: complete causal refusal unrepresentable; no detail/trace truncation")
    if reserve(None,lane,output_bytes=len(raw)): raise RuntimeError("STOP_UNCONFIRMED: terminal output capacity")
    meter["final_output_bytes"]=raw; meter["returned_output_bytes"]=len(raw)
    meter["final_output_in_terminal"]=True
    meter["encoded_output_bytes"]=meter.get("encoded_output_bytes",0)+len(raw)
    return result


def load_retained_index(raw):
    """Authenticate the pinned 202-row index. Does not open archive bodies."""
    try:
        document = canonical_loads(raw, max_bytes=2000000, max_depth=16)
    except ValueError as exc:
        from tools.native_support import retain_source_origin
        retain_source_origin(None,exc)
        return pack("REFUSED", "canonical_ingress_refused", detail={"cause": str(exc)})
    cause = validate_document(document, RETAINED_INDEX_SCHEMA)
    if cause is not None:
        return pack("REFUSED", cause)
    if canonical_sha256(document) != RETAINED_INDEX_SHA256:
        return pack("REFUSED", "oracle_not_pinned")
    census = census_wheel_filenames(document.get("rows") or [])
    return _finish(
        pack(
            "OBSERVED",
            None,
            observation={
                "archive_body_reads": 0,
                "positive_count": document.get("positive_count"),
                "census": census,
            },
        )
    )


def census_wheel_filenames(rows):
    from .filename import parse_wheel_filename

    refusals = []
    five = 0
    six = 0
    for row in rows:
        if row.get("class") != "wheel" and row.get("archive_class") != "wheel":
            continue
        parsed, cause = parse_wheel_filename(row.get("filename"))
        if cause is not None:
            refusals.append({"filename": row.get("filename"), "cause": cause})
            continue
        if parsed["build_tag"] is None:
            five += 1
        else:
            six += 1
        if parsed["version"] != row.get("version"):
            refusals.append({"filename": row.get("filename"), "cause": "version_correspondence"})
    return {
        "five_field": five,
        "six_field": six,
        "refusals": refusals,
        "executed": False,
    }


__all__ = (
    "scan_retained",
    "scan_retained_bytes",
    "scan_rows",
    "scan_wheel",
    "scan_deb",
    "project_snapshot",
    "project_provenance",
    "census_wheel_filenames",
    "canonical_bytes",
)
