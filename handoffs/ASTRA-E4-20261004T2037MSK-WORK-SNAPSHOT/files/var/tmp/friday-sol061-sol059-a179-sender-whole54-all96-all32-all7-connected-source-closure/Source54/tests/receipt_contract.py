"""Strict source-bound recording receipts; no installation/kernel authority."""
from datetime import datetime, timezone, timedelta
import hashlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import types
_SOURCE_RAW_OS=types.SimpleNamespace(**vars(os))
_SOURCE_GRAPH=None
RECEIVER_OWNERS=[]
RECEIVER_PENDING=None

TESTS = ("test_canonical", "test_manifest", "test_provenance", "test_archive",
    "test_broker", "test_ledger", "test_custody", "test_install_recovery",
    "test_uninstall", "test_effect_bills")
SOURCES = ("canonical", "pinned_fs", "provenance", "archive", "manifest", "assemble",
    "ledger", "custody_linux", "broker_runtime", "broker_bootstrap", "install_bootstrap",
    "install", "recover", "uninstall")
WORKER_AS_LIMIT = 1_610_612_736
DISPATCHER_AS_LIMIT = 1_610_612_736
SEAL_RESERVE_SECONDS = 600
ASSIGNMENT_WALL_SECONDS = 7200
SOURCE_ASSIGNMENT = "ASTRA-E4-QUALITY-STABLE-ROOT-NAMESPACE-CLOSURE-A074#1"
ASSIGNMENT = SOURCE_ASSIGNMENT
# Frozen source preparation never supplies a currently valid execution deadline.
# These become exact admitted RUN values once, before any generated execution.
ASSIGNMENT_ACCEPTED_UTC = None
ASSIGNMENT_DEADLINE_UTC = None
LEGACY_SOL020_CONTEXT = {
    "assignment": "ASTRA-E4-SOL020-FINAL-SOURCE-CLOSURE#1",
    "accepted_at_utc": "2026-10-01T00:24:42+00:00",
    "deadline_at_utc": "2026-10-01T02:24:42+00:00",
    "wall_seconds": 7200, "seal_reserve_seconds": 600}
ADMISSION_ROOT = Path("/home/jericho/.jericho/quality-source-admission")
ADMISSION_ISSUER = "friday.owner.offline-source-run-admission.v1"
ADMISSION_OWNER = (1000, 1000)
PARENT_EXPECTATION_FD = 198
PARENT_ADMISSION_FD = 199
PARENT_CUSTODY_FDS = (PARENT_EXPECTATION_FD, PARENT_ADMISSION_FD)
FULL_MEMFD_SEALS = (fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK |
    fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE)
ISSUER_ANCESTRY = (("/", 0, 0, 0o755), ("/home", 0, 0, 0o755),
    ("/home/jericho", 1000, 1000, 0o750),
    ("/home/jericho/.jericho", 1000, 1000, 0o700),
    (str(ADMISSION_ROOT), 1000, 1000, 0o700))
RUN_RESOURCE_LIMITS = {
    "worker_address_space_bytes_max": WORKER_AS_LIMIT, "worker_cpu_seconds_max": 300,
    "worker_wall_timeout_seconds_max": 300, "parallel_workers_max": 4,
    "dispatcher_address_space_bytes_max": DISPATCHER_AS_LIMIT,
    "dispatcher_cpu_seconds_max": ASSIGNMENT_WALL_SECONDS,
    "aggregate_address_space_bytes_max": 4 * WORKER_AS_LIMIT + DISPATCHER_AS_LIMIT,
    "memory_bill_bytes_max": 8_589_934_592}
_RUN_CONTEXT = None
_RUN_ADMISSION = None
CAPABILITY_BOUNDARY = "approved-own-source/inert-adapter; arbitrary-Python/kernel/custody NOT_PROVEN"
HARNESS_REQUIRED_CONTROLS = tuple(sorted((
    "historical_sha_cannot_credit_replaced_coverage","passing_method_cannot_credit_missing_required_key",
    "complete_pair_with_partial_focused_evidence_positive","integration_pair_sha_substitution",
    "integration_missing_child_closure","integration_missing_old_pin_proof",
    "wrong_valid_enum_support_outcome", "wrong_valid_enum_projection_outcome", "negative_refusal_cannot_be_pass",
    "undeclared_observation_key", "unbound_extra_subcontrol", "wrong_subcontrol_parent",
    "wrong_collection_count", "boolean_collection_count", "foreign_collected_module", "duplicate_collected_case",
    "stale_declared_owner", "missing_observation_declaration", "unenforced_resource_bound", "bool_resource_bound",
    "peak_usage_over_bill", "missing_actual_usage", "missing_wall_watchdog", "extended_assignment_deadline",
    "seal_reserve_zero", "late_child_admission", "overlong_child_timeout", "component_wrong_valid_enum_outcome",
    "missing_component_role", "stale_component_final_bytes", "wrong_component_schema",
    "component_claims_independent_authority", "component_foreign_evidence_root", "component_stale_method_binding",
    "integration_stale_final_bytes", "integration_wrong_evidence_authority", "integration_false_readiness_claim",
    "integration_missing_finding", "integration_independent_credit_refused", "missing_producer_elapsed_seconds",
    "duplicate_json_wire_key", "writable_receipt_owner_contract", "actual_bounded_file_and_plain_data_adapters",
    "raw_ctypes_factory_unavailable", "private_native_callable_unavailable", "inert_adapter_native_loader_unavailable",
    "inert_adapter_native_address_unavailable", "raw_extension_module_capability_unavailable",
    "raw_ctypes_import_refused", "unapproved_module_import_refused", "unapproved_native_member_refused",
    "prototype_caches_revoked_without_native_invocation", "immutable_adapter_mutation_refused",
    "immutable_adapter_namespace_refused", "coherent_aggregate_substitution", "all_attempts_stopped_before_host_effect",
    "wrong_dispatcher_script", "wrong_dispatcher_label", "wrong_dispatcher_mode", "wrong_dispatcher_source_digest",
    "wrong_dispatcher_environment_scope", "bool_dispatcher_isolation", "wrong_already_passing_observation_owner",
    "wrong_already_passing_matrix_owner", "aggregate_memory_bill_substitution", "aggregate_worker_resource_substitution",
    "aggregate_dispatcher_limit_substitution", "aggregate_missing_dispatcher_usage",
    "run_context_wrong_assignment", "run_context_old_context", "run_context_changed_acceptance",
    "run_context_changed_deadline", "run_context_changed_wall", "run_context_changed_reserve",
    "run_context_changed_resources", "run_context_wrong_issuer", "run_context_wrong_source",
    "run_context_missing_binding", "run_context_unadmitted_dispatcher_mode",
    "new_controls_missing_required_key", "new_controls_wrong_passing_owner",
    "new_controls_wrong_valid_enum", "new_controls_missing_matrix_row",
    "new_controls_wrong_matrix_owner")) )


HARNESS_REQUIRED_CONTROLS = tuple(sorted(set(HARNESS_REQUIRED_CONTROLS) | {
    "original_status_open_with_complete_source_evidence", "original_status_promotion_refused",
    "original_independent_acceptance_refused", "original_unknown_evidence_enum_refused",
    "parent_custody_shape_positive_not_admission", "parent_custody_wrong_owner",
    "parent_custody_wrong_mode", "parent_custody_wrong_identity_type",
    "parent_custody_wrong_namespace", "parent_custody_overlarge_body",
    "mandatory54_collection_source_model_positive",
    "mandatory_custodian_collection_source_omission"}))


def encode(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
        separators=(",", ":")) + "\n").encode("ascii")


def require(condition, reason):
    if not condition:
        raise ValueError("receipt contract: " + reason)


def is_int(value, minimum=0, maximum=None):
    return type(value) is int and value >= minimum and (maximum is None or value <= maximum)


def relative_path(value):
    require(type(value) is str and value and not value.startswith("/") and
        all(part not in ("", ".", "..") for part in value.split("/")), "relative path")
    return value


def raw_member(package, relative, retainer=None):
    path = package / relative_path(relative)
    require(package in path.parents and not any(p.is_symlink() for p in (path, *path.parents)), "linked receipt path")
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
        and not stat.S_IMODE(info.st_mode) & 0o022, "regular unique owned non-writable receipt")
    identity = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_uid,
        value.st_gid, value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    client=raw_client()
    owner=globals().get("_SOURCE_INVENTORY_OWNER")
    if retainer is None and owner is not None and not owner.closed and owner.package==package and relative in owner.members:
        owner.verify()
        row=owner.members[relative]
        require(file_identity(info)==row["identity"] and file_identity(os.fstat(row["fd"]))==row["identity"],
            "cache reads only the same held full-byte/current named9 immutable generation")
        client.note("receipt.Source_generation.byte_reuse",[relative,row["identity"]],
            {"bytes":len(row["raw"]),"sha256":hashlib.sha256(row["raw"]).hexdigest(),"physical_read_performed":False})
        return row["raw"]
    slot=None if retainer is None else retainer.reserve(relative)
    fd=client.call("receipt.raw_member.open",_SOURCE_RAW_OS.open,
        str(path),_SOURCE_RAW_OS.O_RDONLY|_SOURCE_RAW_OS.O_CLOEXEC|_SOURCE_RAW_OS.O_NOFOLLOW)
    if slot is not None:slot["fd"]=fd
    active_error=None
    try:
        require(identity(os.fstat(fd))==identity(info),"opened receipt identity")
        cap=info.st_size+1
        if _SOURCE_READ_METER is not None:
            raw=_SOURCE_READ_METER.read("receipt.raw_member.read",_SOURCE_RAW_OS.pread,(fd,cap,0),fd,0)
        else:
            require(client.actor not in ("controller","receiver"),"controller receiver missing shared Source meter")
            raw=client.call("receipt.raw_member.read",_SOURCE_RAW_OS.pread,fd,cap,0)
        require(identity(os.fstat(fd))==identity(info),"stable opened receipt read")
    except BaseException as error:
        active_error=error
        if slot is not None:slot["first_error"]=error
        raise
    finally:
        if retainer is None or active_error is not None:
            if slot is not None:slot["close_attempted"]=True
            try:
                client.call("receipt.raw_member.close",_SOURCE_RAW_OS.close,fd)
                if slot is not None:slot["closed"]=True
            except BaseException as close_error:
                if slot is not None:slot["close_error"]=close_error
                if active_error is None:raise
    require(identity(path.lstat()) == identity(info) and len(raw) == info.st_size,
        "receipt changed during read")  # ordinary read-induced atime is not drift
    if slot is not None:slot["raw"],slot["identity"]=raw,file_identity(info)
    return raw


def object_member(package, relative):
    raw = raw_member(package, relative)
    def unique_pairs(pairs):
        out = {}
        for key, value in pairs:
            require(key not in out, "duplicate wire key")
            out[key] = value
        return out
    value = json.loads(raw, object_pairs_hook=unique_pairs,
        parse_constant=lambda value: require(False, "nonfinite wire"))
    require(type(value) is dict, "wire object")
    return value, hashlib.sha256(raw).hexdigest()


def source_inventory(package):
    global _SOURCE_INVENTORY_OWNER
    if _SOURCE_INVENTORY_OWNER is not None:
        require(package==_SOURCE_INVENTORY_OWNER.package,"Source inventory package owner cannot change")
        return _SOURCE_INVENTORY_OWNER.verify()
    paths = [package / "src" / (n + ".py") for n in SOURCES]
    paths += [package / "tests" / (n + ".py") for n in TESTS +
        ("install_controls", "support", "run_tests", "execute_pair", "close_package", "harness_selfcheck", "receipt_contract", "admission_custodian")]
    for directory in ("effects", "schemas", "templates"):
        paths += sorted((package / directory).iterdir())
    paths += [package / "README.contract.md", package / "fixtures/coordination/finalize_reports.py"]
    relatives=[str(p.relative_to(package)) for p in paths]
    require(len(relatives)==len(set(relatives))==54,"original full exact Source54 pathset")
    if _SOURCE_GRAPH is not None:_SOURCE_GRAPH.protect(package,relatives)
    owner=SourceInventoryGeneration(package,{relative:None for relative in relatives},
        ("original local full inventory",None if _CLIENT is None else _CLIENT.actor))
    _SOURCE_INVENTORY_OWNER=owner
    _SOURCE_INVENTORY_GENERATIONS.append(owner)
    for relative in relatives:
        raw=raw_member(package,relative,owner)
        owner.expected[relative]=hashlib.sha256(raw).hexdigest()
    return owner.verify()


def canonical_run_projection(value, run):
    """Only fresh run-root names are alpha-renamed; raw evidence stays retained.

    Never rewrite numbers, timestamps, hashes, outcomes, outside paths, suffixes
    or path traversal components. This is logical recording comparison only.
    """
    prefix = str(run)
    if type(value) is str:
        return "$RUN" + value[len(prefix):] if value == prefix or value.startswith(prefix + "/") else value
    if type(value) is list:
        return [canonical_run_projection(item, run) for item in value]
    if type(value) is dict:
        return {key: canonical_run_projection(item, run) for key, item in value.items()}
    return value


