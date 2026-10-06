"""Whole publisher joins for the nine Sol037 residuals.

Source correspondence only. Absence of a future typed input stays
NOT_PROVEN. No cap, sentinel, or authority flag is relaxed here.
"""

from canonical import domain_digest
from contract import ContractError, is_digest
from resource_meter import HashlibProxy

hashlib = HashlibProxy()

BODY_FIELDS = (
    "members",
    "dependencies",
    "abi",
    "resources",
    "custody",
)


def exact_path_name(path, needle):
    if type(path) is not str or type(needle) is not str or needle == "":
        return False
    if path == needle:
        return True
    if needle.startswith("/") and path == needle[1:]:
        return True
    if "/" in needle:
        return False
    return path.rsplit("/", 1)[-1] == needle


def clear_body_aliases(presented):
    if type(presented) is not dict:
        return 0
    cleared = 0
    for key, value in list(presented.items()):
        if type(value) is bytes:
            presented[key] = None
            cleared += 1
    return cleared


class alias_window:
    def __init__(self, presented):
        self.presented = presented

    def __enter__(self):
        return self.presented

    def __exit__(self, exc_type, exc, tb):
        clear_body_aliases(self.presented)
        return False


def bind_other_document(row, contract, custody_body, selected_bound, performing=None):
    cap = contract.get("capability") if type(contract) is dict else None
    result = contract.get("verification_result") if type(contract) is dict else None
    actor = {
        "document_kind": row["document_kind"],
        "key_fingerprint": None if type(cap) is not dict else cap.get("key_fingerprint"),
        "path": row["path"],
        "produced_by_this_package": False,
        "result_sha256": None if type(result) is not dict else domain_digest(
            "friday.lab815.verification-result.v1", result
        ),
        "runtime": "NOT_RUN",
        "sha256": row["sha256"],
        "size": row["size"],
    }
    # Actor custody is a later domain. It is not written back into raw custody.
    actor_sha = domain_digest("friday.lab839.external-actor-custody.v1", actor)
    # An OpenPGP Ubuntu/Node capability cannot be promoted into an unrelated
    # class actor. That role stays separate from a typed installed observation.
    if cap is not None or result is not None:
        raise ContractError('document_kind')
    observation_closed = bool(performing is not None and
        performing['status']=='STRUCTURALLY_BOUND' and custody_body is not None and selected_bound)
    if observation_closed:
        actor=performing['body']['custody']
        actor_sha=performing['custody_sha256']
    runtime_join = None if performing is None else performing.get('runtime_consumer')
    runtime_closed = bool(observation_closed and type(runtime_join) is dict
        and runtime_join.get('status') == 'STRUCTURALLY_BOUND'
        and runtime_join.get('full_body_consumed') is True
        and runtime_join.get('document_kind') == row['document_kind']
        and runtime_join.get('custody_sha256') == domain_digest('friday.sol037.document-custody.v1', custody_body))
    return {
        "actor_custody": actor,
        "actor_custody_sha256": actor_sha,
        "cause": "full_future_class_actor_consumed" if runtime_closed else "full_material_selector_runtime_inventory_absent",
        'installed_observation_status':'STRUCTURALLY_BOUND' if observation_closed else 'NOT_PROVEN',
        "material_status": "STRUCTURALLY_BOUND" if runtime_closed else "NOT_PROVEN",
        "publisher_proof": False,
        "runtime": "NOT_RUN",
        'class_runtime_consumer':runtime_join,
    }


def join_node_projection(node, expected_node):
    if type(node) is not dict or type(expected_node) is not dict:
        return {
            "cause": "node_projection_absent",
            "publisher_proof": False,
            "runtime": "NOT_RUN",
            "status": "NOT_PROVEN",
        }
    archive_sha = node.get("archive_sha256")
    authoritative = expected_node.get("authoritative_sha256")
    diagnostic = expected_node.get("diagnostic_sha256")
    name_ok = node.get("archive_filename") == expected_node.get("filename")
    size_ok = node.get("archive_size") == expected_node.get("size")
    sha_ok = is_digest(authoritative) and archive_sha == authoritative
    diagnostic_rejected = not (archive_sha == diagnostic and archive_sha != authoritative)
    closed = bool(
        name_ok and size_ok and sha_ok and diagnostic_rejected and node.get("full_contract_closed") is True
    )
    return {
        "cause": "node_projected_metadata",
        "diagnostic_rejected": bool(diagnostic_rejected),
        "name_ok": bool(name_ok),
        "publisher_proof": False,
        "runtime": "NOT_RUN",
        "sha_ok": bool(sha_ok),
        "size_ok": bool(size_ok),
        "status": "STRUCTURALLY_BOUND" if closed else "NOT_PROVEN",
    }


