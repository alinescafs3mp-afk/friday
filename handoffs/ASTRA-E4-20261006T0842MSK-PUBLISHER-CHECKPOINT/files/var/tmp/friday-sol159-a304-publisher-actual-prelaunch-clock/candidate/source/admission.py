"""Externally installed enrollment and independently signed Root admission.

The package has no signing key, install action, issuer factory or GO switch.
Enrollment is installed by the existing Root-tool owner after independent
review. Actor metadata, UID, a caller pipe or a digest label is not enrollment.
"""
import os
import stat
from common import (Refused, INPUT_MAX, READ_MAX, OUTPUT_MAX, RAM_MAX,
                    SLOTS_MAX, WORKERS_MAX, MEMBERS_MAX, exact, integer, text, digest,
                    parse, canonical, sha, domain, mono, bounded_tree_paths)
from custody import open_absolute, identity9, Held
from lifetime import OwnedFDs
from observer import proc_start, raw_file

ENROLLMENT = "/etc/friday/root-publisher/enrollment.json"
ENROLLMENT_FIELDS = ("schema","installed_by_uid","independent_review_sha256",
    "root_tool","admission","admission_signature","admission_key",
    "signature_tool","signature_tool_dependencies","bootstrap_helper_profile","cgroup","output_root",
    "source_manifest","source_files","consumer_manifest","consumer_files",
    "schema_pins","control_tuples","role_caps","root_namespace","selector_namespace")
ADMISSION_FIELDS = ("schema","root_namespace","nonce","issued_ns","expires_ns",
    "root_tool_pid","root_tool_start_ticks","root_tool_sha256","source_manifest_sha256",
    "consumer_manifest_sha256","image","tools","inputs","members","ordinary",
    "coverage","role_caps","effects","cgroup","output_root","independent_review_sha256",
    "launch_mode","root_retention_pin","independent_selector","capacity_plan")
ROLE_CAPS = {"read_bytes":READ_MAX,"output_bytes":OUTPUT_MAX,"ram_bytes":RAM_MAX,
    "slots":SLOTS_MAX,"workers":WORKERS_MAX,"wall_seconds":4200,
    "seal_reserve_seconds":600,"network":0,"retries":0,
    "artifacts":350,"members":512,"input_bytes":2_000_000,"document_bytes":80_000_000}
ALL15 = ("authenticate-ubuntu-indexes","authenticate-ubuntu-archives",
    "authenticate-node-archive","hold-unrar-publisher-gap","authenticate-wheels",
    "map-cpython-venv","map-lib-dynload","map-native-loader","map-browser-resources",
    "map-data-closure","bind-candidate","bind-golden","assemble-members",
    "write-final-manifest","external-custody")


def protected_enrollment(hash_owner):
    """Root-owned pathname custody, complete bytes, stable9; no candidate path."""
    if hash_owner is None:
        raise Refused("preobserver_hash_owner")
    parts = ENROLLMENT[1:].split("/")
    p = ""
    for part in parts[:-1]:
        p += "/"+part
        s = os.lstat(p)
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or stat.S_IMODE(s.st_mode)&0o022:
            raise Refused("root_enrollment_custody")
    before = os.lstat(ENROLLMENT)
    if not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600:
        raise Refused("root_enrollment_custody")
    book = OwnedFDs(credit=hash_owner)
    hash_owner.retain_journal(book)
    fd = -1
    try:
        fd = open_absolute(ENROLLMENT, journal=book)
        if identity9(before) != identity9(os.fstat(fd)) or not 0 < before.st_size <= INPUT_MAX:
            raise Refused("root_enrollment_size")
        hash_owner.before_read(before.st_size+1)
        raw = os.read(fd,before.st_size+1)
        hash_owner.read_debit(len(raw))
        if len(raw) != before.st_size or identity9(os.fstat(fd)) != identity9(before) or identity9(os.lstat(ENROLLMENT)) != identity9(before):
            raise Refused("root_enrollment_drift")
        e = exact(parse(raw),ENROLLMENT_FIELDS,"root_enrollment_schema")
        if e["schema"] != "friday.a138.externally-installed-root-enrollment.v1" or e["installed_by_uid"] != 0:
            raise Refused("root_enrollment")
        if e["role_caps"] != ROLE_CAPS: raise Refused("role_caps")
        # The bootstrap/selector signature helper is also an owned native
        # child. Its entire runtime dependency inventory is independently
        # enrolled, then held BEFORE fork and retained on STOP_UNCONFIRMED.
        dependencies=e["signature_tool_dependencies"]
        if type(dependencies) is not list or not 1<=len(dependencies)<=128:
            raise Refused("signature_tool_dependency_inventory")
        if len({r["path"] for r in dependencies})!=len(dependencies):
            raise Refused("signature_tool_dependency_inventory")
        for name in ("root_namespace","selector_namespace"): text(e[name],32)
        if e["root_namespace"] == e["selector_namespace"]: raise Refused("root_selector_independence")
        digest(e["independent_review_sha256"])
        return e, {"path":ENROLLMENT,"bytes":len(raw),"sha256":sha(raw, admitted=hash_owner),
                   "identity9_decimal_strings":identity9(before)}
    finally:
        if fd >= 0:
            book.close_one(fd)
        if book.fds:
            import sys
            current = sys.exc_info()[1]
            fact = {"fds": [{"fd": held, "holder": book.meta.get(held, {}).get("holder"),
                "credit": book.meta.get(held, {}).get("credit"),
                "identity9_decimal_strings": book.meta.get(held, {}).get("identity9_decimal_strings"),
                "status": book.meta.get(held, {}).get("status")} for held in sorted(book.fds)]}
            if current is None:
                exc = Refused("temporary_fd_close_unconfirmed", detail=fact)
                exc.retained_journal = book
                raise exc
            current.retained_journal = book
        else:
            hash_owner.journals.remove(book)