def expected_environment(run):
    return {"HOME": str(run / "home"), "TMPDIR": str(run / "tmp"), "PATH": "/usr/bin:/bin",
        "LANG": "C", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"}


def instant(value):
    require(type(value) is str, "timestamp string")
    result = datetime.fromisoformat(value)
    require(result.tzinfo is not None, "timezone-aware timestamp")
    return result.astimezone(timezone.utc)



def run_context():
    require(_RUN_CONTEXT is not None and _RUN_ADMISSION is not None,
        "independent finite RUN admission required; SOURCE preparation is not authority")
    return json.loads(encode(_RUN_CONTEXT))


def run_binding():
    run_context()
    return json.loads(encode(_RUN_ADMISSION))


def run_context_flags():
    run_binding()
    return []  # authority is inherited sealed custody, never a caller CLI pin


def file_identity(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
        info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def unique_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate RUN admission wire key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda value: require(False, "nonfinite RUN admission"))
    require(type(value) is dict and encode(value) == raw, "canonical RUN admission bytes")
    return value


def read_sealed_custody(fd, maximum):
    """Consume only a preexisting read-only fully sealed held object; never mint."""
    info = os.fstat(fd)
    require(stat.S_ISREG(info.st_mode) and (info.st_uid, info.st_gid) == ADMISSION_OWNER
        and info.st_nlink == 0 and stat.S_IMODE(info.st_mode) == 0o400
        and 0 < info.st_size <= maximum, "bounded nonroot held custody object")
    require(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        and fcntl.fcntl(fd, fcntl.F_GET_SEALS) & FULL_MEMFD_SEALS == FULL_MEMFD_SEALS,
        "read-only fully sealed immutable parent transport")
    cap=info.st_size+1
    raw=(_SOURCE_READ_METER.read("receipt.sealed_custody",_SOURCE_RAW_OS.pread,(fd,cap,0),fd,0)
        if _SOURCE_READ_METER is not None else observe_root_call(
            "receipt.sealed_custody",_SOURCE_RAW_OS.pread,fd,cap,0))
    require(file_identity(os.fstat(fd)) == file_identity(info) and len(raw) == info.st_size,
        "before/opened/after held custody identity")
    return raw, file_identity(info)


def validate_parent_expectation(expected):
    """Pure shape checks; inherited parent transport supplies independent trust."""
    require(type(expected) is dict and set(expected) == {"schema", "issuer",
        "source_assignment", "admission_sha256", "admission_identity",
        "namespace_identities", "run_context", "source_hashes"}
        and expected["schema"] == "friday.a056.independent-parent-custody.v1"
        and expected["issuer"] == ADMISSION_ISSUER
        and expected["source_assignment"] == SOURCE_ASSIGNMENT,
        "independent parent expectation exact schema/issuer/source")
    require(type(expected["admission_sha256"]) is str
        and re.fullmatch(r"[0-9a-f]{64}", expected["admission_sha256"]) is not None,
        "externally fixed whole-byte issuer pin")
    identity = expected["admission_identity"]
    require(type(identity) is list and len(identity) == 9
        and all(is_int(value) for value in identity)
        and stat.S_ISREG(identity[2]) and stat.S_IMODE(identity[2]) == 0o400
        and identity[3:6] == [*ADMISSION_OWNER, 1] and 0 < identity[6] <= 65536,
        "externally fixed issuer body owner/mode/link/size/identity")
    ancestors = expected["namespace_identities"]
    require(type(ancestors) is list and len(ancestors) == len(ISSUER_ANCESTRY),
        "exact independently pinned issuer ancestry")
    for row, (name, uid, gid, mode) in zip(ancestors, ISSUER_ANCESTRY):
        require(type(row) is dict and set(row) == {"path", "identity"}
            and row["path"] == name and type(row["identity"]) is list
            and len(row["identity"]) == 9 and all(is_int(v) for v in row["identity"])
            and stat.S_ISDIR(row["identity"][2])
            and stat.S_IMODE(row["identity"][2]) == mode
            and row["identity"][3:5] == [uid, gid],
            "declared protected nonroot issuer namespace owner/mode/identity")
    return expected


def validate_context_document(document, source_hashes):
    """Pure strict document checks, separate from independent issuer admission."""
    keys = {"schema", "issuer", "source_assignment", "source_hashes", "run_context"}
    require(type(document) is dict and set(document) == keys and
        document["schema"] == "friday.a049.offline-run-admission.v1" and
        document["issuer"] == ADMISSION_ISSUER and document["source_assignment"] == SOURCE_ASSIGNMENT,
        "exact independent issuer/source assignment")
    require(document["source_hashes"] == source_hashes and type(source_hashes) is dict and source_hashes,
        "exact admitted complete source/harness/config/schema inventory")
    context = document["run_context"]
    require(type(context) is dict and set(context) == {
        "schema", "assignment", "generation", "run_id", "accepted_at_utc", "deadline_at_utc",
        "wall_seconds", "seal_reserve_seconds", "resources", "allowed_modes"},
        "exact finite RUN context schema")
    require(context["schema"] == "friday.a049.offline-run-context.v1"
        and type(context["assignment"]) is str and
        re.fullmatch(r"[A-Z0-9][A-Z0-9-]{3,159}#[1-9][0-9]{0,5}", context["assignment"]) is not None
        and context["assignment"] not in (SOURCE_ASSIGNMENT, LEGACY_SOL020_CONTEXT["assignment"],
            "ASTRA-E4-QUALITY-COLLECTION-WIRE-FINAL-CLOSURE-A059#1",
            "ASTRA-E4-QUALITY-COHERENT-CONTEXT-CONTROLS-CLOSURE-A049#1")
        and is_int(context["generation"], 1, 1_000_000)
        and context["assignment"].rsplit("#", 1)[1] == str(context["generation"])
        and type(context["run_id"]) is str and re.fullmatch(r"[0-9a-f]{64}", context["run_id"]) is not None,
        "new separately admitted RUN identity")
    require(is_int(context["wall_seconds"], SEAL_RESERVE_SECONDS + 1, ASSIGNMENT_WALL_SECONDS)
        and type(context["seal_reserve_seconds"]) is int
        and context["seal_reserve_seconds"] == SEAL_RESERVE_SECONDS,
        "finite original wall ceiling and seal reserve")
    accepted, end = instant(context["accepted_at_utc"]), instant(context["deadline_at_utc"])
    require(context["accepted_at_utc"] == accepted.isoformat()
        and context["deadline_at_utc"] == end.isoformat()
        and (end - accepted).total_seconds() == context["wall_seconds"],
        "exact immutable accepted/deadline/wall relationship")
    resources = context["resources"]
    require(type(resources) is dict and set(resources) == set(RUN_RESOURCE_LIMITS)
        and all(type(resources[key]) is int and resources[key] == expected
            for key, expected in RUN_RESOURCE_LIMITS.items()), "exact RUN resource ceilings")
    modes = context["allowed_modes"]
    require(type(modes) is list and modes == sorted(set(modes)) and modes
        and set(modes) <= {"collect", "selfcheck", "affected", "full-pair"},
        "explicit independently admitted bounded modes")
    return json.loads(encode(context))


def validate_namespace_snapshot(observed, expected):
    """Exact ordered public identity comparison, never an origin/admission check.

    Runtime is a sibling of the independent admission leaf. It is not an input
    to this fixed namespace; every field of every actual protected row is still
    checked, including integer nanosecond timestamps. No refresh or projection.
    """
    require(type(observed) is list and type(expected) is list
        and len(observed) == len(expected) == len(ISSUER_ANCESTRY),
        "exact protected namespace snapshot arity")
    for actual, pinned, (name, uid, gid, mode) in zip(observed, expected, ISSUER_ANCESTRY):
        for row in (actual, pinned):
            require(type(row) is dict and set(row) == {"path", "identity"}
                and row["path"] == name and type(row["identity"]) is list
                and len(row["identity"]) == 9 and all(is_int(v) for v in row["identity"])
                and stat.S_ISDIR(row["identity"][2])
                and stat.S_IMODE(row["identity"][2]) == mode
                and row["identity"][3:5] == [uid, gid],
                "exact protected namespace row owner/mode/nine integer fields")
        require(actual["identity"] == pinned["identity"],
            "exact independently pinned issuer ancestry")
    return observed


def load_run_context(package):
    """Consume externally admitted parent custody, never source/CLI selfapproval.

    The independently admitted parent receiver authenticates both held objects
    against its separately authoritative task before inheritance. This loader
    has no issuer-writing or object-minting path. UID is custody metadata, not
    proof of independence. Arbitrary hostile same-UID Python is NOT_PROVEN.
    """
    global _RUN_CONTEXT, _RUN_ADMISSION, ASSIGNMENT_ACCEPTED_UTC, ASSIGNMENT_DEADLINE_UTC
    expected_raw, expectation_identity = read_sealed_custody(PARENT_EXPECTATION_FD, 131072)
    expected = validate_parent_expectation(unique_object(expected_raw))
    raw, held_identity = read_sealed_custody(PARENT_ADMISSION_FD, 65536)
    expected_sha256 = expected["admission_sha256"]
    target = ADMISSION_ROOT / (expected_sha256 + ".json")
    require(package not in target.parents and ADMISSION_ROOT not in package.parents,
        "independent issuer namespace outside writable source/recording roots")
    parents = [Path(name) for name, *_ in ISSUER_ANCESTRY]
    before = [{"path": str(parent), "identity": file_identity(parent.lstat())}
        for parent in parents]
    validate_namespace_snapshot(before, expected["namespace_identities"])
    info = target.lstat()
    require(file_identity(info) == expected["admission_identity"],
        "exact independently pinned nonroot issuer file identity")
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        require(file_identity(os.fstat(fd)) == file_identity(info), "opened issuer identity")
        named_raw = b""
        while len(named_raw) <= 65536:
            cap=min(65537-len(named_raw),8192)
            part=(_SOURCE_READ_METER.read("receipt.named_issuer",_SOURCE_RAW_OS.read,(fd,cap),fd)
                if _SOURCE_READ_METER is not None else observe_root_call(
                    "receipt.named_issuer",_SOURCE_RAW_OS.read,fd,cap))
            if not part:
                break
            named_raw += part
        require(file_identity(os.fstat(fd)) == file_identity(info) and len(named_raw) == info.st_size,
            "stable bounded issuer read")
    finally:
        os.close(fd)
    after = [{"path": str(parent), "identity": file_identity(parent.lstat())}
        for parent in parents]
    validate_namespace_snapshot(after, expected["namespace_identities"])
    require(file_identity(target.lstat()) == file_identity(info) and after == before
        and named_raw == raw and hashlib.sha256(raw).hexdigest() == expected_sha256,
        "sealed body/named body/external whole-byte issuer pin and identity")
    document = unique_object(raw)
    context = validate_context_document(document, source_inventory(package))
    require(expected["source_hashes"] == document["source_hashes"]
        and expected["run_context"] == context, "separate exact parent source/context expectation")
    now = datetime.now(timezone.utc)
    require(instant(context["accepted_at_utc"]) <= now < instant(context["deadline_at_utc"])
        - timedelta(seconds=context["seal_reserve_seconds"]),
        "actual execution admission inside finite immutable RUN with reserve")
    binding = {"admission_path": str(target), "admission_sha256": expected_sha256,
        "issuer": document["issuer"], "source_assignment": SOURCE_ASSIGNMENT,
        "source_hashes": document["source_hashes"], "run_context": context,
        "parent_custody": {"schema": expected["schema"],
            "expectation_sha256": hashlib.sha256(expected_raw).hexdigest(),
            "expectation_identity": expectation_identity, "held_admission_identity": held_identity,
            "admission_identity": expected["admission_identity"],
            "namespace_identities": expected["namespace_identities"],
            "transport": "INDEPENDENT_PARENT_READONLY_FULLY_SEALED_HELD_OBJECTS"}}
    if _RUN_CONTEXT is not None:
        require(_RUN_CONTEXT == context and _RUN_ADMISSION == binding, "no RUN context replacement")
    _RUN_CONTEXT, _RUN_ADMISSION = context, binding
    ASSIGNMENT_ACCEPTED_UTC, ASSIGNMENT_DEADLINE_UTC = context["accepted_at_utc"], context["deadline_at_utc"]
    return current_deadline()


def current_deadline():
    context = run_context()
    return {"assignment_deadline_utc": context["deadline_at_utc"],
        "seal_reserve_seconds": context["seal_reserve_seconds"], "run_context": run_binding()}


def validate_mode_admission(mode):
    require(mode in run_context()["allowed_modes"], "execution mode not independently admitted")


def validate_current_binding(value):
    require(value == run_binding(), "exact same independently authenticated RUN binding")


def validate_deadline(deadline, started, timeout):
    require(type(deadline) is dict and set(deadline) == {
        "assignment_deadline_utc", "seal_reserve_seconds", "run_context"}, "deadline shape")
    require(deadline == current_deadline(), "exact immutable independent RUN deadline/context")
    context = run_context()
    end, began = instant(context["deadline_at_utc"]), instant(started)
    require(instant(context["accepted_at_utc"]) <= began <= end, "child within accepted RUN interval")
    require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 300, "bounded child timeout")
    require((end - began).total_seconds() >= context["seal_reserve_seconds"] + timeout,
        "child admission before common seal deadline")
    return end

