"""Public construction planner. Six stages share one admitted ingress."""

from resource_meter import HashlibProxy
hashlib = HashlibProxy()

from authority import project_trust
from bill import (
    compose_signed_materials,
    link_indexes,
    retain_historical_labels,
    wheel_filename_tag,
    wheel_requires_python_view,
)
from canonical import canonical_bytes, domain_digest, projection_digest
from contract import (
    MAX_ACTIVE_SLOTS,
    MAX_OUTPUT_BYTES,
    MAX_PACKAGES_BYTES,
    MAX_WHOLE_READ_BYTES,
    ContractError,
    _STAGE,
    is_digest,
)
from effects import admit_effect
from fixtures import capability as fixture_capability
from fixtures import clearsign, package_bytes, release_bytes
from formats import parse_clearsign, parse_deb822, parse_release, select_package
from ingress import admit_document, admit_ingress, load_pinned_schema
from pins import (
    ARCHITECTURE,
    BILL_SHA256,
    CANDIDATE_COMMIT,
    CANDIDATE_TREE,
    CHROMIUM_REVISION,
    FFMPEG_REVISION,
    GOLDEN_COMMIT,
    HEADLESS_REVISION,
    INDEX_COUNT,
    NODE_FILENAME,
    NODE_SIZE,
    NODE_VERSION,
    ORIGINAL_ARTIFACT_MAX,
    ORIGINAL_SNAPSHOT_MEMBER_MAX,
    ORIGINAL_STREAM_SLOTS,
    UBUNTU_MINIMUM_COUNT,
    WHEEL_COUNT,
    OPERATION_COUNT,
    PLATFORM,
    PLAYWRIGHT_VERSION,
    PYTHON_ABI,
    PYTHON_VERSION,
    RESOLUTE_UPDATES_INRELEASE_HISTORICAL,
    RESOLUTE_UPDATES_INRELEASE_OBSERVED,
    UBUNTU_FINGERPRINT,
    UBUNTU_ISSUER_ID,
    UNRAR_STATUS,
    UNRAR_VERSION,
    WHEEL_ISSUER_ID,
)
from producer_consumer import bind_producer_consumer
from receipt_chain import assess_node_chain, assess_ubuntu_chain, compare_archive, correspond_algorithms, retain_expected_digest
from schema_validate import validate_document
from resource_meter import WholeMeter, current, reserve_allocation, checkpoint, attach_refusal
from document_windows import FilePageSource
from document_vector import assess_document_vector
from material_literals import consume_wheel_literals
from body_scope import document_scope, temporary_slot
from document_vector import _Lease
from performing_contracts import consume as consume_performing, operation_input, descriptor as performing_descriptor
from whole_join import (
    a009_domain_map,
    alias_window,
    bind_member_hierarchy,
    closure_join_status,
    exact_path_name,
    operation_specific_inputs,
)

_APPROVED_ROOTS = (
    "/opt/friday/quality-toolchain/venv",
    "/work/candidate",
    "/inputs/golden",
)
_VENV_TEXT = "home = /usr/bin\ninclude-system-site-packages = false\nversion = 3.14.4\n"
_FORBIDDEN = ("maintainer_scripts", "source_builds", "sandbox_disable", "quality_scope_reduction", "dynamic_latest")
_DATA_ROLES = ("certificates", "fonts", "locales", "timezones", "nss", "glib", "gpu")
_KIND_CONSUMERS = (
    "ubuntu-inrelease",
    "ubuntu-packages",
    "ubuntu-archive",
    "node-shasums256",
    "node-archive",
)
OPERATIONS = (
    {"name": "authenticate-ubuntu-indexes", "dependencies": (), "destination": "var/lib/apt/lists"},
    {"name": "authenticate-ubuntu-archives", "dependencies": ("authenticate-ubuntu-indexes",), "destination": "var/cache/apt/archives"},
    {"name": "authenticate-node-archive", "dependencies": ("authenticate-ubuntu-archives",), "destination": "opt/node"},
    {"name": "hold-unrar-publisher-gap", "dependencies": ("authenticate-node-archive",), "destination": "opt/unrar"},
    {"name": "authenticate-wheels", "dependencies": ("hold-unrar-publisher-gap",), "destination": "/opt/friday/quality-toolchain/venv/lib/python3.14/site-packages"},
    {"name": "map-cpython-venv", "dependencies": ("authenticate-wheels",), "destination": "/opt/friday/quality-toolchain/venv"},
    {"name": "map-lib-dynload", "dependencies": ("map-cpython-venv",), "destination": "usr/lib/python3.14/lib-dynload"},
    {"name": "map-native-loader", "dependencies": ("map-lib-dynload",), "destination": "lib64"},
    {"name": "map-browser-resources", "dependencies": ("map-native-loader",), "destination": "opt/browser"},
    {"name": "map-data-closure", "dependencies": ("map-browser-resources",), "destination": "usr/share"},
    {"name": "bind-candidate", "dependencies": ("map-data-closure",), "destination": "/work/candidate"},
    {"name": "bind-golden", "dependencies": ("bind-candidate",), "destination": "/inputs/golden"},
    {"name": "assemble-members", "dependencies": ("bind-golden",), "destination": "var/lib/rootfs"},
    {"name": "write-final-manifest", "dependencies": ("assemble-members",), "destination": "var/lib/rootfs/manifest"},
    {"name": "external-custody", "dependencies": ("write-final-manifest",), "destination": "var/lib/custody"},
)
_CLASS_ISSUER = {
    "ubuntu-minimum": UBUNTU_ISSUER_ID,
    "wheel": WHEEL_ISSUER_ID,
    "node": "nodejs.org",
}


def classify_member(path, kind, link_target=None):
    if type(path) is not str or path == "" or "\x00" in path or path.startswith("\\"):
        raise ContractError("member_path")
    parts = path.split("/")
    absolute = path.startswith("/")
    if absolute:
        if not any(path == root or path.startswith(root + "/") for root in _APPROVED_ROOTS):
            raise ContractError("member_path")
        body = parts[1:]
    else:
        if path.startswith("/"):
            raise ContractError("member_path")
        body = parts
    if any(part in ("", ".", "..") for part in body):
        raise ContractError("member_path")
    if kind not in ("regular", "directory", "symlink"):
        raise ContractError("member_kind")
    resolved = None
    if kind == "symlink":
        if type(link_target) is not str or link_target == "" or "\x00" in link_target or link_target.startswith("/"):
            raise ContractError("link_escape")
        stack = body[:-1]
        for part in link_target.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if not stack:
                    raise ContractError("link_escape")
                stack.pop()
                continue
            stack.append(part)
        resolved = ("/" if absolute else "") + "/".join(stack)
    return {
        "path": path,
        "folded": "/".join(part.casefold() for part in parts if part),
        "kind": kind,
        "link_target": link_target,
        "resolved": resolved,
        "effects_available": False,
    }


def _mode(kind, executable):
    if kind == "symlink":
        return "0777"
    if kind == "directory" or executable:
        return "0555"
    if kind == "regular":
        return "0444"
    raise ContractError("mode")


def _member(path, kind, content_sha256, status, executable=False, link_target=None, size=None, uid=None, gid=None):
    classified = classify_member(path, kind, link_target)
    classified["mode"] = _mode(kind, executable)
    classified["content_sha256"] = content_sha256
    classified["status"] = status
    classified["executable"] = bool(executable) and kind == "regular"
    classified["size"] = size
    classified["uid"] = uid
    classified["gid"] = gid
    classified["parent_path"] = "" if path in _APPROVED_ROOTS else path.rsplit("/", 1)[0] if "/" in path else ""
    if kind == "regular":
        classified["nlink"] = None
    return classified


def _member_from_fact(fact):
    kind = fact["kind"]
    mode = fact["mode"]
    member = _member(
        fact["path"],
        kind,
        fact["content_sha256"],
        fact["status"],
        executable=(kind == "regular" and mode == "0555"),
        link_target=fact["link_target"],
        size=fact["size"],
        uid=fact["uid"],
        gid=fact["gid"],
    )
    required_mode = _mode(kind, kind == "regular" and mode == "0555")
    if mode != required_mode:
        raise ContractError("mode")
    member["mode"] = required_mode
    nlink = fact["nlink"]
    if kind == "regular":
        if type(nlink) is not int or isinstance(nlink, bool) or nlink != 1:
            raise ContractError("nlink")
        member["nlink"] = 1
    elif kind == "directory":
        if type(nlink) is int and not isinstance(nlink, bool) and nlink < 2:
            raise ContractError("nlink")
        if type(nlink) is int and not isinstance(nlink, bool):
            member["nlink"] = nlink
    elif type(nlink) is int and not isinstance(nlink, bool):
        member["nlink"] = nlink
    member["claimed_link_target_sha256"] = fact["link_target_sha256"]
    member["device"] = fact.get("device")
    member["mount_domain"] = fact.get("mount_domain")
    member["parent_path"] = fact.get("parent_path")
    # An observed digest is not a complete filesystem observation.
    if member["status"] == "STRUCTURALLY_BOUND" and any(member.get(k) is None for k in ("size", "uid", "gid", "nlink", "device", "mount_domain", "parent_path")):
        member["status"] = "NOT_PROVEN"
        member["cause"] = "complete_member_fact_absent"
    return member


def _admit_whole_resource():
    document = {
        "active_slots_max": MAX_ACTIVE_SLOTS,
        "artifact_max": ORIGINAL_ARTIFACT_MAX,
        "deadline_sec": None,
        "document_bytes_max": MAX_PACKAGES_BYTES,
        "member_max": ORIGINAL_SNAPSHOT_MEMBER_MAX,
        "output_bytes_max": MAX_OUTPUT_BYTES,
        "role": "whole-contract",
        "schema": "friday.lab820.resource.v1",
        "sha256": None,
        "size": None,
        "status": "NOT_PROVEN",
        "whole_read_max": MAX_WHOLE_READ_BYTES,
    }
    validate_document(document, load_pinned_schema("friday.lab820.resource.v1"))
    return document


class _ConstructionMeter:
    def __init__(self):
        if MAX_ACTIVE_SLOTS != ORIGINAL_STREAM_SLOTS or MAX_ACTIVE_SLOTS != 128:
            raise ContractError("held_stream")
        if MAX_WHOLE_READ_BYTES != 512 * MAX_PACKAGES_BYTES:
            raise ContractError("aggregate_budget")
        self.slots = 0
        self.slot_owner = None
        self.peak_slots = 0
        self.document_bytes = 0
        self.members = 0
        self.read_bytes = 0
        self.documents = 0
        self.resource = _admit_whole_resource()

    def charge_slots(self, count):
        if type(count) is not int or isinstance(count, bool) or count < 0:
            raise ContractError("held_stream")
        if self.slots > MAX_ACTIVE_SLOTS-count:
            raise ContractError("held_stream")
        owner=current()
        if self.slots and owner is not self.slot_owner:
            raise ContractError("body_slot_owner")
        if owner is not None:
            owner.slots(count)
        self.slot_owner=owner
        self.slots += count
        self.peak_slots = max(self.peak_slots, self.slots)

    def release_slots(self, count):
        if type(count) is not int or isinstance(count, bool) or count < 0 or count > self.slots:
            if current() is not None: current().cleanup_fault = True
            return False
        # WholeMeter resets its ContextVar before terminal handover. Charge
        # belongs to its original exact owner, not whatever current() says now.
        if self.slot_owner is not None:
            if self.slot_owner.retire_slots(count) is not True: return False
        self.slots -= count
        if not self.slots:self.slot_owner=None
        return True

    def charge_document(self, size):
        if type(size) is not int or isinstance(size, bool) or size < 0:
            raise ContractError("held_stream")
        if size > MAX_PACKAGES_BYTES:
            raise ContractError("aggregate_budget")
        self.documents += 1
        if size > self.document_bytes:
            self.document_bytes = size

    def charge_read(self, size):
        if type(size) is not int or isinstance(size, bool) or size < 0:
            raise ContractError("held_stream")
        if size == 0:
            return
        if self.read_bytes > MAX_WHOLE_READ_BYTES - size:
            raise ContractError("aggregate_budget")
        if current() is not None:
            current().read(size)
        self.read_bytes += size

    def charge_allocation(self, size):
        reserve_allocation(size)