def _member_names(section):
    if type(section) is not dict:
        return None
    members = section.get("members")
    if type(members) is not list:
        return None
    found = []
    for item in members:
        if type(item) is str and item != "":
            found.append(item)
        elif type(item) is dict and type(item.get("name")) is str and item["name"] != "":
            found.append(item["name"])
        else:
            return None
    for key in BODY_FIELDS[1:]:
        if type(section.get(key)) is not dict or not section[key]:
            return None
    return found


def closure_join_status(parsed_bodies, data_packages, interpreter_packages):
    if type(parsed_bodies) is not dict or "data" not in parsed_bodies or "native" not in parsed_bodies:
        return "NOT_PROVEN"
    data_names = _member_names(parsed_bodies["data"])
    native_names = _member_names(parsed_bodies["native"])
    if data_names is None or native_names is None:
        return "NOT_PROVEN"
    have = set(data_names) | set(native_names)
    required = []
    for item in list(data_packages) + list(interpreter_packages):
        if type(item) is str and item != "":
            required.append(item)
        elif type(item) is dict and type(item.get("name")) is str:
            required.append(item["name"])
        else:
            return "NOT_PROVEN"
    if not required or any(name not in have for name in required):
        return "NOT_PROVEN"
    # This old five-field shape has no independent expected installed/edge/
    # ABI/runtime consumer. Full typed bodies are consumed by performing_contracts.
    return "NOT_PROVEN"


def _rows(items, fields):
    rows = []
    for item in items:
        if type(item) is not dict:
            raise ContractError("operation_dependency")
        rows.append({field: item.get(field) for field in fields})
    return rows


def operation_specific_inputs(name, members, expected, composition, presented):
    statuses = {item["status"] for item in members}
    status = members[0]["status"] if len(statuses) == 1 else "NOT_PROVEN"
    record = {
        "join_status": status,
        "member_count": len(members),
        "operation": name,
        "publisher_proof": False,
        "runtime": "NOT_RUN",
    }
    if name == "authenticate-ubuntu-indexes":
        record["indexes"] = _rows(
            expected["ubuntu_minimum"]["indexes"],
            ("id", "suite", "component", "packages_sha256", "size"),
        )
    elif name == "authenticate-ubuntu-archives":
        record["archives"] = _rows(
            composition["packages"],
            ("name", "version", "architecture", "filename", "sha256", "size"),
        )
    elif name == "authenticate-node-archive":
        node = expected["node"]
        record["node"] = {
            "authoritative_sha256": node.get("authoritative_sha256"),
            "diagnostic_sha256": node.get("diagnostic_sha256"),
            "filename": node.get("filename"),
            "size": node.get("size"),
        }
    elif name == "hold-unrar-publisher-gap":
        record["unrar"] = {
            "publisher_digest": expected["unrar"].get("publisher_digest"),
            "status": status,
        }
    elif name == "authenticate-wheels":
        record["wheels"] = _rows(
            composition["wheels"],
            ("filename", "sha256", "size", "requires_python"),
        )
    elif name == "map-cpython-venv":
        record["venv_sha256"] = None if not members else members[0].get("content_sha256")
    elif name == "map-lib-dynload":
        record["lib_dynload"] = presented.get("lib_dynload")
    elif name == "map-native-loader":
        record["loader_sha256"] = presented.get("loader_sha256")
    elif name == "map-browser-resources":
        record["browser"] = _rows(expected["browser"]["archives"], ("filename", "sha256", "size"))
    elif name == "map-data-closure":
        record["roles"] = [item["path"] for item in members]
    elif name == "bind-candidate":
        record["candidate"] = {
            "commit": expected["candidate"]["commit"],
            "tree": expected["candidate"]["tree"],
        }
    elif name == "bind-golden":
        record["golden_sha256"] = presented.get("golden_sha256")
    elif name == "assemble-members":
        record["predecessor_sha256"] = None if not members else members[0].get("input_sha256")
    elif name == "write-final-manifest":
        record["emitted_member_sha256"] = None if not members else members[0].get("content_sha256")
    elif name == "external-custody":
        custody = presented.get("custody")
        record["custody_sha256"] = custody.get("sha256") if type(custody) is dict else None
    else:
        raise ContractError("recipe_operations")
    return record