def validate_resources(resources, *, dispatcher=False):
    validate_current_binding(resources.get("run_context") if type(resources) is dict else None)
    limit = DISPATCHER_AS_LIMIT if dispatcher else WORKER_AS_LIMIT
    cpu = ASSIGNMENT_WALL_SECONDS if dispatcher else 300
    keys = {"run_context", "address_space_bytes_max", "cpu_seconds_max", "actual_rlimit_as",
        "actual_rlimit_cpu", "usage_start", "usage"} | (set() if dispatcher else {"wall_timeout_seconds"})
    require(set(resources) == keys, "strict exact process resource declaration")
    require(type(resources) is dict and resources.get("address_space_bytes_max") == limit
        and type(resources.get("address_space_bytes_max")) is int
        and type(resources.get("cpu_seconds_max")) is int and resources["cpu_seconds_max"] == cpu
        and resources.get("actual_rlimit_as") == [limit, limit]
        and resources.get("actual_rlimit_cpu") == [cpu, cpu]
        and all(type(v) is int for field in ("actual_rlimit_as", "actual_rlimit_cpu") for v in resources[field]), "actual enforced process bounds")
    for name in ("usage", "usage_start"):
        usage = resources.get(name)
        require(type(usage) is list and len(usage) == 16 and all(type(v) in (int, float)
            and math.isfinite(v) and v >= 0 for v in usage), "actual usage wire")
        require(usage[2] <= limit // 1024, "actual peak resident memory within process bill")
    require(all(resources["usage"][i] >= resources["usage_start"][i] for i in (0, 1, 2)), "monotone process usage")
    if not dispatcher:
        require(type(resources.get("wall_timeout_seconds")) in (int, float) and math.isfinite(resources["wall_timeout_seconds"])
            and 0 < resources["wall_timeout_seconds"] <= 300, "actual bounded worker wall watchdog")


def validate_aggregate_resources(resources, worker_resources):
    validate_current_binding(resources.get("run_context") if type(resources) is dict else None)
    require(set(resources) == {"run_context", "parallel_workers_max", "aggregate_address_space_bytes_max",
        "dispatcher_address_space_bytes_max", "dispatcher", "worker_resources"}, "strict aggregate resource declaration")
    require(type(resources) is dict and type(resources.get("parallel_workers_max")) is int
        and resources["parallel_workers_max"] == 4 and type(resources.get("aggregate_address_space_bytes_max")) is int
        and resources["aggregate_address_space_bytes_max"] == 4 * WORKER_AS_LIMIT + DISPATCHER_AS_LIMIT
        and resources["aggregate_address_space_bytes_max"] <= 8_589_934_592
        and type(resources.get("dispatcher_address_space_bytes_max")) is int
        and resources["dispatcher_address_space_bytes_max"] == DISPATCHER_AS_LIMIT, "actual aggregate enforced memory bill")
    require(resources.get("worker_resources") == worker_resources, "exact authenticated worker resource union")
    for worker in worker_resources:
        validate_resources(worker)
    validate_resources(resources.get("dispatcher"), dispatcher=True)


def check_launch(package, launch, run, *, only=None, case_ids=None, collect=False, diagnostic_lane=None, harness=False):
    script = "harness_selfcheck.py" if harness else "run_tests.py"
    argv = ["/usr/bin/python3.14", "-I", "-S", "-B", str(package / "tests" / script),
        "--run-root", str(run)]
    if only:
        argv += ["--only", *only]
    if case_ids:
        argv += ["--case-id", *case_ids]
    if collect:
        argv += ["--collect-only"]
    if diagnostic_lane:
        argv += ["--diagnostic-lane", diagnostic_lane]
    deadline = launch.get("deadline")
    require(type(deadline) is dict, "retained deadline")
    argv += run_context_flags()
    argv += ["--assignment-deadline-utc", deadline.get("assignment_deadline_utc"),
        "--seal-reserve-seconds", str(deadline.get("seal_reserve_seconds"))]
    require(set(launch) == {"argv", "environment", "cwd", "started_at_utc", "run_root", "deadline",
        "stdout_ref", "stderr_ref", "admission_status", "timeout_sec", "rc", "timed_out",
        "completed_at_utc", "elapsed_seconds", "stdout_sha256", "stderr_sha256"}, "strict actual launch receipt")
    require(launch.get("argv") == argv and launch.get("environment") == expected_environment(run)
        and launch.get("cwd") == str(package) and launch.get("run_root") == str(run), "exact argv/env/cwd")
    require(type(launch.get("rc")) is int and launch["rc"] == 0 and launch.get("timed_out") is False
        and launch.get("admission_status") == "RUN", "launch rc/admission types")
    end = validate_deadline(deadline, launch.get("started_at_utc"), launch.get("timeout_sec"))
    elapsed = launch.get("elapsed_seconds")
    require(type(elapsed) in (int, float) and math.isfinite(elapsed) and 0 <= elapsed <= launch["timeout_sec"] + 2, "bounded elapsed")
    before, after = (datetime.fromisoformat(launch[k]) for k in ("started_at_utc", "completed_at_utc"))
    require(before.tzinfo is not None and after.tzinfo is not None and
        abs((after - before).total_seconds() - elapsed) <= 2, "timing crosslink")
    require(after <= end and (end - after).total_seconds() >= deadline["seal_reserve_seconds"] - 2, "completion leaves honest seal reserve")
    for channel in ("stdout", "stderr"):
        expected_ref = str(run.parent.relative_to(package)) + "/" + run.name + "." + channel
        require(launch.get(channel + "_ref") == expected_ref, "log target")
        require(hashlib.sha256(raw_member(package, expected_ref)).hexdigest() == launch.get(channel + "_sha256"), "log digest")
    invocation, _ = object_member(package, str(run.relative_to(package)) + "/invocation.json")
    require(set(invocation) == {"argv", "environment", "started_at_utc", "isolation", "deadline", "resources"}
        | ({"cwd"} if harness else {"initial_sys_path"}), "strict child invocation receipt")
    require(invocation.get("argv") == argv, "child actual argv")
    environment = expected_environment(run) | {"SOL017_RUN_ROOT": str(run)}
    if diagnostic_lane:
        environment["SOL019_DIAGNOSTIC_LANE"] = diagnostic_lane
    if harness:
        environment = expected_environment(run)
    require(invocation.get("environment") == environment, "child actual environment")
    require(invocation.get("deadline") == deadline and instant(invocation["started_at_utc"]) >= before
        and (instant(invocation["started_at_utc"]) - before).total_seconds() <= elapsed + 2, "actual child deadline/start interval")
    validate_resources(invocation.get("resources"))
    isolation = invocation.get("isolation")
    require(type(isolation) is dict and set(isolation) == {"isolated", "no_site", "dont_write_bytecode"}
        and all(type(value) is int and value == 1 for value in isolation.values()), "exact isolation flags")


def combined_contract(module_contracts):
    matrices, observations, semantics, owners = {}, set(), {}, {}
    require(type(module_contracts) is dict, "module contract map")
    for module, contract in module_contracts.items():
        require(module in TESTS and type(contract) is dict and
            set(contract) == {"matrices", "required_observations", "observation_semantics", "matrix_owners"}, "module contract shape")
        require(type(contract["matrices"]) is dict and type(contract["matrix_owners"]) is dict
            and set(contract["matrices"]) == set(contract["matrix_owners"]), "declared matrix owner set")
        for name, rows in contract["matrices"].items():
            require(name not in matrices and type(rows) is list and rows and
                all(type(row) is str and row for row in rows) and rows == sorted(set(rows)), "unique expected rows")
            matrices[name] = rows
            owner = contract["matrix_owners"][name]
            require(type(owner) is str and owner.startswith(module + "."), "declared matrix module owner")
            owners[name] = owner
        keys = contract["required_observations"]
        require(type(keys) is list and keys == sorted(set(keys)) and
            all(type(key) is str and ":" in key for key in keys), "expected observation keys")
        require(type(contract["observation_semantics"]) is dict and set(keys) <= set(contract["observation_semantics"]), "required key exact semantics")
        for key, pairs in contract["observation_semantics"].items():
            require(type(key) is str and ":" in key and type(pairs) is list and pairs
                and all(type(pair) is dict and set(pair) == {"test_id", "outcome"}
                    and type(pair["test_id"]) is str and pair["test_id"].startswith(module + ".")
                    and type(pair["outcome"]) is str and pair["outcome"] in {"PASS", "REFUSED", "CRASH_RETAINED", "INCOMPLETE"} for pair in pairs), "declared exact observation owner/outcome")
            require(len({encode(pair) for pair in pairs}) == len(pairs), "unique semantic pairs")
            require(key not in semantics, "key has one declaring module")
            semantics[key] = pairs
        require(not (observations & set(keys)), "required key has one declaring module")
        observations.update(keys)
    return {"matrices": dict(sorted(matrices.items())), "required_observations": sorted(observations),
        "declared_modules": sorted(module_contracts), "observation_semantics": dict(sorted(semantics.items())),
        "matrix_owners": dict(sorted(owners.items()))}


def validate_collection(collection, source_hashes, selected=TESTS):
    require(type(collection) is dict and collection.get("selected_test_modules") == list(selected)
        and type(collection.get("full_inventory")) is bool and (not collection["full_inventory"] or tuple(selected) == TESTS)
        and type(collection.get("executed_tests")) is int and collection["executed_tests"] == 0
        and collection.get("source_hashes") == source_hashes, "exact independent collection identity")
    counts, ids = collection.get("collection"), collection.get("test_ids")
    require(type(counts) is dict and set(counts) == set(selected)
        and all(is_int(value, 1) for value in counts.values()), "exact module collection counts")
    require(type(ids) is list and ids and all(type(i) is str and re.fullmatch(r"test_[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+", i) for i in ids)
        and ids == sorted(set(ids)), "exact collected ordinary case IDs")
    require(all(sum(i.startswith(module + ".") for i in ids) == counts[module] for module in selected)
        and sum(counts.values()) == len(ids), "module/count/case graph")
    contracts = collection.get("module_contracts")
    require(type(contracts) is dict and set(contracts) == set(selected), "exact declaring module set")
    combined = combined_contract(contracts)
    require(collection.get("control_contract") == combined, "exact collected semantic contract")
    for module, contract in contracts.items():
        module_ids = {i for i in ids if i.startswith(module + ".")}
        require(set(contract["matrix_owners"].values()) <= module_ids and all(pair["test_id"] in module_ids
            for pairs in contract["observation_semantics"].values() for pair in pairs), "all declared owners belong to collected module methods")
    return combined



def static_control_inventory(package):
    document, _ = object_member(package, "schemas/control-inventory.v1.json")
    require(type(document) is dict and set(document) == {
        "schema", "derivation", "baseline", "collection", "test_ids", "control_contract", "additions"}
        and document["schema"] == "friday.a049.static-control-inventory.v1"
        and document["derivation"] == "INHERITED_AUTHENTICATED_DECLARATIONS_PLUS_EXPLICIT_CURRENT_SOURCE_ADDITIONS; NOT_RUNTIME_COLLECTION",
        "strict static source declaration inventory, never observed collection credit")
    require(document["baseline"]["cases"] == 462 and document["baseline"]["required_keys"] == 642
        and document["baseline"]["matrices"] == 373 and document["baseline"]["rows"] == 11807,
        "retained old subset obligations")
    require(len(document["test_ids"]) == 464
        and len(document["control_contract"]["required_observations"]) == 657
        and len(document["control_contract"]["matrices"]) == 374
        and sum(map(len, document["control_contract"]["matrices"].values())) == 11819,
        "explicit mandatory addition unions; totals cannot replace identities")
    return document


def validate_expected_union(package, collection):
    expected = static_control_inventory(package)
    selected = collection.get("selected_test_modules")
    require(type(selected) is list and selected and len(selected) == len(set(selected))
        and set(selected) <= set(TESTS), "exact selected source declaration modules")
    ids = [i for i in expected["test_ids"] if i.split(".", 1)[0] in selected]
    counts = {module: expected["collection"][module] for module in selected}
    require(collection.get("test_ids") == ids and collection.get("collection") == counts,
        "all retained old and mandatory new ordinary source methods")
    whole = expected["control_contract"]
    owner_in = lambda owner: owner.split(".", 1)[0] in selected
    semantics = {key: pairs for key, pairs in whole["observation_semantics"].items()
        if any(owner_in(pair["test_id"]) for pair in pairs)}
    matrices = {name: rows for name, rows in whole["matrices"].items()
        if owner_in(whole["matrix_owners"][name])}
    wanted = {"declared_modules": sorted(selected), "matrices": matrices,
        "matrix_owners": {name: whole["matrix_owners"][name] for name in matrices},
        "required_observations": [key for key in whole["required_observations"] if key in semantics],
        "observation_semantics": semantics}
    require(collection.get("control_contract") == wanted,
        "exact source-owned mandatory cases/keys/rows/outcomes/matrix owners")
    return wanted


def validate_actual_collection(package, collection, source_hashes, launch=None):
    """Strict current collection receipt; declaration-only models use the pure helper."""
    validate_collection(collection, source_hashes)
    validate_expected_union(package, collection)
    require(set(collection) == {"collection", "completed_at_utc", "control_contract", "deadline",
        "elapsed_seconds", "executed_tests", "full_inventory", "module_contracts", "resources",
        "selected_test_modules", "source_hashes", "started_at_utc", "test_ids"}
        and collection.get("full_inventory") is True, "strict actual full collection receipt")
    validate_resources(collection.get("resources"))
    began, ended = instant(collection.get("started_at_utc")), instant(collection.get("completed_at_utc"))
    elapsed = collection.get("elapsed_seconds")
    validate_deadline(collection.get("deadline"), collection.get("started_at_utc"),
        collection["resources"]["wall_timeout_seconds"])
    require(type(elapsed) in (int, float) and math.isfinite(elapsed) and 0 <= elapsed <= 300
        and began <= ended <= instant(run_context()["deadline_at_utc"]) - timedelta(seconds=SEAL_RESERVE_SECONDS)
        and abs((ended - began).total_seconds() - elapsed) <= 2, "actual collection finite RUN interval")
    if launch is not None:
        require(collection["deadline"] == launch["deadline"]
            and instant(launch["started_at_utc"]) <= began <= ended <= instant(launch["completed_at_utc"])
            and elapsed <= launch["elapsed_seconds"] + 2, "collection actual retained launch interval")
        invocation, _ = object_member(package,
            str(Path(launch["run_root"]).relative_to(package)) + "/invocation.json")
        require(collection["resources"]["usage_start"] == invocation["resources"]["usage_start"],
            "collection actual retained enforcement start")
    return collection


def validate_projection(projection, expected_contract, expected_ids, source_hashes, *, complete):
    require(projection.get("source_hashes") == source_hashes, "complete source/harness/config digest set")
    require(projection.get("control_contract") == expected_contract and
        projection.get("forbidden_effect_attempts") == [], "contract/effect taint")
    controls = projection.get("controls")
    require(type(controls) is list and controls and all(type(r) is dict and r.get("status") == "PASS"
        and type(r.get("id")) is str for r in controls), "successful exact controls")
    require(len({r["id"] for r in controls}) == len(controls), "duplicate control IDs")
    passed = {r["id"] for r in controls}
    ordinary = sorted(r["id"] for r in controls if r["id"] in expected_ids)
    require(ordinary == sorted(expected_ids), "exact ordinary test ID multiset")
    for row in controls:
        if row["id"] in expected_ids:
            require(set(row) == {"id", "status"}, "ordinary case exact wire")
        else:
            parent = row.get("parent_test_id")
            require(set(row) == {"id", "status", "parent_test_id"} and parent in expected_ids
                and parent in passed and row["id"].startswith(parent + " (") and row["id"].endswith(")")
                and len(row["id"]) > len(parent) + 3, "extra subcontrol binds executed ordinary parent")
    matrices, owners = projection.get("matrices"), projection.get("matrix_test_ids")
    require(type(matrices) is dict and type(owners) is dict and set(matrices) == set(owners), "matrix ownership graph")
    if complete:
        require(set(matrices) == set(expected_contract["matrices"]), "entire expected matrix set")
    require(set(matrices) <= set(expected_contract["matrices"]), "unexpected matrix")
    for name, matrix in matrices.items():
        expected = expected_contract["matrices"][name]
        require(matrix == {"expected": expected, "observed": expected} and owners[name] in expected_ids
            and owners[name] == expected_contract["matrix_owners"][name],
            "exact rows / owning actual method")
    observed_keys, identities = set(), set()
    for row in projection.get("observations", []):
        require(type(row) is dict and set(row) == {"category", "key", "outcome", "evidence", "test_id"}
            and type(row["evidence"]) is dict and all(type(row[k]) is str and row[k] for k in ("category", "key", "outcome", "test_id")), "observation wire")
        require(row["test_id"] in expected_ids and row["test_id"] in passed, "observation owning actual method")
        identity = (row["category"], row["key"], row["test_id"])
        require(identity not in identities, "duplicate observation identity")
        identities.add(identity)
        require(row["outcome"] in {"PASS", "REFUSED", "CRASH_RETAINED", "INCOMPLETE"}, "observation semantics")
        require(row["outcome"] != "INCOMPLETE" or row["category"].endswith("-gap"), "explicit proof gap category")
        key = row["category"] + ":" + row["key"]
        require(key in expected_contract["observation_semantics"] and {"test_id": row["test_id"], "outcome": row["outcome"]}
            in expected_contract["observation_semantics"][key], "exact key expected outcome and owning method")
        observed_keys.add(key)
    if complete:
        require(set(expected_contract["required_observations"]) <= observed_keys, "mandatory actual observations")
    return passed


def validate_worker(package, launch, collection, source_hashes, *, diagnostic_lane=None):
    validate_actual_collection(package, collection, source_hashes)
    run = Path(launch["run_root"])
    record, _ = object_member(package, str(run.relative_to(package)) + "/run-result.json")
    projection, semantic = object_member(package, str(run.relative_to(package)) + "/semantic-projection.json")
    validate_current_binding(projection.get("run_context"))
    require(set(projection) == {"run_context", "collection", "controls", "observations", "matrices",
        "source_hashes", "forbidden_effect_attempts", "control_contract", "control_closure",
        "module_contracts", "matrix_test_ids"}, "strict worker raw/semantic projection schema")
    raw_projection, raw_sha = object_member(package, str(run.relative_to(package)) + "/raw-projection.json")
    require(record.get("raw_projection_sha256") == raw_sha and
        canonical_run_projection(raw_projection, run) == projection, "raw-to-logical projection binding")
    selected, case_ids = record.get("selected_test_modules"), record.get("selected_test_ids")
    require(type(selected) is list and selected and len(set(selected)) == len(selected) and set(selected) <= set(TESTS), "selected module set")
    require(type(case_ids) is list and case_ids and case_ids == sorted(set(case_ids)) and
        set(case_ids) <= set(collection["test_ids"]), "partial exact case set")
    require(all(identifier.split(".", 1)[0] in selected for identifier in case_ids), "selected case/module membership")
    partial = record.get("partial_case_subset")
    require(type(partial) is bool, "partial subset exact flag")
    check_launch(package, launch, run, only=selected, case_ids=case_ids if partial else None, diagnostic_lane=diagnostic_lane)
    expected_counts = {module: collection["collection"][module] for module in selected}
    require(projection.get("collection") == expected_counts, "exact independent worker module counts")
    if not partial:
        require(case_ids == [i for i in collection["test_ids"] if i.split(".", 1)[0] in selected], "entire selected module case set")
    contracts = {module: collection["module_contracts"][module] for module in selected}
    require(projection.get("module_contracts") == contracts, "worker declaration equals independent collection")
    contract = combined_contract(contracts)
    require(set(record) == {"schema", "started_at_utc", "completed_at_utc", "elapsed_seconds", "rc", "success",
        "test_count", "subcontrol_count", "failures", "errors", "skips", "semantic_sha256", "raw_projection_sha256",
        "selected_test_modules", "full_inventory", "diagnostic_lane", "official_credit", "selected_test_ids",
        "partial_case_subset", "control_closure", "host_effect_fence_installed", "source_hashes", "deadline",
        "capability_boundary", "resources"}, "strict actual worker result wire")
    require(record.get("schema") == "friday.a049.official-test-run.v1" and record.get("success") is True
        and type(record.get("rc")) is int and record["rc"] == 0 and record.get("host_effect_fence_installed") is True
        and record.get("full_inventory") is False and record.get("official_credit") is False
        and record.get("diagnostic_lane") == diagnostic_lane, "worker status / authority")
    require(record.get("semantic_sha256") == semantic, "worker projection digest")
    require(record.get("source_hashes") == source_hashes and record.get("deadline") == launch["deadline"]
        and record.get("capability_boundary") == CAPABILITY_BOUNDARY, "source/deadline/capability crosslinks")
    require(is_int(record.get("subcontrol_count"), 1) and record["subcontrol_count"] == len(projection.get("controls", [])), "exact ordinary and subcontrol count")
    began, ended = instant(record.get("started_at_utc")), instant(record.get("completed_at_utc"))
    elapsed = record.get("elapsed_seconds")
    require(type(elapsed) in (int, float) and math.isfinite(elapsed) and 0 <= elapsed <= launch["elapsed_seconds"] + 2
        and instant(launch["started_at_utc"]) <= began <= ended <= instant(launch["completed_at_utc"])
        and abs((ended - began).total_seconds() - elapsed) <= 2, "worker retained actual interval")
    require(is_int(record.get("test_count"), 1) and record["test_count"] == len(case_ids) and
        all(type(record.get(k)) is int and record[k] == 0 for k in ("failures", "errors", "skips")), "worker actual test counts")
    resources = record.get("resources", {})
    validate_resources(resources)
    invocation, _ = object_member(package, str(run.relative_to(package)) + "/invocation.json")
    require(resources["usage_start"] == invocation["resources"]["usage_start"]
        and all(resources[k] == invocation["resources"][k] for k in ("actual_rlimit_as", "actual_rlimit_cpu", "address_space_bytes_max", "cpu_seconds_max", "wall_timeout_seconds")), "worker invocation enforcement crosslink")
    validate_projection(projection, contract, case_ids, source_hashes, complete=not partial)
    require(record.get("control_closure") == projection.get("control_closure"), "worker closure crosslink")
    if not partial:
        require(record["control_closure"].get("complete") is True, "whole worker closure")
    return record, projection


def validate_diagnostic_worker(package, launch, collection, source_hashes, lane):
    require(lane in {"sol020_controls", "sol020_install", "sol020_harness"}
        and Path(launch["run_root"]).parent == package / "fixtures/coordination" / lane / "diagnostic-runs", "declared private diagnostic root")
    return validate_worker(package, launch, collection, source_hashes, diagnostic_lane=lane)


def validate_pass(package, detail, run, source_hashes):
    require(detail.get("run_root") == str(run) and type(detail.get("rc")) is int and detail["rc"] == 0, "pass target/status")
    check_launch(package, detail["collection_launch"], run, collect=True)
    collection, _ = object_member(package, str(run.relative_to(package)) + "/collection.json")
    validate_actual_collection(package, collection, source_hashes, detail["collection_launch"])
    ids = collection.get("test_ids")
    require(type(ids) is list and ids and ids == sorted(set(ids)), "collected exact ID set")
    workers = detail.get("workers")
    require(type(workers) is list and workers, "retained worker graph")
    union, roots = [], set()
    expected_union = {"run_context": run_binding(), "collection": collection["collection"], "controls": [],
        "observations": [], "matrices": {}, "matrix_test_ids": {},
        "source_hashes": source_hashes, "forbidden_effect_attempts": [],
        "module_contracts": collection["module_contracts"], "control_contract": collection["control_contract"],
        "control_closure": {"complete": True, "missing_matrices": [], "unexpected_matrices": [],
            "mismatched_matrices": [], "missing_observations": [], "kernel_or_privileged_enforcement_credit": False}}
    intervals = []
    records = []
    validate_worker_namespace(workers, run)
    for number, launch in enumerate(workers, 1):
        require(launch.get("run_root") not in roots and str(Path(launch["run_root"]).parent) == str(run.parent), "distinct bounded workers")
        roots.add(launch["run_root"])
        record, part = validate_worker(package, launch, collection, source_hashes)
        require(instant(launch["started_at_utc"]) >= instant(detail["collection_launch"]["completed_at_utc"]), "workers admitted after independent collection completed")
        require(launch["deadline"] == detail["collection_launch"]["deadline"], "common absolute pass assignment deadline")
        records.append(record)
        union.extend(record["selected_test_ids"])
        for name in ("controls", "observations"):
            expected_union[name].extend(part[name])
        for name in ("matrices", "matrix_test_ids"):
            require(not (set(expected_union[name]) & set(part[name])), "duplicate worker matrix graph")
            expected_union[name].update(part[name])
        intervals.extend([(datetime.fromisoformat(launch["started_at_utc"]), 1),
            (datetime.fromisoformat(launch["completed_at_utc"]), -1)])
    concurrent = 0
    for _, delta in sorted(intervals, key=lambda row: (row[0], row[1])):
        concurrent += delta
        require(0 <= concurrent <= 4, "actual worker overlap exceeds four")
    require(sorted(union) == ids, "all collected cases executed exactly once")
    record, _ = object_member(package, str(run.relative_to(package)) + "/run-result.json")
    projection, sha = object_member(package, str(run.relative_to(package)) + "/semantic-projection.json")
    for name in ("controls", "observations"):
        expected_union[name].sort(key=encode)
    validate_exact_union(projection, expected_union)
    require(set(record) == {"schema", "started_at_utc", "completed_at_utc", "elapsed_seconds", "rc", "success",
        "test_count", "subcontrol_count", "failures", "errors", "skips", "semantic_sha256", "selected_test_modules",
        "full_inventory", "host_effect_fence_installed", "control_closure", "source_hashes", "selected_test_ids",
        "deadline", "worker_launches", "collection_launch", "dispatcher_invocation", "resources", "execution",
        "worker_result_refs"}, "strict aggregate result wire")
    require(record.get("schema") == "friday.a049.official-test-run.v1" and record.get("success") is True
        and type(record.get("rc")) is int and record["rc"] == 0 and record.get("full_inventory") is True
        and record.get("semantic_sha256") == sha and record.get("test_count") == len(ids)
        and record.get("host_effect_fence_installed") is True, "aggregate source result")
    resources = record.get("resources", {})
    validate_aggregate_resources(resources, [r["resources"] for r in records])
    validate_dispatcher_invocation(record.get("dispatcher_invocation"), package, source_hashes, label=run.name.rsplit("-", 1)[0])
    require(record.get("deadline") == detail["collection_launch"]["deadline"]
        and record.get("source_hashes") == source_hashes
        and record.get("selected_test_ids") == ids and record.get("subcontrol_count") == len(projection["controls"]), "aggregate deadline/source/case/subcontrol crosslinks")
    require(record.get("worker_launches") == workers and record.get("collection_launch") == detail["collection_launch"], "exact actual launches and resource intervals")
    require(record.get("selected_test_modules") == list(TESTS) and is_int(record.get("test_count"), 1)
        and is_int(record.get("subcontrol_count"), 1) and all(type(record.get(key)) is int and record[key] == 0
            for key in ("failures", "errors", "skips")), "aggregate exact modules/count/status wire")
    before, after = instant(record.get("started_at_utc")), instant(record.get("completed_at_utc"))
    elapsed = record.get("elapsed_seconds")
    require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0
        and abs((after - before).total_seconds() - elapsed) <= 2
        and before <= instant(detail["collection_launch"]["started_at_utc"])
        and all(instant(w["completed_at_utc"]) <= after for w in workers)
        and after <= instant(record["deadline"]["assignment_deadline_utc"]), "aggregate actual enforced assignment interval")
    require(record.get("worker_result_refs") == [str(Path(w["run_root"]).relative_to(package)) + "/run-result.json" for w in workers], "worker result links")
    require(projection.get("collection") == collection["collection"] and
        projection.get("module_contracts") == collection["module_contracts"], "aggregate collection graph")
    validate_projection(projection, collection["control_contract"], ids, source_hashes, complete=True)
    require(record.get("control_closure") == projection.get("control_closure") and record["control_closure"].get("complete") is True, "aggregate closure")
    return record, projection, sha


