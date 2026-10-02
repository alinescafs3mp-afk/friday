"""Defensive consumers of separately selected future stock and ordinary DATA.

The trusted embedding fixes loaded objects, evidence and complete expected bytes
before a call. Admission JSON cannot construct this selection or grant authority.
These adapters make no current Root/Image/runtime claim.
"""
from .bounds import reserve,release_live
from .canonical import canonical_bytes_bounded, canonical_sha256_metered
from .digests import content_sha256
from .schema_validate import validate_role


def validate_stock_roles(decoded,resources,meter):
    for role in ("raw_selector","roster","runtime","source","recipe","output"):
        cause = validate_role(role,decoded[role],resources,meter)
        if cause:
            return cause
    return None


def produce_stock_bodies(actual_raw_bodies,resources,meter):
    """Future typed producer from separately supplied ACTUAL complete bodies.
    No role/golden/native/installed observation, approval or authority is made
    up here. An absent body is an error; matching hashes are only DATA identity.
    """
    from .context import ROLES,_administrative
    from .pins import BILL_SHA256
    if not isinstance(actual_raw_bodies,dict) or set(actual_raw_bodies)!=set(ROLES):
        return None,"ingress_context_unbound"
    decoded={}; bodies={}
    for role in ROLES:
        raw=actual_raw_bodies[role]
        limit=201326592 if role=="output" else 67108864 if role in ("raw_selector","recipe") else 2000000
        if not isinstance(raw,bytes) or len(raw)>limit: return None,"ingress_context_unbound"
        tick=reserve(resources,meter,read_bytes=len(raw),work_bytes=len(raw)*8,live_bytes=len(raw)*16)
        if tick: return None,tick
        decoded[role]=_administrative(raw)
        digest=content_sha256(raw,resources,meter)
        if role=="bill" and digest!=BILL_SHA256: return None,"ingress_context_unbound"
        bodies[role]={"raw":raw,"sha256":digest}
    cause=validate_stock_roles(decoded,resources,meter)
    return (None,cause) if cause else (bodies,None)


def metered_identity(value,resources,meter):
    return canonical_sha256_metered(value,resources,meter)


def recipe_identity(recipe):
    # Namespace/installed/authority observations and later manifest binding must
    # not cause a materials/manifest cycle. The policy itself is selected.
    excluded = {"sha256","snapshot_facts","provenance_facts","installed_members",
                "created_directories","namespace","artifact_authorities","candidate"}
    return {key:value for key,value in recipe.items() if key not in excluded}


def bind_plan_roster(plan,roster,admission,rows,resources,meter):
    allocation = reserve(resources,meter,work_bytes=len(rows)*2048,live_bytes=len(rows)*4096)
    if allocation:
        return allocation
    paths = [row["relative_path"] for row in rows]
    expected = plan["positive_relative_paths"]
    purpose = roster["purpose"]
    if len(paths) != len(set(paths)) or not set(paths) <= set(expected):
        return "plan_unbound"
    counts = {key:0 for key in ("wheel","ubuntu_minimum","kernel_qualified")}
    for row in rows:
        kind = row.get("class",row.get("archive_class"))
        if kind not in counts:
            return "plan_unbound"
        counts[kind] += 1
    if roster["class_counts"] != counts:
        return "plan_unbound"
    if purpose == "retained-202-whole":
        if admission["index_role"] != "retained-202" or paths != expected or len(rows) != plan["positive_count"] or counts != plan["class_counts"]:
            return "plan_unbound"
    elif purpose == "retained-exact-subset":
        if admission["index_role"] != "scan-index" or not paths:
            return "plan_unbound"
        # A subset is an explicitly selected calibration, never whole202 credit.
        positions = [expected.index(path) for path in paths]
        if positions != sorted(positions):
            return "plan_unbound"
    else:
        return "plan_unbound"
    if admission.get("assert_document") == "filename-census":
        return "plan_unbound"
    proposal = plan["resources_proposed_not_granted"]
    for key in ("max_archive_bytes","max_expanded_bytes","max_members","max_member_name_bytes",
                "max_depth","max_fds","max_output_bytes","max_wall_ms"):
        # Proposal does not mint a grant; selected admission must also retain the
        # original ceiling policy rather than quietly enlarge its scope.
        if resources["ceilings"][key] > proposal[key]:
            return "plan_unbound"
    return None