def bind_member_hierarchy(members):
    by_path = {}
    unknown = 0
    inconsistent = 0
    for member in members:
        path = member["path"]
        roots=('/opt/friday/quality-toolchain/venv','/work/candidate','/inputs/golden')
        parent = "" if path in roots else path.rsplit("/", 1)[0] if "/" in path else ""
        if member.get("parent_path") != parent:
            inconsistent += 1
        if path in by_path:
            inconsistent += 1
        by_path[path] = member
        if member.get("device") is None or member.get("uid") is None or member.get("gid") is None:
            unknown += 1
    for member in members:
        parent = member.get("parent_path") or ""
        if parent == "":
            continue
        if parent not in by_path:
            inconsistent += 1
            continue
        holder = by_path[parent]
        if holder.get("kind") != "directory":
            inconsistent += 1
        if member.get("device") is not None and holder.get("device") not in (None, member.get("device")):
            inconsistent += 1
        for key in ("uid", "gid"):
            if member.get(key) is not None and holder.get(key) not in (None, member.get(key)):
                inconsistent += 1
    closed = unknown == 0 and inconsistent == 0 and len(members) > 0
    return {
        "closed": closed,
        "inconsistent": inconsistent,
        "member_count": len(members),
        "publisher_proof": False,
        "runtime": "NOT_RUN",
        "status": "STRUCTURALLY_BOUND" if closed else "NOT_PROVEN",
        "unknown_device_or_owner": unknown,
    }


def a009_domain_map(emitted_body, manifest, presented, operations, custody_consumer=None):
    emitted_sha = None
    if type(emitted_body) is str:
        emitted_sha = hashlib.sha256(emitted_body.encode("ascii")).hexdigest()
    inventory_sha = manifest.get("manifest_sha256") if type(manifest) is dict else None
    custody = presented.get("custody") if type(presented) is dict else None
    custody_sha = custody.get("sha256") if type(custody) is dict else None
    body = {
        "custody_sha256": custody_sha,
        "domains_distinct": bool(
            emitted_sha is not None and inventory_sha is not None and emitted_sha != inventory_sha
        ),
        "emitted_manifest_file_sha256": emitted_sha,
        "inventory_manifest_sha256": inventory_sha,
        "operation_count": len(operations),
        "self_digest_excluded": True,
    }
    preimage=dict(body)
    body["domain_sha256"] = domain_digest("friday.lab839.a009-emitted-domain.v1", preimage)
    body['domain_preimage']=preimage
    body['custody_consumer']=custody_consumer
    body["closed"] = bool(body["domains_distinct"] and custody_consumer is not None
        and custody_consumer['status']=='STRUCTURALLY_BOUND'
        and custody_consumer['raw_sha256']==custody_sha
        and custody_consumer['body']['input_body'].get('inventory_manifest_sha256')==inventory_sha
        and custody_consumer['body']['input_body'].get('emitted_manifest_file_sha256')==emitted_sha)
    body["publisher_proof"] = False
    body["runtime"] = "NOT_RUN"
    return body