def validate_worker_namespace(workers, run):
    require(type(workers) is list and workers, "nonempty worker namespace")
    for number, launch in enumerate(workers, 1):
        require(launch.get("run_root") == str(run.parent / (run.name + "-worker" + str(number))),
            "worker belongs to this exact pass namespace")


def validate_exact_union(projection, expected_union):
    require(projection == expected_union, "aggregate must equal authenticated worker evidence union exactly")


def validate_pair(package, pair, label):
    require(type(pair) is dict and set(pair) == {"schema", "launches", "complete", "success", "source_hashes",
        "deadline", "resources", "official_full_pair", "started_at_utc", "completed_at_utc", "elapsed_seconds",
        "dispatcher_invocation", "budget_stop"} and pair.get("budget_stop") is None, "strict whole pair receipt")
    require(pair.get("schema") == "friday.a049.test-pair-execution.v1" and pair.get("success") is True
        and pair.get("complete") is True and type(pair.get("launches")) is list and len(pair["launches"]) == 2, "exact two-pass pair")
    source_hashes = source_inventory(package)
    require(pair.get("official_full_pair") is True, "subset pair cannot receive complete official credit")
    validate_resources(pair.get("resources"), dispatcher=True)
    validate_dispatcher_invocation(pair.get("dispatcher_invocation"), package, source_hashes, label=label)
    passes = [validate_pass(package, detail, package / "fixtures/official-runs" / (label + "-" + str(n)), source_hashes)
        for n, detail in enumerate(pair["launches"], 1)]
    require(pair.get("source_hashes") == source_hashes and pair.get("deadline") == pair["launches"][0]["collection_launch"]["deadline"]
        == pair["launches"][1]["collection_launch"]["deadline"], "common final pair assignment source/deadline")
    require(passes[0][2] == passes[1][2], "two identical semantic projections")
    for record, _, _ in passes:
        require(record["dispatcher_invocation"] == pair["dispatcher_invocation"]
            and all(pair["resources"]["usage"][i] >= record["resources"]["dispatcher"]["usage"][i] for i in (0, 1, 2)), "pair aggregate actual dispatcher crosslink")
    return passes


