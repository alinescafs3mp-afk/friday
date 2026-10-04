"""A trusted stock embedding selects administrative bytes separately from scan input.

The stock argument is a dependency supplied by the embedding, never decoded from
admission JSON. It conveys no Root, publisher, installation or execution authority.
Bodies are bounded canonical documents with explicit role-specific projections.
"""

from .canonical import canonical_loads, canonical_sha256
from .pins import BILL_SHA256, CODEC_PINS, FUTURE_PLAN_SHA256, HELD_ROOT

ROLES = ("bill", "raw_selector", "roster", "runtime", "source", "recipe", "output")


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _administrative(raw):
    import json
    from .canonical import _walk
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result
    def reject(_value):
        raise ValueError("non_finite_number")
    document = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject)
    _walk(document, 1, 32)
    return document


def _bill_matches(bill, rows):
    archive_bill = bill.get("acquisition_bill")
    if not isinstance(archive_bill, dict):
        return False
    indexed = {}
    for key,kind in (("python94_wheels","wheel"),("ubuntu106","ubuntu_minimum"),("kernel_verification_archives","kernel_qualified")):
        entries = archive_bill.get(key)
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("relative_path") in indexed:
                return False
            indexed[entry.get("relative_path")] = (entry,kind)
    for row in rows:
        selected = indexed.get(row.get("relative_path"))
        if not isinstance(selected,tuple):
            return False
        entry,kind = selected
        if row.get("class",row.get("archive_class")) != kind:
            return False
        archive = entry.get("archive") if isinstance(entry.get("archive"), dict) else entry
        for field in ("name", "version"):
            if entry.get(field) != row.get(field):
                return False
        if archive.get("sha256") != row.get("sha256", row.get("expected_sha256")) or archive.get("size") != row.get("size", row.get("expected_size")):
            return False
        if entry.get("relative_path", "").rsplit("/", 1)[-1] != row.get("filename"):
            return False
        if row.get("class", row.get("archive_class")) == "wheel":
            for field in ("python_tag", "abi_tag", "platform_tag", "metadata_raw_sha256"):
                if field in row and entry.get(field) != row.get(field):
                    return False
            if entry.get("requires_python") != row.get("requires_python", row.get("requires_python_raw")):
                return False
        elif entry.get("architecture") != row.get("architecture"):
            return False
    return True