def admit_approved_kernel(artifacts, computed, pins):
    contract = computed.get("kernel_contract_sha256") if type(computed) is dict else None
    kernel_pins = []
    if type(pins) is list:
        for pin in pins:
            if type(pin) is dict and pin.get("document_kind") == "kernel":
                kernel_pins.append(pin)
    rows = []
    admitted = False
    if len(kernel_pins) == 2 and is_digest(contract):
        for pin in kernel_pins:
            digest = pin.get("sha256")
            size = pin.get("size")
            if not is_digest(digest) or type(size) is not int or isinstance(size, bool):
                rows = []
                break
            rows.append({"sha256": digest, "size": size,'path':pin['relative_path']})
        if len(rows) == 2 and len({row['path'] for row in rows})==2:
            rows.sort(key=lambda item: (item["sha256"], item["size"]))
            admitted = domain_digest("friday.lab839.kernel-addition.v1", rows) == contract
    for artifact in artifacts if type(artifacts) is list else []:
        if artifact.get("class") != "kernel":
            continue
        if artifact.get("upstream_authority") in (None, ""):
            artifact["upstream_authority"] = "NOT_PROVEN"
        if not admitted:
            artifact["upstream_authority"] = "NOT_PROVEN"
        else:
            chosen=[row for row in rows if row['path']==artifact['filename']]
            if len(chosen)!=1 or artifact.get('sha256')!=chosen[0]['sha256'] or artifact.get('size')!=chosen[0]['size']:
                admitted=False
                artifact['upstream_authority']='NOT_PROVEN'
    return {
        "admitted": admitted,
        "publisher_proof": False,
        "required_count": 2,
        "runtime": "NOT_RUN",
        "supplied_count": len(kernel_pins),
    }


def derive_root_input(approval, raw_receipt, computed, envelope, kernel, approval_result=None):
    cap = raw_receipt.get("verifier_capability") if type(raw_receipt) is dict else None
    dependencies = cap.get("dependencies") if type(cap) is dict else None
    environment = cap.get("environment_entries") if type(cap) is dict else None
    ordered = [
        {"step": "issuer", "value": approval.get("issuer_id")},
        {"step": "key", "value": approval.get("key_fingerprint")},
        {
            "step": "raw_result",
            "value": None if type(raw_receipt) is not dict else raw_receipt.get("verification_result_sha256"),
        },
        {"step": "material_capability", "value": cap},
        {"step": "root_capability", "value": approval.get("capability_sha256")},
        {"step": "root_verification_result", "value": approval_result},
        {
            "step": "dependency",
            "value": None if type(dependencies) is not list else domain_digest(
                "friday.lab839.capability-dependencies.v1", dependencies
            ),
        },
        {
            "step": "environment",
            "value": None if type(environment) is not list else domain_digest(
                "friday.lab839.capability-environment.v1", environment
            ),
        },
        {
            "step": "verifier",
            "value": None if type(cap) is not dict else {
                k:cap.get(k) for k in ('executable','executable_sha256','argv','algorithm_class','keyring_sha256')},
        },
        {"step": "actor_custody", "value": approval.get("custody_sha256")},
        {"step": "artifact_order", "value": envelope["owner_approval"]["artifact_order_sha256"]},
        {"step": "signer_set", "value": envelope["owner_approval"]["signer_set_sha256"]},
    ]
    values_closed = all(item["value"] not in (None, "") for item in ordered)
    material_closed=(type(cap) is dict and type(dependencies) is list and
        bool(dependencies) and type(environment) is list and
        is_digest(cap.get('executable_sha256')) and is_digest(cap.get('dependency_closure_sha256')) and
        is_digest(cap.get('environment_digest')) and raw_receipt.get('proof_status') in ('AUTHENTICATED','STRUCTURALLY_BOUND'))
    root_closed=(type(approval_result) is dict and approval_result.get('decision')=='AUTHENTICATED' and
        approval_result.get('capability_sha256')==approval.get('capability_sha256') and
        approval_result.get('custody_sha256')==approval.get('custody_sha256'))
    return {
        "closed": bool(values_closed and material_closed and root_closed and kernel.get("admitted") is True),
        "kernel": kernel,
        "ordered": ordered,
        "publisher_proof": False,
        "release_trusted": False,
        "runtime": "NOT_RUN",
    }


def expected_public_output(spec, golden=None):
    if type(spec) is not dict or "cause" not in spec or "stage" not in spec or "status" not in spec:
        raise ContractError("control_unknown")
    if golden is not None:
        # The independently selected full golden, never a stage/status summary.
        return golden
    if spec.get("match") == "refuse" or spec.get("status") == "REFUSED":
        return {
            "accepted": False,
            "cause": spec["cause"],
            "effects_denied": True,
            "go": False,
            "publisher_proof": False,
            "ready_for_exec": False,
            "schema": "friday.sol037.public-refusal.v1",
            "stage": spec["stage"],
            "status": "REFUSED",
        }
    return None