def validate_dispatcher_invocation(invocation, package, hashes, *, label=None, mode="full-pair"):
    require(type(invocation) is dict and set(invocation) == {
        "argv", "cwd", "environment", "environment_scope", "source_sha256", "isolation", "run_context"},
        "dispatcher actual invocation shape")
    validate_current_binding(invocation["run_context"])
    argv = invocation.get("argv")
    prefix = ["/usr/bin/python3.14", "-I", "-S", "-B", str(package / "tests/execute_pair.py"), "--label"]
    require(type(argv) is list and argv[:6] == prefix and len(argv) >= 7
        and type(argv[6]) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,60}", argv[6]) is not None
        and (label is None or argv[6] == label) and invocation.get("cwd") == str(package)
        and invocation.get("source_sha256") == hashes["tests/execute_pair.py"], "dispatcher argv/cwd/current source identity")
    expected = prefix + [argv[6]] + run_context_flags()
    validate_mode_admission(mode)
    if mode == "collect":
        expected += ["--collect-readiness"]
    elif mode == "selfcheck":
        expected += ["--selfcheck-harness"]
    elif mode == "affected" and argv[7:] == ["--focused-checks"]:
        expected += ["--focused-checks"]
    elif mode == "affected":
        require(argv[7:8] == ["--only"] and argv[8:] and all(n in TESTS for n in argv[8:]),
            "explicit affected module set")
        expected += ["--only"] + argv[8:]
    require(argv == expected, "dispatcher exact admitted finite mode/context flags")
    require(type(invocation.get("environment")) is dict and set(invocation["environment"]) == {
        "HOME", "TMPDIR", "PATH", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE"}
        and all(value is None or type(value) is str for value in invocation["environment"].values())
        and invocation.get("environment_scope") == "consumed-fields-only; protected-startup NOT_PROVEN",
        "dispatcher honest environment scope")
    require(invocation.get("isolation") == {"isolated": 1, "no_site": 1, "dont_write_bytecode": 1}
        and all(type(value) is int for value in invocation["isolation"].values()), "dispatcher actual isolated flags")


def validate_focused(package, document):
    keys = {"schema", "source_hashes", "collection_ref", "collection_sha256", "collection_launch", "workers",
        "method_ids", "source_credit", "independent", "deadline", "resources", "run_context", "dispatcher_invocation"}
    optional = {"selfcheck_launch", "selfcheck_ref", "selfcheck_sha256"}
    require(type(document) is dict and set(document) in (keys, keys | optional), "strict focused wire keys")
    require(document.get("schema") == "friday.a049.focused-revalidation.v1"
        and document.get("source_credit") is False and document.get("independent") is False, "focused diagnostic authority")
    hashes = source_inventory(package)
    require(document.get("source_hashes") == hashes, "focused current final bytes")
    validate_current_binding(document.get("run_context"))
    validate_dispatcher_invocation(document.get("dispatcher_invocation"), package, hashes, mode="affected")
    collection, digest = object_member(package, document["collection_ref"])
    require(digest == document.get("collection_sha256"), "focused authenticated collection")
    collection_run = package / Path(document["collection_ref"]).parent
    check_launch(package, document["collection_launch"], collection_run, collect=True)
    validate_actual_collection(package, collection, hashes, document["collection_launch"])
    require(document["deadline"] == document["collection_launch"]["deadline"], "focused common immutable assignment deadline")
    validate_resources(document["resources"], dispatcher=True)
    workers = document.get("workers")
    require(type(workers) is list and workers, "focused nonempty actual workers")
    ids, observations, matrices, resources = [], [], {}, []
    roots = set()
    intervals = []
    for launch in workers:
        require(launch.get("run_root") not in roots and launch.get("deadline") == document["collection_launch"]["deadline"], "focused distinct workers/common assignment")
        roots.add(launch["run_root"])
        record, projection = validate_worker(package, launch, collection, hashes)
        intervals.extend(((instant(launch["started_at_utc"]), 1), (instant(launch["completed_at_utc"]), -1)))
        ids.extend(record["selected_test_ids"])
        observations.extend(projection["observations"])
        require(not (set(matrices) & set(projection["matrices"])), "focused duplicate matrix owner")
        matrices.update(projection["matrices"])
        resources.append(record["resources"])
    require(len(ids) == len(set(ids)) and sorted(ids) == document.get("method_ids"), "focused exact successful ordinary union")
    concurrent = 0
    for _, delta in sorted(intervals, key=lambda item: (item[0], item[1])):
        concurrent += delta
        require(0 <= concurrent <= 4, "focused actual worker overlap within enforced memory bill")
    ordinary = sorted(ids)
    if document.get("selfcheck_launch") is not None:
        selfcheck = validate_harness_selfcheck(package, document["selfcheck_launch"], hashes)
        selfcheck_raw, selfcheck_sha = object_member(package, document["selfcheck_ref"])
        require(document["selfcheck_launch"]["deadline"] == document["deadline"]
            and document["selfcheck_ref"] == str(Path(document["selfcheck_launch"]["run_root"]).relative_to(package)) + "/run-receipt.json"
            and selfcheck_sha == document.get("selfcheck_sha256") and selfcheck_raw == selfcheck, "focused retained actual selfcheck")
        ordinary += ["harness_selfcheck.main"]
    required = set(collection["control_contract"]["required_observations"])
    observed = {row["category"] + ":" + row["key"] for row in observations}
    return {"method_ids": sorted(ordinary), "observations": observations, "matrices": matrices,
        "resources": {"workers": resources}, "control_contract": collection["control_contract"],
        "complete": sorted(ids) == collection["test_ids"] and set(matrices) == set(collection["control_contract"]["matrices"]) and required <= observed}


def validate_harness_selfcheck(package, launch, hashes, *, diagnostic_lane=None):
    run = Path(launch["run_root"])
    check_launch(package, launch, run, harness=True, diagnostic_lane=diagnostic_lane)
    record, _ = object_member(package, str(run.relative_to(package)) + "/run-receipt.json")
    require(set(record) == {"schema", "success", "rc", "started_at_utc", "completed_at_utc", "elapsed_seconds",
        "invocation", "source_hashes", "controls", "error", "required_controls", "control_ids", "deadline", "resources",
        "capability_boundary", "intentional_unavailable_capability_probes", "real_privileged_effects", "real_network_calls",
        "kernel_enforcement_credit", "usage"}, "strict selfcheck complete current wire")
    require(record.get("schema") == "friday.a049.harness-selfcheck.v1" and record.get("success") is True
        and type(record.get("rc")) is int and record["rc"] == 0 and record.get("error") is None
        and record.get("source_hashes") == hashes and hashes == source_inventory(package)
        and record.get("deadline") == launch["deadline"] and record.get("capability_boundary") == CAPABILITY_BOUNDARY,
        "actual final-byte selfcheck source/status/boundary")
    require(record.get("real_privileged_effects") == 0 and type(record.get("real_privileged_effects")) is int
        and record.get("real_network_calls") == 0 and type(record.get("real_network_calls")) is int
        and record.get("kernel_enforcement_credit") is False, "selfcheck no privileged/kernel credit")
    rows = record.get("controls")
    require(type(rows) is list and rows and all(type(row) is dict and set(row) == {"id", "status"}
        and type(row["id"]) is str and row["status"] == "PASS" for row in rows), "actual selfcheck controls")
    ids = sorted(row["id"] for row in rows)
    require(len(ids) == len(set(ids)) and record.get("control_ids") == ids
        and record.get("required_controls") == list(HARNESS_REQUIRED_CONTROLS)
        and set(HARNESS_REQUIRED_CONTROLS) <= set(ids), "source-declared exact mandatory selfcheck controls")
    validate_resources(record.get("resources"))
    invocation, _ = object_member(package, str(run.relative_to(package)) + "/invocation.json")
    require(record.get("invocation") == invocation and invocation["resources"]["usage_start"] == record["resources"]["usage_start"], "selfcheck exact retained invocation/enforcement")
    before, after = instant(record.get("started_at_utc")), instant(record.get("completed_at_utc"))
    elapsed = record.get("elapsed_seconds")
    require(type(elapsed) in (int, float) and math.isfinite(elapsed) and 0 <= elapsed <= launch["elapsed_seconds"] + 2
        and instant(launch["started_at_utc"]) <= before <= after <= instant(launch["completed_at_utc"])
        and abs((after - before).total_seconds() - elapsed) <= 2, "actual selfcheck bounded interval")
    return record


ROOT_WRITER_FD = 197
ACTOR_WRITER_FD = 196
MAGIC = b"A132"
HEADER_BYTES = 72
PAYLOAD_BYTES = 3072
_CLIENT = None
_SOURCE_READ_METER = None

class SourceInventoryGeneration:
 """Source-owned current full bytes; duplicates reuse only this held generation.
 The reviewed actor graph cannot mutate the selected 54 member paths. This
 is not a new arbitrary-malicious-writer or kernel-sandbox obligation.
 """
 def __init__(self,package,expected,binding):
  self.package=package;self.expected=dict(expected);self.binding=binding
  self.members={};self.pending=None;self.close_errors=[];self.closed=False
  self.first_close_error=None;self.close_recording_error=None
  self.bindings=[binding]
 def reserve(self,relative):
  slot={"path":relative,"fd":None,"identity":None,"raw":None,"first_error":None,
   "close_attempted":False,"close_error":None,"closed":False}
  self.pending=slot
  self.members[relative]=slot
  return slot
 def scan(self):
  result={}
  for relative,pin in self.expected.items():
   raw=raw_member(self.package,relative,self)
   require(hashlib.sha256(raw).hexdigest()==pin,"current full Source54 generation pin")
   result[relative]=pin
  self.verify()
  return result
 def verify(self):
  require(not self.closed and len(self.members)==len(self.expected)==54,
   "same complete owned Source54 generation required")
  for relative,pin in self.expected.items():
   row=self.members[relative];path=self.package/relative
   before=file_identity(path.lstat())
   opened=file_identity(os.fstat(row["fd"]))
   require(before==opened==row["identity"] and not path.is_symlink()
    and hashlib.sha256(row["raw"]).hexdigest()==pin,
    "current full9 plus same owned full immutable byte generation")
   require(file_identity(os.fstat(row["fd"]))==opened
    and file_identity(path.lstat())==before,"current held/named after full9")
  return dict(self.expected)
 def close(self):
  for row in self.members.values():
   if row["fd"] is None or row["close_attempted"]:continue
   row["close_attempted"]=True
   try:
    raw_client().call("receipt.Source_generation.close",_SOURCE_RAW_OS.close,row["fd"])
    row["closed"]=True
   except BaseException as error:
    row["close_error"]=error
    if self.first_close_error is None:self.first_close_error=error
    try:self.close_errors.append(error)
    except BaseException as recorder:
     row["cleanup_recording_error"]=recorder;self.close_recording_error=recorder
  self.closed=all(row["closed"] for row in self.members.values())
  if not self.closed:
   if self.first_close_error is not None:raise self.first_close_error
   raise RuntimeError("Source_generation_close_unconfirmed")

_SOURCE_INVENTORY_OWNER=None
_SOURCE_INVENTORY_GENERATIONS=[]
def full_source_inventory_generation(package,expected,binding):
 global _SOURCE_INVENTORY_OWNER
 require(type(expected) is dict and len(expected)==54,"original complete Source54")
 if _SOURCE_INVENTORY_OWNER is not None:
  owner=_SOURCE_INVENTORY_OWNER
  require(owner.package==package and owner.expected==expected and not owner.closed,
   "never refresh/rebind a different Source54 or terminated immutable generation")
  owner.verify()
  require(_SOURCE_GRAPH is not None and all(str(package/relative) in _SOURCE_GRAPH.protected for relative in expected),
   "actual same-process reviewed write fence for legal full-byte ownership reuse")
  owner.bindings.append(binding)
  raw_client().note("receipt.Source_generation.bound_reuse",[binding],
   {"members":54,"same_held_fullbytes_current9":True,"fresh_physical_scan_performed":False})
  return dict(owner.expected)
 if _SOURCE_GRAPH is not None:_SOURCE_GRAPH.protect(package,expected)
 owner=SourceInventoryGeneration(package,expected,binding)
 _SOURCE_INVENTORY_OWNER=owner
 _SOURCE_INVENTORY_GENERATIONS.append(owner)
 return owner.scan()

def bind_source_read_meter(meter):
    global _SOURCE_READ_METER
    if _SOURCE_READ_METER is not None and _SOURCE_READ_METER is not meter:
        raise ValueError("same-process Source read meter cannot change")
    require(meter.maximum==16777216,"unchanged Source controller 16MiB cap")
    _SOURCE_READ_METER=meter

def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
        allow_nan=False, separators=(",", ":")).encode("ascii")

def id9(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
        info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns]