def bind_stock(stock, admission, index_raw, rows, meter):
    """Join actual independently supplied bodies to all selected rows and consumers."""
    from .bounds import reserve, performing_requested
    from .digests import content_sha256
    from .schema_validate import bind_schema_roster

    resources = admission.get("resources")
    if not performing_requested(resources):
        grant = resources.get("grant") if isinstance(resources, dict) else None
        return "ingress_context_unbound" if isinstance(grant, dict) and grant.get("filesystem_read") is True else None
    required_stock={"bodies", "schema_roster", "source_roster", "codec_modules", "implementation_binding", "input_binding"}
    if not isinstance(stock, dict) or set(stock) not in (required_stock,required_stock|{'root_holder'}):
        return "ingress_context_unbound"
    context = admission.get("context")
    if not isinstance(context, dict) or admission.get("scanner_role") != "scan_retained" or admission.get("held_role") != "held-custody":
        return "ingress_context_unbound"
    if admission.get("index_role") not in ("retained-202", "scan-index") or context.get("authority") not in (None, False, "pending"):
        return "ingress_context_unbound"
    grant = resources.get("grant")
    if (not isinstance(grant, dict) or grant.get("actual_permission") is not True or
        grant.get("filesystem_read") is not True or grant.get("role") != "readonly-archive-scan" or
        not _digest(grant.get("sha256")) or context.get("grant_sha256") != grant["sha256"]):
        return "resource_grant_absent"
    selected = context.get("selected_bodies")
    bodies = stock.get("bodies")
    if not isinstance(selected, dict) or set(selected) != set(ROLES) or not isinstance(bodies, dict) or set(bodies) != set(ROLES):
        return "ingress_context_unbound"
    decoded = {}
    for role in ROLES:
        item = bodies[role]
        pin = selected[role]
        if not isinstance(item, dict) or set(item) != {"raw", "sha256"} or not isinstance(pin, dict) or set(pin) != {"sha256"}:
            return "ingress_context_unbound"
        raw = item["raw"]
        body_limit = 201326592 if role == "output" else 67108864 if role in ("raw_selector", "recipe") else 2000000
        if not isinstance(raw, bytes) or len(raw) > body_limit or not _digest(item["sha256"]):
            return "ingress_context_unbound"
        tick = reserve(resources, meter, read_bytes=len(raw), work_bytes=len(raw) * 8, live_bytes=len(raw) * 16)
        if tick:
            return tick
        if content_sha256(raw,resources,meter) != item["sha256"] or pin["sha256"] != item["sha256"]:
            return "ingress_context_unbound"
        try:
            decoded[role] = _administrative(raw)
        except (ValueError, MemoryError, UnicodeError) as raw_origin:
            from tools.native_support import retain_source_origin
            retain_source_origin(meter,raw_origin)
            return "ingress_context_unbound"
    raw_sha = content_sha256(index_raw,resources,meter)
    if context.get("index_sha256") != raw_sha or context.get("index_size") != len(index_raw):
        return "oracle_not_pinned"
    if bodies["bill"]["sha256"] != BILL_SHA256 or context.get("bill_sha256") != BILL_SHA256 or context.get("held_root") != HELD_ROOT:
        return "ingress_context_unbound"
    from .schema_validate import validate_value
    plan = context.get("plan")
    plan_schema = "friday.astra.e4.lab821.future-readonly-scan-manifest.v1"
    if not isinstance(plan, dict) or plan.get("schema") != plan_schema:
        return "plan_unbound"
    plan_validation_cause = validate_value(plan_schema,plan,resources,meter)
    if plan_validation_cause:
        return plan_validation_cause
    from .contracts import metered_identity, validate_stock_roles, bind_plan_roster, bind_loaded_implementation
    plan_sha,plan_hash_cause = metered_identity(plan,resources,meter)
    if plan_hash_cause:
        return plan_hash_cause
    if plan_sha != FUTURE_PLAN_SHA256 or admission.get("future_plan_sha256") != FUTURE_PLAN_SHA256:
        return "plan_unbound"
    bill, selectors, roster = decoded["bill"], decoded["raw_selector"], decoded["roster"]
    if not all(isinstance(x, dict) for x in decoded.values()):
        return "ingress_context_unbound"
    role_cause = validate_stock_roles(decoded,resources,meter)
    if role_cause:
        return role_cause
    plan_cause = bind_plan_roster(plan,roster,admission,rows,resources,meter)
    if plan_cause:
        return plan_cause
    from .contracts import bind_held_scope
    scope_cause = bind_held_scope(roster["held_scope"],rows,resources,meter)
    if scope_cause:
        return scope_cause
    if not _bill_matches(bill, rows) or roster.get("rows") != rows or roster.get("index_sha256") != raw_sha:
        return "ingress_context_unbound"
    if context.get("roster_sha256") != bodies["roster"]["sha256"]:
        return "ingress_context_unbound"
    paths = [row.get("relative_path") for row in rows]
    if len(paths) != len(set(paths)):
        return "custody_membership_changed"
    pins = selectors.get("entries")
    if not isinstance(pins, list) or len(pins) != len(paths):
        return "ingress_context_unbound"
    companion = {}
    for item in pins:
        if not isinstance(item, dict) or set(item) != {"relative_path", "archive_sha256", "publisher_raw_sha256", "metadata_content_sha256", "publisher_raw_utf8", "publisher_record_ordinal"}:
            return "ingress_context_unbound"
        path = item["relative_path"]
        if path in companion or path not in paths or not _digest(item["archive_sha256"]):
            return "ingress_context_unbound"
        for key in ("publisher_raw_sha256", "metadata_content_sha256"):
            if item[key] is not None and not _digest(item[key]):
                return "ingress_context_unbound"
        companion[path] = item
    for row in rows:
        item = companion[row["relative_path"]]
        if item["archive_sha256"] != row.get("sha256", row.get("expected_sha256")):
            return "ingress_context_unbound"
        if "metadata_raw_sha256" in row and item["publisher_raw_sha256"] != row["metadata_raw_sha256"]:
            return "ingress_context_unbound"
        selected_class = row.get("class", row.get("archive_class"))
        if selected_class == "wheel":
            publisher_cause = bind_publisher_record(item, row, bill, resources, meter)
            if publisher_cause:
                return publisher_cause
        elif any(item[key] is not None for key in ("publisher_raw_sha256", "metadata_content_sha256", "publisher_raw_utf8", "publisher_record_ordinal")):
            return "ingress_context_unbound"
    if "content_pins" in context:
        projection = [{"relative_path": item["relative_path"], "publisher_raw_sha256": item["publisher_raw_sha256"],
                       "metadata_content_sha256": item["metadata_content_sha256"]} for item in pins]
        if context["content_pins"] != projection:
            return "ingress_context_unbound"
    runtime = decoded["runtime"]
    lifetime = runtime.get("custody_lifetime")
    from .custody import ROOT_LIFETIME,RootHolderEndpoint,RootHolderClient
    subset_lifetime={"mode":"per-material-complete-observation-generation.v1",
                    "max_simultaneous_archive_fds":1,"aggregate_is_simultaneous_custody":False,
                    "reopen_substitution":False}
    if lifetime not in (subset_lifetime,ROOT_LIFETIME):
        return "ingress_context_unbound"
    # Whole means genuinely simultaneous Root custody, never sequential Source
    # receipt composition. Subsets cannot be relabeled as Root-held whole202.
    whole=decoded['roster']['purpose']=='retained-202-whole'
    if whole!=(lifetime==ROOT_LIFETIME): return 'ingress_context_unbound'
    endpoint=stock.get('root_holder')
    if whole and not isinstance(endpoint,RootHolderEndpoint): return 'held_fd_unretained'
    if not whole and endpoint is not None: return 'ingress_context_unbound'
    if whole and plan["custody_lifetime"] != ROOT_LIFETIME:
        return "plan_unbound"
    if not whole and lifetime!=subset_lifetime:
        return 'plan_unbound'
    static_resources = {key: value for key, value in resources.items() if key not in ("started_ns", "now_ns", "rss_bytes", "install_recipe")}
    if runtime["resources"] != static_resources:
        return "ingress_context_unbound"
    caps = resources.get("compression_capabilities")
    if not isinstance(caps, list) or any(not isinstance(x, dict) or not isinstance(x.get("method"), str) for x in caps):
        return "codec_capability_unpinned"
    if len({x["method"] for x in caps}) != len(caps) or runtime["codecs"] != caps or context.get("codecs") != {x["method"]: x for x in caps}:
        return "codec_capability_unpinned"
    modules = stock["codec_modules"]
    if not isinstance(modules, dict) or runtime["module_sha256"] != {k: v.get("sha256") for k, v in modules.items()}:
        return "codec_capability_unpinned"
    for cap in caps:
        method = cap.get("method")
        pin = CODEC_PINS.get(method)
        module = modules.get(method)
        if not isinstance(pin, dict) or not isinstance(module, dict) or set(module) != {"module", "sha256", "protocol", "allocation_contract", "evidence_raw"}:
            return "codec_capability_unpinned"
        if cap.get("implementation") != pin["implementation"] or cap.get("capability_sha256") != pin["capability_sha256"] or not _digest(module["sha256"]):
            return "codec_capability_unpinned"
        if cap.get("protocol") != module["protocol"]:
            return "codec_capability_unsupported"
    source = decoded["source"]
    if source["source_roster"] != stock["source_roster"] or source["schema_roster"] != stock["schema_roster"]:
        return "ingress_context_unbound"
    schema_cause = bind_schema_roster(stock["schema_roster"], resources, meter)
    if schema_cause:
        return schema_cause
    source_cause = bind_source_roster(stock["source_roster"], resources, meter)
    if source_cause:
        return source_cause
    implementation_cause = bind_loaded_implementation(stock,source,runtime,resources,meter)
    if implementation_cause:
        return implementation_cause
    recipe, output = decoded["recipe"], decoded["output"]
    # Keep the large installation body in the independent stock argument only.
    if context.get("recipe") != {"sha256": recipe.get("sha256")} or context.get("output") != {"schema":output["schema"],"expected_output_sha256":output["expected_output_sha256"]}:
        return "ingress_context_unbound"
    from .schema_validate import RESULT_SCHEMA
    if output["schema"] != RESULT_SCHEMA or not _digest(output["expected_output_sha256"]):
        return "ingress_context_unbound"
    if not isinstance(recipe.get("destinations"), list):
        return "ingress_context_unbound"
    from .contracts import recipe_identity as identity_projection
    recipe_sha,recipe_hash_cause = metered_identity(identity_projection(recipe),resources,meter)
    if recipe_hash_cause:
        return recipe_hash_cause
    if recipe.get("sha256") != recipe_sha:
        return "ingress_context_unbound"
    candidate = recipe.get("candidate")
    if isinstance(candidate, dict) and candidate.get("recipe_sha256") not in (None, recipe["sha256"]):
        return "ingress_context_unbound"
    targets = set()
    for entry in recipe["destinations"]:
        if not isinstance(entry, dict) or set(entry) != {"source_relative_path", "source_domain", "source_path", "destination", "action"}:
            return "ingress_context_unbound"
        key = (entry["source_relative_path"], entry["source_domain"], entry["source_path"])
        if key in targets or key[0] not in paths or key[1] not in ("wheel", "control", "data"):
            return "ingress_context_unbound"
        targets.add(key)
        if entry["source_domain"] == "control":
            if entry["action"] != "inventory" or entry["destination"] is not None:
                return "ingress_context_unbound"
        elif entry["source_path"] == "." and entry["action"] == "root_directory_inventory" and entry["destination"] is None:
            pass
        elif entry["action"] != "install" or not isinstance(entry["destination"], str):
            return "ingress_context_unbound"
    meter["stock_context"] = decoded
    meter["codec_modules"] = modules
    meter["content_companion"] = companion
    meter["whole_custody"] = True
    meter["custody_lifetime"] = lifetime
    meter["held_scope"] = roster["held_scope"]
    meter["input_binding"] = stock["input_binding"]
    if whole:
        # The native entry's actual inherited inventory already owns fd3.
        # Transfer its existing channel lease rather than charging it twice.
        tick=reserve(resources,meter,fds=0 if meter.get('native_owner') is not None else 1,
                     live_bytes=16384,work_bytes=4096)
        if tick: return tick
        # Client initialization owns the endpoint BEFORE take can detach it.
        meter['root_holder_endpoint']=endpoint
        try:
            channel,peer,generation=endpoint.take()
            client=RootHolderClient.__new__(RootHolderClient)
            meter['root_holder_client']=client
            RootHolderClient.__init__(client,channel,peer,generation,resources,meter,prepaid=True)
        finally:
            if endpoint.channel is None: meter.pop('root_holder_endpoint',None)
    resources["install_recipe"] = recipe
    return None