def bind_loaded_implementation(stock,source,runtime,resources,meter):
    """Bind actual loaded code objects and codec objects to independent selection."""
    import marshal
    from .public import scan_retained,scan_retained_bytes
    from .compression import decode_admitted
    from .schema_validate import validate_document
    from .zip_wheel import scan_wheel
    from .deb_archive import scan_deb
    selected = stock.get("implementation_binding")
    keys = {"objects","code_sha256","codec_objects","source_modules","source_globals","helper_graph_code_sha256",
            "source_global_state_sha256","native_contract"}
    if not isinstance(selected,dict) or set(selected) != keys:
        return "ingress_context_unbound"
    actual = {"scan_retained":scan_retained,"scan_retained_bytes":scan_retained_bytes,
              "decode_admitted":decode_admitted,"validate_document":validate_document,
              "scan_wheel":scan_wheel,"scan_deb":scan_deb}
    if not isinstance(selected["objects"],dict) or set(selected["objects"]) != set(actual):
        return "ingress_context_unbound"
    declared = source["implementation_contract"]
    from tools.state_binding import bind_native
    if selected['native_contract']!=declared['native_contract'] or selected['source_global_state_sha256']!=declared['source_global_state_sha256']:
        return 'ingress_context_unbound'
    native_cause=bind_native(selected['native_contract'],resources,meter)
    if native_cause: return native_cause
    if selected["code_sha256"] != declared["source_code_sha256"] or set(selected["code_sha256"]) != set(actual):
        return "ingress_context_unbound"
    for name,function in actual.items():
        if selected["objects"][name] is not function:
            return "ingress_context_unbound"
        tick = reserve(resources,meter,work_bytes=2097152,live_bytes=2097152)
        if tick:
            return tick
        raw=None
        try:
            raw = marshal.dumps(function.__code__)
            if len(raw)>1048576 or content_sha256(raw,resources,meter) != selected["code_sha256"][name]:
                return "ingress_context_unbound"
        finally:
            raw=None
            release_live(meter,2097152)
    graph_cause = bind_loaded_source_graph(selected,declared,resources,meter)
    if graph_cause:
        return graph_cause
    modules = stock["codec_modules"]
    if not isinstance(selected["codec_objects"],dict) or set(selected["codec_objects"]) != set(modules):
        return "codec_capability_unpinned"
    from .pins import CODEC_PINS
    if set(modules) != set(CODEC_PINS) or set(runtime["allocation_contracts"]) != set(CODEC_PINS):
        return "codec_capability_unpinned"
    for method,module in modules.items():
        if not isinstance(module,dict) or set(module) != {"module","sha256","protocol","allocation_contract","evidence_raw"}:
            return "codec_capability_unpinned"
        if selected["codec_objects"][method] is not module["module"]:
            return "codec_capability_unpinned"
        evidence_raw = module["evidence_raw"]
        if not isinstance(evidence_raw,bytes) or len(evidence_raw)>262144:
            return "codec_capability_unpinned"
        tick = reserve(resources,meter,read_bytes=len(evidence_raw),work_bytes=len(evidence_raw)*8,
                       live_bytes=len(evidence_raw)*16)
        if tick:
            return tick
        evidence_sha = content_sha256(evidence_raw,resources,meter)
        if declared["codec_evidence_sha256"].get(method) != evidence_sha:
            return "codec_capability_unpinned"
        from .context import _administrative
        evidence = _administrative(evidence_raw)
        evidence_cause = validate_role("allocation",evidence,resources,meter)
        if evidence_cause:
            return evidence_cause
        if evidence != module["allocation_contract"] or evidence != runtime["allocation_contracts"][method]:
            return "codec_capability_unpinned"
        if evidence["method"] != method or evidence["module_sha256"] != module["sha256"] or evidence["protocol"] != module["protocol"]:
            return "codec_capability_unpinned"
    meter["implementation_bound"] = True
    return None