def _hex_payload(value):
    if type(value) is not str or value == "" or not value.isascii():
        raise ContractError("held_stream")
    try:
        reserve_allocation(len(value)//2)
        return bytes.fromhex(value)
    except ValueError as exc:
        raise ContractError("held_stream") from exc


def _dict_payload(page, allow_absent):
    has_body = "body" in page
    has_hex = "body_hex" in page
    if not has_body and not has_hex:
        if allow_absent:
            return None
        raise ContractError("held_stream")
    body = None
    if has_body:
        if type(page["body"]) is not bytes:
            raise ContractError("held_stream")
        body = page["body"]
    if has_hex:
        decoded = _hex_payload(page["body_hex"])
        if body is not None and decoded != body:
            raise ContractError("held_stream")
        body = decoded
    return body


def _page_body(page, declared, digest, meter=None):
    if type(declared) is not int or isinstance(declared, bool) or declared < 0:
        raise ContractError("held_stream")
    if declared == 0:
        if type(page) is bytes:
            if len(page) != 0:
                raise ContractError("held_stream")
            return None
        if type(page) is str:
            if page != "":
                raise ContractError("held_stream")
            return None
        if type(page) is not dict:
            raise ContractError("held_stream")
        payload = _dict_payload(page, True)
        if payload not in (None, b""):
            raise ContractError("held_stream")
        return None
    if type(page) is bytes:
        payload = page
    elif type(page) is str:
        payload = _hex_payload(page)
    elif type(page) is dict:
        payload = _dict_payload(page, False)
    else:
        raise ContractError("held_stream")
    if len(payload) > declared:
        raise ContractError("aggregate_budget")
    if len(payload) != declared:
        raise ContractError("held_stream")
    if meter is not None:
        meter.charge_read(len(payload))
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ContractError("held_stream")
    return payload


class _Held:
    def __init__(self, pages, meter):
        self.pages = pages
        self.assemblies = {}
        self.meter = meter

    def _join(self, kind, path, digest):
        record = self.pages.get((kind, path))
        if record is None:
            return None
        if digest is not None and record["digest"] != digest:
            raise ContractError("held_stream")
        cell=temporary_slot(self.meter,1)
        self.meter.charge_allocation(record["size"])
        blob = b"".join(record["pages"])
        cell['body']=blob
        if len(blob) != record["size"] or hashlib.sha256(blob).hexdigest() != record["digest"]:
            raise ContractError("held_stream")
        # The scope owns the full producer result/charge through actual consumer
        # completion, including join/hash failure and retained traceback frames.
        return blob

    def get(self, digest):
        if type(digest) is not str:
            return None
        selected = [key for key, record in self.pages.items() if record["digest"] == digest]
        if len(selected) > 1:
            raise ContractError("ambiguous_document")
        if not selected:
            return None
        kind, path = selected[0]
        lease=_Lease(self,kind,path,digest)
        lease.__enter__()
        return lease.body

    def get_document(self, kind, path, digest=None):
        return self._join(kind, path, digest)


def _reject_page_oversize(page, declared):
    if type(page) is bytes and len(page) > declared:
        raise ContractError("aggregate_budget")
    if type(page) is str and len(page) // 2 > declared:
        raise ContractError("aggregate_budget")
    if type(page) is dict:
        body = page.get("body")
        if type(body) is bytes and len(body) > declared:
            raise ContractError("aggregate_budget")
        encoded = page.get("body_hex")
        if type(encoded) is str and len(encoded) // 2 > declared:
            raise ContractError("aggregate_budget")


def _bind_streams(context, streams, meter=None):
    if meter is None:
        meter = _ConstructionMeter()
    if context is None:
        if streams is not None:
            raise ContractError("held_stream")
        return {}
    if type(streams) is FilePageSource:
        return streams.bind(context, meter)
    if streams is None:
        streams = {}
    if type(streams) is not dict:
        raise ContractError("held_stream")
    sequence = context["page_sequence"]
    if sequence is None:
        chosen = context["streams"]
        window = MAX_ACTIVE_SLOTS
    elif type(sequence) is list:
        chosen = sequence
        window = 512
    else:
        raise ContractError("held_stream")
    if type(chosen) is not list or len(chosen) > window:
        raise ContractError("held_stream")
    identities = set()
    digests = set()
    grouped = {}
    order = []
    for item in chosen:
        if type(item) is not dict:
            raise ContractError("held_stream")
        digest = item["sha256"]
        page_index = item["page_index"]
        page_count = item["page_count"]
        page_size = item["page_size"]
        if type(page_index) is not int or isinstance(page_index, bool):
            raise ContractError("held_stream")
        if type(page_count) is not int or isinstance(page_count, bool):
            raise ContractError("held_stream")
        if type(page_size) is not int or isinstance(page_size, bool):
            raise ContractError("held_stream")
        if page_count < 1 or page_index < 0 or page_index >= page_count or page_size < 0:
            raise ContractError("held_stream")
        if page_size > MAX_PACKAGES_BYTES:
            raise ContractError("aggregate_budget")
        identity = (item["path"], item["kind"], page_index)
        if identity in identities:
            raise ContractError("held_stream")
        identities.add(identity)
        digests.add(digest)
        key = (item["path"], item["kind"])
        if key not in grouped:
            order.append(key)
            grouped[key] = []
        grouped[key].append(item)
    if type(sequence) is list:
        catalog = {item["sha256"] for item in context["streams"]}
        if not catalog.issubset(digests):
            raise ContractError("held_stream")
    if set(streams) != digests:
        raise ContractError("held_stream")
    pages = {}
    for key in order:
        pending = grouped[key]
        meter.charge_slots(len(pending))
        records = []
        for item in pending:
            digest = item["sha256"]
            page = streams[digest]
            if type(page) is dict:
                if page.get("page_size") != item["page_size"] or page.get("page_index") != item["page_index"]:
                    raise ContractError("held_stream")
                if page.get("sha256") not in (None, digest):
                    raise ContractError("held_stream")
            meter.charge_read(item["page_size"])
            _reject_page_oversize(page, item["page_size"])
            body = _page_body(page, item["page_size"], digest, meter)
            records.append({
                "body": body,
                "body_sha256": item.get("body_sha256"),
                "custody_sha256": item.get("custody_sha256"),
                "kind": item["kind"],
                "page_count": item["page_count"],
                "page_index": item["page_index"],
                "page_size": item["page_size"],
                "path": item["path"],
                "sha256": digest,
                "size": item["size"],
            })
        declared_sizes = {item["size"] for item in records}
        counts = {item["page_count"] for item in records}
        if len(declared_sizes) != 1 or len(counts) != 1:
            raise ContractError("held_stream")
        declared = records[0]["size"]
        if type(declared) is not int or isinstance(declared, bool) or declared < 0:
            raise ContractError("held_stream")
        if declared > MAX_PACKAGES_BYTES:
            raise ContractError("aggregate_budget")
        total = sum(item["page_size"] for item in records)
        if total > MAX_PACKAGES_BYTES:
            raise ContractError("aggregate_budget")
        meter.charge_document(declared)
        positive = any(item["page_size"] > 0 for item in records)
        if positive:
            body_ids = {item["body_sha256"] for item in records}
            if len(body_ids) != 1 or not is_digest(records[0]["body_sha256"]):
                raise ContractError("held_stream")
            count = records[0]["page_count"]
            indexes = sorted(item["page_index"] for item in records)
            if len(records) != count or indexes != list(range(count)):
                raise ContractError("held_stream")
            if total != declared:
                raise ContractError("held_stream")
            if any(item["body"] is None for item in records):
                raise ContractError("held_stream")
            ordered = sorted(records, key=lambda item: item["page_index"])
            meter.charge_allocation(total)
            blob = b"".join(item["body"] for item in ordered)
            if len(blob) != total or len(blob) > MAX_PACKAGES_BYTES:
                raise ContractError("aggregate_budget")
            body_id = records[0]["body_sha256"]
            if hashlib.sha256(blob).hexdigest() != body_id:
                raise ContractError("held_stream")
            page_bodies = []
            for item in ordered:
                page_bodies.append(item["body"])
                item["body"] = None
            pages[(key[1], key[0])] = {"digest": body_id, "pages": page_bodies, "size": total}
            del blob
        # Resident page bodies remain reachable in pages and in caller streams.
        # Their slots are retained for the complete public call lifetime.
        if not positive:
            meter.release_slots(len(records))
    return _Held(pages, meter)


def enforce_dag(operations):
    if type(operations) is not list or not operations:
        raise ContractError("operation_dependency")
    names = []
    for operation in operations:
        name = operation["name"]
        if type(name) is not str or name in names:
            raise ContractError("duplicate_operation")
        names.append(name)
    index = {name: position for position, name in enumerate(names)}
    for operation in operations:
        dependencies = operation.get("dependencies", [])
        if type(dependencies) is not list and type(dependencies) is not tuple:
            raise ContractError("operation_dependency")
        seen = set()
        for dependency in dependencies:
            if dependency in seen or dependency not in index or index[dependency] >= index[operation["name"]]:
                raise ContractError("operation_dependency")
            seen.add(dependency)
    return True


def _approval(manifest_sha256):
    approval = {
        "schema": "friday.lab815.external-issuer-approval.v1",
        "produced_by_this_package": False,
        "effects_granted": False,
        "issuer_id": "unpinned-root",
        "key_fingerprint": UBUNTU_FINGERPRINT,
        "capability_sha256": None,
        "attempt_generation": 1,
        "artifact_set_sha256": None,
        "signer_set_sha256": None,
        "manifest_sha256": manifest_sha256,
        "recipe_sha256": None,
        "rootfs_sha256": None,
        "golden_sha256": None,
        "tool_sha256": None,
        "candidate_commit": CANDIDATE_COMMIT,
        "candidate_tree": CANDIDATE_TREE,
        "golden_commit": GOLDEN_COMMIT,
        "bill_sha256": BILL_SHA256,
        "approval_sha256": None,
        "projection_digest": None,
        "signature_sha256": None,
        "custody_sha256": None,
    }
    approval["approval_sha256"] = domain_digest("friday.lab815.external-approval-absent.v1", {"status": "ABSENT"})
    approval["projection_digest"] = projection_digest(approval)
    return approval


def _require_operations(expected):
    rows = expected["operations"]
    if type(rows) is not list or not rows:
        raise ContractError("recipe_operations")
    enforce_dag([{"name": row["name"], "dependencies": list(row["dependencies"])} for row in rows])
    if len(rows) != OPERATION_COUNT or len(OPERATIONS) != OPERATION_COUNT:
        raise ContractError("recipe_operations")
    for spec, admitted in zip(OPERATIONS, rows):
        if admitted["name"] != spec["name"] or tuple(admitted["dependencies"]) != spec["dependencies"]:
            raise ContractError("recipe_operations")
        if admitted["effects_available"] is not False or admitted["link_policy"] != "lexical-chroot":
            raise ContractError("operation_policy")
        if type(admitted["member_limit"]) is not int or not 94 <= admitted["member_limit"] <= 20000:
            raise ContractError("member_limit")
    return rows


def _kinds(expected):
    found = {}
    for row in expected["document_kinds"]:
        if row["consumer"] in found:
            raise ContractError("document_kind")
        found[row["consumer"]] = row["document_kind"]
    for consumer in _KIND_CONSUMERS:
        if found.get(consumer) != consumer:
            raise ContractError("document_kind")
    return found


def _bytes(value):
    if value is None:
        return None
    if type(value) is bytes:
        return value
    if type(value) is not str:
        raise ContractError("document_type")
    if len(value)>80000000 or not value.isascii(): raise ContractError('document_size')
    reserve_allocation(len(value)+1)
    try:
        return value.encode("ascii")
    except UnicodeError as exc:
        raise ContractError("document_type") from exc


def _hex_bytes(value):
    if value is None:
        return None
    if type(value) is not str:
        raise ContractError("archive_type")
    if len(value)>160000000 or not value.isascii(): raise ContractError('document_size')
    reserve_allocation(len(value)//2+1)
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise ContractError("archive_type") from exc


def _pin_row(presented, kind, sha256, size):
    if not is_digest(sha256) or type(size) is not int or isinstance(size, bool):
        return None
    found = None
    for pin in presented.get("held_member_pins") or []:
        if pin.get("document_kind") == kind and pin.get("sha256") == sha256 and pin.get("size") == size:
            if found is not None:
                return None
            found = pin
    return found


def _pin_matches(presented, kind, sha256, size):
    return _pin_row(presented, kind, sha256, size) is not None


def _owners(pin):
    if pin is None:
        return None, None
    uid = pin.get("uid")
    gid = pin.get("gid")
    if type(uid) is not int or isinstance(uid, bool):
        uid = None
    if type(gid) is not int or isinstance(gid, bool):
        gid = None
    return uid, gid


def _observation_status(item):
    if type(item) is not dict:
        return "NOT_PROVEN"
    if item.get("status") == "ADMITTED" and is_digest(item.get("sha256")):
        return "STRUCTURALLY_BOUND"
    return "NOT_PROVEN"


def _adopt_fact_field(member, key, value):
    current = member.get(key)
    if value is None:
        return
    if current is None or current == value:
        member[key] = value
        return
    raise ContractError("operation_dependency")


def _apply_operation_facts(name, members, context):
    if context is None:
        return members
    raw = context["operation_members"]
    if raw is None:
        return members
    if type(raw) is not list:
        raise ContractError("operation_dependency")
    if not raw:
        return members
    chosen = []
    for fact in raw:
        if type(fact) is not dict or fact.get("operation") != name:
            continue
        chosen.append(fact)
    if not chosen:
        return members
    if len(chosen) > ORIGINAL_SNAPSHOT_MEMBER_MAX:
        raise ContractError("member_limit")
    built = []
    seen = set()
    for fact in chosen:
        path = fact.get("path")
        if type(path) is not str or path in seen:
            raise ContractError("duplicate_member")
        seen.add(path)
        built.append(_member_from_fact(fact))
    if True:
        # Independently supplied facts may complete unknown metadata, but may
        # not replace the computation or the selected external custody bytes.
        actual={item["path"]:item for item in members}
        if set(actual)!=seen:
            raise ContractError("operation_computation")
        for item in built:
            computed=actual[item["path"]]
            for field in ("kind","mode","link_target","content_sha256","size"):
                if computed.get(field) is not None and item.get(field)!=computed[field]:
                    raise ContractError("operation_computation")
    if len(built) > ORIGINAL_SNAPSHOT_MEMBER_MAX:
        raise ContractError("member_limit")
    return built


def _plan_performing_operations(expected,presented,composition,context,meter):
    specs=_require_operations(expected)
    descriptors=context['performing_contracts']['operations']
    if {row['target'] for row in descriptors}!={row['name'] for row in specs} or len(descriptors)!=OPERATION_COUNT:
        raise ContractError('recipe_operations')
    if composition['full_document_vector']['all_materials_closed'] is not True or composition['closure_view']['full_runtime_dependency_custody_joins']!='STRUCTURALLY_BOUND':
        raise ContractError('operation_predecessor_pending')
    outputs={}; produced=[]
    for spec in specs:
        predecessors=[outputs[name] for name in spec['dependencies']]
        inputs=operation_input(spec['name'],expected,composition,presented,predecessors)
        joined=consume_performing(context,composition['_held_provider'],'operations',spec['name'],inputs)
        if joined is None:
            raise ContractError('operation_computation')
        observation=joined['body']
        inventory=[]
        for fact in observation['members']:
            clean={k:v for k,v in fact.items() if k!='source_ref'}
            inventory.append(_member_from_fact(clean))
        if bind_member_hierarchy(inventory)['closed'] is not True:
            raise ContractError('operation_computation')
        owned=set(observation['output_paths'])
        if any(f['operation']!=spec['name'] for f in observation['members'] if f['path'] in owned):
            raise ContractError('operation_computation')
        members=[m for m in inventory if m['path'] in owned]
        if not members or len(members)>spec['member_limit']:
            raise ContractError('member_limit')
        emitted=None
        if spec['name'] in ('assemble-members','write-final-manifest'):
            rows=[dict(m) for m in produced]
            blob=canonical_bytes(rows)
            digest=hashlib.sha256(blob).hexdigest()
            path='var/lib/rootfs/members' if spec['name']=='assemble-members' else 'var/lib/rootfs/manifest/manifest'
            files=[m for m in members if m['path']==path]
            if len(files)!=1 or files[0]['content_sha256']!=digest or files[0]['size']!=len(blob):
                raise ContractError('operation_computation')
            if spec['name']=='write-final-manifest':emitted=blob.decode('ascii')
        inherited=domain_digest('friday.sol037.full-predecessor-closure.v1',
            [{'name':p['name'],'output_sha256':p['output_sha256']} for p in predecessors])
        predecessor_closed=all(p['status']=='STRUCTURALLY_BOUND' for p in predecessors)
        for member in members:
            member['input_sha256']=inherited
            if not predecessor_closed:member['status']='NOT_PROVEN'
        if len(produced)>ORIGINAL_SNAPSHOT_MEMBER_MAX-len(members):
            raise ContractError('member_limit')
        if any(m['path'] in {p['path'] for p in produced} for m in members):
            raise ContractError('duplicate_member')
        body={'name':spec['name'],'destination':spec['destination'],
              'dependencies':list(spec['dependencies']),'effects_available':False,
              'members':members,'publisher_proof':False,'specific_inputs':inputs,
              'performing_observation':joined,'fixed_fallback':False}
        status='STRUCTURALLY_BOUND' if predecessor_closed and all(m['status']=='STRUCTURALLY_BOUND' for m in members) else 'NOT_PROVEN'
        outputs[spec['name']]={'name':spec['name'],'destination':spec['destination'],
            'members':members,'effects_available':False,'publisher_proof':False,'status':status,
            'full_output_body':body,'output_sha256':domain_digest('friday.sol037.full-operation-output.v1',body),
            'performing_ref':{k:performing_descriptor(context,'operations',spec['name'])[k] for k in ('kind','path','sha256','document_sha256','offset','size')},
            'emitted_body_canonical':emitted}
        produced.extend(members)
    if bind_member_hierarchy(produced)['closed'] is not True:
        raise ContractError('operation_computation')
    meter.members=len(produced)
    return [outputs[row['name']] for row in specs],produced


def _plan_operations(expected, presented, composition, context=None, meter=None):
    if context is not None and context.get('performing_contracts') is not None and context['performing_contracts']['operations']:
        return _plan_performing_operations(expected,presented,composition,context,meter)
    specs = _require_operations(expected)
    limits = {row["name"]: row["member_limit"] for row in specs}
    if len(expected["ubuntu_minimum"]["indexes"]) > limits["authenticate-ubuntu-indexes"]:
        raise ContractError("member_limit")
    if len(composition["packages"]) > limits["authenticate-ubuntu-archives"]:
        raise ContractError("member_limit")
    if len(expected["wheels"]["items"]) > limits["authenticate-wheels"]:
        raise ContractError("member_limit")
    if len(expected["browser"]["archives"]) > limits["map-browser-resources"]:
        raise ContractError("member_limit")
    if len(_DATA_ROLES) > limits["map-data-closure"]:
        raise ContractError("member_limit")
    by_name = {}
    packages = composition["packages"]
    indexes = expected["ubuntu_minimum"]["indexes"]
    index_members = []
    for index in indexes:
        pin = _pin_row(presented, "ubuntu-index", index["packages_sha256"], index["size"])
        uid, gid = _owners(pin)
        index_members.append(_member(
            "var/lib/apt/lists/" + index["id"],
            "regular",
            index["packages_sha256"],
            "STRUCTURALLY_BOUND" if pin is not None else "NOT_PROVEN",
            size=index["size"],
            uid=uid,
            gid=gid,
        ))
    archive_members = []
    for package in packages:
        pin = _pin_row(presented, "ubuntu-archive", package["sha256"], package["size"])
        status = "STRUCTURALLY_BOUND" if package["observation_status"] == "ADMITTED" or pin is not None else "NOT_PROVEN"
        uid, gid = _owners(pin)
        archive_members.append(_member(
            "var/cache/apt/archives/" + package["filename"],
            "regular",
            package["sha256"],
            status,
            size=package["size"],
            uid=uid,
            gid=gid,
        ))
    node = expected["node"]
    if node["filename"] != NODE_FILENAME or node["version"] != NODE_VERSION or node["size"] != NODE_SIZE:
        raise ContractError("node_identity")
    if node["diagnostic_is_authority"] is not False:
        raise ContractError("node_authority")
    node_pin = _pin_row(presented, "node-archive", node["authoritative_sha256"], NODE_SIZE)
    if node_pin is not None:
        node_sha = node["authoritative_sha256"]
        node_status = "STRUCTURALLY_BOUND"
    else:
        node_sha = None
        node_status = "NOT_PROVEN"
    node_uid, node_gid = _owners(node_pin)
    node_member = _member("opt/node/" + NODE_FILENAME, "regular", node_sha, node_status, size=NODE_SIZE, uid=node_uid, gid=node_gid)
    unrar = expected["unrar"]
    if unrar["version"] != UNRAR_VERSION or unrar["candidate_binary_is_provenance"] is not False:
        raise ContractError("unrar_gap")
    unrar_digest = presented["unrar_publisher_digest"]
    if unrar_digest is None and unrar["publisher_digest"] is None:
        unrar_status = "BLOCKED_PUBLISHER_GAP"
    elif is_digest(unrar_digest) and unrar_digest == unrar["publisher_digest"]:
        unrar_status = "STRUCTURALLY_BOUND"
    else:
        unrar_status = "BLOCKED_PUBLISHER_GAP"
    unrar_pin = None
    for pin in presented.get("held_member_pins") or []:
        if pin.get("document_kind") == "unrar" and is_digest(unrar_digest) and pin.get("sha256") == unrar_digest:
            unrar_pin = pin
            break
    unrar_uid, unrar_gid = _owners(unrar_pin)
    unrar_size = unrar_pin.get("size") if unrar_pin is not None and type(unrar_pin.get("size")) is int else None
    unrar_member = _member(
        "opt/unrar/unrar",
        "regular",
        unrar_digest if is_digest(unrar_digest) else None,
        unrar_status,
        executable=unrar_status == "STRUCTURALLY_BOUND",
        size=unrar_size,
        uid=unrar_uid,
        gid=unrar_gid,
    )
    wheel_members = []
    for item in composition["wheels"]:
        view = item
        pin = _pin_row(presented, "wheel", view["sha256"], view["size"])
        uid, gid = _owners(pin)
        wheel_members.append(_member(
            "/opt/friday/quality-toolchain/venv/lib/python3.14/site-packages/" + view["filename"],
            "regular",
            view["sha256"],
            "STRUCTURALLY_BOUND" if pin is not None else "NOT_PROVEN",
            size=view["size"],
            uid=uid,
            gid=gid,
        ))
    if "include-system-site-packages = false" not in _VENV_TEXT.split("\n"):
        raise ContractError("venv_site")
    venv_raw = _VENV_TEXT.encode("ascii")
    venv_member = _member(
        "/opt/friday/quality-toolchain/venv/pyvenv.cfg",
        "regular",
        hashlib.sha256(venv_raw).hexdigest(),
        "STRUCTURALLY_BOUND",
        size=len(venv_raw),
    )
    dyn_row = presented["lib_dynload"] if type(presented["lib_dynload"]) is dict else None
    dyn = _observation_status(dyn_row)
    dyn_sha = dyn_row["sha256"] if dyn == "STRUCTURALLY_BOUND" else None
    dyn_size = dyn_row.get("size") if dyn == "STRUCTURALLY_BOUND" and type(dyn_row.get("size")) is int else None
    dyn_member = _member("usr/lib/python3.14/lib-dynload", "directory", dyn_sha, dyn, size=dyn_size)
    loader = "STRUCTURALLY_BOUND" if is_digest(presented["loader_sha256"]) else "NOT_PROVEN"
    loader_member = _member(
        "lib64/ld-linux-x86-64.so.2",
        "regular",
        presented["loader_sha256"] if loader == "STRUCTURALLY_BOUND" else None,
        loader,
        executable=loader == "STRUCTURALLY_BOUND",
    )
    browser = expected["browser"]
    if browser["playwright"] != PLAYWRIGHT_VERSION or browser["chromium"] != CHROMIUM_REVISION:
        raise ContractError("browser_identity")
    if browser["headless"] != HEADLESS_REVISION or browser["ffmpeg"] != FFMPEG_REVISION:
        raise ContractError("browser_identity")
    browser_members = []
    for archive in browser["archives"]:
        sha = archive["sha256"] if is_digest(archive.get("sha256")) else None
        size = archive["size"] if type(archive.get("size")) is int and not isinstance(archive.get("size"), bool) else None
        pin = _pin_row(presented, "browser", sha, size) if sha is not None and size is not None else None
        uid, gid = _owners(pin)
        browser_members.append(_member(
            "opt/browser/" + archive["filename"],
            "regular",
            sha,
            "STRUCTURALLY_BOUND" if pin is not None else "NOT_PROVEN",
            size=size,
            uid=uid,
            gid=gid,
        ))
    data_members = []
    admitted_data = {}
    for row in presented["data_observations"]:
        if row["role"] in admitted_data:
            raise ContractError("duplicate_observation")
        admitted_data[row["role"]] = row
    for role in _DATA_ROLES:
        row = admitted_data.get(role)
        status = _observation_status(row) if row is not None else "NOT_PROVEN"
        digest = row["sha256"] if status == "STRUCTURALLY_BOUND" else None
        size = row.get("size") if status == "STRUCTURALLY_BOUND" and type(row.get("size")) is int else None
        data_members.append(_member("usr/share/" + role, "regular", digest, status, size=size))
    if expected["candidate"]["commit"] != CANDIDATE_COMMIT or expected["candidate"]["tree"] != CANDIDATE_TREE:
        raise ContractError("candidate")
    candidate_sha = domain_digest("friday.lab822.candidate-identity.v1", {
        "commit": expected["candidate"]["commit"],
        "tree": expected["candidate"]["tree"],
    })
    candidate_member = _member("/work/candidate/TREE", "regular", candidate_sha, "NOT_PROVEN")
    golden_sha = presented["golden_sha256"] if is_digest(presented["golden_sha256"]) else None
    golden_pin = None
    if golden_sha is not None:
        for pin in presented.get("held_member_pins") or []:
            if pin.get("document_kind") == "golden" and pin.get("sha256") == golden_sha:
                golden_pin = None if golden_pin is not None else pin
                if golden_pin is None:
                    break
    golden_status = "STRUCTURALLY_BOUND" if golden_sha is not None else "NOT_PROVEN"
    golden_uid, golden_gid = _owners(golden_pin)
    golden_size = golden_pin.get("size") if golden_pin is not None else None
    golden_member = _member(
        "/inputs/golden/TREE",
        "regular",
        golden_sha,
        golden_status,
        size=golden_size,
        uid=golden_uid,
        gid=golden_gid,
    )
    groups = {
        "authenticate-ubuntu-indexes": index_members,
        "authenticate-ubuntu-archives": archive_members,
        "authenticate-node-archive": [node_member],
        "hold-unrar-publisher-gap": [unrar_member],
        "authenticate-wheels": wheel_members,
        "map-cpython-venv": [venv_member],
        "map-lib-dynload": [dyn_member],
        "map-native-loader": [loader_member],
        "map-browser-resources": browser_members,
        "map-data-closure": data_members,
        "bind-candidate": [candidate_member],
        "bind-golden": [golden_member],
    }
    produced = []
    for spec in OPERATIONS:
        checkpoint()
        emitted_body = None
        members = groups.get(spec["name"])
        if spec["name"] == "assemble-members":
            content = domain_digest("friday.lab820.operation-input.v1", [
                {
                    "content_sha256": item["content_sha256"],
                    "gid": item.get("gid"),
                    "mode": item["mode"],
                    "nlink": item.get("nlink"),
                    "path": item["path"],
                    "size": item.get("size"),
                    "status": item["status"],
                    "uid": item.get("uid"),
                }
                for item in produced
            ])
            members = [_member("var/lib/rootfs/members", "regular", content, "STRUCTURALLY_BOUND")]
        elif spec["name"] == "write-final-manifest":
            rows = [
                {
                    "content_sha256": item["content_sha256"],
                    "gid": item.get("gid"),
                    "kind": item["kind"],
                    "link_target": item.get("link_target"),
                    "mode": item["mode"],
                    "nlink": item.get("nlink"),
                    "path": item["path"],
                    "size": item.get("size"),
                    "status": item["status"],
                    "uid": item.get("uid"),
                }
                for item in produced
            ]
            blob = canonical_bytes(rows)
            if len(blob) > MAX_OUTPUT_BYTES:
                raise ContractError("aggregate_budget")
            if meter is not None:
                meter.charge_allocation(len(blob))
            manifest_digest = hashlib.sha256(blob).hexdigest()
            emitted_body = blob.decode("ascii")
            members = [_member(
                "var/lib/rootfs/manifest/manifest",
                "regular",
                manifest_digest,
                "STRUCTURALLY_BOUND",
                size=len(blob),
            )]
        elif spec["name"] == "external-custody":
            custody_row = presented["custody"] if type(presented["custody"]) is dict else None
            custody = _observation_status(custody_row)
            digest = custody_row["sha256"] if custody == "STRUCTURALLY_BOUND" else None
            custody_size = custody_row.get("size") if custody == "STRUCTURALLY_BOUND" and type(custody_row.get("size")) is int else None
            members = [_member("var/lib/custody/receipt", "regular", digest, custody, size=custody_size)]
        members = _apply_operation_facts(spec["name"], members, context)
        if len(members) > limits[spec["name"]]:
            raise ContractError("member_limit")
        if spec["dependencies"]:
            predecessors = [by_name[name] for name in spec["dependencies"]]
            predecessor_closed = all(item["status"] == "STRUCTURALLY_BOUND" for item in predecessors)
            inherited = domain_digest("friday.sol037.full-predecessor-closure.v1", predecessors)
            for member in members:
                member["input_sha256"] = inherited
                if member["status"] == "ACCEPTED":
                    member["status"] = "NOT_PROVEN"
                    member["cause"] = "unbound_accepted"
                elif not predecessor_closed and member["status"] == "STRUCTURALLY_BOUND":
                    member["status"] = "NOT_PROVEN"
                    member["cause"] = "predecessor_pending"
        if len(produced)>ORIGINAL_SNAPSHOT_MEMBER_MAX-len(members):
            raise ContractError("member_limit")
        produced.extend(members)
        statuses = {item["status"] for item in members}
        specific_inputs = operation_specific_inputs(spec["name"], members, expected, composition, presented)
        full_output_body = {
            "name":spec["name"],"destination":spec["destination"],
            "dependencies":list(spec["dependencies"]),"effects_available":False,
            "members":members,"publisher_proof":False,
            "specific_inputs":specific_inputs,"fixed_fallback":True,
        }
        output_sha256 = domain_digest("friday.sol037.full-operation-output.v1", full_output_body)
        by_name[spec["name"]] = {
            "name": spec["name"],
            "status": members[0]["status"] if len(statuses) == 1 else "NOT_PROVEN",
            "effects_available": False,
            "destination": spec["destination"],
            "members": members,
            "output_sha256": output_sha256,
            "publisher_proof": False,
            "full_output_body": full_output_body,
            "emitted_body_canonical": emitted_body,
        }
    if len(produced) > ORIGINAL_SNAPSHOT_MEMBER_MAX:
        raise ContractError("member_limit")
    if meter is not None:
        meter.members = len(produced)
        if meter.members > ORIGINAL_SNAPSHOT_MEMBER_MAX:
            raise ContractError("member_limit")
    return [by_name[spec["name"]] for spec in OPERATIONS], produced


def _mount_path(path):
    roots = (
        ("/opt/friday/quality-toolchain/venv", "venv"),
        ("/work/candidate", "candidate"),
        ("/inputs/golden", "golden"),
    )
    for root, domain in roots:
        if path == root or path.startswith(root + "/"):
            relative = path[len(root):].lstrip("/")
            return domain, relative or "root"
    relative = path[1:] if path.startswith("/") else path
    return "rootfs", relative


def _lex_join(base, target):
    if type(target) is not str or target == "" or target.startswith("/") or "\x00" in target:
        raise ContractError("link_escape")
    stack = base.split("/")[:-1] if base else []
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                raise ContractError("link_escape")
            stack.pop()
            continue
        stack.append(part)
    return "/".join(stack)


def _follow_components(mount, base, target, by_key, hop_limit):
    if type(target) is not str or target == "" or target.startswith("/") or "\x00" in target:
        raise ContractError("link_escape")
    parts = base.split("/")[:-1] if base else []
    pending = target.split("/")
    seen = []
    hops = 0
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ContractError("link_escape")
            parts.pop()
            continue
        parts.append(part)
        cursor = "/".join(parts)
        owner = by_key.get((mount, cursor))
        if pending and owner is None:
            raise ContractError("link_dangling")
        if pending and owner is not None and owner["kind"] not in ("directory", "symlink"):
            raise ContractError("member_kind")
        if owner is not None and owner["kind"] == "symlink" and pending:
            if cursor in seen:
                raise ContractError("link_loop")
            seen.append(cursor)
            hops += 1
            if hops > hop_limit:
                raise ContractError("link_loop")
            link = owner["link_target"]
            if type(link) is not str or link == "" or link.startswith("/") or "\x00" in link:
                raise ContractError("link_escape")
            parts.pop()
            pending = link.split("/") + pending
    return "/".join(parts)


def _close_links(members):
    by_key = {}
    for item in members:
        key = (item["mount_domain"], item["path"])
        if key in by_key:
            raise ContractError("duplicate_member")
        by_key[key] = item
    for item in members:
        if item["kind"] != "symlink":
            continue
        mount = item["mount_domain"]
        cursor = _follow_components(mount, item["path"], item["link_target"], by_key, len(members) + 1)
        seen = []
        owner = None
        for _ in range(len(members) + 1):
            if (mount, cursor) not in by_key:
                raise ContractError("link_dangling")
            if cursor in seen:
                raise ContractError("link_loop")
            owner = by_key[(mount, cursor)]
            if owner["kind"] != "symlink":
                break
            seen.append(cursor)
            cursor = _follow_components(mount, owner["path"], owner["link_target"], by_key, len(members) + 1)
        else:
            raise ContractError("link_loop")
        digest = hashlib.sha256(item["link_target"].encode("utf-8")).hexdigest()
        claimed = item.pop("claimed_link_target_sha256", None)
        if claimed is not None and claimed != digest:
            raise ContractError("link_target_identity")
        item["link_target_sha256"] = digest
    return True


def final_manifest_projection(operations):
    if len(operations) != OPERATION_COUNT:
        raise ContractError("recipe_operations")
    seen = set()
    folded = set()
    members = []
    for operation in operations:
        if operation["effects_available"] is not False:
            raise ContractError("effects_available")
        for member in operation["members"]:
            if member["path"] in seen or member["folded"] in folded:
                raise ContractError("duplicate_member")
            seen.add(member["path"])
            folded.add(member["folded"])
            mount, relative = _mount_path(member["path"])
            supplied_mount = member.get("mount_domain")
            if supplied_mount is not None and supplied_mount != mount:
                raise ContractError("mount_domain")
            raw_nlink = member.get("nlink")
            if member["kind"] == "regular":
                if raw_nlink is not None and (type(raw_nlink) is not int or isinstance(raw_nlink, bool) or raw_nlink != 1):
                    raise ContractError("nlink")
                nlink = raw_nlink
            elif member["kind"] == "directory":
                if raw_nlink is None:
                    nlink = None
                elif type(raw_nlink) is int and not isinstance(raw_nlink, bool) and raw_nlink >= 2:
                    nlink = raw_nlink
                else:
                    raise ContractError("nlink")
            elif type(raw_nlink) is int and not isinstance(raw_nlink, bool):
                nlink = raw_nlink
            else:
                nlink = None
            row = {
                "path": relative,
                "kind": member["kind"],
                "mode": member["mode"],
                "nlink": nlink,
                "uid": member.get("uid"),
                "gid": member.get("gid"),
                "executable": member["executable"],
                "mount_domain": mount,
                "size": member.get("size") if type(member.get("size")) is int else None,
                "sha256": member["content_sha256"] if is_digest(member["content_sha256"]) else None,
                "link_target": member["link_target"],
                "link_target_sha256": None,
                "device": member.get("device"),
                "parent_path": None if member.get("parent_path") is None else _mount_path(member["parent_path"])[1],
            }
            if "claimed_link_target_sha256" in member:
                row["claimed_link_target_sha256"] = member["claimed_link_target_sha256"]
            members.append(row)
    members.sort(key=lambda item: (item["mount_domain"], item["path"].encode("utf-8")))
    _close_links(members)
    hashed = list(members)
    return {
        "schema": "friday.lab815.final-manifest-projection.v1",
        "member_count": len(members),
        "members": members,
        "manifest_sha256": domain_digest("friday.lab815.final-manifest.v1", hashed),
        "rootfs_sha256": domain_digest("friday.lab822.rootfs-members.v1", hashed),
        "image_sha256": None,
        "effects_available": False,
        "publisher_proof": False,
    }


def _admitted_sha(members, needle):
    found = None
    for member in members:
        if not exact_path_name(member["path"], needle):
            continue
        digest = member.get("sha256")
        if not is_digest(digest):
            continue
        if found is not None and found != digest:
            raise ContractError("snapshot_identity")
        found = digest
    return found


def _compare_snapshot_identity(members, fills):
    needles = (
        ('creation_tool_sha256','creation-tool'),
        ('root_sha256','candidate-root'),
        ("broker_package_sha256", "broker"),
        ("import_suffixes_sha256", "import-suffixes"),
        ("kernel_contract_sha256", "kernel-contract"),
        ("loader_contract_sha256", "loader-contract"),
        ("preflight_sha256", "preflight"),
        ("quality_gate_sha256", "quality-gate"),
        ("runtime_contract_sha256", "runtime-contract"),
    )
    for field, needle in needles:
        filled = fills.get(field)
        if filled is None:
            continue
        admitted = _admitted_sha(members, needle)
        if not is_digest(filled) or admitted is None or filled != admitted:
            raise ContractError("snapshot_identity")
    controller = fills.get("controller_path")
    if controller is not None and not any(exact_path_name(member["path"], controller) for member in members):
        raise ContractError("snapshot_identity")


def project_snapshot(manifest, expected, materials_sha256, presented, context=None, held=None):
    fills = presented["snapshot_fields"]
    snapshot_members = []
    for member in manifest["members"]:
        snapshot_members.append({
            "executable": member["executable"],
            "gid": member["gid"],
            "kind": member["kind"],
            "link_target": member["link_target"],
            "link_target_sha256": member["link_target_sha256"],
            "mode": member["mode"],
            "mount_domain": member["mount_domain"],
            "nlink": member["nlink"],
            "path": member["path"],
            "sha256": member["sha256"],
            "size": member["size"],
            "uid": member["uid"],
            "device": member["device"],
            "parent_path": member["parent_path"],
        })
    candidate = {
        "broker_package_sha256": fills["broker_package_sha256"],
        "commit": expected["candidate"]["commit"],
        "controller_path": fills["controller_path"],
        "preflight_sha256": fills["preflight_sha256"],
        "quality_gate_sha256": fills["quality_gate_sha256"],
        "root_sha256": fills["root_sha256"],
        "tree": expected["candidate"]["tree"],
    }
    filled_rootfs = fills["rootfs_sha256"]
    computed_rootfs = manifest["rootfs_sha256"]
    if filled_rootfs not in (None, computed_rootfs):
        raise ContractError("rootfs_identity")
    _compare_snapshot_identity(manifest["members"], fills)
    platform = {
        "architecture": expected["platform"]["architecture"],
        "import_suffixes_sha256": fills["import_suffixes_sha256"],
        "kernel_contract_sha256": fills["kernel_contract_sha256"],
        "loader_contract_sha256": fills["loader_contract_sha256"],
        "python_abi": expected["platform"]["python_abi"],
        "rootfs_sha256": computed_rootfs,
    }
    snapshot_members.sort(key=lambda item: (item["mount_domain"], item["path"].encode("utf-8")))
    _close_links(snapshot_members)
    snapshot = {
        "candidate": candidate,
        "contract": "a009",
        "created_utc": fills["created_utc"],
        "creation_tool_sha256": fills["creation_tool_sha256"],
        "materials_sha256": materials_sha256,
        "members": snapshot_members,
        "platform": platform,
        "root_merkle_sha256": domain_digest("friday.lab820.snapshot-members.v1", snapshot_members),
        "runtime_contract_sha256": fills["runtime_contract_sha256"],
        "schema": "snapshot-manifest.v1",
        "snapshot_id": None,
    }
    snapshot["snapshot_id"] = domain_digest("friday.lab820.snapshot-id.v1", {
        key: value for key, value in snapshot.items() if key != "snapshot_id"
    })
    validate_document(snapshot, load_pinned_schema("snapshot-manifest.v1"))
    if context is not None and context.get('performing_contracts') is not None:
        observation=consume_performing(context,held,'snapshot','a009',
            {k:v for k,v in snapshot.items() if k!='snapshot_id'})
        if observation is None:
            raise ContractError('snapshot_identity')
        if observation['body']['runtime']['tool_sha256']!=snapshot['creation_tool_sha256']:
            raise ContractError('snapshot_identity')
    return snapshot


def _stage(status, cause):
    return {"status": status, "cause": cause, "publisher_proof": False, "effects_denied": True}



def _consume_closures(expected, context=None, held=None):
    data_packages = expected["data_packages"]
    interpreter_packages = expected["interpreter_packages"]
    native_closure = expected["native_closure"]
    data_closure = expected["data_closure"]
    if type(data_packages) is not list or type(interpreter_packages) is not list:
        raise ContractError("closure_membership")
    if type(native_closure) is not dict or type(data_closure) is not dict:
        raise ContractError("closure_membership")
    if len(data_packages) < 7 or len(interpreter_packages) < 7:
        raise ContractError("closure_membership")
    bodies={} if context is None or context["closure_bodies"] is None else context["closure_bodies"]
    body_bound={}
    parsed_bodies={}
    for role,contract in (("data",data_closure),("native",native_closure)):
        raw=bodies.get(role)
        if raw is None:
            body_bound[role]=False
            continue
        blob=raw.encode("ascii")
        from canonical import parse_exact
        parsed=parse_exact(blob,max_bytes=2000000,max_depth=12,max_items=512,max_string=8000)
        if type(parsed) is not dict or set(parsed)!={"members","dependencies","abi","resources","custody"}:
            raise ContractError("closure_membership")
        # Five-field diagnostic bodies and full typed bodies have distinct
        # selected identities. A legacy hash never drives full public closure.
        if hashlib.sha256(blob).hexdigest()!=contract["legacy_body_sha256"]:
            raise ContractError("closure_body")
        body_bound[role]=True
        parsed_bodies[role]=parsed
    performing={}
    if context is not None and context.get('performing_contracts') is not None:
        for role,contract in (("data",data_closure),("native",native_closure)):
            joined=consume_performing(context,held,'closures',role,
                {'role':role,'data_packages':data_packages,'interpreter_packages':interpreter_packages})
            if joined is not None:
                if joined['raw_sha256']!=contract.get('body_sha256'):
                    raise ContractError('closure_body')
                performing[role]=joined
    installed_join_status='STRUCTURALLY_BOUND' if set(performing)=={'data','native'} else closure_join_status(parsed_bodies,data_packages,interpreter_packages)
    runtime_closed = bool(set(performing)=={'data','native'} and all(
        p['runtime_consumer']['status']=='STRUCTURALLY_BOUND' and p['runtime_consumer']['full_body_consumed'] is True
        for p in performing.values()))
    status = 'STRUCTURALLY_BOUND' if runtime_closed and installed_join_status=='STRUCTURALLY_BOUND' else 'NOT_PROVEN'
    return {
        "data_closure": data_closure,
        "data_packages": data_packages,
        "interpreter_packages": interpreter_packages,
        "native_closure": native_closure,
        "publisher_proof": False,
        "data_body_sha256": data_closure.get("body_sha256") if is_digest(data_closure.get("body_sha256")) else None,
        "native_body_sha256": native_closure.get("body_sha256") if is_digest(native_closure.get("body_sha256")) else None,
        "status": status,
        'legacy_diagnostic_body_bound':body_bound,
        'legacy_diagnostic_body_identities':{r:c['legacy_body_sha256'] for r,c in (('data',data_closure),('native',native_closure))},
        "admitted_bodies": bodies,
        'performing_consumers':performing,
        'installed_dependency_resource_custody_preimage_joins':installed_join_status,
        "full_runtime_dependency_custody_joins": status,
        'runtime_remaining_cause':None if runtime_closed else 'full_future_class_runtime_inputs_absent',
    }


def _require_receipt_records(expected, context):
    if context is None:
        return {}
    receipts = context["document_receipts"]
    found = {}
    for item in receipts:
        if item["diagnostic_authority"] is not False:
            raise ContractError("diagnostic_authority")
        if item["body_held"] is True:
            matched = [
                page
                for page in (context["page_sequence"] if context["page_sequence"] is not None else context["streams"])
                if page["path"] == item["path"] and page["kind"] == item["document_kind"]
            ]
            if not matched:
                raise ContractError("body_not_paged")
            page_count = matched[0]["page_count"]
            indexes = []
            for page in matched:
                if page["page_count"] != page_count or page["page_size"] <= 0:
                    raise ContractError("body_not_paged")
                indexes.append(page["page_index"])
            indexes.sort()
            if len(matched) != page_count or indexes != list(range(page_count)):
                raise ContractError("body_not_paged")
        key = (item["document_kind"], item["path"])
        if key in found:
            raise ContractError("duplicate_receipt")
        found[key] = item
    packages = expected["ubuntu_minimum"]["packages"]
    if len(packages) < UBUNTU_MINIMUM_COUNT:
        raise ContractError("receipt_membership")
    for package in packages:
        key = ("ubuntu-archive", package["filename"])
        if key not in found:
            raise ContractError("receipt_membership")
        row = found[key]
        if row["sha256"] is not None and row["sha256"] != package["sha256"]:
            raise ContractError("receipt_membership")
        if row["size"] is not None and row["size"] != package["size"]:
            raise ContractError("archive_size")
    indexes = expected["ubuntu_minimum"]["indexes"]
    if len(indexes) < INDEX_COUNT:
        raise ContractError("receipt_membership")
    for index in indexes:
        receipt_path = index["id"] + "/" + index["member_name"]
        if ("ubuntu-packages", receipt_path) not in found:
            raise ContractError("receipt_membership")
        if ("ubuntu-inrelease", index["id"]) not in found:
            raise ContractError("receipt_membership")
        p_row=found[("ubuntu-packages",receipt_path)]
        i_row=found[("ubuntu-inrelease",index["id"])]
        if p_row["sha256"] not in (None,index["packages_sha256"]) or p_row["size"] not in (None,index["size"]) or i_row["sha256"] not in (None,index["inrelease_sha256"]):
            raise ContractError("receipt_membership")
    node = expected["node"]
    node_key = ("node-archive", node["filename"])
    if node_key not in found:
        raise ContractError("receipt_membership")
    node_row = found[node_key]
    selected = node_row["sha256"]
    authoritative = node.get("authoritative_sha256")
    if selected == node["diagnostic_sha256"] and selected != authoritative:
        raise ContractError("diagnostic_authority")
    if is_digest(authoritative) and selected is not None and selected != authoritative:
        raise ContractError("node_authority")
    if node_row["size"] != NODE_SIZE:
        raise ContractError("archive_size")
    wheels = expected["wheels"]["items"]
    if len(wheels) < WHEEL_COUNT:
        raise ContractError("receipt_membership")
    for item in wheels:
        key = ("wheel", item["filename"])
        if key not in found:
            raise ContractError("receipt_membership")
        row = found[key]
        if row["sha256"] is not None and row["sha256"] != item["sha256"]:
            raise ContractError("receipt_membership")
        if row["size"] is not None and row["size"] != item["size"]:
            raise ContractError("archive_size")
    if ("node-shasums256", "SHASUMS256.txt") not in found:
        raise ContractError("receipt_membership")
    return found




def _collect_held(context, held, kind):
    parts = []
    if type(held) is FilePageSource:
        return parts
    chosen_digest=context['inrelease_sha256'] if kind=='ubuntu-inrelease' else context['packages_sha256']
    candidates=[item for item in context['document_receipts'] if item['document_kind']==kind and item.get('body_held') is True]
    if len(candidates)!=1 and chosen_digest is None:
        return parts
    for item in candidates:
        if item["document_kind"] != kind or item.get("body_held") is not True:
            continue
        digest = item.get("sha256")
        if chosen_digest is not None and digest!=chosen_digest:
            continue
        lease=_Lease(held,kind,item['path'],digest)
        lease.__enter__()
        blob=lease.body
        if type(blob) is not bytes:
            raise ContractError("body_not_paged")
        parts.append(blob)
        if len(parts)>1:
            raise ContractError('ambiguous_document')
    return parts


def _receipts_closed(context, receipt_index, ubuntu_receipt, node_receipt):
    if context is None or context.get("require_predecessor_receipts") is not True:
        return True
    if ubuntu_receipt["proof_status"] not in ("STRUCTURALLY_BOUND", "AUTHENTICATED"):
        return False
    if node_receipt["proof_status"] not in ("STRUCTURALLY_BOUND", "AUTHENTICATED"):
        return False
    if type(receipt_index) is not dict:
        return False
    for item in receipt_index.values():
        if type(item) is not dict or item.get("decision") not in ("STRUCTURALLY_BOUND", "AUTHENTICATED"):
            return False
    return True


@document_scope
def _plan_construction(authority_raw, expected_raw, presented_raw, context_raw=None, streams=None):
    _STAGE[0] = "ingress"
    meter = _ConstructionMeter()
    context = None
    if context_raw is not None:
        context = admit_document(context_raw, "friday.lab824.independent-trust-context.v1")
        if current() is not None:
            current().configure(context["public_resource"])
    held = _bind_streams(context, streams, meter)
    authority, expected, presented = admit_ingress(authority_raw, expected_raw, presented_raw)
    with alias_window(presented):
        if context is not None:
            inrelease_parts = _collect_held(context, held, "ubuntu-inrelease")
            if inrelease_parts:
                # The retained diagnostic tuple may select one full document,
                # never concatenate separately signed messages.
                if len(inrelease_parts)==1:
                    current_body = presented["ubuntu_clearsign"]
                    if current_body is None:
                        presented["ubuntu_clearsign"] = inrelease_parts[0]
                    elif current_body != inrelease_parts[0]:
                        raise ContractError("held_shadows_presented")
            elif presented["ubuntu_clearsign"] is None and context["inrelease_sha256"] is not None:
                blob = held.get(context["inrelease_sha256"])
                if type(blob) is bytes:
                    presented["ubuntu_clearsign"] = blob
            package_parts = _collect_held(context, held, "ubuntu-packages")
            if package_parts:
                if len(package_parts)==1:
                    current_body = presented["packages_text"]
                    if current_body is None:
                        presented["packages_text"] = package_parts[0]
                    elif current_body != package_parts[0]:
                        raise ContractError("held_shadows_presented")
            elif presented["packages_text"] is None and context["packages_sha256"] is not None:
                blob = held.get(context["packages_sha256"])
                if type(blob) is bytes:
                    presented["packages_text"] = blob
        kinds = _kinds(expected)
        if presented["member_probe"] is not None:
            _STAGE[0] = "plan"
            classify_member(presented["member_probe"], presented["member_probe_kind"], presented["member_probe_link"])
            _STAGE[0] = "ingress"
        retain_historical_labels(expected)
        if expected["platform"]["architecture"] != ARCHITECTURE or expected["platform"]["operating_system"] != "ubuntu-26.04":
            raise ContractError("platform")
        if expected["platform"]["python_version"] != PYTHON_VERSION or expected["platform"]["python_abi"] != PYTHON_ABI:
            raise ContractError("python_abi")
        for name in _FORBIDDEN:
            if expected["policy"][name] != "FORBIDDEN":
                raise ContractError("policy")
        _STAGE[0] = "wheel"
        for item in expected["wheels"]["items"]:
            wheel_filename_tag(item)
        python_view = None
        if presented["requires_python_observation"] is not None:
            _STAGE[0] = "requires_python"
            python_view = wheel_requires_python_view(presented["requires_python_observation"])
            _STAGE[0] = "bill"
        chain = {
            "composed_ubuntu": 0,
            "composed_indexes": 0,
            "document_kinds": kinds,
            "dash_unescaped": False,
        }
        if presented["release_expected"] is not None:
            validate_document(presented["release_expected"], load_pinned_schema("friday.lab820.release-member.v1"))
        if presented["package_expected"] is not None:
            validate_document(presented["package_expected"], load_pinned_schema("friday.lab820.package-row.v1"))
        if presented["node_authoritative"] is not None:
            validate_document(presented["node_authoritative"], load_pinned_schema("friday.lab820.node-authoritative.v1"))
        if presented["approval_result"] is not None:
            validate_document(presented["approval_result"], load_pinned_schema("friday.lab820.approval-result.v1"))
        if presented["ubuntu_clearsign"] is not None:
            clearsign_raw = _bytes(presented["ubuntu_clearsign"])
            _STAGE[0] = "algorithm"
            parsed = parse_clearsign(clearsign_raw)
            algorithm_capability = presented["ubuntu_capability"] if presented["ubuntu_capability"] is not None else fixture_capability("ubuntu-inrelease")
            algorithm_result = presented["ubuntu_result"]
            if algorithm_result is not None and (type(algorithm_result) is not dict or "algorithm" not in algorithm_result):
                raise ContractError("algorithm_correspondence")
            if type(algorithm_capability) is dict and "algorithm_class" in algorithm_capability:
                correspond_algorithms(parsed, algorithm_capability, algorithm_result)
            chain["hash_header"] = parsed["hash_header"]
            chain["signed_body_sha256"] = parsed["signed_body_sha256"]
            chain["dash_unescaped"] = any(line.startswith(b"- ") for line in clearsign_raw.split(b"\n"))
            if presented["release_cleartext"] is not None:
                _STAGE[0] = "release"
                selected = parse_release(_bytes(presented["release_cleartext"]), presented["release_expected"])
                chain["release_member"] = selected["member_name"]
            if presented["packages_text"] is not None and presented["package_expected"] is not None:
                _STAGE[0] = "package_fields"
                chosen = select_package(_bytes(presented["packages_text"]), presented["package_expected"])
                chain["package_filename"] = chosen["filename"]
                chain["unknown_fields"] = chosen["unknown_fields"]
        elif presented["packages_text"] is not None:
            _STAGE[0] = "deb822"
            parse_deb822(_bytes(presented["packages_text"]), max_bytes=2000000)
        _STAGE[0] = "retain"
        retained = retain_expected_digest(RESOLUTE_UPDATES_INRELEASE_HISTORICAL, RESOLUTE_UPDATES_INRELEASE_OBSERVED)
        if retained["adopted_sha256"] != RESOLUTE_UPDATES_INRELEASE_HISTORICAL or retained["replaced"] is not False:
            raise ContractError("historical_pin_replaced")
        _STAGE[0] = "ubuntu_receipt"
        receipt_index = _require_receipt_records(expected, context)
        closure_view = _consume_closures(expected, context, held)
        document_vector = assess_document_vector(expected,context,held,receipt_index)
        document_vector["all_materials_closed"] = bool(
            document_vector["all_materials_closed"]
            and closure_view["full_runtime_dependency_custody_joins"] == "STRUCTURALLY_BOUND"
        )
        ubuntu_capability = presented["ubuntu_capability"] if presented["ubuntu_capability"] is not None else fixture_capability("ubuntu-inrelease")
        node_capability = presented["node_capability"] if presented["node_capability"] is not None else fixture_capability("node-shasums256")
        ubuntu_archive_pin = None
        if presented["package_expected"] is not None:
            want_sha = presented["package_expected"].get("sha256")
            want_size = presented["package_expected"].get("size")
            ubuntu_archive_pin = _pin_row(presented, "ubuntu-archive", want_sha, want_size)
        ubuntu_archive_bytes = _hex_bytes(presented["ubuntu_archive_hex"])
        if type(held) is not FilePageSource and ubuntu_archive_bytes is None and presented["package_expected"] is not None:
            want_sha = presented["package_expected"].get("sha256")
            blob = held.get(want_sha) if is_digest(want_sha) else None
            if type(blob) is bytes:
                ubuntu_archive_bytes = blob
                if ubuntu_archive_pin is None:
                    ubuntu_archive_pin = {"sha256": want_sha, "size": len(blob)}
        _STAGE[0] = "ubuntu_receipt"
        ubuntu_receipt = assess_ubuntu_chain(
            ubuntu_capability,
            presented["ubuntu_expected_capability"],
            authority["approved_fingerprint"],
            _bytes(presented["ubuntu_clearsign"]),
            _bytes(presented["packages_text"]),
            ubuntu_archive_bytes,
            presented["release_expected"],
            presented["package_expected"],
            load_pinned_schema("verifier-capability.v1"),
            load_pinned_schema("verification-result.v1"),
            load_pinned_schema("raw-publisher-receipt.v1"),
            result=presented["ubuntu_result"],
            archive_pin=ubuntu_archive_pin,
            expected_result_sha256=(
                context["ubuntu_expected_result_sha256"]
                if context is not None and context["ubuntu_expected_result_sha256"] is not None
                else authority["expected_result_sha256"]
            ),
        )
        if presented["ubuntu_signed_body_override"] is not None:
            ubuntu_receipt["signed_body_sha256"] = presented["ubuntu_signed_body_override"]
            from semantics import validate_receipt_semantics
            validate_receipt_semantics(ubuntu_receipt)
        if presented["synthetic_publisher_proof"] is True:
            ubuntu_receipt["publisher_proof"] = True
            from semantics import validate_receipt_semantics
            validate_receipt_semantics(ubuntu_receipt)
        _STAGE[0] = "node_receipt"
        node_auth_sha = authority["node_authoritative_sha256"]
        node_result_pin = None
        signer_sha = authority["node_signer_sha256"]
        if context is not None:
            if context["node_authoritative_sha256"] is not None:
                node_auth_sha = context["node_authoritative_sha256"]
            if context["node_expected_result_sha256"] is not None:
                node_result_pin = context["node_expected_result_sha256"]
            if context["node_signer_sha256"] is not None:
                signer_sha = context["node_signer_sha256"]
        if type(held) is not FilePageSource and context is not None and context["node_archive"] is not None:
            ref = context["node_archive"]
            node_pin = {"sha256": ref["sha256"], "size": ref["size"]}
            node_archive_bytes = held.get(ref["sha256"])
        else:
            node_pin = _pin_row(presented, "node-archive", node_auth_sha, NODE_SIZE)
            node_archive_bytes = None
            if node_pin is None and context is not None:
                pages = [item for item in context["streams"] if item["kind"] == "node-archive"]
                if len(pages) == 1 and pages[0]["sha256"] != expected["node"]["diagnostic_sha256"]:
                    node_pin = {"sha256": None, "size": pages[0]["size"]}
        if signer_sha is not None:
            used = node_capability["key_fingerprint"] if type(node_capability) is dict else None
            if type(used) is not str or hashlib.sha256(used.encode("ascii")).hexdigest() != signer_sha:
                raise ContractError("fingerprint_mismatch")
        if context is not None and type(held) is not FilePageSource:
            for item in context["document_receipts"]:
                if item["document_kind"] != "node-shasums256" or item["body_held"] is not True:
                    continue
                digest = item.get("sha256")
                blob = held.get_document("node-shasums256",item["path"],digest) if type(digest) is str else None
                if type(blob) is not bytes:
                    for page in context["streams"]:
                        if page["kind"] == "node-shasums256" and page["path"] == item["path"]:
                            blob = held.get(page["sha256"])
                            break
                if type(blob) is not bytes:
                    raise ContractError("body_not_paged")
                current_body = presented["node_clearsign"]
                if current_body is None:
                    presented["node_clearsign"] = blob
                elif current_body != blob:
                    raise ContractError("held_shadows_presented")
                break
        node_receipt = assess_node_chain(
            node_capability,
            presented["node_expected_capability"],
            authority["signer_fingerprint"],
            presented["node_authoritative"],
            _bytes(presented["node_clearsign"]),
            node_archive_bytes if type(node_archive_bytes) is bytes else None,
            load_pinned_schema("verifier-capability.v1"),
            load_pinned_schema("verification-result.v1"),
            load_pinned_schema("raw-publisher-receipt.v1"),
            result=presented["node_result"],
            archive_pin=node_pin,
            expected_result_sha256=node_result_pin,
            authority_archive_sha256=node_auth_sha,
        )
        _STAGE[0] = "bill"
        observations = list(presented["ubuntu_observations"])
        composition = compose_signed_materials(expected, observations, presented["wheel_observations"], receipt_index)
        composition["wheels"]=consume_wheel_literals(context,held,expected["wheels"]["items"],composition["wheels"])
        composition["full_document_vector"]=document_vector
        composition['closure_view']=closure_view
        # This private provider is consumed only by the full operation route;
        # it is removed before any public JSON metadata can escape.
        composition['_held_provider']=held
        material_by={row['path']:row for row in document_vector['other_documents'] if row['document_kind']=='wheel'}
        for view in composition['wheels']:
            proof=material_by.get(view['filename'])
            closed=proof is not None and proof['installed_observation_status']=='STRUCTURALLY_BOUND'
            if closed:
                joined=proof['performing_consumer']; body=joined['body']
                domains={'abi_sha256':domain_digest('friday.a117.abi.v1',body['abi']),
                         'body_sha256':proof['sha256'],
                         'custody_sha256':joined['custody_sha256'],
                         'installed_inventory_sha256':domain_digest('friday.a117.installed-members.v1',body['members']),
                         'member_sha256':domain_digest('friday.a117.installed-members.v1',body['members']),
                         'resource_sha256':domain_digest('friday.a117.resources.v1',body['resources']),
                         'selected_record_sha256':view['selected_artifact_selector_sha256']}
                for key,value in domains.items():
                    if view.get(key) is not None and view[key]!=value:
                        raise ContractError('wheel_observation')
                closed=all(is_digest(view.get(key)) and view[key]==value for key,value in domains.items())
                view['performing_domain_preimages']={'abi':body['abi'],'members':body['members'],
                    'resources':body['resources'],'runtime':body['runtime'],'custody':body['custody']}
            runtime_closed=bool(closed and proof['material_status']=='STRUCTURALLY_BOUND' and joined['runtime_consumer']['status']=='STRUCTURALLY_BOUND')
            view['installed_body_runtime_custody_status']='STRUCTURALLY_BOUND' if runtime_closed else 'NOT_PROVEN'
            view['complete_installed_observation_comparison']='STRUCTURALLY_BOUND' if closed else 'NOT_PROVEN'
            view['installed_runtime_remaining_cause']=None if runtime_closed else 'full_future_class_runtime_inputs_absent'
        chain["composed_ubuntu"] = composition["ubuntu_count"]
        chain["composed_indexes"] = composition["index_count"]
        if composition["ubuntu_count"] < 106 or composition["wheel_count"] < 94 or composition["index_count"] < 13:
            raise ContractError("ubuntu_minimum_count")
        link_indexes(expected["ubuntu_minimum"]["packages"], expected["ubuntu_minimum"]["indexes"])
        _STAGE[0] = "plan"
        operations, produced = _plan_operations(expected, presented, composition, context, meter)
        del composition['_held_provider']
        hierarchy = bind_member_hierarchy(produced)
        manifest = final_manifest_projection(operations)
        if expected["image_alternative"]["preexisting_image_sha256"] is not None and manifest["image_sha256"] is not None:
            raise ContractError("image_sha")
        effect = admit_effect({
            "schema": "friday.lab815.effect-capability.v1",
            "effect": "assemble-rootfs",
            "available": False,
            "reason": "SOURCE_PHASE_DENIAL",
            "max_bytes": 0,
            "network_calls": 0,
            "subprocesses": 0,
            "publisher_proof": False,
        }, load_pinned_schema("effect-capability.v1"))
        probe = None
        if presented["probe_archive_hex"] is not None:
            _STAGE[0] = "probe"
            probe = compare_archive(
                _hex_bytes(presented["probe_archive_hex"]),
                presented["probe_expected_sha256"],
                presented["probe_expected_size"],
            )
        approval = None if presented["withhold_approval"] else (
            presented["approval"] if presented["approval"] is not None else _approval(manifest["manifest_sha256"])
        )
        predecessor_open = not _receipts_closed(context, receipt_index, ubuntu_receipt, node_receipt)
        if context is not None and context["document_contracts"] is not None:
            predecessor_open = predecessor_open or document_vector["all_materials_closed"] is not True or any(item["status"]!="STRUCTURALLY_BOUND" for item in operations)
        if approval is None or predecessor_open:
            trust = None
        else:
            _STAGE[0] = "trust"
            trust = project_trust(
                approval,
                presented["approval_expected"],
                ubuntu_receipt,
                load_pinned_schema("external-issuer-approval.v1"),
                load_pinned_schema("raw-publisher-receipt.v1"),
                load_pinned_schema("material-provenance.v1"),
                load_pinned_schema("trusted-root-projection.v1"),
                expected,
                authority["root_issuer_sha256"],
                approval_result=presented["approval_result"],
                observations=observations,
                admitted_signers=presented["admitted_signers"],
                authority_identity={
                    "issuer_id": authority["approved_issuer_id"],
                    "key_fingerprint": authority["approved_fingerprint"],
                },
                computed={
                    "golden_sha256": presented["golden_sha256"] if is_digest(presented["golden_sha256"]) else None,
                    "manifest_sha256": manifest["manifest_sha256"],
                    "recipe_sha256": domain_digest("friday.lab822.recipe.v1", [
                        {
                            "destination": item["destination"],
                            "name": item["name"],
                            "output_sha256": item["output_sha256"],
                            "status": item["status"],
                        }
                        for item in operations
                    ]),
                    "rootfs_sha256": manifest["rootfs_sha256"],
                    "tool_sha256": presented["snapshot_fields"]["creation_tool_sha256"] if is_digest(presented["snapshot_fields"]["creation_tool_sha256"]) else None,
                    "kernel_contract_sha256": presented["snapshot_fields"]["kernel_contract_sha256"],
                },
                pins=presented["held_member_pins"],
                receipt_records=composition["receipt_vector"],
            )
        snapshot = None
        if trust is not None:
            snapshot = project_snapshot(manifest, expected, trust["materials_sha256"], presented, context, held)
        binding = {"status": "NOT_PROVEN", "cause": "presented_observation_absent", "publisher_proof": False, "effects_denied": True}
        compared = []
        for present_key, expected_key, issuer_key in (
            ("ubuntu_binding", "ubuntu_binding_expected", "ubuntu-minimum"),
            ("wheel_binding", "wheel_binding_expected", "wheel"),
            ("node_binding", "node_binding_expected", "node"),
        ):
            if presented[present_key] is None and presented[expected_key] is None:
                continue
            _STAGE[0] = "binding"
            if presented[present_key] is None or presented[expected_key] is None:
                raise ContractError("binding_set")
            compared.append(bind_producer_consumer(
                presented[present_key],
                presented[expected_key],
                load_pinned_schema("producer-consumer-binding.v1"),
                _CLASS_ISSUER[issuer_key],
            ))
        if compared:
            binding = compared[0]
            for item in compared[1:]:
                if binding.get("status") == "STRUCTURAL_COMPARED":
                    binding = item
        algorithm_cause = "sha256_correspondence"
        algorithm_status = "NOT_PROVEN"
        if ubuntu_receipt["hash_header"] == "SHA512" and ubuntu_receipt["algorithm_status"] == "SUPPORTED":
            algorithm_cause = "sha512_correspondence"
            algorithm_status = "ACCEPTED"
        elif ubuntu_receipt["hash_header"] == "SHA256" and ubuntu_receipt["algorithm_status"] == "SUPPORTED":
            algorithm_cause = "sha256_correspondence"
            algorithm_status = "ACCEPTED"
        schema = load_pinned_schema("producer-consumer-binding.v1")
        chain_ready = chain["composed_ubuntu"] >= 106 and chain["composed_indexes"] >= 13
        if context is not None and context["require_predecessor_receipts"] is True:
            chain_ready = (
                ubuntu_receipt["proof_status"] in ("STRUCTURALLY_BOUND", "AUTHENTICATED")
                and node_receipt["proof_status"] in ("STRUCTURALLY_BOUND", "AUTHENTICATED")
            )
        stages = {
            "ingress": _stage("ACCEPTED", "independent_ingress"),
            "chain": _stage("ACCEPTED" if chain_ready else "NOT_PROVEN", "raw_chain"),
            "ubuntu_receipt": _stage(ubuntu_receipt["proof_status"], ubuntu_receipt["cause"]),
            "node_receipt": _stage(node_receipt["proof_status"], node_receipt["cause"]),
            "algorithm": _stage(algorithm_status, algorithm_cause),
            "bill": _stage("ACCEPTED", "ubuntu_projection"),
            "wheel": _stage("ACCEPTED", "wheel_tag"),
            "retain": _stage(retained["status"], retained["cause"]),
            "venv": _stage("ACCEPTED", "venv_site"),
            "unrar": _stage(
                next(item["status"] for item in operations if item["name"] == "hold-unrar-publisher-gap"),
                "unrar",
            ),
            "browser": _stage(
                "STRUCTURALLY_BOUND" if is_digest(presented["browser_attribution_sha256"]) else "NOT_PROVEN",
                "browser_attribution",
            ),
            "image": _stage("PLANNED_EFFECTS_UNAVAILABLE", "image_sha_optional"),
            "effect": _stage("DENIED", "source_phase_denial"),
            "trust": _stage("NOT_PROVEN", "broker_envelope") if trust is None else _stage(trust["projection"]["comparison_status"], trust["projection"]["reason"]),
            "release_trust": _stage("NOT_PROVEN", "release_untrusted"),
            "projection": _stage("ACCEPTED", "projection_digest"),
            "schema": _stage("ACCEPTED", "schema_descriptor_admitted") if schema["max_depth"] >= 8 else _stage("NOT_PROVEN", "schema_pin"),
            "binding": binding if "status" in binding else _stage("NOT_PROVEN", "presented_observation_absent"),
            "probe": _stage(probe["status"], probe["cause"]) if probe is not None else _stage("NOT_PROVEN", "archive_bytes_absent"),
            "dash": _stage("ACCEPTED" if chain.get("dash_unescaped") else "NOT_PROVEN", "dash_unescape"),
            "requires_python": _stage("ACCEPTED", "requires_python_normalization") if python_view is not None else _stage("NOT_PROVEN", "requires_python_normalization"),
            "plan": _stage("PLANNED_EFFECTS_UNAVAILABLE", "source_phase"),
            "controls": _stage("NOT_RUN", "controls_not_run"),
        }
        if not _receipts_closed(context, receipt_index, ubuntu_receipt, node_receipt):
            stages["trust"] = _stage("NOT_PROVEN", "predecessor_pending")
        if presented["packages_text"] is not None and b"\n " in _bytes(presented["packages_text"]):
            stages["deb822"] = _stage("ACCEPTED", "continuation_preserved")
        else:
            stages["deb822"] = _stage("NOT_PROVEN", "continuation_preserved")
        if chain.get("release_member"):
            stages["release"] = _stage("ACCEPTED", "release_member")
        else:
            stages["release"] = _stage("NOT_PROVEN", "release_member")
        if chain.get("unknown_fields"):
            stages["package_fields"] = _stage("ACCEPTED", "unknown_field_preserved")
        else:
            stages["package_fields"] = _stage("NOT_PROVEN", "unknown_field_preserved")
        emitted_manifest = next(item["emitted_body_canonical"] for item in operations if item["name"]=="write-final-manifest")
        custody_input={'inventory_manifest_sha256':manifest['manifest_sha256'],
                       'emitted_manifest_file_sha256':None if emitted_manifest is None else hashlib.sha256(emitted_manifest.encode('ascii')).hexdigest()}
        custody_consumer=consume_performing(context,held,'materials','a009-custody',custody_input)
        domains = a009_domain_map(emitted_manifest, manifest, presented, operations,custody_consumer)
        return {
            "status": "PLANNED_EFFECTS_UNAVAILABLE",
            "cause": "source_phase",
            "stages": stages,
            "composition_counts": {
                "ubuntu_count": composition["ubuntu_count"],
                "wheel_count": composition["wheel_count"],
                "index_count": composition["index_count"],
                "issuer_ids": composition["issuer_ids"],
                "publisher_proof": False,
            },
            "composition": composition,
            "full_document_vector": document_vector,
            "full_operation_outputs": operations,
            'root_input_path':None if trust is None else trust['root_input_path'],
            "manifest_projection": manifest,
            "emitted_manifest_file_body": next(item["emitted_body_canonical"] for item in operations if item["name"]=="write-final-manifest"),
            "whole_source_ready": False,
            "whole_closure_residual": "joins_present_not_proven_until_future_typed_inputs",
            "construction_slots": {"active": meter.slots, "peak": meter.peak_slots},
            "hierarchy": hierarchy,
            "a009_domains": domains,
            "operations": [
                {
                    "destination": item["destination"],
                    "member_count": len(item["members"]),
                    "name": item["name"],
                    "output_sha256": item["output_sha256"],
                    "status": item["status"],
                }
                for item in operations
            ],
            "manifest_sha256": manifest["manifest_sha256"],
            "snapshot_schema": None if snapshot is None else snapshot["schema"],
            "effect": effect,
            "trust_release": False if trust is None else trust["projection"]["release_trusted"],
            "python_view": python_view,
            "publisher_proof": False,
            "release_trusted": False,
            "closure_view": closure_view,
            "controls_executed": False,
            "accepted": False,
            "ready_for_exec": False,
            "go": False,
            "receipt_count": len(receipt_index),
            "receipt_vector": composition["receipt_vector"],
            "ubuntu_receipt": ubuntu_receipt,
            "node_receipt": node_receipt,
            "provenance": None if trust is None else trust["provenance"],
            "snapshot": snapshot,
            "operation_identities": [
                {
                    "destination": item["destination"],
                    "members": [
                        {
                            "content_sha256": member["content_sha256"],
                            "custody_sha256": member.get("custody_sha256"),
                            "device": member.get("device"),
                            "gid": member.get("gid"),
                            "parent_path": member.get("parent_path"),
                            "input_sha256": member.get("input_sha256"),
                            "kind": member["kind"],
                            "link_target": member.get("link_target"),
                            "mode": member["mode"],
                            "mount_domain": _mount_path(member["path"])[0],
                            "nlink": member.get("nlink"),
                            "path": member["path"],
                            "size": member.get("size"),
                            "status": member["status"],
                            "uid": member.get("uid"),
                        }
                        for member in item["members"]
                    ],
                    "name": item["name"],
                    "output_sha256": item["output_sha256"],
                    "status": item["status"],
                }
                for item in operations
            ],
        }


def _bounded_public_plan(authority_raw, expected_raw, presented_raw, context_raw=None, streams=None):
    _install_standalone_completion_receiver()
    _STAGE[0]="ingress"
    try:
        from contextlib import nullcontext
        with (WholeMeter() if current() is None else nullcontext(current())) as whole:
            outcome=_plan_construction(authority_raw,expected_raw,presented_raw,context_raw,streams)
            outcome["resource_observations"]=whole.report()
            from canonical import canonical_bytes
            # The complete outcome is checked; no field dropping/truncation.
            encoded=canonical_bytes(outcome)
            if len(encoded)>MAX_OUTPUT_BYTES:
                raise ContractError("whole_output")
            whole.check()
            return outcome
    except (MemoryError,RecursionError) as exc:
        raise ContractError("whole_allocation") from exc
    except (OSError,UnicodeError) as exc:
        raise ContractError("resource_or_input_io") from exc


def _install_standalone_completion_receiver():
    """Direct five-argument entry joins the existing whole-domain receiver.

    The held loader's receiver is left in place. This fallback checks the
    actual domain rows for the call. Settled CLOSED or NO_RETURN rows return
    the existing receipt. Unsettled rows stay on the domain holder. No second
    root list and no one-call or 128-row cutoff is installed.
    """
    import os
    import resource_meter as meter
    if getattr(meter,'OUTER_COMPLETE_CALL',None) is not None:
        return
    def standalone_complete(roots):
        if type(roots) is not dict or roots.get('owner_pid')!=os.getpid():
            return None
        rows=roots.get('rows')
        domain=roots.get('domain')
        if type(rows) is not tuple or type(domain) is not dict:
            return None
        history=domain.get('history')
        if type(history) is not list:
            return None
        call_sequence=roots.get('call_sequence')
        actual=tuple(row for row in history if row.get('call_sequence')==call_sequence)
        if len(rows)!=len(actual) or any(left is not right for left,right in zip(rows,actual)):
            return None
        whole=roots.get('whole')
        settled=whole is not None and roots.get('result') is getattr(whole,'completion_result',None) and roots.get('error') is getattr(whole,'completion_error',None) and all(type(row) is dict and row.get('status') in ('CLOSED','NO_RETURN') for row in rows)
        if settled:
            if whole is None:
                return None
            # This is the same actual full object used by the held-loader
            # receiver, not an ACK for a selected subset of error/body roots.
            whole.accepted_call_custody=roots
            held=getattr(meter,'ACCEPTED_SAME_PID_CALLS',None)
            if held is None:
                held=[]
                meter.ACCEPTED_SAME_PID_CALLS=held
            held.append(whole.accepted_call_custody)
            return {'owner_pid':os.getpid(),'call_sequence':call_sequence,'actual_roots_received':True}
        transfers=domain.get('unsettled_transfers')
        if type(transfers) is not list:
            transfers=[]
            domain['unsettled_transfers']=transfers
        transfers.append(roots)
        return None
    meter.OUTER_COMPLETE_CALL=standalone_complete

def plan_construction(authority_raw, expected_raw, presented_raw, context_raw=None, streams=None):
    """Same public five-argument entry; one original-clock performing envelope."""
    _install_standalone_completion_receiver()
    from contextlib import nullcontext
    whole = WholeMeter() if current() is None else current()
    try:
        with (whole if current() is None else nullcontext(whole)):
            return whole.bind_result(_bounded_public_plan(authority_raw,expected_raw,presented_raw,context_raw,streams))
    except ContractError as exc:
        attach_refusal(exc, whole)
        raise


def plan_construction_wire(authority_raw, expected_raw, presented_raw, context_raw=None, streams=None):
    """One actual complete wire encoding inside the original call envelope."""
    _install_standalone_completion_receiver()
    whole=WholeMeter()
    try:
        with whole:
            outcome=plan_construction(authority_raw,expected_raw,presented_raw,context_raw,streams)
            wire=canonical_bytes(outcome)
            if len(wire)>MAX_OUTPUT_BYTES:raise ContractError('whole_output')
            whole.check()
            return whole.bind_result(wire)
    except ContractError as exc:
        attach_refusal(exc,whole)
        raise