def bind_publisher_record(item, row, bill, resources, meter):
    from .bounds import reserve
    from .digests import content_sha256
    from .filename import pep503_name
    raw_text = item["publisher_raw_utf8"]
    ordinal = item["publisher_record_ordinal"]
    if not isinstance(raw_text, str) or len(raw_text) > 1048576 or not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
        return "ingress_context_unbound"
    tick = reserve(resources, meter, work_bytes=len(raw_text) * 8, live_bytes=len(raw_text) * 64)
    if tick:
        return tick
    raw = raw_text.encode("utf-8")
    if content_sha256(raw,resources,meter) != item["publisher_raw_sha256"]:
        return "ingress_context_unbound"
    expected = [entry for entry in bill["acquisition_bill"]["python94_wheels"] if entry["relative_path"] == row["relative_path"]]
    if len(expected) != 1 or expected[0]["metadata_raw_sha256"] != item["publisher_raw_sha256"]:
        return "ingress_context_unbound"
    try:
        document = _administrative(raw)
        selected = document["urls"][ordinal]
        info = document["info"]
    except (ValueError, TypeError, KeyError, IndexError) as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(meter,raw_origin)
        return "ingress_context_unbound"
    if (not isinstance(selected, dict) or selected.get("filename") != row["filename"] or
        selected.get("digests", {}).get("sha256") != row.get("sha256", row.get("expected_sha256")) or
        selected.get("size") != row.get("size", row.get("expected_size")) or
        selected.get("requires_python") != row.get("requires_python", row.get("requires_python_raw")) or
        pep503_name(info.get("name")) != pep503_name(row["name"]) or info.get("version") != row["version"]):
        return "ingress_context_unbound"
    return None