def value(item):
    if type(item) in (bytearray,memoryview):
        view=memoryview(item)
        return {"type":"buffer_snapshot","buffer_kind":type(item).__name__,
            "bytes":value(view.tobytes()),"nbytes":view.nbytes,"readonly":view.readonly,
            "format":view.format,"itemsize":view.itemsize,"ndim":view.ndim,
            "shape":list(view.shape),"strides":list(view.strides),
            "suboffsets":list(view.suboffsets),"contiguous":view.contiguous}
    if item is None or type(item) in (str, bool, int):
        return item
    if type(item) is float:
        return {"type": "float_hex", "value": item.hex()}
    if type(item) is bytes:
        pin=hashlib.sha256(item).hexdigest()
        if len(item)==4139478 and pin=="e7255f04bc4fa806960fcca79b6b37b67202417170ed59c3b9101863e3e91224":
            return {"type":"held_bytes","length":len(item),"sha256":pin}
        return {"type": "bytes", "length": len(item), "sha256": hashlib.sha256(item).hexdigest(),
            "raw_hex": item.hex()}
    if isinstance(item, os.stat_result):
        return {"type": "identity9", "value": id9(item)}
    if isinstance(item, (list, tuple)):
        return [value(v) for v in item]
    if type(item) in (set, frozenset):
        return {"type": "set", "value": [value(v) for v in sorted(item)]}
    if type(item) is dict:
        return {"type": "mapping", "value": [[value(k), value(v)] for k, v in item.items()]}
    if isinstance(item, Path):
        return {"type":"path","value":os.fspath(item)}
    if hasattr(item, "fd") and hasattr(item, "events"):
        return {"type": "selector_key", "fd": item.fd, "events": item.events, "data": value(item.data)}
    if isinstance(item, int):
        return {"type": "integer_subtype", "name": type(item).__name__, "value": int(item)}
    if isinstance(item, os.PathLike):
        return {"type":"path","value":os.fspath(item)}
    if hasattr(item,"args") and hasattr(item,"returncode"):
        return {"type":"completed_process","args":value(item.args),"returncode":item.returncode,
            "stdout":value(getattr(item,"stdout",None)),"stderr":value(getattr(item,"stderr",None))}
    if hasattr(item, "fileno"):
        return {"type": "file_handle", "fd": item.fileno(), "name": value(getattr(item, "name", None))}
    if hasattr(item, "pid"):
        return {"type": "process", "pid": item.pid, "returncode": getattr(item, "returncode", None)}
    if hasattr(item,"get_map"):
        return {"type":"selector_map","entries":value(dict(item.get_map()))}
    if callable(item):
        return {"type":"callable_reference","module":getattr(item,"__module__",None),
            "qualname":getattr(item,"__qualname__",type(item).__qualname__)}
    if hasattr(item,"done") and hasattr(item,"cancelled"):
        return {"type":"future_state","done":item.done(),"cancelled":item.cancelled()}
    raise TypeError("unrepresented_a132_actor_value:" + type(item).__name__)

def exception_projection(e):
    if e is None:return None
    owners=[e];ids={id(e):0};nodes=[]
    def ref(error):
        if error is None:return None
        if id(error) not in ids:ids[id(error)]=len(owners);owners.append(error)
        return ids[id(error)]
    def graph_value(item):
     if isinstance(item,BaseException):return {'type':'error_ref','id':ref(item)}
     if type(item) in (list,tuple):return [graph_value(v) for v in item]
     if type(item) is dict:return {'type':'mapping','value':[[graph_value(k),graph_value(v)] for k,v in item.items()]}
     return value(item)
    for error in owners:
        frames=[];tb=error.__traceback__
        while tb is not None:
            c=tb.tb_frame.f_code;frames.append([c.co_filename,tb.tb_lineno,c.co_name]);tb=tb.tb_next
        nodes.append({'id':ids[id(error)],'type':type(error).__name__,'module':type(error).__module__,
            'args':graph_value(error.args),'text':str(error),'errno':getattr(error,'errno',None),
            'filename':graph_value(getattr(error,'filename',None)),'filename2':graph_value(getattr(error,'filename2',None)),
            'state':graph_value(vars(error)),'notes':graph_value(getattr(error,'__notes__',None)),
            'frames':frames,'cause':ref(error.__cause__),'context':ref(error.__context__),
            'suppress_context':error.__suppress_context__,'groups':[ref(x) for x in getattr(error,'exceptions',())]})
    return {'schema':'friday.error-graph.v1','root':0,'nodes':nodes,'truncated':False}

class Producer:
 def __init__(self,fd,actor,ends):
  self.fd,self.actor,self.ends=fd,actor,tuple(ends)
  self.failed=False;self.failure=None;self.raw_events=[];self.pending_raw=None
  self.recorder_errors=[];self.recorder_pending=None;self.effects=[];self.pending_effect=None
  self.close_attempted=False;self.close_error=None;self.close_state="OWNED"
  self.emergency_raw=[None]*7;self.first_failed_raw=[None]*7;self.emergency_damage=[None,None]
  self.last_recording_allocation_error=None
  self.error_graphs=[];self.pending_error_graph=None;self.error_graph_capture_error=None
  self.pid,self.parent=os.getpid(),os.getppid()
  self.sequence=self.frames=self.written_bytes=self.attempts=0
  self.terminal_sent=False;self.lock=threading.RLock()
  self._write,self._select=os.write,select.select
  self._time,self._mono=time.time,time.monotonic
  os.set_blocking(fd,False)
  self.identity=id9(os.fstat(fd))
  self.note("channel.attach",[],{"fd":fd,"pipe_identity":self.identity,
   "parent_pid":self.parent,"actual_pid":self.pid})
 def left(self):
  return min(self.ends[0]-self._time(),self.ends[1]-self._mono())
 def damage(self,raw,error):
  self.failed=True
  if self.failure is None:
   first=self.first_failed_raw
   first[0]=raw[0];first[1]=raw[1];first[2]=raw[2];first[3]=raw[3]
   first[4]=raw[4];first[5]=raw[5];first[6]=raw[6]
   self.emergency_damage[0]=first;self.emergency_damage[1]=error
   self.failure=self.emergency_damage
  self.recorder_pending=self.emergency_damage
  try:
   additional=[raw,error];self.recorder_pending=additional;self.recorder_errors.append(additional)
  except BaseException as later:self.last_recording_allocation_error=later
 def note(self,operation,arguments,result=None,error=None):
  raw_owner=self.emergency_raw
  raw_owner[0]=self.sequence;raw_owner[1]=operation;raw_owner[2]=arguments
  raw_owner[3]=result;raw_owner[4]=error;raw_owner[5]=raw_owner[6]=None
  self.pending_raw=raw_owner
  if error is not None:
   self.pending_error_graph=error
   try:
    graph=capture_raw_exception_graph(error)
    self.pending_error_graph=graph;self.error_graphs.append(graph)
   except BaseException as capture:
    self.error_graph_capture_error=capture
    self.damage(raw_owner,capture)
  try:
   with self.lock:
    raw_owner=[self.sequence,operation,arguments,result,error,None,None]
    self.pending_raw=raw_owner
    self.raw_events.append(raw_owner)
    self.sequence+=1
    if self.failed:return
    graph=None if error is None else exception_projection(error)
    row={"schema":"friday.a137.actor-event.v3","actor":self.actor,
     "pid":self.pid,"parent_pid":self.parent,"sequence":raw_owner[0],
     "operation":operation,"arguments":value(arguments),
     "result":value(result) if error is None else None,
     "error":None if error is None else {"type":type(error).__name__,
      "module":type(error).__module__,"errno":getattr(error,"errno",None),
      "args":graph["nodes"][0]["args"],"text":str(error),
      "filename":value(getattr(error,"filename",None)),
      "filename2":value(getattr(error,"filename2",None)),"graph":graph},
     "producer_data_not_Root_authority":True}
    raw=canonical(row);raw_owner[5]=raw
    digest=hashlib.sha256(raw).digest()
    for at in range(0,len(raw),PAYLOAD_BYTES):
     payload=raw[at:at+PAYLOAD_BYTES]
     header=(MAGIC+self.pid.to_bytes(8,"big")+raw_owner[0].to_bytes(8,"big")
      +len(raw).to_bytes(8,"big")+at.to_bytes(8,"big")
      +len(payload).to_bytes(4,"big")+digest)
     if len(header)!=HEADER_BYTES:raise ValueError("a132_transport_header")
     frame=header+payload;raw_owner[6]=[frame,0,None]
     while True:
      left=self.left()
      if left<=0:raise TimeoutError("a132_original_end_transport")
      if not self._select([],[self.fd],[],left)[1]:
       raise TimeoutError("a132_original_end_transport")
      self.attempts+=1
      try:count=self._write(self.fd,frame)
      except BlockingIOError as blocked:
       raw_owner[6][2]=blocked
       continue
      except BaseException as failure:
       raw_owner[6][2]=failure
       raise
      raw_owner[6][1]=count
      self.written_bytes+=count
      if count!=len(frame):raise OSError(errno.EIO,"a132_atomic_frame_incomplete")
      self.frames+=1
      break
  except BaseException as failure:self.damage(self.pending_raw,failure)
 def call(self,operation,function,*args,**kwargs):
  cleanup=operation=="close" or operation.endswith(".close")
  if not cleanup:self.check()
  slot=[operation,args,kwargs,None,None]
  self.pending_effect=slot
  self.effects.append(slot)
  arguments=[args,kwargs]
  end_operation=operation+".end"
  self.note(operation+".begin",arguments)
  if not cleanup:self.check()
  try:result=function(*args,**kwargs)
  except BaseException as error:
   slot[4]=error
   self.note(end_operation,arguments,error=error)
   raise
  slot[3]=result
  self.note(end_operation,arguments,result)
  return result
 def check(self):
  if self.failed:raise RuntimeError("a132_lossless_recording_failed")
 def reference(self):
  return {"schema":"friday.a132.Root-held-actor-events-ref.v1","actor":self.actor,
   "pid":self.pid,"event_count":self.sequence,"frames_sent":self.frames,
   "transport_successful_bytes":self.written_bytes,"transport_attempts":self.attempts,
   "recording_failure":None if self.failure is None else {
    "event_sequence":self.failure[0][0],"operation":self.failure[0][1],
    "full_raw_owner_retained":True,"durable_independent_custody":"NOT_CONFIRMED"},
   "producer_complete_claim":not self.failed,
   "independent_Root_confirmation":"REQUIRED_NOT_SUPPLIED_BY_SOURCE"}
 def terminal(self,code):
  self.note("actor.terminal",[],{"native_exit_intent":code,
   "recording_state_before_terminal":self.reference()})
  self.terminal_sent=not self.failed
 def close(self):
  if self.close_attempted:
   if self.close_state!="CLOSED":raise RuntimeError("ambiguous_close_no_retry")
   return
  self.close_attempted=True;self.close_state="CLOSE_ATTEMPTED"
  self.note("actor.final_close.begin",[self.fd])
  try:os.close(self.fd)
  except BaseException as error:
   self.close_error=error;self.close_state="AMBIGUOUS_CLOSE"
   self.note("actor.final_close.error",[self.fd],error=error)
   raise
  self.close_state="CLOSED"

_ATTACH_OWNER=None
_ATTACH_STATE="NEVER_ATTEMPTED"
def attach_caller(ends):
 global _CLIENT,_ATTACH_OWNER,_ATTACH_STATE
 if _ATTACH_STATE!="NEVER_ATTEMPTED":raise RuntimeError("attachment_generation_already_attempted")
 _ATTACH_STATE="OWNED_PARTIAL"
 _ATTACH_OWNER=[None,None,None,[None,None],None,[False,False]]
 _ATTACH_OWNER[4]=Producer.__new__(Producer)
 fd=None
 try:
  parent=os.getppid()
  fd=os.open("/proc/%d/fd/%d"%(parent,ROOT_WRITER_FD),os.O_WRONLY|os.O_CLOEXEC)
  _ATTACH_OWNER[0]=fd
  info=os.fstat(fd)
  if not __import__("stat").S_ISFIFO(info.st_mode):raise ValueError("a132_Root_pipe_required")
  _ATTACH_OWNER[1]=os.dup2(fd,ACTOR_WRITER_FD,inheritable=False)
  if fd!=ACTOR_WRITER_FD:
   _ATTACH_OWNER[5][0]=True
   os.close(fd)
   _ATTACH_OWNER[0]=None
  Producer.__init__(_ATTACH_OWNER[4],ACTOR_WRITER_FD,"caller",ends)
  _CLIENT=_ATTACH_OWNER[4];_ATTACH_STATE="ATTACHED"
 except BaseException as error:
  _ATTACH_OWNER[2]=error;_ATTACH_STATE="FAILED"
  for index,owned in enumerate(_ATTACH_OWNER[:2]):
   if owned is not None and not _ATTACH_OWNER[5][index] and (index==0 or owned!=fd):
    _ATTACH_OWNER[5][index]=True
    try:
     os.close(owned);_ATTACH_OWNER[index]=None
    except BaseException as close_error:_ATTACH_OWNER[3][index]=close_error
  raise
 return _CLIENT
def attach_inherited(actor,ends):
 global _CLIENT,_ATTACH_OWNER,_ATTACH_STATE
 if _ATTACH_STATE=="ATTACHED":
  if _CLIENT.actor!=actor:raise RuntimeError("a132_actor_ownership_rebinding")
  return _CLIENT
 if _ATTACH_STATE!="NEVER_ATTEMPTED":raise RuntimeError("attachment_generation_already_attempted")
 _ATTACH_STATE="OWNED_PARTIAL"
 _ATTACH_OWNER=[ACTOR_WRITER_FD,None,None,[None,None],None,[False,False]]
 _ATTACH_OWNER[4]=Producer.__new__(Producer)
 _ATTACH_OWNER[4].fd=ACTOR_WRITER_FD
 try:
  info=os.fstat(ACTOR_WRITER_FD)
  if not __import__("stat").S_ISFIFO(info.st_mode):raise ValueError("a132_inherited_Root_pipe_required")
  Producer.__init__(_ATTACH_OWNER[4],ACTOR_WRITER_FD,actor,ends)
  _CLIENT=_ATTACH_OWNER[4];_ATTACH_STATE="ATTACHED"
 except BaseException as error:
  _ATTACH_OWNER[2]=error;_ATTACH_STATE="FAILED";_ATTACH_OWNER[5][0]=True
  try:
   os.close(ACTOR_WRITER_FD);_ATTACH_OWNER[0]=None
  except BaseException as close_error:_ATTACH_OWNER[3][0]=close_error
  raise
 return _CLIENT

def observed_fds(ordinary):
    if _CLIENT is None:
        raise RuntimeError("a132_missing_performing_actor")
    _CLIENT.check()
    return tuple(ordinary) + (ACTOR_WRITER_FD,)

def note(operation, arguments, result=None, error=None):
    if _CLIENT is None:
        raise RuntimeError("a132_unattached_actor_event")
    _CLIENT.note(operation, arguments, result, error)

def raw_client():
    if _CLIENT is None:
        raise RuntimeError("a132_unattached_actor")
    return _CLIENT