def bind_loaded_source_graph(selected,declared,resources,meter):
    """Bind every currently loaded package function/method and global object.

    Exact bytecode includes nested code constants; a six-entry wrapper binding
    is insufficient.  This closed SOURCE graph is not a native DSO/load audit or
    a proof that transitive mutable globals cannot change concurrently.
    """
    import sys, types, marshal
    names = ('__init__','bounds','broker_map','canonical','causes','compression',
        'context','contracts','controls','custody','deb_archive','digests','filename',
        'fixtures','guards','normalize','pins','public','raw_relations','schema_validate',
        'semantics','zip_wheel')
    modules = selected['source_modules']
    globals_selected = selected['source_globals']
    selected_code = selected['helper_graph_code_sha256']
    if (not isinstance(modules,dict) or set(modules) != set(names) or
        not isinstance(globals_selected,dict) or set(globals_selected) != set(names) or
        not isinstance(selected_code,dict) or selected_code != declared['helper_graph_code_sha256']):
        return 'ingress_context_unbound'
    tick = reserve(resources,meter,work_bytes=1048576,live_bytes=2097152)
    if tick:
        return tick
    reached = set()
    package = __package__
    for name in names:
        qualified = package if name == '__init__' else package+'.'+name
        module = sys.modules.get(qualified)
        if not isinstance(module,types.ModuleType) or modules[name] is not module:
            return 'ingress_context_unbound'
        namespace = vars(module)
        actual_globals = {key:value for key,value in namespace.items() if not key.startswith('__')}
        chosen = globals_selected[name]
        if not isinstance(chosen,dict) or set(chosen) != set(actual_globals):
            return 'ingress_context_unbound'
        for key,value in actual_globals.items():
            if chosen[key] is not value:
                return 'ingress_context_unbound'
            functions = []
            if isinstance(value,types.FunctionType) and value.__module__ == qualified:
                functions.append((key,value))
            elif isinstance(value,type) and value.__module__ == qualified:
                for method,item in vars(value).items():
                    if isinstance(item,(staticmethod,classmethod)):
                        item = item.__func__
                    if isinstance(item,types.FunctionType):
                        functions.append((key+'.'+method,item))
                    elif isinstance(item,property):
                        for accessor in ('fget','fset','fdel'):
                            function = getattr(item,accessor)
                            if isinstance(function,types.FunctionType):
                                functions.append((key+'.'+method+'.'+accessor,function))
            for symbol,function in functions:
                code_key = name+'::'+symbol
                tick = reserve(resources,meter,work_bytes=2097152,live_bytes=2097152)
                if tick:
                    return tick
                raw=None
                try:
                    raw = marshal.dumps(function.__code__)
                    if len(raw)>1048576 or content_sha256(raw,resources,meter) != selected_code.get(code_key):
                        return 'ingress_context_unbound'
                    reached.add(code_key)
                finally:
                    raw=None
                    release_live(meter,2097152)
    if reached != set(selected_code):
        return 'ingress_context_unbound'
    meter['loaded_source_global_selection'] = globals_selected
    meter['loaded_source_modules'] = modules
    from tools.state_binding import source_state_cause
    state_cause=source_state_cause(modules,selected['source_global_state_sha256'])
    if state_cause: return state_cause
    meter['loaded_source_state_selection']=selected['source_global_state_sha256']
    release_live(meter,2097152)
    return None