def actual_root_tool(enrollment, meter):
    """Actual executing Root tool, not supplied PID/UID/executable fields."""
    pin = enrollment["root_tool"]
    pid = os.getpid()
    proc = proc_start(pid,meter)
    executable = "/proc/self/exe"
    fd_hold=meter.reserve("actual-root-tool-exe-FD-before-open",allocation=65536,slots=1)
    book = OwnedFDs(credit=fd_hold,meter=meter)
    meter.retain_local_owner(book)
    fd=-1
    try:
        fd = book.acquire(os.open,executable,os.O_RDONLY|os.O_CLOEXEC,holder="root-tool-exe")
        s = os.fstat(fd)
        if identity9(s) != pin["identity9_decimal_strings"]:
            raise Refused("existing_root_tool_identity")
        hold = meter.reserve("actual-root-tool-hash",reads=s.st_size,hash_bytes=s.st_size,allocation=65536)
        import hashlib
        h = hashlib.sha256(); at = 0
        try:
            while at < s.st_size:
                raw = os.pread(fd,min(65536,s.st_size-at),at)
                if not raw: raise Refused("existing_root_tool_short")
                hold.commit(reads=len(raw));hold.commit(hash_bytes=len(raw));h.update(raw);at += len(raw)
            if h.hexdigest() != pin["sha256"] or identity9(os.fstat(fd)) != identity9(s):
                raise Refused("existing_root_tool_sha")
        finally: hold.release()
        # Installed enrollment also selects the exact pre-existing native entry,
        # argv and current tool process start; replays in a different process fail.
        with Held(pin["path"],pin,meter,maximum=80_000_000) as named:
            if identity9(os.fstat(fd)) != named.before: raise Refused("existing_root_tool_named_join")
        return {"pid":pid,"start_ticks":proc["start_ticks"],"exe_sha256":pin["sha256"],
                "exe_identity9":identity9(s),"raw_stat":proc["raw_stat"],
                "actual_uid":os.getuid(),"actual_gid":os.getgid()}
    finally:
        book.close()
        if not book.fds:
            fd_hold.release();meter.retire_local_owner(book)
        if book.fds:
            import sys
            current=sys.exc_info()[1]
            meta=book.meta.get(next(iter(book.fds)), {})
            fact={"fd":fd,"credit":meta.get("credit"),"holder":meta.get("holder"),
                "identity9_decimal_strings":meta.get("identity9_decimal_strings"),"status":meta.get("status")}
            if current is None:
                exc=Refused("temporary_fd_close_unconfirmed", detail=fact)
                exc.retained_journal=book
                raise exc
            current.retained_journal=book