def bind_source_roster(roster, resources, meter):
    import os
    if not isinstance(roster, dict) or not 1 <= len(roster) <= 64:
        return "ingress_context_unbound"
    directory = os.path.dirname(os.path.abspath(__file__))
    from .custody import owned_metadata_directory
    enumeration = owned_metadata_directory(directory,roster,resources,meter)
    enumeration = enumeration or ('held_fd_unretained' if meter.get('fd_close_uncertainties') else None)
    if enumeration:
        return enumeration
    for name, pin in roster.items():
        if not isinstance(name, str) or "/" in name or not isinstance(pin, dict) or set(pin) != {"size", "sha256"}:
            return "ingress_context_unbound"
        size = pin["size"]
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= 1048576 or not _digest(pin["sha256"]):
            return "ingress_context_unbound"
        from .custody import owned_metadata_bytes
        raw,observed,cause = owned_metadata_bytes(os.path.join(directory,name),1048576,resources,meter)
        cause = cause or ('held_fd_unretained' if meter.get('fd_close_uncertainties') else None)
        if cause:
            return cause
        if len(raw) != size or observed["sha256"] != pin["sha256"]:
            return "ingress_context_unbound"
    return None


def effects_cause(resources, meter):
    if isinstance(meter,dict) and meter.get('fd_close_uncertainties'):
        return 'held_fd_unretained'
    grant = resources.get("grant") if isinstance(resources, dict) else None
    if isinstance(grant, dict) and grant.get("filesystem_read") is True:
        from .bounds import performing_requested
        if not performing_requested(resources) or not isinstance(meter, dict) or not isinstance(meter.get("stock_context"), dict):
            return "ingress_context_unbound"
        from .contracts import loaded_globals_cause
        return loaded_globals_cause(meter)
    return None