def loaded_globals_cause(meter):
    """Recheck selected package global identities before an archive/native effect."""
    modules = meter.get('loaded_source_modules')
    selected = meter.get('loaded_source_global_selection')
    if not isinstance(modules,dict) or not isinstance(selected,dict):
        return 'ingress_context_unbound'
    count = sum(len(vars(module)) for module in modules.values())
    tick = reserve(meter.get('active_resources'),meter,work_bytes=count*512)
    if tick:
        return tick
    for name,module in modules.items():
        namespace = vars(module)
        chosen = selected[name]
        for key,value in namespace.items():
            if not key.startswith('__') and (key not in chosen or chosen[key] is not value):
                return 'ingress_context_unbound'
        if sum(not key.startswith('__') for key in namespace) != len(chosen):
            return 'ingress_context_unbound'
    from tools.state_binding import source_state_cause,bind_native
    cause=source_state_cause(modules,meter.get('loaded_source_state_selection'))
    if cause: return cause
    cause=bind_native(meter.get('native_transitive_contract'),meter.get('active_resources'),meter)
    if cause: return cause
    return None


def bind_independent_output(output,input_binding,index_raw,held_raw,admission_raw,resources,meter):
    """Complete fixed inputs, public object, exact bytes and reached-phase oracle."""
    actual = {key:content_sha256(raw,resources,meter) for key,raw in
              (("index",index_raw),("held",held_raw),("admission",admission_raw))}
    if not isinstance(input_binding,dict) or set(input_binding) != {"index","held","admission"} or actual != input_binding:
        return "oracle_not_pinned"
    text = output["expected_output_ascii"]
    tick = reserve(resources,meter,work_bytes=len(text)*2,live_bytes=len(text)*2)
    if tick:
        return tick
    raw = text.encode("ascii")
    cap = resources["ceilings"]["max_output_bytes"]
    expected = canonical_bytes_bounded(output["expected_output"],cap,resources,meter)
    if expected is None:
        return "resource_ceiling_exceeded"
    same_bytes = raw == expected
    expected_size = len(expected)
    expected = None
    release_live(meter,expected_size)
    if not same_bytes or content_sha256(raw,resources,meter) != output["expected_output_sha256"]:
        return "oracle_not_pinned"
    if output["expected_output"].get("phase_trace") != output["expected_trace"]:
        return "oracle_not_pinned"
    stock = meter["stock_context"]
    preconditions = output["preconditions"]
    rows = stock["roster"]["rows"]
    expected_result = output["expected_output"]
    if (output["purpose"] != stock["roster"]["purpose"] or
        preconditions["custody_lifetime"] != stock["runtime"]["custody_lifetime"] or
        preconditions["schema_roster"] != stock["source"]["schema_roster"] or
        preconditions["source_roster"] != stock["source"]["source_roster"] or
        preconditions["class_counts"] != stock["roster"]["class_counts"] or
        preconditions["archive_count"] != len(rows) or
        preconditions["recipe_identity_sha256"] != stock["recipe"]["sha256"]):
        return "oracle_not_pinned"
    from .pins import CODEC_PINS
    if preconditions["required_codecs"] != list(CODEC_PINS):
        return "oracle_not_pinned"
    from .schema_validate import RESULT_SCHEMA,validate_document
    expected_cause = validate_document(expected_result,RESULT_SCHEMA,resources,meter)
    if expected_cause:
        return expected_cause
    if expected_result.get("status") != "OBSERVED" or expected_result.get("cause") is not None:
        return "oracle_not_pinned"
    observations = expected_result.get("archive_observations")
    joins = expected_result.get("source_joins")
    if not isinstance(observations,list) or not isinstance(joins,list) or len(observations) != len(rows) or len(joins) != len(rows):
        return "oracle_not_pinned"
    for row,observed,join in zip(rows,observations,joins):
        selected_path = row["relative_path"]
        selected_sha = row.get("sha256",row.get("expected_sha256"))
        if (observed["relative_path"],observed["archive_sha256"],observed["status"],observed["cause"],
            join["source_relative_path"],join["archive_sha256"]) != (selected_path,selected_sha,"OBSERVED",None,selected_path,selected_sha):
            return "oracle_not_pinned"
    namespace = expected_result.get("final_namespace")
    selected_namespace,cause = installed_inventory(stock["recipe"],resources,meter)
    if cause:
        return cause
    ordered = [selected_namespace[path] for path in sorted(selected_namespace,key=lambda value:value.encode("utf-8"))]
    if namespace != ordered:
        return "oracle_not_pinned"
    inventory_sha,cause = metered_identity(namespace,resources,meter)
    if cause:
        return cause
    if inventory_sha != preconditions["member_inventory_sha256"] or expected_result.get("whole_materials_sha256") != preconditions["materials_sha256"]:
        return "oracle_not_pinned"
    return material_receipt_relations(expected_result,resources,meter)