def capture_raw_exception_graph(error):
    """Full raw cause/context/group links with exact original traceback owners.

    Original objects are owned first. This is NOT a native heap size/durable
    transport bound; C state and final outside serialization remain mandatory.
    """
    graph={"root":error,"pending":error,"nodes":[],"complete_python_links":False}
    todo=[error];seen=set()
    while todo:
        current=todo.pop()
        if id(current) in seen:continue
        seen.add(id(current));graph["pending"]=current
        row={"error":current,"args":current.args,"traceback":current.__traceback__,
            "cause":current.__cause__,"context":current.__context__,
            "suppress_context":current.__suppress_context__,"notes":getattr(current,"__notes__",None),
            "groups":getattr(current,"exceptions",()),"python_state":vars(current)}
        graph["nodes"].append(row)
        for linked in (row["cause"],row["context"],*row["groups"]):
            if linked is not None:todo.append(linked)
    graph["pending"]=None;graph["complete_python_links"]=True
    return graph

class _RootObservedModule:
    OPERATIONS = {"open","close","read","pread","write","pwrite","fstat","lstat","stat",
        "readlink","listdir","getpid","getppid","pidfd_open","wait4","set_blocking",
        "dup2","fcntl","memfd_create","fchmod","fsync","getrlimit","setrlimit",
        "getrusage","time","monotonic","Popen","run","pthread_sigmask","signal","setitimer"}
    def __init__(self,module,actor):
        object.__setattr__(self,"raw",module)
        object.__setattr__(self,"owner",actor)
    def __getattr__(self,name):
        item=getattr(self.raw,name)
        if name in self.OPERATIONS:
            return lambda *args,**kwargs: raw_client().call(self.owner+"."+name,item,*args,**kwargs)
        return item
    def __setattr__(self,name,item):
        setattr(self.raw,name,item)

def bind_root_observer(client):
    global _CLIENT
    if _CLIENT is not None and _CLIENT is not client:
        raise ValueError("Root observer channel owner cannot change")
    _CLIENT=client

def install_root_observer(actor,ends):
    global time,select,threading
    import time,select,threading
    client=attach_inherited(actor,ends)
    client.check()
    sys.modules["_friday_receipt_owner"]=sys.modules.get(__name__) or types.SimpleNamespace(**globals())
    install_source_graph(actor)
    return client

def instrument_root_module(namespace,role):
    for name in ("os","time","resource","subprocess","fcntl","signal"):
        module=namespace.get(name)
        if module is not None and not isinstance(module,_RootObservedModule):
            namespace[name]=_RootObservedModule(module,role)
    if _SOURCE_GRAPH is not None and "ThreadPoolExecutor" in namespace:
        import concurrent.futures
        namespace["ThreadPoolExecutor"]=concurrent.futures.ThreadPoolExecutor
    return raw_client()

def observed_load(name,path):
    """One receipt owner and literal source-read custody before execution."""
    import importlib.util
    if Path(path).name=="receipt_contract.py":
        owner=sys.modules.get("_friday_receipt_owner")
        if owner is not None:return owner
    slot={"name":name,"path":path,"module":None,"raw":None,"error":None}
    graph=_SOURCE_GRAPH
    if graph is None:raise PermissionError("owned loader required after receipt bootstrap")
    graph.pending=slot;graph.owners.append(slot)
    try:
        spec=importlib.util.spec_from_file_location(name,path)
        slot["module"]=importlib.util.module_from_spec(spec)
        slot["raw"]=raw_member(Path(path).parents[1],"/".join(Path(path).parts[-2:]))
        sys.modules[name]=slot["module"]
        raw_client().note("loader.literal_source",[name,str(path)],slot["raw"])
        raw_client().check()
        exec(compile(slot["raw"],str(path),"exec",dont_inherit=True),slot["module"].__dict__)
        raw_client().note("loader.execute.returned",[name,str(path)],None)
        instrument_root_module(slot["module"].__dict__,name)
        return slot["module"]
    except BaseException as error:
        slot["error"]=error
        raw_client().note("loader.execute.error",[name,str(path)],error=error)
        sys.modules.pop(name,None)
        raise

class OwnedStream:
    def __init__(self,raw,owner):
        self.raw,self.owner=raw,owner
        self.close_attempted=False;self.close_error=None
        self.owned_fd=raw.fileno()
    def __getattr__(self,name):
        item=getattr(self.raw,name)
        if name in ("read","read1","readinto","readinto1","readline","readlines","write","writelines",
            "flush","seek","tell","truncate"):
            return lambda *args,**kwargs:self.owner.invoke("stream."+name,item,args,kwargs)
        return item
    def __enter__(self):return self
    def __exit__(self,kind,error,tb):
        try:self.close()
        except BaseException:
            if error is None:raise
        return False
    def __iter__(self):return self
    def __next__(self):
        raw=self.readline()
        if not raw:raise StopIteration
        return raw
    def close(self):
        if self.close_attempted:
            if self.close_error is not None:raise RuntimeError("ambiguous stream close: no retry")
            return
        self.close_attempted=True
        try:
            result=self.owner.invoke("stream.close",self.raw.close,(),{})
            if self.owner.pending.get("raw_error") is not None:
                self.close_error=self.owner.pending["raw_error"]
                return None
            generation=self.owner.fd_generations.get(self.owned_fd)
            if generation is not None:generation["close_attempted"]=generation["close_returned"]=True
            return result
        except BaseException as error:self.close_error=error;raise

class SourceGraphOwner:
    """Approved-code observation; preserves the existing reviewed-code fence.

    Does not claim a hostile-Python kernel sandbox. Raw owners remain separate
    from the fallible producer projection. Popen owns its partial object before
    initialization, including stock streams/child/error state on any exception.
    """
    def __init__(self,role):
        import threading
        self.role=role;self.local=threading.local();self.owners=[];self.pending=None
        self.first_error=None;self.cleanup_errors=[];self.popen=[];self.fd_generations={}
        self.fd_history=[];self.fd_pending=None
        self.protected=set();self.protected_parent=set();self.raw_streams=[]
        self.raw_call_serial=0;self.physical_writes=[];self.physical_pending=None
        self.channel_serial=0;self.physical_gaps=[]
        self.write_lock=threading.RLock()
    def protect(self,package,relatives):
        for relative in relatives:
            path=str(package/relative)
            self.protected.add(path)
            self.protected_parent.update(str(parent) for parent in Path(path).parents if package==parent or package in parent.parents)
    def fence(self,operation,args,kwargs):
        if not self.protected:return
        name=operation.removeprefix("os.")
        write_names={"write","pwrite","writev","pwritev","ftruncate","fchmod","fsync","fdatasync"}
        path_names={"unlink","remove","rmdir","rename","replace","chmod","truncate"}
        targets=[]
        if name in write_names and args:targets=[_SOURCE_RAW_OS.readlink("/proc/self/fd/"+str(args[0]))]
        elif name in path_names:targets=[_target for _target in args[:2] if isinstance(_target,(str,bytes,Path))]
        elif name=="open" and len(args)>1 and args[1]&(_SOURCE_RAW_OS.O_WRONLY|_SOURCE_RAW_OS.O_RDWR|_SOURCE_RAW_OS.O_CREAT|_SOURCE_RAW_OS.O_TRUNC|_SOURCE_RAW_OS.O_APPEND):targets=[args[0]]
        elif operation in ("stream.open","stream.fdopen") and args:
            mode=kwargs.get("mode",args[1] if len(args)>1 else "r")
            if any(letter in str(mode) for letter in "wax+"):
                targets=[_SOURCE_RAW_OS.readlink("/proc/self/fd/"+str(args[0])) if type(args[0]) is int else args[0]]
        for target in targets:
            path=Path(_SOURCE_RAW_OS.fsdecode(target))
            if not path.is_absolute():
                directory=kwargs.get("dir_fd")
                path=Path(_SOURCE_RAW_OS.getcwd() if directory is None else _SOURCE_RAW_OS.readlink("/proc/self/fd/"+str(directory)))/path
            require(str(path) not in self.protected and str(path) not in self.protected_parent,
                "same-generation reviewed Source member/ancestor immutable")
    def invoke(self,operation,function,args,kwargs):
        if operation in ("os.write","os.pwrite","os.writev","os.pwritev","raw.write") and not getattr(self.local,"recorder",False):
            # Serializes actual physical writes within this actor/process;
            # cross-process order is separately reconstructed from real reads.
            with self.write_lock:return self._invoke(operation,function,args,kwargs)
        return self._invoke(operation,function,args,kwargs)
    def _invoke(self,operation,function,args,kwargs):
        if getattr(self.local,"recorder",False):
            # Suppress recursive event projection, NOT physical Source reads.
            # The meter retains its own raw row before the actual operation.
            meter=_SOURCE_READ_METER
            if meter is not None and operation in ('os.read','os.pread','os.readv','os.preadv'):
                if operation in ('os.readv','os.preadv'):return meter.adapter(operation,function,args,kwargs)
                return meter.read(operation,function,args,args[0],args[2] if operation=='os.pread' else None)
            if meter is not None and operation in ('raw.read','raw.readinto'):
                requested=args[0] if operation=='raw.read' else memoryview(args[0]).nbytes
                return meter.raw_read(operation,function,args,kwargs,requested,function.__self__.fileno(),
                    'bytes' if operation=='raw.read' else 'count',() if operation=='raw.read' else (args[0],))
            return function(*args,**kwargs)
        slot={"operation":operation,"arguments":[args,kwargs],"raw_result":None,
            "raw_error":None,"recording_error":None,"active_error":sys.exc_info()[1]}
        self.pending=slot;self.owners.append(slot)
        if operation in ('os.open','os.memfd_create','os.pidfd_open','os.dup','os.dup2','os.pipe','os.pipe2'):
            allocation={'effect_owner':slot,'state':'ACQUIRE_STARTED','raw_return':None,
                'raw_error':None,'cells':[{'fd':None,'close_attempted':False,'close_returned':False},
                    {'fd':None,'close_attempted':False,'close_returned':False}]}
            self.fd_pending=allocation;self.fd_history.append(allocation)
            slot['allocation']=allocation
        bound=getattr(function,"__self__",None)
        cleanup=operation.endswith(".close") or operation in ("Pool.shutdown","Thread.join","Popen.wait","Popen.poll")
        if not cleanup:raw_client().check()
        try:
            if operation.startswith(("stream.","raw.")) and not operation.endswith(".__init__") and bound is not None and hasattr(bound,"fileno"):
                channel={"fd":None,"encoding":None,"errors":None,"name":None}
                slot["channel"]=channel
                channel["fd"]=bound.fileno()
                channel["encoding"]=getattr(bound,"encoding",None)
                channel["errors"]=getattr(bound,"errors",None)
                channel["name"]=getattr(bound,"name",None)
                channel["unbuffered_stock_fileio"]=(type(bound).__module__=="_io"
                    and type(bound).__name__=="FileIO")
                channel["generation"]=getattr(bound,"_source_channel_generation",None)
            self.fence(operation,args,kwargs)
            if operation in ("os.write","raw.write"):
                channel=slot.setdefault("channel",{})
                fd=channel.get("fd",args[0] if operation=="os.write" else None)
                channel["fd"]=fd;channel["identity9"]=id9(_SOURCE_RAW_OS.fstat(fd))
                slot['physical_payload_before']=bytes(memoryview(args[1] if operation=='os.write' else args[0]))
                channel['physical_payload_before']=slot['physical_payload_before']
                channel["physical_sequence"]=self.raw_call_serial;self.raw_call_serial+=1
                channel["pipe_buf"]=_SOURCE_RAW_OS.fpathconf(fd,"PC_PIPE_BUF") if stat.S_ISFIFO(channel["identity9"][2]) else None
            if not cleanup:raw_client().check()
            if operation=="os.close":
                generation=self.fd_generations.get(args[0])
                if generation is not None:
                    if generation.get("close_returned"):
                        slot["operation"]="os.close.already_confirmed_no_effect"
                        self.record(slot)
                        return None
                    require(not generation.get("close_attempted"),"ambiguous close generation: no retry")
                    generation["close_attempted"]=True
            if operation in ("raw.read","raw.readinto") and _SOURCE_READ_METER is not None:
                requested=args[0] if operation=="raw.read" else memoryview(args[0]).nbytes
                slot["raw_result"]=_SOURCE_READ_METER.raw_read(operation,function,args,kwargs,
                    requested,slot["channel"]["fd"],"bytes" if operation=="raw.read" else "count",
                    () if operation=="raw.read" else (args[0],))
            elif operation in ("os.read","os.pread") and _SOURCE_READ_METER is not None:
                slot["raw_result"]=_SOURCE_READ_METER.read(operation,function,args,args[0],
                    args[2] if operation=="os.pread" else None)
            elif _SOURCE_READ_METER is not None and (operation in ("os.readv","os.preadv")
                    or operation in ("stream.read","stream.read1","stream.readinto",
                        "stream.readinto1","stream.readline","stream.readlines")):
                slot["raw_result"]=_SOURCE_READ_METER.adapter(operation,function,args,kwargs,
                    slot.get("channel",{}).get("fd"))
            else:slot["raw_result"]=function(*args,**kwargs)
            if 'allocation' in slot:
                allocation['raw_return']=slot['raw_result']
                allocation['state']='RETURNED_OWNED'
                returned=slot['raw_result'] if operation in ('os.pipe','os.pipe2') else (slot['raw_result'],)
                for cell,fd in zip(allocation['cells'],returned):
                    cell['fd']=fd;cell['owner']=slot
                    self.fd_generations[fd]=cell
            if operation=="raw.write":
                # Raw FileIO.write is the actual buffer/encoder output edge.
                # Its returned prefix, not text acceptance, defines pipe bytes.
                slot["physical_bytes"]=slot['physical_payload_before'][:slot["raw_result"]] if slot["raw_result"] is not None else b""
                slot["physical_serial"]=slot["channel"]["physical_sequence"]
                self.physical_pending=slot;self.physical_writes.append(slot)
            if operation=="os.close" and generation is not None:generation["close_returned"]=True
        except BaseException as error:
            slot["raw_error"]=error
            slot['raw_traceback']=error.__traceback__
            if 'allocation' in slot:
                allocation['raw_error']=error
                if allocation['state']=='ACQUIRE_STARTED':allocation['state']='ACQUIRE_UNKNOWN'
            if self.first_error is None:self.first_error=error
            self.record(slot)
            if cleanup and slot["active_error"] is not None:return None
            raise
        self.record(slot)
        return slot["raw_result"]
    def record(self,slot):
        self.local.recorder=True
        try:
            args,kwargs=slot["arguments"]
            wire_args=args[1:] if slot["operation"].startswith(("Popen.","Selector.","Thread.","Pool.")) else args
            arguments=[wire_args,kwargs]
            if "channel" in slot:arguments.append(slot["channel"])
            raw_client().note(self.role+".internal."+slot["operation"],arguments,
                slot["raw_result"],slot["raw_error"])
        except BaseException as recorder:slot["recording_error"]=recorder
        finally:self.local.recorder=False