def qualify(enrollment, root_fact, observer, native):
    """The sole pre-admission native verb is externally enrolled signature check."""
    with Held(enrollment["admission"]["path"],enrollment["admission"],observer,INPUT_MAX,True) as a, \
         Held(enrollment["admission_signature"]["path"],enrollment["admission_signature"],observer,INPUT_MAX,True) as sig, \
         Held(enrollment["admission_key"]["path"],enrollment["admission_key"],observer,INPUT_MAX,True) as key:
        raw = a.read(INPUT_MAX)
        value = exact(parse(raw,observer),ADMISSION_FIELDS,"admission_schema")
        verified = native.verify_admission(a,sig,key,enrollment)
        if type(verified) is not dict: raise Refused("admission_bootstrap_record")
        if verified.get("lifetime_phase") != "PUBLIC_STOCK_PROCESS_EXITED":
            raise Refused("admission_public_stock_phase")
        if verified.get("public_stock_parent_legal_retirement_accounted") not in (True,1):
            raise Refused("admission_parent_legal_retirement")
        if verified.get("public_stock_observables_consumed") not in (True,1):
            raise Refused("admission_public_stock_observables")
        if verified.get("last_child_transport_end_confirmed") not in (0,False):
            raise Refused("admission_false_postexec_transport_end")
        # Source/Root/image qualification is an external prerequisite for
        # running this code, not author NOT_RUN metadata in a runtime result.
        # Check the actual bound parent observations returned by this caller.
        for name in ("stock_exec_prefix_retained","stock_image_replacement_confirmed","kernel_process_lifetime_ended","atomic_spawn_pidfd_bound","existing_parent_bank_full_read","parent_FD_ends","child_IO_known","final_IO_attempted_once"):
            if type(verified.get(name)) is not int or verified[name] != 1:
                raise Refused("admission_parent_observation_" + name)
        if verified["exit_code"] != 0: raise Refused("admission_signature")
        if value["schema"] != "friday.a138.independent-root-admission.v1":
            raise Refused("admission_schema")
        if value["root_tool_pid"] != root_fact["pid"] or value["root_tool_start_ticks"] != root_fact["start_ticks"] or value["root_tool_sha256"] != root_fact["exe_sha256"]:
            raise Refused("admission_actual_root_tool")
        if value["root_namespace"] != enrollment["root_namespace"] or value["role_caps"] != ROLE_CAPS:
            raise Refused("admission_role")
        integer(value["issued_ns"],10**21);integer(value["expires_ns"],10**21)
        if not value["issued_ns"] <= mono() <= value["expires_ns"] or value["expires_ns"]-value["issued_ns"] > 4200*10**9:
            raise Refused("admission_lifetime")
        if value["source_manifest_sha256"] != enrollment["source_manifest"]["sha256"] or value["consumer_manifest_sha256"] != enrollment["consumer_manifest"]["sha256"] or value["independent_review_sha256"] != enrollment["independent_review_sha256"]:
            raise Refused("admission_review_snapshot")
        if value["cgroup"] != enrollment["cgroup"] or value["output_root"] != enrollment["output_root"]:
            raise Refused("admission_owned_resources")
        if value["effects"] != list(ALL15): raise Refused("admission_all15")
        if value["launch_mode"] not in ("perform-and-retain","retained-consumer"):raise Refused("launch_mode")
        if value["launch_mode"]=="retained-consumer" and value["root_retention_pin"] is None:raise Refused("independent_Root_retention_required")
        from roles import validate_admission
        validate_admission(value,enrollment,observer)
        text(value["nonce"],128)
        # No source-created key or signature enters this path.
        return value, {"raw_ref":a.pin_now(),"signature_ref":sig.pin_now(),
            "key_ref":key.pin_now(),"actual_signature_execution":verified,
            "qualified_at_ns":mono(),"root_fact":root_fact,
            "source_issued_grant":False}


def full_snapshot(enrollment, meter):
    """Every byte/path/schema/control hash is checked before Source loading."""
    retained = []
    for manifest_key,files_key in (("source_manifest","source_files"),("consumer_manifest","consumer_files")):
        with Held(enrollment[manifest_key]["path"],enrollment[manifest_key],meter,INPUT_MAX,True) as m:
            manifest = parse(m.read(INPUT_MAX),meter)
            base = os.path.dirname(m.path)
            declared = enrollment[files_key]
            if type(declared) is not list or len(declared) > 350: raise Refused("source_pathset")
            paths = set()
            for row in declared:
                rel = row["relative_path"]
                if rel in paths or row["path"] != base+"/"+rel: raise Refused("source_pathset")
                paths.add(rel)
                with Held(row["path"],row,meter,80_000_000,True) as held:
                    retained.append(held.pin_now())
            actual = set()
            for path in bounded_tree_paths(base, MEMBERS_MAX, directory_mode=0o700, files_only=True, refuse_symlinks=True):
                actual.add(os.path.relpath(path, base))
            if actual != paths|{os.path.basename(m.path)}: raise Refused("source_exact_pathset")
            if {row["path"] for row in manifest["members"]} != paths:
                raise Refused("source_manifest_pathset")
            by_rel = {row["relative_path"]:row for row in declared}
            for member in manifest["members"]:
                pin = by_rel[member["path"]]
                if member["sha256"] != pin["sha256"] or member["bytes"] != pin["bytes"]:
                    raise Refused("source_manifest_join")
    consumer = {row["relative_path"]:row for row in enrollment["consumer_files"]}
    if len(enrollment["schema_pins"]) != 20: raise Refused("all20_schemas")
    for name,expected in enrollment["schema_pins"].items():
        pin = consumer.get("schemas/"+name+".json")
        if pin is None or pin["sha256"] != expected: raise Refused("all20_schema_sha")
    # The source package cannot replace the independent complete catalog.
    catalog = consumer["controls/catalog.json"]
    with Held(catalog["path"],catalog,meter,INPUT_MAX,True) as held:
        controls = parse(held.read(INPUT_MAX),meter)["controls"]
    tuples = [{k:row[k] for k in ("id","scenario","status","cause","stage","match")} for row in controls]
    if len(tuples) != 69 or tuples != enrollment["control_tuples"]:
        raise Refused("all69_exact_tuples")
    return retained