def material_receipt_relations(result,resources,meter):
    """Reconstruct every complete observation byte identity, not a status-only oracle."""
    receipts = result.get('material_output_receipts')
    observations = result.get('archive_observations')
    if not isinstance(receipts,list) or not isinstance(observations,list) or len(receipts) != len(observations):
        return 'oracle_not_pinned'
    for receipt,entry in zip(receipts,observations):
        if (receipt['relative_path'],receipt['archive_sha256']) != (entry['relative_path'],entry['archive_sha256']):
            return 'oracle_not_pinned'
        raw = canonical_bytes_bounded(entry['observation'],resources['ceilings']['max_output_bytes'],resources,meter)
        if raw is None:
            return 'resource_ceiling_exceeded'
        try:
            if len(raw) != receipt['observation_bytes'] or content_sha256(raw,resources,meter) != receipt['observation_sha256']:
                return 'oracle_not_pinned'
        finally:
            raw_size=len(raw)
            raw=None
            release_live(meter,raw_size)
        custody = entry['observation']['per_archive_custody']
        if custody['terminal_identity'] != receipt['identity'] or custody['terminal_content_sha256'] != receipt['archive_sha256'] or custody['named_path_checked'] is not True:
            return 'oracle_not_pinned'
    return None


def installed_inventory(recipe,resources,meter):
    """Validate complete source/created/installed inventory and its selected mount."""
    rows = recipe["installed_members"]
    created = recipe["created_directories"]
    namespace = recipe["namespace"]
    root = namespace["root"]
    if root["path"] != "" or root["type"] != "directory":
        return None,"member_provenance_incomplete"
    by_path = {}
    allocation = reserve(resources,meter,work_bytes=(len(rows)+len(created)+1)*4096,
                         live_bytes=(len(rows)+len(created)+1)*16384)
    if allocation:
        return None,allocation
    # Reserve BEFORE the combined container, sorting, path sets or map copies.
    for row in rows + created + [root]:
        tick = reserve(resources,meter,work_bytes=4096,live_bytes=8192)
        if tick:
            return None,tick
        if row["path"] in by_path:
            return None,"duplicate_member_provenance"
        # Linux symlink permission bits are not a writable content grant. Its
        # literal target and every containing/terminal namespace node are bound
        # separately; regular files and directories must be non-writable.
        if (row["type"] != "symlink" and row["mode"] & 0o222) or row["device"] != root["device"] or row["mount_id"] != root["mount_id"] or row["mount_domain"] != root["mount_domain"]:
            return None,"member_provenance_incomplete"
        selected_identity = row["identity"]
        native_type = {"regular":0o100000,"directory":0o040000,"symlink":0o120000}[row["type"]]
        if selected_identity["full_mode"] & 0o170000 != native_type or selected_identity["full_mode"] & 0o7777 != row["mode"]:
            return None,"member_provenance_incomplete"
        for key in ("device","uid","gid","nlink","size"):
            if key == "size" and row["type"] != "regular":
                continue
            if selected_identity[key] != row[key]:
                return None,"member_provenance_incomplete"
        projection = {k:v for k,v in row.items() if k != "observation_sha256"}
        digest,cause = metered_identity(projection,resources,meter)
        if cause:
            return None,cause
        if digest != row["observation_sha256"]:
            return None,"member_provenance_incomplete"
        if row["type"] == "regular":
            expected_class = "executable" if row["mode"] & 0o111 else "non_executable"
            if row["nlink"] != 1 or row["executable_class"] != expected_class or row["size"] is None or row["sha256"] is None or row["link_target"] is not None or row["link_target_sha256"] is not None:
                return None,"member_provenance_incomplete"
        elif row["type"] == "symlink":
            target = row["link_target"]
            if not isinstance(target,str) or row["sha256"] is not None or row["size"] is not None or row["executable_class"] is not None:
                return None,"member_provenance_incomplete"
            tick = reserve(resources,meter,work_bytes=len(target),live_bytes=len(target)*4)
            if tick:
                return None,tick
            if content_sha256(target.encode("utf-8"),resources,meter) != row["link_target_sha256"]:
                return None,"member_provenance_incomplete"
        else:
            if any(row[key] is not None for key in ("size","sha256","link_target","link_target_sha256","executable_class")):
                return None,"member_provenance_incomplete"
        by_path[row["path"]] = row
    directory_paths = {path for path,row in by_path.items() if row["type"] == "directory"}
    installed_paths = {row["path"] for row in rows}
    planned_paths = {row["destination"] for row in recipe["destinations"] if row["action"] == "install"}
    supplemental = recipe["supplemental_bindings"]
    extra_paths = set()
    extra_artifacts = {(row["filename"],row["sha256"]):row for row in recipe["supplemental_artifacts"]}
    if len(extra_artifacts) != len(recipe["supplemental_artifacts"]):
        return None,"member_provenance_incomplete"
    for binding in supplemental:
        path = binding["destination"]
        observed = binding["installed_observation"]
        if path in planned_paths or path in extra_paths or (binding["artifact_filename"],binding["artifact_sha256"]) not in extra_artifacts or by_path.get(path) != observed:
            return None,"member_provenance_incomplete"
        if observed["path"] != path or observed["type"] == "directory" and path in {row["path"] for row in created}:
            return None,"member_provenance_incomplete"
        selected = {"artifact_filename":binding["artifact_filename"],"artifact_sha256":binding["artifact_sha256"],"source_member_path":binding["source_member_path"],
                    "installed_observation":observed,"destination":path}
        binding_sha,cause = metered_identity(selected,resources,meter)
        if cause:
            return None,cause
        if binding_sha != binding["source_binding_sha256"]:
            return None,"member_provenance_incomplete"
        # DATA equality only; an external stock producer must supply genuine
        # underlying provenance.  No archive body, execution or approval credit.
        extra_paths.add(path)
    planned_paths.update(extra_paths)
    if installed_paths != planned_paths:
        return None,"member_provenance_incomplete"
    required_parents = set()
    for path in planned_paths:
        parts = path.split("/")
        required_parents.update("/".join(parts[:index]) for index in range(1,len(parts)))
    created_paths = {row["path"] for row in created}
    if created_paths != required_parents-installed_paths:
        return None,"member_provenance_incomplete"
    plan = {row["path"]:row for row in recipe["created_directory_plan"]}
    if len(plan) != len(recipe["created_directory_plan"]) or set(plan) != created_paths:
        return None,"member_provenance_incomplete"
    for row in created:
        if plan[row["path"]] != {key:row[key] for key in ("path","uid","gid","mode")}:
            return None,"member_provenance_incomplete"
    if any(row["type"] != "directory" for row in created) or {row["path"] for row in namespace["directories"]} != directory_paths - {""}:
        return None,"member_provenance_incomplete"
    for row in namespace["directories"]:
        if by_path.get(row["path"]) != row:
            return None,"member_provenance_incomplete"
    for path,row in by_path.items():
        if path and (path.startswith("/") or any(part in ("",".","..") for part in path.split("/"))):
            return None,"member_provenance_incomplete"
        if path:
            parent = path.rsplit("/",1)[0] if "/" in path else ""
            if parent not in directory_paths:
                return None,"member_provenance_incomplete"
    return by_path,None