def install_source_graph(role):
    global _SOURCE_GRAPH
    if _SOURCE_GRAPH is not None:return _SOURCE_GRAPH
    import builtins,io,posix,subprocess,selectors,threading,concurrent.futures,_pyio,_io
    graph=SourceGraphOwner(role);_SOURCE_GRAPH=graph
    for name in ("open","close","read","pread","readv","preadv","write","pwrite","writev","pwritev",
        "fstat","lstat","stat","readlink","listdir","mkdir","unlink","remove","rmdir",
        "rename","replace","fchmod","chmod","ftruncate","truncate","fsync","fdatasync","pipe","pipe2",
        "dup","dup2","waitpid","wait4","pidfd_open","memfd_create","set_blocking"):
        original=getattr(_SOURCE_RAW_OS,name,None)
        if original is None:continue
        def observed(*args,_name=name,_raw=original,**kwargs):
            return graph.invoke("os."+_name,_raw,args,kwargs)
        setattr(os,name,observed)
        if hasattr(posix,name):setattr(posix,name,observed)
    class ObservedFileIO(io.FileIO):
        def __init__(self,*args,**kwargs):
            slot={"operation":"raw.FileIO.partial","raw_result":self,"arguments":[args,kwargs],
                "raw_error":None,"recording_error":None}
            graph.pending=slot;graph.owners.append(slot);graph.raw_streams.append(self)
            graph.fd_history.append(slot)
            self._source_close_attempted=False;self._source_close_error=None
            self._source_channel_generation=[os.getpid(),graph.channel_serial]
            graph.channel_serial+=1
            try:graph.invoke("raw.FileIO.__init__",super().__init__,args,kwargs)
            except BaseException as error:slot["raw_error"]=error;raise
        def read(self,size=-1):
            if size is None or size<0:return self.readall()
            return graph.invoke("raw.read",super().read,(size,),{})
        def readinto(self,buffer):return graph.invoke("raw.readinto",super().readinto,(buffer,),{})
        def readall(self):
            slot={"operation":"raw.readall","chunks":[],"raw_result":None,"raw_error":None,"pending_chunk":None}
            graph.pending=slot;graph.owners.append(slot)
            try:
                while True:
                    # The unchanged actor's remaining budget bounds each actual
                    # physical request; the loop ends only on actual EOF/None.
                    cap=io.DEFAULT_BUFFER_SIZE
                    if _SOURCE_READ_METER is not None:
                        with _SOURCE_READ_METER.lock:
                            cap=min(cap,max(1,_SOURCE_READ_METER.maximum-
                                _SOURCE_READ_METER.upper_used-_SOURCE_READ_METER.inflight))
                    raw=self.read(cap);slot["pending_chunk"]=raw
                    if raw is None:
                        slot["raw_result"]=None if not slot["chunks"] else b"".join(slot["chunks"])
                        return slot["raw_result"]
                    if not raw:
                        slot["raw_result"]=b"".join(slot["chunks"])
                        return slot["raw_result"]
                    slot["chunks"].append(raw);slot["pending_chunk"]=None
            except BaseException as error:slot["raw_error"]=error;raise
        def write(self,buffer):
            info=_SOURCE_RAW_OS.fstat(self.fileno())
            if stat.S_ISFIFO(info.st_mode):
                buffer=memoryview(buffer).cast("B")[:_SOURCE_RAW_OS.fpathconf(self.fileno(),"PC_PIPE_BUF")]
            # A short raw return is part of RawIOBase's original contract; the
            # real BufferedWriter retains/retries its unaccepted suffix itself.
            return graph.invoke("raw.write",super().write,(buffer,),{})
        def close(self):
            if self._source_close_attempted:
                if self._source_close_error is not None:raise RuntimeError("ambiguous raw stream close: no retry")
                return
            self._source_close_attempted=True
            try:return graph.invoke("raw.close",super().close,(),{})
            except BaseException as error:self._source_close_error=error;raise
    class ObservedRaw(io.RawIOBase):
        """Actual existing raw descriptor below transferred stdio buffers."""
        def __init__(self,raw):
            self.raw=raw;self._source_channel_generation=[os.getpid(),graph.channel_serial]
            raw._source_channel_generation=self._source_channel_generation
            graph.channel_serial+=1;self.close_attempted=False;self.close_error=None
            graph.raw_streams.append(self)
        def fileno(self):return self.raw.fileno()
        @property
        def name(self):return self.raw.name
        def readable(self):return self.raw.readable()
        def writable(self):return self.raw.writable()
        def seekable(self):return self.raw.seekable()
        def isatty(self):return graph.invoke("raw.isatty",self.raw.isatty,(),{})
        def readinto(self,buffer):return graph.invoke("raw.readinto",self.raw.readinto,(buffer,),{})
        def read(self,size=-1):
            if size is None or size<0:
                # RawIOBase.readall calls this same observed raw.read repeatedly.
                return io.RawIOBase.readall(self)
            return graph.invoke("raw.read",self.raw.read,(size,),{})
        def write(self,buffer):
            info=_SOURCE_RAW_OS.fstat(self.fileno())
            if stat.S_ISFIFO(info.st_mode):
                buffer=memoryview(buffer).cast("B")[:_SOURCE_RAW_OS.fpathconf(self.fileno(),"PC_PIPE_BUF")]
            return graph.invoke("raw.write",self.raw.write,(buffer,),{})
        def seek(self,*args):return graph.invoke("raw.seek",self.raw.seek,args,{})
        def tell(self):return graph.invoke("raw.tell",self.raw.tell,(),{})
        def flush(self):return graph.invoke("raw.flush",self.raw.flush,(),{})
        def close(self):
            if self.close_attempted:
                if self.close_error is not None:raise RuntimeError("ambiguous transferred raw close: no retry")
                return
            self.close_attempted=True
            try:
                # RawIOBase.closed is set without a second physical descriptor close.
                io.RawIOBase.close(self)
                graph.invoke("raw.close",self.raw.close,(),{})
            except BaseException as error:self.close_error=error;raise
    # Stock Python open validates modes/arguments and builds the ACTUAL C
    # Buffered/Text layers on the observed raw object. No result re-encoding.
    # Its error-order compatibility with stock C open requires independent
    # qualification; startup/imports and pre-existing streams remain explicit.
    _pyio.FileIO=ObservedFileIO
    for name in ("BufferedReader","BufferedWriter","BufferedRandom","TextIOWrapper"):
        setattr(_pyio,name,getattr(io,name))
    _pyio.text_encoding=io.text_encoding
    for module,name in ((builtins,"open"),(io,"open"),(os,"fdopen")):
        original=_pyio.open
        def opened(*args,_raw=original,_name=name,**kwargs):
            raw=graph.invoke("stream."+_name,_raw,args,kwargs)
            graph.pending={"operation":"stream.partial_owner","raw_result":raw,"raw_error":None}
            graph.owners.append(graph.pending);graph.raw_streams.append(raw)
            owned=OwnedStream(raw,graph)
            graph.fd_generations[owned.owned_fd]={"owner":graph.pending,"fd":owned.owned_fd}
            graph.pending={"operation":"stream.owner","raw_result":owned,"raw_error":None}
            graph.owners.append(graph.pending)
            return owned
        setattr(module,name,opened)
    def source_open_code(path):
        # importlib's real SourceFileLoader.get_data uses _io.open_code, not
        # builtins.open. Newly reached loader physical reads now use the same
        # observed raw path. Pre-attachment interpreter/import IO stays OPEN.
        return io.open(path,"rb")
    _io.open_code=source_open_code
    original_popen=subprocess.Popen
    class ObservedPopen(original_popen):
        def __init__(self,*args,**kwargs):
            slot={"process":self,"arguments":[args,kwargs],"error":None}
            graph.pending=slot;graph.popen.append(slot)
            try:graph.invoke("Popen.__init__",original_popen.__init__,(self,*args),kwargs)
            except BaseException as error:slot["error"]=error;raise
        def _stream_method(self,name,*args,**kwargs):
            return graph.invoke("Popen."+name,getattr(original_popen,name),(self,*args),kwargs)
        def communicate(self,*args,**kwargs):return self._stream_method("communicate",*args,**kwargs)
        def wait(self,*args,**kwargs):return self._stream_method("wait",*args,**kwargs)
        def poll(self,*args,**kwargs):return self._stream_method("poll",*args,**kwargs)
        def send_signal(self,*args,**kwargs):return self._stream_method("send_signal",*args,**kwargs)
        def kill(self,*args,**kwargs):return self._stream_method("kill",*args,**kwargs)
        def terminate(self,*args,**kwargs):return self._stream_method("terminate",*args,**kwargs)
    subprocess.Popen=ObservedPopen
    selector_aliases={}
    for name in ("SelectSelector","PollSelector","EpollSelector","DevpollSelector","KqueueSelector"):
        original=getattr(selectors,name,None)
        if original is None:continue
        def observed_selector_class(base):
            class ObservedSelector(base):
                def __init__(self,*args,**kwargs):
                    slot={"selector":self,"arguments":[args,kwargs],"error":None,"close_attempted":False}
                    self._source_owner_slot=slot;graph.pending=slot;graph.owners.append(slot)
                    try:graph.invoke("Selector.__init__",base.__init__,(self,*args),kwargs)
                    except BaseException as error:slot["error"]=error;raise
                def _source_method(self,name,*args,**kwargs):
                    return graph.invoke("Selector."+name,getattr(base,name),(self,*args),kwargs)
                def select(self,*args,**kwargs):return self._source_method("select",*args,**kwargs)
                def register(self,*args,**kwargs):return self._source_method("register",*args,**kwargs)
                def unregister(self,*args,**kwargs):return self._source_method("unregister",*args,**kwargs)
                def modify(self,*args,**kwargs):return self._source_method("modify",*args,**kwargs)
                def close(self):
                    slot=self._source_owner_slot
                    if slot["close_attempted"]:
                        if slot.get("close_error") is not None:raise RuntimeError("ambiguous selector close: no retry")
                        return
                    slot["close_attempted"]=True
                    try:
                        result=self._source_method("close")
                        if graph.pending.get("raw_error") is not None:slot["close_error"]=graph.pending["raw_error"]
                        return result
                    except BaseException as error:slot["close_error"]=error;raise
            return ObservedSelector
        wrapped=observed_selector_class(original);selector_aliases[original]=wrapped
        setattr(selectors,name,wrapped)
    if selectors.DefaultSelector in selector_aliases:selectors.DefaultSelector=selector_aliases[selectors.DefaultSelector]
    if getattr(subprocess,"_PopenSelector",None) in selector_aliases:
        subprocess._PopenSelector=selector_aliases[subprocess._PopenSelector]
    for name in ("start","join"):
        original=getattr(threading.Thread,name)
        def thread_method(self,*args,_name=name,_raw=original,**kwargs):
            return graph.invoke("Thread."+_name,_raw,(self,*args),kwargs)
        setattr(threading.Thread,name,thread_method)
    original_pool=concurrent.futures.ThreadPoolExecutor
    class ObservedPool(original_pool):
        def __init__(self,*args,**kwargs):
            slot={"pool":self,"arguments":[args,kwargs],"error":None}
            graph.pending=slot;graph.owners.append(slot)
            try:graph.invoke("Pool.__init__",original_pool.__init__,(self,*args),kwargs)
            except BaseException as error:slot["error"]=error;raise
        def submit(self,*args,**kwargs):return graph.invoke("Pool.submit",original_pool.submit,(self,*args),kwargs)
        def shutdown(self,*args,**kwargs):return graph.invoke("Pool.shutdown",original_pool.shutdown,(self,*args),kwargs)
    concurrent.futures.ThreadPoolExecutor=ObservedPool
    for name in ("stdin","stdout","stderr"):
        stream=getattr(sys,name,None)
        if stream is not None and not isinstance(stream,OwnedStream):
            slot={"operation":"startup.stream.transfer","name":name,"text":stream,
                "buffer":getattr(stream,"buffer",None),"raw":None,"replacement":None,"error":None}
            graph.pending=slot;graph.owners.append(slot)
            graph.physical_gaps.append(slot)
            # Preserve the exact original text/buffer/decoder/encoder/newline
            # and pending flush state. A replacement raw layer cannot recreate
            # that state. Calls are observed semantically; native startup and
            # existing C raw physical IO are explicitly NOT_QUALIFIED.
            slot['replacement']=OwnedStream(stream,graph)
            setattr(sys,name,slot['replacement'])
    return graph

def observe_root_call(role,function,*args,**kwargs):
    return raw_client().call(role,function,*args,**kwargs)

def observer_terminal(code):
    client=raw_client()
    errors=[]
    for owner in _SOURCE_INVENTORY_GENERATIONS:
        if not owner.closed:
            try:owner.close()
            except BaseException as error:errors.append(error)
    for name in ("stdout","stderr"):
        stream=getattr(sys,name,None)
        if stream is not None:
            try:stream.flush()
            except BaseException as error:errors.append(error)
    if errors:
        client.note("actor.cleanup.errors",[],error=errors[0])
        client.check()
        raise errors[0]
    client.terminal(code)
    client.check()

_A132_ORIGINAL_REQUIRE = require
def require(condition, reason):
    if _CLIENT is not None:
        _CLIENT.note("receipt.guard",[condition,reason],condition)
    return _A132_ORIGINAL_REQUIRE(condition,reason)