def bind_held_scope(scope,rows,resources,meter):
    tick = reserve(resources,meter,work_bytes=(len(rows)+len(scope["directories"]))*4096,
                   live_bytes=(len(rows)+len(scope["directories"]))*4096)
    if tick:
        return tick
    root = scope["root"]
    if root["path"] != "" or root["identity"]["full_mode"] & 0o170000 != 0o040000:
        return "held_root_unpinned"
    required = {""}
    for row in rows:
        parts = row["relative_path"].split("/")
        required.update("/".join(parts[:index]) for index in range(1,len(parts)))
    by_path = {}
    for directory in [root]+scope["directories"]:
        if directory["path"] in by_path or directory["identity"]["full_mode"] & 0o170000 != 0o040000:
            return "held_root_unpinned"
        if directory["identity"]["device"] != root["identity"]["device"] or directory["mount_id"] != root["mount_id"] or directory["mount_domain"] != root["mount_domain"]:
            return "held_root_unpinned"
        by_path[directory["path"]] = directory
    if set(by_path) != required:
        return "held_root_unpinned"
    meter["held_scope_index"] = by_path
    return None


def provenance_relations(snapshot,provenance,recipe,resources,meter):
    """Consume separately fixed future approval/authority/material identities.

    Null pending facts remain null. Matching metadata is never a signature,
    present Root/Image, runtime execution or release approval.
    """
    candidate = recipe["candidate"]
    platform = snapshot["platform"]
    authorities = provenance["approved_authorities"]
    approval = provenance["owner_approval"]
    manifest_sha,cause = metered_identity(snapshot,resources,meter)
    if cause:
        return cause
    if provenance["manifest_sha256"] is not None and provenance["manifest_sha256"] != manifest_sha:
        return "member_provenance_incomplete"
    if provenance["assembly_recipe_sha256"] != recipe["sha256"]:
        return "member_provenance_incomplete"
    artifacts = provenance["artifacts"]
    if artifacts != sorted(artifacts,key=lambda row:row["filename"].encode("utf-8")) or len({row["filename"] for row in artifacts}) != len(artifacts):
        return "member_provenance_incomplete"
    seen = set()
    for authority in authorities:
        tick = reserve(resources,meter,work_bytes=len(artifacts)*512,live_bytes=4096)
        if tick:
            return tick
        key = (authority["artifact_filename"], authority["artifact_sha256"])
        rows = [row for row in artifacts if (row["filename"], row["sha256"]) == key]
        if len(rows) != 1 or key in seen:
            return "member_provenance_incomplete"
        seen.add(key)
        row = rows[0]
        if row["upstream_authority"] != authority["authority_id"]:
            return "member_provenance_incomplete"
        for key in ("signed_index_sha256","detached_signature_sha256","accepted_signer_fingerprint","verifier_sha256"):
            if authority[key] != row[key]:
                return "member_provenance_incomplete"
        if not isinstance(candidate,dict) or not isinstance(platform,dict):
            return "member_provenance_incomplete"
        if (authority["candidate_id"],authority["generation"],authority["manifest_sha256"],authority["rootfs_identity"],authority["golden_sha256"]) != (candidate["id"],recipe["generation"],manifest_sha,platform["rootfs_identity"],recipe["selected_broker_golden_sha256"]):
            return "member_provenance_incomplete"
    expected = {(row["filename"], row["sha256"]) for row in artifacts if row["upstream_authority"] is not None}
    if expected != seen:
        return "member_provenance_incomplete"
    if approval is not None:
        if not isinstance(candidate,dict) or not isinstance(platform,dict):
            return "member_provenance_incomplete"
        artifact_sha,cause = metered_identity(artifacts,resources,meter)
        if cause:
            return cause
        signers = sorted({row["accepted_signer_fingerprint"] for row in artifacts if row["accepted_signer_fingerprint"] is not None})
        signer_sha,cause = metered_identity(signers,resources,meter)
        if cause:
            return cause
        expected = {"artifact_set_sha256":artifact_sha,"signer_set_sha256":signer_sha,"manifest_sha256":manifest_sha,
            "candidate_id":candidate["id"],"generation":recipe["generation"],"rootfs_identity":platform["rootfs_identity"],
            "golden_sha256":recipe["selected_broker_golden_sha256"]}
        if {key:approval[key] for key in expected} != expected:
            return "member_provenance_incomplete"
        approval_sha,cause = metered_identity(expected,resources,meter)
        if cause:
            return cause
        if approval_sha != approval["approval_sha256"]:
            return "member_provenance_incomplete"
    return None
