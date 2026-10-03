"""A048 SOURCE ONLY: finite supervisor for one reviewed Node verifier.
The supervisor's own pre-limited bootstrap Python/prlimit and this exact source
are independently trusted and pinned by the host, before bootstrap Python.
That trust is not created by this program or a hash receipt. No invocation now.
"""
import base64
import datetime
import fcntl
import hashlib
import json
import math
import os
import re
import resource
import select
import signal
import stat
import sys
import time

ASSIGNMENT = "ASTRA-E4-NODE-LAUNCH-OWNERSHIP-EVIDENCE-CLOSURE-A048"
PREFIX = "/var/tmp/friday-astra-a224-node-prospective-error-graph-producer-both-receiver-inert-source"
BASE = "/var/tmp/friday-astra-material-acquisition-20261001-a023-g1"
TARGET = "node-v22.23.2-linux-x64.tar.xz"
EXPECTED = "CC68F5A3106FF448322E48ED27F5E38D5B0A215F"
ARCHIVE_SHA = "d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307"
SOURCE_SHA = "a627290ae91076649ac22ddd066ca01fcfa6b66e661324dd18227ff3af91cea0"
CONTRACT_SHA = "78696677e5dc89062d12148402a6053dcb2b7ef94ee941c2c2ed5c5f7ee96bc5"
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
SUP_AS, PARENT_AS, GPG_AS = 33554432, 117440512, 67108864
STREAM, OUTPUT = 32768, 65536
START = time.monotonic_ns()
DEADLINE = START  # malformed invocation cannot gain an execution window
INNER_DEADLINE = START
DIRS, HELD = {}, {}
CHILD = None
FORK_STATE = "NOT_ATTEMPTED"
TRACKED = []
TRANSPORT = []
REGISTRATION_SIGNALS = set(signal.valid_signals()) - {signal.SIGKILL, signal.SIGSTOP}
LAUNCH_RESERVE = {"null": None, "copies": [None, None, None, None],
                  "gate": [None, None], "probe": None, "gate_identity": None}
EMPTY_PRE_FORK = False
CLEANUP_END = None
CONTRACT_HASH_BINDING = {"fixed_binding": "sealed_contract_full_sha256",
                         "provider": "supervisor.CONTRACT_SHA", "caller_override": False}
DEADLINE_BINDING = {"parameter": "supervisor_inner_deadline_monotonic_ns"}
HOME_BINDING = {"parameter": "supervisor_created_private_home_name"}
HOME = {"state": "NOT_PLANNED", "name": None, "parent_identity": None,
        "prelaunch_absent": None, "postexit_absent": None}
STOP = False
R = {"schema": "friday.e4.node.actual-supervisor.sol062.v1", "assignment": ASSIGNMENT,
     "generation": 1, "status": "NOT_PROVEN", "scope": "exact Node publisher byte chain only",
     "qualified_trust": "Independently admitted installed bootstrap/verifier Python, stdlib/libc, prlimit, GPG/gpgv, loaders/libraries, Linux/proc/pidfd/subreaper and unprivileged exclusive host assumptions; source and receipts do not establish that trust.",
     "custody": {}, "resources": {},
     "resource_schema": "friday.e4.node.supervisor-resources.a172.v1",
     "owned_processes": {}, "home": HOME,
     "output": {}, "process_history": [], "fork_registration": {}, "bootstrap_origin": "Separate host admission, never inner JSON success"}

# Entire authoritative tool/input map is fixed here, not accepted from argv/env.
PINS = {
    "prlimit": ("/usr/bin/prlimit", 27616, "cb28811cb3902773c1a0f3ac0ea554c7a1e232724e9d4cae055ede54b65a7bc4", 65536, True),
    "python": ("/usr/bin/python3.14", 7477160, "52e0a13e60a981d8c4b6478be2ba5176f69da07948a056bf49cf6f077e30cb41", 8388608, True),
    "source": (PREFIX + "/verifier.py", 132822, SOURCE_SHA, 262144, False),
    "contract": (PREFIX + "/launch-contract.json", 51509, CONTRACT_SHA, 262144, False),
    "gpg": ("/usr/bin/gpg", 1186456, "c8f3dc07a34ab0a0a081368b482724b79a2a6d6b7aa856741c09edf7af854754", 2097152, True),
    "gpgv": ("/usr/bin/gpgv", 302296, "95ecd00d02b79d091f103af175b1dbf95b5b0c66503104bfcea447cca7e3829d", 1048576, True),
    "bill": ("/home/jericho/.jericho/runtime/subagent-lifecycle/ASTRA-E4-ACQUISITION-INPUT-DRIFT-SUCCESSOR-A032-G1-BILL.json", 466956, "cd973b83717015b985055482c9835e7ac1cf8311120c71461d4c0f17ad2d50a2", 1048576, False),
    "receipts": (BASE + "/transport-receipts.ndjson", 400979, "180c44ad4439a96f89335a62e35a5668327a228ce164d1f31b503b867b05fe9b", 4194304, False),
    "raw": (BASE + "/metadata/node/SHASUMS256.txt.asc", 4696, "cfcf12eb3146d641be185d08ca018166b98495b1d8e5f7f94ab77f9965b651d2", 262144, False),
    "key": (BASE + "/metadata/node/publisher-key.asc", 3163, "e31e1aa40a8331f01d753cef475f7b9eab934fc25f5f0b36995bfd80bd66ad27", 262144, False),
    "archive": (BASE + "/archives/node/" + TARGET, 31058332, ARCHIVE_SHA, 33554432, False),
    "original_archive": ("/home/jericho/.jericho/runtime/friday-toolchain22.bSt8Uu/archives/" + TARGET, 31058332, ARCHIVE_SHA, 33554432, False),
}


class Refusal(Exception):
    pass


class MissingSample(Refusal):
    pass


def require(ok, why):
    if not ok:
        raise Refusal(why)


def unknown(why):
    global STOP
    STOP = True
    raise Refusal("STOP_UNCONFIRMED: " + why)



FD_SIMULTANEOUS_CAP = 256
FD_HISTORY_CAP = FD_SIMULTANEOUS_CAP
FD_HISTORY_USED = 0
FD_HISTORY = [None] * FD_HISTORY_CAP
FD_LIFETIME_ACQUIRED = 0
FD_CONFIRMED_RETIREMENTS = 0
FD_UNREAD_HOLDS = 0
FD_AUDIT = hashlib.sha256(b"friday.lab868.fd-lifetime-audit.v1\n")

def note_fd_audit(kind, ordinal, number, role):
    FD_AUDIT.update(("%s %d %s %s\n" % (
        kind, ordinal, "-" if number is None else str(number), role or "-")).encode("ascii", "replace"))

def reclaim_confirmed_slot(record):
    global FD_HISTORY_USED, FD_CONFIRMED_RETIREMENTS
    if record is None or record.get("unread_prefix_hold") or record.get("state") != "CONFIRMED_CLOSED":
        return
    for index in range(FD_HISTORY_USED):
        if FD_HISTORY[index] is record:
            last = FD_HISTORY_USED - 1
            FD_HISTORY[index] = FD_HISTORY[last]
            FD_HISTORY[last] = None
            FD_HISTORY_USED = last
            FD_CONFIRMED_RETIREMENTS += 1
            note_fd_audit("R", FD_CONFIRMED_RETIREMENTS, record.get("number"), record.get("where"))
            return

def hold_unread_prefix(record):
    global FD_UNREAD_HOLDS
    if record is None or record.get("unread_prefix_hold"):
        return
    record["unread_prefix_hold"] = True
    FD_UNREAD_HOLDS += 1

def fd_lifetime_audit():
    return {"simultaneous_cap": FD_SIMULTANEOUS_CAP,
            "lifetime_acquisitions": FD_LIFETIME_ACQUIRED,
            "confirmed_retirements": FD_CONFIRMED_RETIREMENTS,
            "live_unretired": FD_HISTORY_USED,
            "audit_sha256": FD_AUDIT.hexdigest(),
            "nofile_not_raised": True,
            "reclaim_only_after_confirmed_close": True,
            "unread_prefix_holds": FD_UNREAD_HOLDS}

FD_GENERATION = 0
FD_SIGNALS = set(signal.valid_signals()) - {signal.SIGKILL, signal.SIGSTOP}
# SOL062: private prospective cells are not public receipts or summaries.
# Fixed existing256 slots. Width/stock exhaustion/whole fit still need proof.
ERROR_OBJECT_CAP = 256
ERROR_OBJECTS = [None] * ERROR_OBJECT_CAP
ERROR_TRACEBACKS = [None] * ERROR_OBJECT_CAP
ERROR_PROJECTED = [None] * ERROR_OBJECT_CAP
ERROR_MARKERS = [{"$private_pending_error": i} for i in range(ERROR_OBJECT_CAP)]
ERROR_USED = 0


def retain_error_object(exc):
    global ERROR_USED
    for i in range(ERROR_USED):
        if ERROR_OBJECTS[i] is exc:
            return i
    if ERROR_USED >= ERROR_OBJECT_CAP:
        raise Refusal("original fixed error-owner capacity exhausted before publication") from exc
    i = ERROR_USED
    # Raw first fault and its caught traceback precede every projection.
    ERROR_OBJECTS[i] = exc
    ERROR_TRACEBACKS[i] = exc.__traceback__
    ERROR_USED = i + 1
    return i


EMERGENCY_CAP = 108
EMERGENCY_OBJECTS = [None] * EMERGENCY_CAP
EMERGENCY_TRACEBACKS = [None] * EMERGENCY_CAP
EMERGENCY_PROJECTED = [None] * EMERGENCY_CAP
EMERGENCY_MARKERS = [{"$private_pending_error": "EMERGENCY", "slot": i} for i in range(EMERGENCY_CAP)]
EMERGENCY_USED = 0
EMERGENCY_BEYOND = 0


PARENT_PLACED = 0
PARENT_IMAGE = b""
# SOL072: These are actual local owners, NOT outside physical-body acceptance.
# A retained encoded stream / raw exception pointer is not the full native body.
# Every phase below is reached at most once; no overwritten "last error" slot.
PARENT_IO = {"state": "PREFIX", "pending": None, "base": 0, "sent": 0,
             "read_result": None, "read_bytes": 0, "write_bytes": 0,
             "final_read_reserved": 0, "final_read_remaining": 0}
PUBLICATION_FAILURES = {"prefix": [None, None], "encoding": [None, None],
                        "commit": [None, None]}
EMERGENCY_BEYOND_BODIES = bytearray()
EMERGENCY_BEYOND_MARKER = {"$private_pending_error": "EMERGENCY_BEYOND"}
PIPE_UNWRITTEN_TAIL = False
PREOWNED_CAP_STOP = False


def _ascii_piece(text, limit):
    if type(text) is bytes:
        raw = text
    else:
        raw = str(text).encode("ascii", "backslashreplace")
    if len(raw) > limit:
        raw = raw[:limit]
    return raw


def _append_field(chunks, label, raw):
    chunks.append(label.encode("ascii"))
    chunks.append(b" ")
    chunks.append(str(len(raw)).encode("ascii"))
    chunks.append(b"\n")
    chunks.append(raw)


def render_original_body(exc, nested=True):
    # Field bytes only. This is not the public JSON publication.
    chunks = []
    try:
        _append_field(chunks, "type", _ascii_piece(type(exc).__name__, 32))
        _append_field(chunks, "module", _ascii_piece(type(exc).__module__, 64))
        try:
            message = str(exc)
        except BaseException:
            message = ""
        _append_field(chunks, "message", _ascii_piece(message, 900))
        errno = getattr(exc, "errno", None)
        if errno is not None:
            _append_field(chunks, "errno", _ascii_piece(errno, 32))
        for name, limit in (("filename", 160), ("filename2", 160)):
            value = getattr(exc, name, None)
            if type(value) is str or type(value) is bytes:
                _append_field(chunks, name, _ascii_piece(value, limit))
        notes = getattr(exc, "__notes__", None)
        if type(notes) is list:
            for note in notes[:16]:
                _append_field(chunks, "note", _ascii_piece(note, 900))
        tb = getattr(exc, "__traceback__", None)
        for fault_slot in OUTSIDE_FAILURES.values():
            if fault_slot[0] is exc:
                tb = fault_slot[1]
                break
        seen = 0
        while tb is not None and seen < 128:
            code = tb.tb_frame.f_code
            _append_field(chunks, "file", _ascii_piece(code.co_filename, 160))
            _append_field(chunks, "function", _ascii_piece(code.co_name, 64))
            _append_field(chunks, "line", _ascii_piece(tb.tb_lineno, 32))
            _append_field(chunks, "lasti", _ascii_piece(tb.tb_lasti, 32))
            tb = tb.tb_next
            seen += 1
        if nested:
            for label, other in (("cause", getattr(exc, "__cause__", None)),
                                 ("context", getattr(exc, "__context__", None))):
                if other is not None and other is not exc:
                    _append_field(chunks, label, render_original_body(other, False))
    except BaseException:
        if not chunks:
            _append_field(chunks, "type", b"UNRENDERED")
    return b"".join(chunks)


def receiver_room():
    # The fd the parent already holds. Caps stay the original selected ones.
    for name in ("PUBLIC_OUTPUT", "OUTPUT", "PUBLIC_CAP"):
        value = globals().get(name)
        if type(value) is int and value > 0:
            return value
    return 32768


def derived_raw_room():
    return derived_stock_bounds()[2]


OUTSIDE_BODY = {
    "alias": 8,
    "held": None,
    "end": 0,
    "marker": b"A206BODY\n",
    "cap": 32768,
    "relation": b"",
    "fault": False,
    "sealed": False,
    "consumed": False,
    "inherited": False,
    "retire_allowed": False,
    "disposition": None,
    "outside_timer_refreshed": False,
    "custody": None,
    "po880_is_not_this_body": False,
}

# SOL079: finite, local first-fault cells. These are NOT native-body custody.
# Each native failure phase terminates that path; commit/unlock are separate.
# Original error journals/caps are unchanged, and whole cost fit stays open.
OUTSIDE_FAILURES = {
    "acquire": [None, None], "adopt": [None, None],
    "commit": [None, None], "unlock": [None, None],
    "seal": [None, None], "consume": [None, None], "end": [None, None],
    "publication": [None, None],
}


def retain_outside_failure(phase, exc):
    slot = OUTSIDE_FAILURES[phase]
    if slot[0] is None:
        slot[0] = exc
        slot[1] = exc.__traceback__
    elif slot[0] is not exc:
        # No replay/overwrite after a terminated native phase. The new original
        # remains the explicit cause of the reached top-level refusal.
        raise Refusal("outside failure phase already terminated") from exc
    # Raw object/TB first; no deferred_error, rendering or IO in this store.
    return outside_body_fault(phase)


def publish_outside_failures(record):
    rows = [
        {"phase": phase, "error": deferred_error(slot[0]),
         "caught_traceback_retained": slot[1] is not None}
        for phase, slot in OUTSIDE_FAILURES.items() if slot[0] is not None
    ]
    if rows:
        record["outside_body_errors"] = rows


def outside_body_fault(reason):
    OUTSIDE_BODY["fault"] = True
    OUTSIDE_BODY["fault_reason"] = reason
    return False


def outside_body_number(number):
    if OUTSIDE_BODY.get("retire_allowed"):
        return False
    held = OUTSIDE_BODY.get("held")
    if isinstance(held, int) and int(held) == number:
        return True
    alias = OUTSIDE_BODY.get("alias_lease")
    if isinstance(alias, int) and int(alias) == number:
        return True
    # Known returned body allocations remain held even if wrapper/fstat or
    # unmask fails before OUTSIDE_BODY assignment. Never guess a bare slot8.
    # Unknown/no-number acquisitions remain unresolved, never closed/reclaimed.
    history = globals().get("FD_ALLOCATION_HISTORY")
    if history is not None:
        for record in history[:globals().get("FD_ALLOCATION_USED", 0)]:
            if (record is not None and record.get("role") in
                    ("outside-body", "outside-body-alias") and
                    record.get("state") == "RETURNED" and
                    type(record.get("number")) is int and record["number"] == number):
                return True
    history = globals().get("FD_HISTORY")
    if history is not None:
        for record in history[:globals().get("FD_HISTORY_USED", 0)]:
            if (record is not None and record.get("where") == "outside-body-alias" and
                    record.get("state") == "RETURNED" and
                    type(record.get("number")) is int and record["number"] == number):
                return True
    return False



def outside_relation_bytes():
    parts = []
    for ordinal in range(6):
        parts.append(b"F%d\n" % ordinal)
    non_graph, graph_budget, raw_room, _inner, _supervisor_cap, _outer, _base64 = derived_stock_bounds()
    parts.append(b"L %d %d %d\n" % (non_graph, graph_budget, raw_room))
    for index in range(ERROR_USED):
        exc = ERROR_OBJECTS[index]
        if exc is not None:
            parts.append(b"E\n" + render_original_body(exc))
    for index in range(EMERGENCY_USED):
        exc = EMERGENCY_OBJECTS[index]
        if exc is not None:
            parts.append(b"M\n" + render_original_body(exc))
    if EMERGENCY_BEYOND_BODIES:
        parts.append(b"B\n" + bytes(EMERGENCY_BEYOND_BODIES))
    for phase, slot in OUTSIDE_FAILURES.items():
        if slot[0] is not None:
            parts.append(b"O " + phase.encode("ascii") + b"\n" +
                         render_original_body(slot[0]))
    return b"".join(parts)


def open_caller_outside_body():
    try:
        opener = globals().get("allocate_owned")
        require(opener is not None and OUTSIDE_BODY["held"] is None,
                "outside body opener missing")
        lease = opener(os.memfd_create, "outside-body", "A206-outside-body",
                       os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        lease_type = globals().get("OwnedFD")
        require(lease_type is not None and isinstance(lease, lease_type),
                "outside body lease lost its allocation type")
        OUTSIDE_BODY["held"] = lease
        marker = OUTSIDE_BODY["marker"]
        wrote = os.pwrite(lease, marker, 0)
        require(wrote == len(marker), "outside body header short")
        OUTSIDE_BODY["end"] = len(marker)
        # Existing exact preown -> ACQUIRE_UNKNOWN -> native return -> publish
        # protocol; its original short signal mask covers dup2+typed publication,
        # not subsequent fcntl/append/cleanup, and never rearms the absolute clock.
        alias = opener(os.dup2, "outside-body-alias", int(lease), 8,
                       inheritable=True)
        OUTSIDE_BODY["alias_lease"] = alias
        flags = fcntl.fcntl(alias, fcntl.F_GETFD)
        fcntl.fcntl(alias, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
        OUTSIDE_BODY["inherited"] = False
        return True
    except BaseException as exc:
        retain_outside_failure("acquire", exc)
        raise


def adopt_outside_body():
    try:
        info = os.fstat(8)
        require(stat.S_ISREG(info.st_mode), "outside body alias is not regular")
        size = info.st_size
        require(0 < size <= OUTSIDE_BODY["cap"], "outside body alias extent")
        head = os.pread(8, len(OUTSIDE_BODY["marker"]), 0)
        require(head == OUTSIDE_BODY["marker"], "outside body marker missing")
        adopter = globals().get("adopt_exact_fd")
        if adopter is None:
            OUTSIDE_BODY["held"] = 8
        else:
            OUTSIDE_BODY["held"] = adopter(8, "outside-body-alias")
        OUTSIDE_BODY["end"] = size
        OUTSIDE_BODY["inherited"] = True
        return True
    except BaseException as exc:
        retain_outside_failure("adopt", exc)
        raise


def commit_outside_body():
    if OUTSIDE_BODY["fault"] or OUTSIDE_BODY["sealed"] or OUTSIDE_BODY["held"] is None:
        return outside_body_fault("commit refused")
    fd = OUTSIDE_BODY["held"]
    locked = False
    try:
        # Per-process POSIX record lock. A lock tied to the shared open-file
        # description follows dup and fork, so it does not serialize the
        # caller, supervisor, and verifier. The wait stays inside due().
        while not locked:
            try:
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 0, 0, os.SEEK_SET)
                locked = True
            except BlockingIOError:
                globals()["due"]()
                time.sleep(0.01)
        info = os.fstat(fd)
        size = info.st_size
        if size < OUTSIDE_BODY["end"]:
            return outside_body_fault("outside body shrank")
        OUTSIDE_BODY["end"] = size
        marker = OUTSIDE_BODY["marker"]
        if os.pread(fd, len(marker), 0) != marker:
            return outside_body_fault("marker lost")
        relation = outside_relation_bytes()
        if type(relation) is not bytes or len(relation) > OUTSIDE_BODY["cap"]:
            return outside_body_fault("relation cap")
        previous = OUTSIDE_BODY["relation"]
        if previous:
            if not relation.startswith(previous):
                return outside_body_fault("relation diverged")
            chunk = relation[len(previous):]
        else:
            chunk = b"R\n" + relation
        if not chunk:
            OUTSIDE_BODY["relation"] = relation
            return True
        if OUTSIDE_BODY["end"] == 0 or OUTSIDE_BODY["end"] + len(chunk) > OUTSIDE_BODY["cap"]:
            return outside_body_fault("append cap")
        wrote = 0
        while wrote < len(chunk):
            n = os.pwrite(fd, chunk[wrote:], OUTSIDE_BODY["end"] + wrote)
            if type(n) is not int or n <= 0:
                return outside_body_fault("short outside append")
            wrote += n
        OUTSIDE_BODY["end"] += wrote
        OUTSIDE_BODY["relation"] = relation
        return True
    except BaseException as exc:
        return retain_outside_failure("commit", exc)
    finally:
        if locked:
            try:
                fcntl.lockf(fd, fcntl.LOCK_UN, 0, 0, os.SEEK_SET)
            except BaseException as exc:
                # A release error never replaces the independently retained
                # primary append/lock/read/render/write error.
                return retain_outside_failure("unlock", exc)



def remember_emergency_body(rendered):
    if type(rendered) is not bytes or OUTSIDE_BODY["sealed"] or OUTSIDE_BODY["held"] is None:
        return outside_body_fault("emergency body unavailable")
    return commit_outside_body()


def seal_outside_body():
    if OUTSIDE_BODY["fault"] or OUTSIDE_BODY["sealed"] or OUTSIDE_BODY["inherited"] or OUTSIDE_BODY["held"] is None:
        return outside_body_fault("seal unavailable")
    fd = OUTSIDE_BODY["held"]
    try:
        info = os.fstat(fd)
        if info.st_size < OUTSIDE_BODY["end"]:
            return outside_body_fault("seal shrink")
        OUTSIDE_BODY["end"] = info.st_size
        if OUTSIDE_BODY["end"] <= len(OUTSIDE_BODY["marker"]):
            return outside_body_fault("seal empty relation")
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW
                    | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        OUTSIDE_BODY["sealed"] = True
        return True
    except BaseException as exc:
        return retain_outside_failure("seal", exc)


def consume_outside_body():
    if OUTSIDE_BODY["fault"] or OUTSIDE_BODY["held"] is None:
        outside_body_fault("consume refused")
        return None
    fd = OUTSIDE_BODY["held"]
    try:
        info = os.fstat(fd)
        size = info.st_size
        if type(size) is not int or size <= 0 or size > OUTSIDE_BODY["cap"]:
            outside_body_fault("consume extent")
            return None
        body = os.pread(fd, size, 0)
        if type(body) is not bytes or len(body) != size or not body.startswith(OUTSIDE_BODY["marker"]):
            outside_body_fault("consume marker")
            return None
        OUTSIDE_BODY["consumed"] = True
        OUTSIDE_BODY["end"] = size
        OUTSIDE_BODY["custody"] = "ORIGINAL_MEMFD_NOT_RENDERED_TRANSPORT"
        OUTSIDE_BODY["po880_is_not_this_body"] = True
        return body
    except BaseException as exc:
        retain_outside_failure("consume", exc)
        return None


def finish_outside_end():
    def invalidate_visible_terminal():
        receipt = globals().get("R")
        if type(receipt) is dict:
            if "qualified_node_only" in receipt:
                receipt["qualified_node_only"] = False
            if "qualified" in receipt:
                receipt["qualified"] = False
            if receipt.get("status") in (
                    "QUALIFIED_NODE_RAW_PUBLISHER_CHAIN_PROVEN",
                    "QUALIFIED_NODE_PUBLISHER_CHAIN_AND_OWN_CAUSES_ONLY",
                    "QUALIFIED_SUPERVISED_NODE_RAW_PUBLISHER_CHAIN_PROVEN",
                    "STARTUP_CONTROL_CAUSE_CONFIRMED"):
                receipt["status"] = "NOT_PROVEN"
        if "qualified" in globals():
            globals()["qualified"] = False
        if "verified" in globals():
            globals()["verified"] = False

    def observe_outside_lease():
        held = OUTSIDE_BODY.get("held")
        owned = globals().get("OwnedFD")
        adopted = globals().get("AllocationFD")
        typed = ((owned is not None and isinstance(held, owned)) or
                 (adopted is not None and isinstance(held, adopted)))
        if not typed:
            return outside_body_fault("outside lease untyped")
        info = os.fstat(held)
        if not stat.S_ISREG(info.st_mode):
            return outside_body_fault("outside lease not regular")
        if not OUTSIDE_BODY["inherited"]:
            alias = OUTSIDE_BODY.get("alias_lease")
            if owned is None or not isinstance(alias, owned) or int(alias) != 8:
                return outside_body_fault("outside alias lease missing")
            os.fstat(alias)
        return True

    # The original interval stays armed through publication, cleanup, and end.
    # Closing this memfd would not retire the original native body, so this
    # path does not close it and does not claim retirement while that body is open.
    disposition = "NOT_RETIRED"
    try:
        if not observe_outside_lease() or OUTSIDE_BODY["fault"]:
            invalidate_visible_terminal()
        elif not OUTSIDE_BODY["inherited"]:
            receipt = globals().get("R")
            success = (OUTSIDE_BODY["sealed"] and OUTSIDE_BODY["consumed"]
                       and not OUTSIDE_BODY["fault"] and not globals().get("STOP")
                       and type(receipt) is dict and receipt.get("qualified_node_only"))
            if not success:
                invalidate_visible_terminal()
    except BaseException as exc:
        retain_outside_failure("end", exc)
        disposition = "NOT_RETIRED"
        OUTSIDE_BODY["retire_allowed"] = False
        invalidate_visible_terminal()
    OUTSIDE_BODY["disposition"] = disposition
    OUTSIDE_BODY["outside_timer_refreshed"] = False
    OUTSIDE_BODY["timer_disarmed"] = False
    receipt = globals().get("R")
    if type(receipt) is dict:
        receipt["outside_finite_end"] = {
            "disposition": disposition,
            "outside_timer_refreshed": False,
            "timer_disarmed": False,
            "timer_still_enforcing": True,
            "os_exit_is_not_custody": True,
        }
    return disposition


def preowned_record(body):
    return b"PO880 %d\n" % len(body) + body


def publication_due():
    due(0)


def publication_read_room(size):
    require(RUNTIME_READS + size <= RUNTIME_READ_LIMIT,
                "original supervisor read room before publication read")


def reserve_final_parent_reads(record):
    # For a regular file the final append reads prefix P once and frame F once:
    # P+F <= original cap. Pipes spend no readback, but do not get extra room.
    # This one future allowance is NOT an observed read and NOT a new grant.
    room = receiver_room()
    publication_read_room(room)
    PARENT_IO["final_read_reserved"] = room
    PARENT_IO["final_read_remaining"] = room
    record["publication_transport_costs"] = {
        "schema": "friday.sol072.ordered-transport-cost.v1",
        "original_stream_cap": room, "prefix_written_bytes": PARENT_PLACED,
        "known_prefix_read_bytes": PARENT_IO["read_bytes"],
        "final_read_upper_bytes": room, "final_read_is_observed": False,
        "full_original_native_body_accepted": False,
        "native_allocator_decoder_end_cost": "UNKNOWN_NOT_ZERO",
    }


def parent_read_once(size, offset):
    # Prospective original read allowance, exact returned bytes then charged.
    # The prospective check is not a whole allocator/decoder/native cost proof.
    publication_read_room(size)
    if PARENT_IO["final_read_reserved"]:
        require(size <= PARENT_IO["final_read_remaining"],
                "final append read exceeds ONE prospective original allowance")
        PARENT_IO["final_read_remaining"] -= size
    body = os.pread(1, size, offset)
    PARENT_IO["read_result"] = body  # before the fallible charge/projection
    PARENT_IO["read_bytes"] += len(body)
    charge_runtime(len(body))
    return body


def append_parent_bytes(blob, final=False):
    # One ordered stream on BOTH pipe and regular receiver. Never overwrite
    # an admitted prefix and never append over an uncertain physical tail.
    global PARENT_PLACED, PARENT_IMAGE, PIPE_UNWRITTEN_TAIL, PREOWNED_CAP_STOP
    require(PARENT_IO["state"] == "PREFIX" and PARENT_IO["pending"] is None,
            "publication cannot resume after a partial or final write")
    PARENT_IO["pending"] = blob  # actual full local body before first fallible IO
    PARENT_IO["base"] = PARENT_PLACED
    PARENT_IO["sent"] = 0
    PARENT_IO["state"] = "WRITING"
    try:
        require(type(blob) is bytes and bool(blob), "nonempty original transport bytes")
        if PARENT_PLACED + len(blob) > receiver_room():
            PREOWNED_CAP_STOP = True
            require(False, "prefix plus frame exceed ONE original receiver cap")
        info = os.fstat(1)
        regular = stat.S_ISREG(info.st_mode)
        if regular:
            require(info.st_size == PARENT_PLACED, "unknown existing receiver tail")
            if final and PARENT_PLACED:
                require(parent_read_once(PARENT_PLACED, 0) == PARENT_IMAGE,
                        "existing prefix changed before append")
        else:
            flags = fcntl.fcntl(1, fcntl.F_GETFL)
            fcntl.fcntl(1, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        if regular and final and PARENT_IO["base"] == 0:
            require(False, "final frame refuses pwrite at zero")
        if regular and PARENT_IO["base"] < PARENT_PLACED:
            require(False, "publication refuses overwrite of a placed prefix")
        view = memoryview(blob)
        while PARENT_IO["sent"] < len(blob):
            publication_due()
            sent = PARENT_IO["sent"]
            block = view[sent:sent + 4096]
            try:
                wrote = (os.pwrite(1, block, PARENT_IO["base"] + sent)
                         if regular else os.write(1, block))
            except BlockingIOError:
                select.select([], [1], [], 0.01)
                continue
            require(0 < wrote <= len(block), "receiver progress must be exact")
            PARENT_IO["sent"] += wrote  # preserve exact partial-prefix cursor
            PARENT_IO["write_bytes"] += wrote
        if regular:
            require(parent_read_once(len(blob), PARENT_IO["base"]) == blob,
                    "receiver append readback differs")
            require(os.fstat(1).st_size == PARENT_IO["base"] + len(blob),
                    "receiver append extent differs; never truncate a prior body")
        publication_due()  # failure here still invalidates terminal success
        # Original blob/cursor remain retained if this allocation fails.
        PARENT_IMAGE = PARENT_IMAGE + blob
        PARENT_PLACED = PARENT_IO["base"] + len(blob)
        PARENT_IO["state"] = "FRAME_WRITTEN" if final else "PREFIX"
        if not final:
            PARENT_IO["pending"] = None
            PARENT_IO["read_result"] = None
        return True
    except BaseException:
        PARENT_IO["state"] = "FAULT"
        PIPE_UNWRITTEN_TAIL = True
        raise


def place_parent_bytes(blob):
    return append_parent_bytes(blob)


def retain_publication_failure(phase, exc):
    # Finite once-only publication phase, not a new whole-journal capacity.
    slot = PUBLICATION_FAILURES[phase]
    slot[0] = exc
    slot[1] = exc.__traceback__
    # The exact original object+TB precede any rendered diagnostic/deferred_error.
    # This is still LOCAL until the missing original native receiver is supplied.
    return deferred_error(exc)


def snapshot_owned_originals():
    # Fitted records stay inside the derived raw room. Overflow is returned
    # whole so the parent fd can hold it up to the existing receiver cap.
    global PREOWNED_CAP_STOP
    parts = []
    overflow = []
    used = 0
    room = derived_raw_room()

    def take(blob):
        nonlocal used
        if type(blob) is not bytes or not blob:
            return
        if used + len(blob) > room:
            PREOWNED_CAP_STOP = True
            overflow.append(blob)
            return
        parts.append(blob)
        used += len(blob)

    for index in range(ERROR_USED):
        exc = ERROR_OBJECTS[index]
        if exc is not None:
            take(b"E\n" + render_original_body(exc))
    for index in range(EMERGENCY_USED):
        exc = EMERGENCY_OBJECTS[index]
        if exc is not None:
            take(b"M\n" + render_original_body(exc))
    if EMERGENCY_BEYOND_BODIES:
        take(b"B\n" + bytes(EMERGENCY_BEYOND_BODIES))
    for phase, slot in OUTSIDE_FAILURES.items():
        if slot[0] is not None:
            take(b"O " + phase.encode("ascii") + b"\n" +
                 render_original_body(slot[0]))
    return b"".join(parts), overflow


def peel_preowned(raw):
    bodies = []
    complete = True
    while raw.startswith(b"PO880 "):
        nl = raw.find(b"\n")
        if not (0 < nl < 32):
            complete = False
            break
        parts = raw[:nl].split(b" ")
        if len(parts) != 2 or not parts[1].isdigit():
            complete = False
            break
        size = int(parts[1])
        start = nl + 1
        if size < 0 or start + size > len(raw):
            complete = False
            bodies.append(raw[start:])
            raw = b""
            break
        bodies.append(raw[start:start + size])
        raw = raw[start + size:]
    return bodies, raw, complete


def publish_preowned_then_frame(record, encode_frame):
    # PO880 is a TRANSPORT of rendered fields, NOT preowned physical custody.
    # A failed prefix is terminal for this ordered stream; no second encoding,
    # no guessed tail offset, no later marker appended after a partial frame.
    try:
        publish_outside_failures(record)
        snapshot, overflow = snapshot_owned_originals()
        record["preowned_original_bodies"] = {
            "encoding": "utf-8", "text": snapshot.decode("ascii"),
            "bytes": len(snapshot), "sha256": hashlib.sha256(snapshot).hexdigest(),
            "parent_placed_before_encode": False,
            "cap_stop": PREOWNED_CAP_STOP, "pipe_unwritten_tail": PIPE_UNWRITTEN_TAIL,
            "custody_kind": "RENDERED_TRANSPORT_NOT_ORIGINAL_NATIVE_BODY",
        }
        require(place_parent_bytes(preowned_record(snapshot)), "complete first prefix")
        for blob in overflow:
            require(place_parent_bytes(preowned_record(blob)), "complete overflow prefix")
        extra_fn = globals().get("partial_preowned_bytes")
        if extra_fn is not None:
            extra = extra_fn()
            if extra:
                require(place_parent_bytes(preowned_record(b"P\n" + extra)),
                        "complete already retained partial transport prefix")
        record["preowned_original_bodies"]["parent_placed_before_encode"] = True
        record["preowned_original_bodies"]["cap_stop"] = PREOWNED_CAP_STOP
        record["preowned_original_bodies"]["pipe_unwritten_tail"] = PIPE_UNWRITTEN_TAIL
    except BaseException as exc:
        marker = retain_publication_failure("prefix", exc)
        record["preowned_place_failure"] = marker
        return None
    try:
        reserve_final_parent_reads(record)
        return encode_frame()
    except BaseException as exc:
        marker = retain_publication_failure("encoding", exc)
        record["publication_failure"] = marker
        return None


def retain_emergency(exc):
    global EMERGENCY_USED, EMERGENCY_BEYOND
    for i in range(EMERGENCY_USED):
        if EMERGENCY_OBJECTS[i] is exc:
            return EMERGENCY_MARKERS[i]
    if EMERGENCY_USED >= EMERGENCY_CAP:
        # The new original is not aliased to the previous marker. Its fields
        # are retained in the derived raw room and, when that room is full,
        # on the parent fd the process already holds. This does not raise.
        EMERGENCY_BEYOND += 1
        rendered = render_original_body(exc)
        if rendered not in EMERGENCY_BEYOND_BODIES:
            room = derived_raw_room()
            if len(EMERGENCY_BEYOND_BODIES) + len(rendered) + 1 <= room:
                if EMERGENCY_BEYOND_BODIES:
                    EMERGENCY_BEYOND_BODIES.extend(b"\n")
                EMERGENCY_BEYOND_BODIES.extend(rendered)
        try:
            remember_emergency_body(rendered)
        except BaseException:
            pass
        return EMERGENCY_BEYOND_MARKER
    i = EMERGENCY_USED
    EMERGENCY_OBJECTS[i] = exc
    EMERGENCY_TRACEBACKS[i] = exc.__traceback__
    EMERGENCY_USED = i + 1
    return EMERGENCY_MARKERS[i]

def deferred_error(exc):
    # First 256 distinct causes stay in the original journal. Reached causes
    # after that stay in the derived emergency receiver. No static exhausted
    # marker is published. This function does not raise.
    try:
        return ERROR_MARKERS[retain_error_object(exc)]
    except BaseException:
        return retain_emergency(exc)


def publish_errors(record):
    # Called at a publication boundary, not inside mandatory raw/close loops.
    # Every public failure remains a COMPLETE v2 graph after replacement.
    # Identity, not attacker-shaped JSON, selects our private marker.
    seen = []
    def walk(value):
        if value is EMERGENCY_BEYOND_MARKER:
            return {"schema": "friday.lab883.emergency-beyond-bodies.v1",
                    "ordinal": EMERGENCY_BEYOND,
                    "bodies_ascii": bytes(EMERGENCY_BEYOND_BODIES).decode("ascii", "backslashreplace")}
        for slot in range(EMERGENCY_USED):
            if value is EMERGENCY_MARKERS[slot]:
                require(EMERGENCY_OBJECTS[slot] is not None, "emergency original cause is not owned")
                if EMERGENCY_PROJECTED[slot] is None:
                    EMERGENCY_PROJECTED[slot] = project_owned_error(EMERGENCY_OBJECTS[slot])
                return EMERGENCY_PROJECTED[slot]
        for i in range(ERROR_USED):
            if value is ERROR_MARKERS[i]:
                if ERROR_PROJECTED[i] is None:
                    ERROR_PROJECTED[i] = project_owned_error(ERROR_OBJECTS[i])
                return ERROR_PROJECTED[i]
        if type(value) not in (dict, list):
            return value
        if any(value is old for old in seen):
            return value
        seen.append(value)
        if type(value) is dict:
            for key in value:
                value[key] = walk(value[key])
        else:
            for i in range(len(value)):
                value[i] = walk(value[i])
        return value
    return walk(record)


def validate_stock_error_graph(graph):
    # Both receivers check ALL full fields/references, not only copied cause.
    # This validates shape/full reversible stock bytes, NOT native semantics.
    require(type(graph) is dict and set(graph) == {"schema", "root", "nodes", "values",
            "stock_cause", "native_object_reconstruction"}
            and graph["schema"] == "friday.sol060.error-graph.v2"
            and graph["native_object_reconstruction"] == "NOT_QUALIFIED",
            "complete original v2 error envelope required")
    nodes, values, root = graph["nodes"], graph["values"], graph["root"]
    require(type(nodes) is list and 0 < len(nodes) <= 128 and type(values) is list
            and len(values) <= 128 and type(root) is int and 0 <= root < len(nodes),
            "full v2 arenas/root malformed")
    def error_ref(index, nullable=True):
        require((nullable and index is None) or
                (type(index) is int and 0 <= index < len(nodes)), "full error reference outside arena")
    def value_ref(cell):
        require(type(cell) is dict and len(cell) == 1, "full typed error value required")
        if "literal" in cell:
            require(type(cell["literal"]) in (type(None), bool, int, str), "stock literal type changed")
        elif "float_hex" in cell:
            require(type(cell["float_hex"]) is str and
                    float.fromhex(cell["float_hex"]).hex() == cell["float_hex"], "exact stock float hex")
        elif "error_ref" in cell:
            error_ref(cell["error_ref"], False)
        else:
            require(set(cell) == {"value_ref"} and type(cell["value_ref"]) is int
                    and 0 <= cell["value_ref"] < len(values), "full value reference outside arena")
    for i, node in enumerate(nodes):
        require(type(node) is dict and set(node) == {"id", "module", "type", "message", "args",
                "state", "errno", "filename", "filename2", "notes", "native_field_presence", "traceback", "cause", "context",
                "suppress_context", "group"} and type(node["id"]) is int and node["id"] == i,
                "full v2 node fields/identity malformed")
        require(all(type(node[k]) is str for k in ("module", "type", "message")) and
                type(node["suppress_context"]) is bool, "original stock cause fields missing")
        for k in ("args", "state", "errno", "filename", "filename2", "notes"):
            value_ref(node[k])
        presence = node["native_field_presence"]
        require(type(presence) is dict and set(presence) == {"errno", "filename", "filename2", "__notes__"}
                and all(type(v) is bool for v in presence.values()), "original native absence is not explicit null")
        for key, present in presence.items():
            field = "notes" if key == "__notes__" else key
            require(present or node[field] == {"literal": None}, "absent stock native field has invented value")
        error_ref(node["cause"]); error_ref(node["context"])
        group = node["group"]
        require(group is None or type(group) is list, "stock exception-group presence")
        if group is not None:
            for member in group:
                error_ref(member, False)
        frames = node["traceback"]
        require(type(frames) is list and len(frames) <= 128, "complete traceback arena")
        for frame in frames:
            require(type(frame) is dict and set(frame) == {"file", "function", "line", "lasti"}
                    and type(frame["file"]) is str and type(frame["function"]) is str
                    and type(frame["line"]) is int and type(frame["lasti"]) is int,
                    "complete stock frame coordinates")
    for i, cell in enumerate(values):
        require(type(cell) is dict and type(cell.get("id")) is int and cell["id"] == i,
                "full typed value identity")
        kind = cell.get("kind")
        if kind in ("bytes", "bytearray"):
            require(set(cell) == {"id", "kind", "bytes", "sha256", "base64"}
                    and type(cell["bytes"]) is int and cell["bytes"] >= 0
                    and type(cell["base64"]) is str and type(cell["sha256"]) is str,
                    "complete stock byte preimage fields")
            raw = base64.b64decode(cell["base64"], validate=True)
            require(len(raw) == cell["bytes"] and hashlib.sha256(raw).hexdigest() == cell["sha256"]
                    and base64.b64encode(raw).decode("ascii") == cell["base64"],
                    "full stock byte preimage/count/SHA not lossless")
        elif kind in ("list", "tuple"):
            require(set(cell) == {"id", "kind", "items"} and type(cell["items"]) is list,
                    "complete original typed sequence")
            for item in cell["items"]:
                value_ref(item)
        else:
            require(kind == "dict" and set(cell) == {"id", "kind", "pairs"}
                    and type(cell["pairs"]) is list, "complete typed dict")
            for pair in cell["pairs"]:
                require(type(pair) is list and len(pair) == 2, "complete original dict pair")
                value_ref(pair[0]); value_ref(pair[1])
    stock = {"type": nodes[root]["type"], "message": nodes[root]["message"]}
    require(graph["stock_cause"] == stock, "stock cause must join actual full graph root")
    return stock


GRAPH_PUBLIC_BUDGET = 8192
GRAPH_CHARGED = 0


ARGV_CEILING = 1024

def derived_catch_capacity():
    # verifier.call sites in main, not a post-run count. Two of the six pass
    # one writable fd. secondary_capacity is 2 + 2*cells + streams.
    call_sites, streams, writable = 6, 3, 1
    slots = streams + writable
    secondary = 2 + 2 * slots + streams
    return call_sites * (1 + secondary) + 8 + 16

def derived_stock_bounds():
    # Metadata reserved before any producer spawn. Raw bodies are the channel
    # tail and are not counted twice. An 8192 check after the graph exists is
    # not this bound.
    call_sites, streams, writable = 6, 3, 1
    slots = streams + writable
    per_cell, per_call = 160, 480
    samples, homes, header, publication = 8 * 120, 2 * 160, 256, 192
    non_graph = (call_sites * (per_call + ARGV_CEILING + slots * per_cell)
                 + samples + homes + header + publication)
    inner, supervisor_cap, outer = 32768, 65536, 2097152
    rest = inner - non_graph
    graph_budget = rest // 3
    raw_room = rest - graph_budget
    base64_supervisor = 4 * ((supervisor_cap + 2) // 3)
    return (non_graph, graph_budget, raw_room, inner, supervisor_cap, outer, base64_supervisor)


CHANNEL_TAIL_USED = 0


def channel_raw_room():
    _non_graph, _graph_budget, raw_room, _inner, _supervisor_cap, _outer, _base64 = derived_stock_bounds()
    return raw_room - CHANNEL_TAIL_USED


def structural_graph_bytes(graph):
    # One byte per ascii character plus a small fixed frame. json.dumps remains the hard cap.
    total = 128
    nodes = graph.get("nodes") if type(graph) is dict else None
    values = graph.get("values") if type(graph) is dict else None
    if type(nodes) is list:
        for node in nodes:
            total += 96
            if type(node) is not dict:
                continue
            for key in ("module", "type", "message"):
                text = node.get(key)
                total += len(text) if type(text) is str else 0
            frames = node.get("traceback")
            if type(frames) is list:
                for frame in frames:
                    total += 32
                    if type(frame) is not dict:
                        continue
                    for key in ("file", "function"):
                        text = frame.get(key)
                        total += len(text) if type(text) is str else 0
    if type(values) is list:
        for cell in values:
            total += 48
            if type(cell) is not dict:
                continue
            for key in ("base64", "sha256"):
                text = cell.get(key)
                total += len(text) if type(text) is str else 0
    return total


def channel_pairs(text):
    def pairs(items):
        seen = set()
        out = {}
        for key, value in items:
            require(type(key) is str and key not in seen, "duplicate channel key")
            seen.add(key)
            out[key] = value
        return out
    def bad_constant(_value):
        require(False, "channel nan refused")
    value, end = json.JSONDecoder(object_pairs_hook=pairs, parse_constant=bad_constant).raw_decode(text)
    require(end == len(text), "channel json tail")
    return value


def frame_channel(raw, cap):
    require(type(raw) is bytes and 0 < len(raw) <= cap, "channel frame cap")
    nl = raw.find(b"\n")
    require(0 < nl < 64 and raw.startswith(b"FR875 "), "channel frame header")
    parts = raw[:nl].decode("ascii").split(" ")
    require(len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit(), "channel frame lengths")
    n, m = int(parts[1]), int(parts[2])
    require(n >= 2 and m >= 0 and nl + 1 + n + m == len(raw), "channel frame span")
    body = raw[nl + 1:nl + 1 + n]
    tail = raw[nl + 1 + n:]
    return body, tail


def relocate_channel_bodies(node, bag):
    if type(node) is list:
        for item in node:
            relocate_channel_bodies(item, bag)
        return
    if type(node) is not dict:
        return
    encoding = node.get("encoding")
    body = None
    restored = None
    if encoding == "base64" and type(node.get("raw_base64")) is str:
        raw = base64.b64decode(node["raw_base64"], validate=True)
        require(base64.b64encode(raw).decode("ascii") == node["raw_base64"], "channel base64 canonical")
        body, restored = raw, "base64"
    elif encoding == "utf-8" and type(node.get("text")) is str:
        body, restored = node["text"].encode("utf-8"), "utf-8"
    if body is not None:
        require(node.get("bytes") == len(body) and node.get("sha256") == hashlib.sha256(body).hexdigest(),
                "channel body bytes/sha")
        off = len(bag[0])
        bag[0].extend(body)
        node["encoding"] = "channel-raw-span"
        node["restored_encoding"] = restored
        node["span"] = [off, len(body)]
        node["raw_base64"] = None
        node["text"] = None
    for value in list(node.values()):
        if type(value) in (dict, list):
            relocate_channel_bodies(value, bag)


def rehydrate_channel_spans(node, tail):
    if type(node) is list:
        for item in node:
            rehydrate_channel_spans(item, tail)
        return
    if type(node) is not dict:
        return
    if node.get("encoding") == "channel-raw-span":
        span = node.get("span")
        restored = node.get("restored_encoding")
        require(type(span) is list and len(span) == 2 and type(span[0]) is int and type(span[1]) is int
                and span[0] >= 0 and span[1] >= 0 and span[0] + span[1] <= len(tail), "channel span")
        body = bytes(tail[span[0]:span[0] + span[1]])
        require(node.get("bytes") == span[1] == len(body)
                and node.get("sha256") == hashlib.sha256(body).hexdigest(), "channel span sha")
        if restored == "utf-8":
            node["text"] = body.decode("utf-8")
            node["raw_base64"] = None
            node["encoding"] = "utf-8"
        elif restored == "base64":
            node["raw_base64"] = base64.b64encode(body).decode("ascii")
            node["text"] = None
            node["encoding"] = "base64"
        else:
            require(False, "channel restored encoding")
        node.pop("span", None)
        node.pop("restored_encoding", None)
    for value in list(node.values()):
        if type(value) in (dict, list):
            rehydrate_channel_spans(value, tail)


def encode_channel_frame(record, cap):
    copied = json.loads(json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False))
    bag = [bytearray()]
    relocate_channel_bodies(copied, bag)
    body = json.dumps(copied, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    tail = bytes(bag[0])
    raw = b"FR875 %d %d\n" % (len(body), len(tail)) + body + tail
    require(len(raw) <= cap, "channel frame exceeds original cap")
    return raw


def validate_publication_transport(record, cap, prefix_size):
    # Actual decoder consumes complete bytes separately. This validates only
    # disjoint original transport IO reservations, never original-body custody.
    cost = record.get("publication_transport_costs")
    require(type(cost) is dict and cost.get("schema") == "friday.sol072.ordered-transport-cost.v1",
            "current matched publication cost schema")
    require(cost.get("original_stream_cap") == cap
            and type(cost.get("prefix_written_bytes")) is int
            and cost["prefix_written_bytes"] == prefix_size
            and 0 <= prefix_size < cap
            and type(cost.get("known_prefix_read_bytes")) is int
            and cost["known_prefix_read_bytes"] >= 0
            and cost.get("final_read_upper_bytes") == cap
            and cost.get("final_read_is_observed") is False
            and cost.get("full_original_native_body_accepted") is False
            and cost.get("native_allocator_decoder_end_cost") == "UNKNOWN_NOT_ZERO",
            "one original transport cap; unknown native/end costs are not zero")
    return cost


def decode_channel_frame(raw, cap):
    body, tail = frame_channel(raw, cap)
    record = channel_pairs(body.decode("ascii"))
    require(type(record) is dict, "channel frame object")
    rehydrate_channel_spans(record, tail)
    checker = globals().get("validate_received_error_graphs")
    if checker is not None:
        checker(record)
    return record

def charge_graph(n):
    global GRAPH_CHARGED
    _non_graph, graph_budget, _raw_room, _inner, _supervisor_cap, _outer, _base64 = derived_stock_bounds()
    aggregate = graph_budget * 6 if GRAPH_PUBLIC_BUDGET > graph_budget else graph_budget
    require(type(n) is int and 0 <= n <= graph_budget and GRAPH_CHARGED + n <= aggregate,
            "derived stock graph budget exhausted; native object stays owned")
    GRAPH_CHARGED += n

def seal_stock_graph(graph):
    nodes = graph["nodes"]
    require(type(nodes) is list and len(nodes) > 0, "stock graph nodes")
    for node in nodes:
        require(type(node.get("type")) is str and len(node["type"]) <= 32
                and type(node.get("module")) is str and len(node["module"]) <= 64
                and type(node.get("message")) is str and len(node["message"]) <= 900,
                "stock graph field wider than the measured public domain")
        frames = node.get("traceback")
        require(type(frames) is list and len(frames) <= 128, "stock frame count")
        for frame in frames:
            require(type(frame.get("file")) is str and len(frame["file"]) <= 160
                    and type(frame.get("function")) is str and len(frame["function"]) <= 64,
                    "stock frame wider than the measured public domain")
    _non_graph, graph_budget, _raw_room, _inner, _supervisor_cap, _outer, _base64 = derived_stock_bounds()
    require(structural_graph_bytes(graph) <= graph_budget,
            "canonical stock graph exceeds derived inner remainder; native object stays owned")
    raw = json.dumps(graph, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    require(len(raw) <= graph_budget,
            "canonical stock graph exceeds derived inner remainder; native object stays owned")
    validate_stock_error_graph(graph)
    charge_graph(len(raw))
    return graph

def exact_error(exc):
    # Projection occurs only after the private journal owns the native cause.
    root_slot = retain_error_object(exc)
    return project_owned_error(exc)

def project_owned_error(exc):
    # Prospective work is charged before variable-sized Python builders. This
    # is a LOWER BOUND on the unchanged complete JSON, not native-body or RSS
    # qualification. A graph that passes still needs seal_stock_graph's exact
    # whole byte count and both unchanged receiving validators.
    _non_graph, graph_budget, _raw_room, _inner, _supervisor_cap, _outer, _base64 = derived_stock_bounds()
    aggregate = graph_budget * 6 if GRAPH_PUBLIC_BUDGET > graph_budget else graph_budget
    available = min(graph_budget, aggregate - GRAPH_CHARGED)
    require(type(available) is int and available > 0,
            "prospective stock graph budget exhausted; original stays owned")
    work = [0]
    def reserve_lower_bound(units):
        require(type(units) is int and units >= 0 and
                units <= available - work[0],
                "prospective complete stock graph exceeds unchanged wire remainder")
        work[0] += units
    objects, nodes, values, cells = [exc], [], [], []
    def ref(value):
        reserve_lower_bound(1)
        if value is None:
            return None
        require(isinstance(value, BaseException), "stock exception reference type")
        for i, old in enumerate(objects):
            if old is value:
                return i
        require(len(objects) < 128, "original exception-node capacity exhausted")
        objects.append(value)
        return len(objects) - 1
    def val(value):
        reserve_lower_bound(1)
        if isinstance(value, BaseException):
            return {"error_ref": ref(value)}
        if type(value) in (type(None), bool, int, str):
            if type(value) is str:
                # ensure_ascii JSON cannot use fewer bytes than codepoints.
                reserve_lower_bound(len(value))
            elif type(value) is int:
                # 4 bits/digit is a permissive lower bound, including zero.
                # Avoid decimal conversion before this finite admission check.
                reserve_lower_bound(max(0, (value.bit_length() - 1) // 4))
            return {"literal": value}
        if type(value) is float:
            return {"float_hex": value.hex()}
        for i, old in enumerate(values):
            if old is value:
                return {"value_ref": i}
        require(type(value) in (bytes, bytearray, list, tuple, dict),
                "unsupported stock error value; original object remains owned")
        require(len(values) < 128, "original error-value capacity exhausted")
        values.append(value)
        return {"value_ref": len(values) - 1}
    error_index = value_index = 0
    # Args/state/notes can reveal error aliases AFTER the first error frontier.
    # Both frontiers drain to a fixed point; no referenced late node is omitted.
    while error_index < len(objects) or value_index < len(values):
        while error_index < len(objects):
            obj = objects[error_index]
            reserve_lower_bound(1)
            frames = []
            tb = obj.__traceback__
            for slot in range(EMERGENCY_USED):
                if EMERGENCY_OBJECTS[slot] is obj and EMERGENCY_TRACEBACKS[slot] is not None:
                    tb = EMERGENCY_TRACEBACKS[slot]
                    break
            for slot in range(ERROR_USED):
                if ERROR_OBJECTS[slot] is obj:
                    tb = ERROR_TRACEBACKS[slot]
                    break
            for fault_slot in OUTSIDE_FAILURES.values():
                if fault_slot[0] is obj:
                    tb = fault_slot[1]
                    break
            while tb is not None:
                require(len(frames) < 128, "original traceback capacity exhausted")
                file_name = tb.tb_frame.f_code.co_filename
                function_name = tb.tb_frame.f_code.co_name
                reserve_lower_bound(1 + len(file_name) + len(function_name))
                frames.append({"file": file_name, "function": function_name,
                               "line": tb.tb_lineno, "lasti": tb.tb_lasti})
                tb = tb.tb_next
            module_name, type_name = type(obj).__module__, type(obj).__name__
            reserve_lower_bound(len(module_name) + len(type_name))
            # Capture original args/state/alias frontiers before formatting.
            # None is private builder state only: no partial graph is emitted.
            nodes.append({"id": error_index, "module": module_name,
                "type": type_name, "message": None, "args": val(obj.args),
                "state": val(vars(obj)), "errno": val(getattr(obj, "errno", None)),
                "filename": val(getattr(obj, "filename", None)),
                "filename2": val(getattr(obj, "filename2", None)),
                "notes": val(getattr(obj, "__notes__", None)), "traceback": frames,
                "native_field_presence": {k: hasattr(obj, k) for k in ("errno", "filename", "filename2", "__notes__")},
                "cause": ref(obj.__cause__), "context": ref(obj.__context__),
                "suppress_context": obj.__suppress_context__,
                "group": [ref(e) for e in obj.exceptions] if isinstance(obj, BaseExceptionGroup) else None})
            error_index += 1
        while value_index < len(values):
            obj = values[value_index]
            reserve_lower_bound(1)
            if type(obj) in (bytes, bytearray):
                reserve_lower_bound(4 * ((len(obj) + 2) // 3))
                cell = {"id": value_index, "kind": type(obj).__name__, "bytes": len(obj),
                        "sha256": hashlib.sha256(obj).hexdigest(),
                        "base64": base64.b64encode(obj).decode("ascii")}
            elif type(obj) in (list, tuple):
                cell = {"id": value_index, "kind": type(obj).__name__, "items": [val(v) for v in obj]}
            else:
                cell = {"id": value_index, "kind": "dict", "pairs": [[val(k), val(v)] for k,v in obj.items()]}
            cells.append(cell)
            value_index += 1
    # The complete original object/value graph is finite before stock message
    # formatting. Existing selected-class/message factory qualification remains
    # required; no claim bounds arbitrary __str__ native/provider behavior.
    for node, original in zip(nodes, objects):
        message = str(original)
        require(type(message) is str and len(message) <= 900,
                "stock graph field wider than the measured public domain")
        reserve_lower_bound(len(message))
        node["message"] = message
    graph = {"schema": "friday.sol060.error-graph.v2", "root": 0,
             "nodes": nodes, "values": cells,
             "stock_cause": {"type": nodes[0]["type"], "message": nodes[0]["message"]},
             "native_object_reconstruction": "NOT_QUALIFIED"}
    return seal_stock_graph(graph)


def validate_received_error_graphs(record):
    # Whole decoded receipt is bounded by its unchanged original channel cap.
    if type(record) is dict:
        require("$private_pending_error" not in record, "private error owner is not durable public evidence")
        if "outside_body_errors" in record:
            rows = record["outside_body_errors"]
            require(type(rows) is list and 0 < len(rows) <= len(OUTSIDE_FAILURES),
                    "finite outside first-fault relation")
            phases = set()
            for row in rows:
                require(type(row) is dict and set(row) ==
                        {"phase", "error", "caught_traceback_retained"},
                        "outside original failure shape")
                phase = row["phase"]
                require(type(phase) is str and phase in OUTSIDE_FAILURES and
                        phase not in phases and
                        type(row["caught_traceback_retained"]) is bool,
                        "outside original phase identity")
                phases.add(phase)
                error = row["error"]
                require(type(error) is dict and
                        error.get("schema") == "friday.sol060.error-graph.v2",
                        "outside original requires complete error graph, not reason-only/beyond marker")
                # The existing recursive walk below validates this full graph
                # once; do not decode/validate its raw values a second time.
        if record.get("schema") == "friday.sol060.error-graph.v2":
            validate_stock_error_graph(record)
            return
        for child in record.values():
            validate_received_error_graphs(child)
    elif type(record) is list:
        for child in record:
            validate_received_error_graphs(child)


def original_stock_cause(graph):
    return validate_stock_error_graph(graph)


class AllocationFD(int):
    def __new__(cls, record):
        result = int.__new__(cls, record["number"])
        result.allocation = record
        return result


def reserve_allocation(where):
    global FD_GENERATION, FD_HISTORY_USED, FD_LIFETIME_ACQUIRED
    FD_GENERATION += 1
    record = {"generation": FD_GENERATION, "where": where, "number": None,
              "state": "PLANNED", "attempted_once": False,
              "acquire_error_object": None, "acquire_error": None, "close_error_object": None,
              "close_error": None, "token": None}
    # The slot is taken before the native acquire. Unused arena slots stay None.
    require(FD_HISTORY_USED < FD_SIMULTANEOUS_CAP,
            "live unretired descriptors reached simultaneous cap 256; lifetime audit is not this cap")
    FD_HISTORY[FD_HISTORY_USED] = record
    FD_HISTORY_USED += 1
    FD_LIFETIME_ACQUIRED += 1
    note_fd_audit("A", FD_LIFETIME_ACQUIRED, None, where)
    return record


def publish_allocation(record, number):
    record["number"] = number  # native return is owned before wrapper allocation
    record["state"] = "RETURNED"
    require(type(number) is int and number >= 0, "invalid exact returned FD")
    require(not any(old is not record and old["number"] == number and
                old["state"] in ("RETURNED", "RETIRE_UNKNOWN") for old in FD_HISTORY[:FD_HISTORY_USED]),
            "returned FD conflicts with retained allocation; never guess close")
    token = AllocationFD(record)
    record["token"] = token
    return token


def acquire_owned(operation, where, *args, **kwargs):
    record = reserve_allocation(where)
    saved = signal.pthread_sigmask(signal.SIG_BLOCK, FD_SIGNALS)
    try:
        record["state"] = "ACQUIRE_UNKNOWN"
        number = operation(*args, **kwargs)
        return publish_allocation(record, number)
    except BaseException as exc:
        record["acquire_error_object"] = exc
        record["acquire_error"] = deferred_error(exc)
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, saved)


def acquire_pipe(where):
    records = [reserve_allocation(where + ":read"), reserve_allocation(where + ":write")]
    tokens = [None, None]
    saved = signal.pthread_sigmask(signal.SIG_BLOCK, FD_SIGNALS)
    try:
        for record in records:
            record["state"] = "ACQUIRE_UNKNOWN"
        numbers = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        # BOTH numbers known before ANY wrapper/validation can fail.
        records[0]["number"], records[1]["number"] = numbers
        records[0]["state"] = records[1]["state"] = "RETURNED"
        tokens[0] = publish_allocation(records[0], numbers[0])
        tokens[1] = publish_allocation(records[1], numbers[1])
        return tokens
    except BaseException as exc:
        for record in records:
            record["acquire_error_object"] = exc
            record["acquire_error"] = deferred_error(exc)
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, saved)


def adopt_exact_fd(number, where):
    # Only independently verified initial inherited/runtime roles call this.
    return publish_allocation(reserve_allocation(where), number)


def retire_record(record):
    global STOP
    if record.get("unread_prefix_hold"):
        return
    if record["attempted_once"]:
        require(record["state"] == "CONFIRMED_CLOSED",
                "earlier allocation close unconfirmed; NEVER retry")
        return
    require(record["state"] == "RETURNED" and record["number"] is not None,
            "unknown acquisition is not a known returned allocation")
    record["attempted_once"] = True
    record["state"] = "RETIRE_UNKNOWN"  # prospective, sticky before native close
    try:
        os.close(record["number"])
    except BaseException as exc:
        STOP = True
        record["close_error_object"] = exc  # stored before fallible projection
        raise
    record["state"] = "CONFIRMED_CLOSED"
    reclaim_confirmed_slot(record)


def close_allocation(fd, where):
    if fd is None:
        return
    require(isinstance(fd, AllocationFD), "unregistered numeric close refused")
    saved = signal.pthread_sigmask(signal.SIG_BLOCK, FD_SIGNALS)
    try:
        return retire_record(fd.allocation)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, saved)


def finish_allocations():
    global STOP
    first = None
    # Finite same-owner graph. Do not reclose retired old aliases or reuse ints.
    # The outside alias stays open: its close is not the original native end.
    for record in FD_HISTORY[:FD_HISTORY_USED]:
        number = record["number"] if type(record.get("number")) is int else -1
        if outside_body_number(number):
            continue
        if record["state"] == "RETURNED" and not record["attempted_once"]:
            try:
                retire_record(record)
            except BaseException as exc:
                first = first or exc
    # All known physical firstcloses have been attempted before public references.
    for record in FD_HISTORY[:FD_HISTORY_USED]:
        if record["close_error_object"] is not None:
            record["close_error"] = deferred_error(record["close_error_object"])
    unresolved = [record for record in FD_HISTORY[:FD_HISTORY_USED] if record["state"] not in
                  ("PLANNED", "CONFIRMED_CLOSED")
                  and not outside_body_number(record["number"] if type(record.get("number")) is int else -1)]
    R["fd_lifetime_audit"] = fd_lifetime_audit()
    R["descriptor_allocation_history"] = [{k: r[k] for k in
        ("generation", "where", "number", "state", "attempted_once", "acquire_error", "close_error")}
        for r in FD_HISTORY[:FD_HISTORY_USED]]
    if unresolved:
        STOP = True
    if first is not None:
        raise first
    require(not unresolved, "same-owner acquisition/retirement outcomes unresolved")


def due(reserve=5):
    require(time.monotonic_ns() < DEADLINE - reserve * 1000000000,
            "host wall deadline/reserve exceeded")
    if CHILD is not None and not CHILD["reaped"]:
        require(time.monotonic_ns() < INNER_DEADLINE, "actual inner cutoff during supervisory work")


def deadline_alarm(signum, frame):
    raise Refusal("actual supervisor deadline alarm")


def ident(s, directory=False):
    d = {"dev": s.st_dev, "ino": s.st_ino, "uid": s.st_uid, "gid": s.st_gid,
         "mode": s.st_mode}
    if not directory:
        d.update(size=s.st_size, nlink=s.st_nlink, mtime_ns=s.st_mtime_ns,
                 ctime_ns=s.st_ctime_ns)
    return d


def component(path):
    require(path.startswith("/") and "//" not in path, "noncanonical fixed path")
    if path in DIRS:
        return DIRS[path][0]
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    if path == "/":
        fd = acquire_owned(os.open, "open", "/", flags)
    else:
        parent, name = os.path.split(path)
        require(name not in ("", ".", ".."), "bad fixed component")
        pfd = component(parent or "/")
        fd = acquire_owned(os.open, "open", name, flags, dir_fd=pfd)
        try:
            require(ident(os.stat(name, dir_fd=pfd, follow_symlinks=False), True)
                    == ident(os.fstat(fd), True), "component open race")
        except BaseException:
            try:
                close_taken(fd, "component_unpublished")
            except BaseException:
                globals()["STOP"] = True
            raise
    DIRS[path] = (fd, ident(os.fstat(fd), True))
    return fd


def regular(path):
    parent, name = os.path.split(path)
    pfd = component(parent)
    fd = acquire_owned(os.open, "open", name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=pfd)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and ident(before) == ident(os.stat(name, dir_fd=pfd, follow_symlinks=False)),
                "leaf type/link/identity race")
    except BaseException:
        try:
            close_taken(fd, "regular_unpublished")
        except BaseException:
            STOP = True
        raise
    return fd


def digest(fd, size, cap):
    before = ident(os.fstat(fd))
    require(before["size"] == size and 0 <= size <= cap, "fixed pin length/cap")
    h, pos = hashlib.sha256(), 0
    while pos < size:
        due()
        data = os.pread(fd, min(65536, size - pos), pos)
        require(bool(data), "short held input")
        h.update(data)
        pos += len(data)
    require(not os.pread(fd, 1, pos) and ident(os.fstat(fd)) == before,
            "held bytes drift during hash")
    return h.hexdigest()


def hold(name):
    path, size, expected, cap, tool = PINS[name]
    fd = regular(path)
    HELD[name] = {"fd": fd, "identity": ident(os.fstat(fd))}
    s = os.fstat(fd)
    require(s.st_uid == (0 if tool else 1000)
            and stat.S_IMODE(s.st_mode) == (0o755 if tool else 0o600)
            and digest(fd, size, cap) == expected, "independent pin mismatch: " + name)
    R["custody"][name] = {"path": path, "sha256": expected, "bytes": size,
                           "identity": HELD[name]["identity"]}
    return fd


def custody(phase):
    for path, (fd, before) in DIRS.items():
        due()
        current = os.stat("/", follow_symlinks=False) if path == "/" else os.stat(
            os.path.basename(path), dir_fd=DIRS[os.path.dirname(path) or "/"][0],
            follow_symlinks=False)
        require(ident(os.fstat(fd), True) == ident(current, True) == before,
                "held component drift: " + path)
    for name, item in HELD.items():
        path, size, expected, cap, _ = PINS[name]
        fd = regular(path)
        try:
            require(ident(os.fstat(fd)) == ident(os.fstat(item["fd"])) == item["identity"]
                    and digest(item["fd"], size, cap) == expected
                    and digest(fd, size, cap) == expected, "post/launch full-byte drift: " + name)
        finally:
            close_allocation(fd, "lexical_close")
    R["custody_" + phase] = True


def kernel_file(path, cap=16384):
    fd = acquire_owned(os.open, "open", path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        data = os.read(fd, cap + 1)
        require(len(data) <= cap, "kernel observation exceeds cap")
        return data
    finally:
        close_allocation(fd, "lexical_close")


def kernel_at(procfd, name, cap=16384):
    fd = acquire_owned(os.open, "open", name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=procfd)
    try:
        data = os.read(fd, cap + 1)
        require(len(data) <= cap, "held proc observation exceeds cap")
        return data
    finally:
        close_allocation(fd, "lexical_close")


def proc(pid, procfd=None):
    data = (kernel_file("/proc/" + str(pid) + "/status") if procfd is None
            else kernel_at(procfd, "status"))
    values = {}
    for line in data.splitlines():
        if b":" in line:
            k, v = line.split(b":", 1)
            values[k.decode("ascii")] = v.strip()
    fields = {}
    for key in ("VmSize", "VmRSS", "VmHWM", "VmPeak"):
        m = re.fullmatch(rb"([0-9]+) kB", values.get(key, b""))
        if m is None:
            raise MissingSample("complete memory field unavailable: " + key)
        fields[key + "_kib"] = int(m[1])
    require(all(type(v) is int and v >= 0 for v in fields.values()), "numeric mm required")
    return fields, values


def starttime(pid, procfd=None):
    raw = (kernel_file("/proc/" + str(pid) + "/stat") if procfd is None
           else kernel_at(procfd, "stat"))
    text = raw.decode("ascii")
    require(text.startswith(str(pid) + " ("), "held proc numeric identity mismatch")
    # comm may contain blanks/parentheses; fields after final ')' start at3.
    end = text.rfind(")")
    require(end > 0, "malformed proc stat")
    fields = text[end + 2:].split()
    require(len(fields) >= 20 and fields[1].isdigit() and fields[19].isdigit(), "proc identity syntax")
    return int(fields[1]), int(fields[19])


def children(pid, procfd=None):
    raw = (kernel_file("/proc/" + str(pid) + "/task/" + str(pid) + "/children", 4096)
           if procfd is None else kernel_at(procfd, "task/" + str(pid) + "/children", 4096))
    require(re.fullmatch(rb"(?:[0-9]+ )*", raw) is not None, "proc children syntax")
    answer = [int(x) for x in raw.split()]
    require(len(answer) <= 2, "unexpected process fanout")
    return answer


def exited(pfd):
    poller = select.poll()
    poller.register(pfd, select.POLLIN)
    return bool(poller.poll(0))


def credentials(values, expected_ppid=None):
    require(values.get("Uid") == b"1000\t1000\t1000\t1000"
            and values.get("Gid") == b"1000\t1000\t1000\t1000",
            "real/effective/saved/fs credentials not unprivileged expected user")
    require(values.get("Threads") == b"1", "single-thread process required")
    for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        value = values.get(k)
        require(value is not None and re.fullmatch(rb"[0-9a-fA-F]+", value) is not None,
                "missing actual capability mask")
        # Bounding capabilities can exist without actual privilege; NO_NEW_PRIVS
        # plus zero inheritable/permitted/effective/ambient blocks exec gain.
        if k != "CapBnd":
            require(int(value, 16) == 0, "capability exception forbidden")
    if expected_ppid is not None:
        require(values.get("PPid") == str(expected_ppid).encode("ascii"), "child ownership drift")
    require(values.get("NoNewPrivs") == b"1", "NO_NEW_PRIVS missing")


def descriptor_count(pid, cap):
    names = os.listdir("/proc/" + str(pid) + "/fd")
    require(all(x.isdigit() for x in names), "malformed actual descriptor sample")
    # listdir's own closed descriptor can appear in a self listing. Count only
    # actual still-open self descriptors; remote listings may conservatively
    # count a just-closed descriptor, never invent a smaller zero count.
    if pid == "self":
        actual = []
        for name in names:
            try:
                os.fstat(int(name))
            except OSError:
                continue
            actual.append(name)
        names = actual
    require(len(names) <= cap, "actual process FD cap exceeded")
    return len(names)


def sample_self(phase):
    current, values = proc("self")
    credentials(values)
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    require(type(raw) in (int, float) and raw >= 0, "actual raw self high-water missing")
    R["resources"][phase] = {"current": current, "ru_maxrss_self_kib": raw,
                              "elapsed_seconds": (time.monotonic_ns() - START) / 1e9}
    require(raw <= SUP_AS // 1024 and all(v <= SUP_AS // 1024 for v in current.values()),
            "supervisor raw/current memory cap")
    require(resource.getrlimit(resource.RLIMIT_AS) == (SUP_AS, PARENT_AS),
            "supervisor soft/hard AS changed")
    require(resource.getrlimit(resource.RLIMIT_NOFILE) == (64, 128),
            "supervisor effective64/inherited-hard128 FD pair changed")
    R["resources"][phase]["enforced_as_soft_hard_bytes"] = [SUP_AS, PARENT_AS]
    R["resources"][phase]["enforced_nofile_soft_hard"] = [64, 128]


def initialize_kernel_policy():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    require(libc.prctl(38, 1, 0, 0, 0) == 0, "NO_NEW_PRIVS failed")  # PR_SET_NO_NEW_PRIVS
    require(libc.prctl(36, 1, 0, 0, 0) == 0, "CHILD_SUBREAPER failed")
    value = ctypes.c_int(0)
    require(libc.prctl(37, ctypes.addressof(value), 0, 0, 0) == 0 and value.value == 1,
            "actual subreaper observation failed")
    R["kernel_policy"] = {"NoNewPrivs": True, "child_subreaper": True}


def pidfd_number(pfd):
    raw = kernel_file("/proc/self/fdinfo/" + str(pfd), 4096)
    matches = re.findall(rb"(?m)^Pid:\s+(-?[0-9]+)$", raw)
    require(len(matches) == 1, "actual pidfd target identity unavailable")
    return int(matches[0])


def open_proc(pid):
    return acquire_owned(os.open, "open", "/proc/" + str(pid), os.O_RDONLY | os.O_DIRECTORY
                   | os.O_CLOEXEC | os.O_NOFOLLOW)


def record_exit(record, reason):
    require(record["pidfd"] is not None and exited(record["pidfd"]),
            "record exit requires its own held pidfd")
    record["exit_confirmed"] = True
    record["sample"], record["missing_reason"] = None, reason
    record["evidence"].update(exit_confirmed=True, sample=None, missing_reason=reason)


def verify_generation(record, expected_ppid):
    # A pidfd whose fdinfo Pid is -1 is an old, reaped generation, even if the
    # numeric PID (or coarse start tick) has since been reused. Never sample the
    # current numeric /proc path through that old record.
    number = pidfd_number(record["pidfd"])
    if number == -1:
        record_exit(record, "held generation exited and lost its kernel PID")
        return False
    require(number == record["pid"], "held pidfd numeric target disagrees")
    ppid, ticks = starttime(record["pid"], record["procfd"])
    require(ticks == record["start_ticks"] and ppid == expected_ppid,
            "held proc generation/actual parent mismatch")
    if exited(record["pidfd"]):
        record_exit(record, "held generation exit is pidfd-confirmed")
        return False
    return True


def reconcile_descendant(pid, expected_ppid, lineage):
    # Every enumerated child, including an already-seen number, enters here.
    # Hold the current proc directory, then compare actual pidfd lifetime and
    # start identity. Histories are separate records, never a PID-only cache.
    saved = signal.pthread_sigmask(signal.SIG_BLOCK, REGISTRATION_SIGNALS)
    try:
        LAUNCH_RESERVE["probe"] = open_proc(pid)
        probe = LAUNCH_RESERVE["probe"]
        before = starttime(pid, probe)
        require(before[0] == expected_ppid, "enumerated child actual parent mismatch")
        record = None
        for old in TRACKED:
            if old["pid"] != pid or old["pidfd"] is None:
                continue
            target = pidfd_number(old["pidfd"])
            if target == -1:
                record_exit(old, "old held generation exited/reaped before current enumeration")
                continue
            require(target == pid, "old pidfd target inconsistency")
            # A still-associated pidfd cannot silently switch to a new task.
            require(old["start_ticks"] == before[1], "same PID has conflicting held live generation")
            record = old
            break
        if record is None:
            require(len(TRACKED) < 8, "actual owned generation count exceeds trusted eight-call bound")
            evidence = {"pid": pid, "generation": len(TRACKED) + 1, "start_ticks": before[1],
                        "lineage": lineage, "parent_history": [expected_ppid],
                        "sample": None, "last_complete_live_sample": None,
                        "missing_reason": "exact generation registered; no complete live sample yet",
                        "exit_confirmed": False, "reaped_by_supervisor": False,
                        "reap_owner": "verifier until actual subreaper adoption"}
            record = {"pid": pid, "generation": len(TRACKED) + 1, "pidfd": None,
                      "procfd": None, "start_ticks": before[1], "sample": None,
                      "missing_reason": evidence["missing_reason"], "exit_confirmed": False,
                      "reaped": False, "evidence": evidence}
            # Reserve the record before acquiring either owned descriptor.
            TRACKED.append(record)
            R["process_history"].append(evidence)
            record["procfd"], LAUNCH_RESERVE["probe"] = probe, None
            record["pidfd"] = acquire_owned(os.pidfd_open, "pidfd", pid, 0)
            after = starttime(pid, record["procfd"])
            require(before == after and pidfd_number(record["pidfd"]) == pid,
                    "new held pidfd/proc generation was not stable")
        else:
            after = starttime(pid, probe)
            require(before == after and pidfd_number(record["pidfd"]) == pid,
                    "current enumeration changed generation during reconciliation")
            LAUNCH_RESERVE["probe"] = None
            close_taken(probe, "reconcile_probe")
            if expected_ppid not in record["evidence"]["parent_history"]:
                require(expected_ppid == os.getpid() and EMPTY_PRE_FORK
                        and R["kernel_policy"]["child_subreaper"] is True,
                        "adopted lineage is not independently confirmed")
                record["evidence"]["parent_history"].append(expected_ppid)
                record["evidence"]["lineage"] = "confirmed exclusive-launch subreaper adoption"
        verify_generation(record, expected_ppid)
        return record
    finally:
        close_taken(take_reserved("probe"), "reconcile_probe_finally")
        signal.pthread_sigmask(signal.SIG_SETMASK, saved)


def enumerate_owned(parent_pid, parent_procfd, adopted=False):
    answer = []
    for pid in children(parent_pid, parent_procfd):
        try:
            record = reconcile_descendant(
                pid, parent_pid, "confirmed exclusive-launch subreaper adoption"
                if adopted else "actual direct child of held verifier generation")
        except (FileNotFoundError, ProcessLookupError) as exc:
            # A genuinely gone short-lived child has null telemetry with the
            # actual reason. If still enumerated, identity is mandatory.
            R["owned_processes"]["unavailable_" + str(pid)] = {
                "pid": pid, "sample": None, "missing_reason": type(exc).__name__,
                "lineage": "enumerated, then gone before exact generation acquisition"}
            if pid in children(parent_pid, parent_procfd):
                unknown("currently enumerated child exact generation unavailable")
            continue
        answer.append(record)
    return answer


def observe_owned():
    sample_self("last_live_supervisor")
    if not verify_generation(CHILD, os.getpid()):
        return
    try:
        current, values = proc(CHILD["pid"], CHILD["procfd"])
        require(starttime(CHILD["pid"], CHILD["procfd"]) ==
                (os.getpid(), CHILD["start_ticks"]), "parent sample generation changed")
    except (FileNotFoundError, ProcessLookupError, MissingSample) as exc:
        CHILD["sample"] = None
        CHILD["missing_reason"] = type(exc).__name__ + ": " + str(exc)
        R["resources"]["last_parent_sample"] = {
            "sample": None, "missing_reason": CHILD["missing_reason"],
            "complete_live_samples": CHILD["samples"]}
        if not exited(CHILD["pidfd"]):
            raise Refusal("mandatory live verifier memory sample unavailable")
        return
    credentials(values, os.getpid())
    require(all(v <= CHILD["as_bytes"] // 1024 for v in current.values()),
            "verifier actual current memory exceeds its enforced cap")
    CHILD["sample"], CHILD["missing_reason"] = current, None
    CHILD["samples"] += 1
    active = []
    for record in enumerate_owned(CHILD["pid"], CHILD["procfd"]):
        if not verify_generation(record, CHILD["pid"]):
            continue
        try:
            sample, values = proc(record["pid"], record["procfd"])
            require(starttime(record["pid"], record["procfd"]) ==
                    (CHILD["pid"], record["start_ticks"]), "child sample generation changed")
        except (FileNotFoundError, ProcessLookupError, MissingSample) as exc:
            record["sample"], record["missing_reason"] = None, type(exc).__name__ + ": " + str(exc)
            record["evidence"].update(sample=None, missing_reason=record["missing_reason"])
            if not exited(record["pidfd"]):
                raise Refusal("mandatory live GPG-child memory sample unavailable")
            record_exit(record, record["missing_reason"])
            continue
        credentials(values, CHILD["pid"])
        require(all(v <= PARENT_AS // 1024 for v in sample.values()),
                "GPG fork-phase actual memory bound exceeded")
        record["sample"], record["missing_reason"] = sample, None
        record["evidence"].update(sample=sample, last_complete_live_sample=sample, missing_reason=None)
        active.append(record)
    # Also settle old generations that no longer occur in the current child set.
    for record in TRACKED:
        if record["pidfd"] is not None and exited(record["pidfd"]):
            record_exit(record, "that held generation exited; prior sample retained separately")
    # An observed generation may exit between its complete sample and aggregate
    # accounting. Retain its last actual sample in history; terminal null does
    # not become a fake live metric or force a missing-live-sample refusal.
    active = [record for record in active if not record["exit_confirmed"]]
    require(len(active) <= 1, "more than one live verifier-owned child")
    R["resources"]["last_parent_sample"] = {"sample": CHILD["sample"],
                                             "missing_reason": CHILD["missing_reason"],
                                             "complete_live_samples": CHILD["samples"]}
    actual = current["VmSize_kib"] + R["resources"]["last_live_supervisor"]["current"]["VmSize_kib"]
    if active:
        require(active[0]["sample"] is not None, "mandatory live overlap sample unavailable")
        actual += active[0]["sample"]["VmSize_kib"]
    require(actual <= 262144, "actual observed aggregate AS exceeds256MiB")
    R["resources"]["last_observed_aggregate_as_kib"] = actual
    aggregate_samples = {
        "supervisor": dict(R["resources"]["last_live_supervisor"]["current"]),
        "verifier": dict(current),
        "gpg": [dict(record["sample"]) for record in active], "as_kib": actual}
    counts = {"supervisor": descriptor_count("self", 64),
              "verifier": descriptor_count(CHILD["pid"], 128), "gpg": None}
    if not active:
        counts["gpg_missing_reason"] = "no live GPG generation present in this observation"
    if active:
        try:
            # fd enumeration is accepted only across matching held generations.
            if verify_generation(active[0], CHILD["pid"]):
                actual_count = descriptor_count(active[0]["pid"], 128)
                if verify_generation(active[0], CHILD["pid"]):
                    counts["gpg"] = actual_count
                else:
                    counts["gpg_missing_reason"] = "held generation exited during FD observation"
            else:
                counts["gpg_missing_reason"] = "held generation exited before FD observation"
        except (FileNotFoundError, ProcessLookupError):
            require(exited(active[0]["pidfd"]), "mandatory live-child FD sample unavailable")
            counts["gpg_missing_reason"] = "held child generation exited before FD observation"
    observed_sum = counts["supervisor"] + counts["verifier"]
    if counts["gpg"] is not None:
        observed_sum += counts["gpg"]
    require(observed_sum <= 320, "actual observed aggregate FD ceiling")
    R["resources"]["last_fd_counts"] = counts
    # Preserve the original scalar and telemetry fields and bind them to the
    # exact samples used for this genuine live sum. No ending/null sample is
    # fabricated; a failed observation cannot publish a complete aggregate.
    aggregate_samples["fd_counts"] = dict(counts)
    R["resources"]["last_live_aggregate"] = aggregate_samples


def drain():
    for item in TRANSPORT:
        size = os.fstat(item["fd"]).st_size
        require(0 <= size <= STREAM, "kernel transport slot cap exceeded")
        while item["offset"] < size:
            due()
            item["pending_chunk"] = os.pread(item["fd"], min(4096, size - item["offset"]), item["offset"])
            chunk = item["pending_chunk"]
            require(bool(chunk), "short owned transport")
            next_offset = item["offset"] + len(chunk)
            saved = signal.pthread_sigmask(signal.SIG_BLOCK, REGISTRATION_SIGNALS)
            try:
                item["data"].extend(chunk)
                item["offset"] = next_offset
                item["pending_chunk"] = None
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, saved)
        require(sum(len(x["data"]) for x in TRANSPORT) <= OUTPUT, "aggregate transport cap")
    R["output"] = {item["label"]: transport_projection(item) for item in TRANSPORT}


def transport_projection(item):
    raw = bytes(item["data"])
    inner = R.get("inner_complete_evidence")
    framed = (item["label"] == "stdout" and type(inner) is dict and
              encode_channel_frame(inner, STREAM) == raw)
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "raw_base64": None if framed else base64.b64encode(raw).decode("ascii"),
            "encoding": "channel-frame-of-inner" if framed else "base64",
            "produced_bytes": item.get("produced_bytes"),
            "partial": bool(item.get("partial")),
            "pending_raw_base64": base64.b64encode(item.get("pending_chunk") or b"").decode("ascii"),
            "retain_error": item.get("retain_error")}


def restore_transport_raw(record, cell):
    if cell["encoding"] == "channel-frame-of-inner":
        require(cell["raw_base64"] is None and type(record.get("inner_complete_evidence")) is dict,
                "original framed inner preimage absent")
        raw = encode_channel_frame(record["inner_complete_evidence"], STREAM)
    elif cell["encoding"] == "same-inner-json-preimage":
        require(cell["raw_base64"] is None and type(record.get("inner_complete_evidence")) is dict,
                "original same inner preimage absent")
        raw = json.dumps(record["inner_complete_evidence"], ensure_ascii=True,
                         allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"
    else:
        require(cell["encoding"] == "base64" and type(cell["raw_base64"]) is str,
                "full transport raw encoding required")
        raw = base64.b64decode(cell["raw_base64"], validate=True)
    require(len(raw) == cell["bytes"] and hashlib.sha256(raw).hexdigest() == cell["sha256"],
            "recovered full transport byte/count/SHA mismatch")
    return raw


def take_reserved(key, index=None):
    if index is None:
        fd = LAUNCH_RESERVE[key]
        LAUNCH_RESERVE[key] = None
        return fd
    fd = LAUNCH_RESERVE[key][index]
    LAUNCH_RESERVE[key][index] = None
    return fd


def close_taken(fd, where):
    return close_allocation(fd, where)

def close_launch_reserve():
    saved = signal.pthread_sigmask(signal.SIG_BLOCK, REGISTRATION_SIGNALS)
    try:
        for key in ("null", "probe"):
            close_taken(take_reserved(key), "launch_reserve:" + key)
        for key in ("copies", "gate"):
            for index in range(len(LAUNCH_RESERVE[key])):
                close_taken(take_reserved(key, index), "launch_reserve:" + key)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, saved)


def retain_produced_transport():
    # Bytes already in the memfd stay in the owner buffer before any close.
    # A complete drain is left untouched. A short prefix is partial, not success.
    total = sum(len(item["data"]) for item in TRANSPORT)
    for item in TRANSPORT:
        # Never overwrite an uncommitted returned chunk or retry a failed read.
        if item.get("pending_chunk") is not None or item.get("retain_error_object") is not None:
            item["partial"] = True
            continue
        try:
            if item.get("fd") is None:
                continue
            try:
                size = os.fstat(item["fd"]).st_size
            except BaseException as exc:
                item["partial"] = True
                item["retain_error_object"] = exc
                item["retain_error"] = deferred_error(exc)
                size = None
            item["produced_bytes"] = size
            if size is None:
                continue
            while item["offset"] < size and len(item["data"]) < STREAM and total < OUTPUT:
                room = min(4096, STREAM - len(item["data"]), OUTPUT - total, size - item["offset"])
                if room <= 0:
                    break
                try:
                    item["pending_chunk"] = os.pread(item["fd"], room, item["offset"])
                    chunk = item["pending_chunk"]
                except BaseException as exc:
                    item["partial"] = True
                    item["retain_error_object"] = exc
                    item["retain_error"] = deferred_error(exc)
                    break
                if not chunk:
                    item["partial"] = True
                    item["prefix_end"] = "SHORT"
                    item["retain_error"] = "short transport"
                    break
                next_offset = item["offset"] + len(chunk)
                saved = signal.pthread_sigmask(signal.SIG_BLOCK, REGISTRATION_SIGNALS)
                try:
                    item["data"].extend(chunk)
                    item["offset"] = next_offset
                    item["pending_chunk"] = None
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, saved)
                total += len(chunk)
            if item["offset"] < size:
                item["partial"] = True
            elif item.get("pending_chunk") is None and item.get("retain_error") is None:
                # Completion is about the settled exact producer AND full stable FD.
                item["partial"] = not (CHILD is not None and CHILD["reaped"])
        except BaseException as exc:
            item["retain_error_object"] = exc
            item["partial"] = True
            item["retain_error"] = deferred_error(exc)
            # The next independent cell is still attempted before any FD close.
    for item in TRANSPORT:
        settled = (item.get("partial") is False and item.get("pending_chunk") is None
                   and item.get("retain_error_object") is None
                   and item.get("produced_bytes") is not None
                   and len(item.get("data") or b"") == item.get("produced_bytes"))
        if not settled:
            hold_unread_prefix(getattr(item.get("fd"), "allocation", None))
    first_projection = None
    for item in TRANSPORT:
        try:
            R["output"][item["label"]] = transport_projection(item)
        except BaseException as exc:
            item["retain_error_object"] = exc
            item["partial"] = True
            item["retain_error"] = deferred_error(exc)
            first_projection = first_projection or exc
    if first_projection is not None:
        raise first_projection


def register_parent(origin):
    pid = CHILD["pid"]
    require(type(pid) is int and pid > 0 and not CHILD["reaped"],
            "exact unreaped returned/recovered child PID required")
    CHILD["creation_origin"] = origin
    if CHILD["procfd"] is None:
        CHILD["procfd"] = open_proc(pid)
    before = starttime(pid, CHILD["procfd"])
    require(before[0] == os.getpid(), "created verifier is not our actual direct child")
    CHILD["start_ticks"] = before[1]
    if CHILD["pidfd"] is None:
        CHILD["pidfd"] = acquire_owned(os.pidfd_open, "pidfd", pid, 0)
    require(starttime(pid, CHILD["procfd"]) == before
            and pidfd_number(CHILD["pidfd"]) == pid,
            "created verifier held pidfd/proc generation not stable")
    CHILD["evidence"].update(pid=pid, start_ticks=before[1],
                              creation_origin=origin, exact_generation_registered=True)
    return CHILD


def recover_pending_fork(end):
    # Recovery is narrow and qualified: empty direct-child baseline, one
    # exclusive fork site and the pre-created held gate shared with that child.
    # No caller PID, guessed child number, process group or broad kill.
    require(EMPTY_PRE_FORK and time.monotonic_ns() < end,
            "pending fork recovery lacks confirmed empty/exclusive launch boundary")
    actual = children(os.getpid())
    if len(actual) != 1:
        unknown("fork return unavailable; exact created child cannot be established")
    CHILD["pid"] = actual[0]
    CHILD["procfd"] = open_proc(actual[0])
    before = starttime(actual[0], CHILD["procfd"])
    require(before[0] == os.getpid(), "recovered gate child parent mismatch")
    CHILD["creation_origin"] = "confirmed exclusive-launch direct-child recovery"
    CHILD["start_ticks"] = before[1]
    CHILD["evidence"].update(pid=actual[0], start_ticks=before[1],
                              creation_origin=CHILD["creation_origin"])
    if LAUNCH_RESERVE["gate"][0] is not None:
        gate = os.stat("fd/" + str(LAUNCH_RESERVE["gate"][0]), dir_fd=CHILD["procfd"])
        require({"dev": gate.st_dev, "ino": gate.st_ino, "mode": gate.st_mode}
                == LAUNCH_RESERVE["gate_identity"], "recovered child does not hold our exact gate")
    else:
        require(not CHILD["gate_release_written"],
                "lost returned PID after effects cannot use unreleased-gate recovery")
    require(starttime(actual[0], CHILD["procfd"]) == before,
            "recovered gate lineage changed before registration")
    register_parent("confirmed held-gate direct-child recovery")
    return CHILD


def child_startup_gate(end, saved_mask):
    # No verifier exec or input/archive/tool effect precedes the sole one-byte
    # parent release after stable exact PID/pidfd/proc registration.
    # This child address space clears its own slot copy before close. The
    # parent table is a separate reference and retires through close_launch_reserve.
    write_end = take_reserved("gate", 1)
    close_taken(write_end, "child_gate_write")
    readfd = take_reserved("gate", 0)
    while time.monotonic_ns() < end:
        try:
            token = os.read(readfd, 1)
        except BlockingIOError:
            select.select([readfd], [], [], min(0.01, max(0, (end - time.monotonic_ns()) / 1e9)))
            continue
        charge_child_runtime(len(token))
        if token == b"\x01":
            close_taken(readfd, "child_gate_read")
            return
        os._exit(125)  # EOF/refused release; still an exact reapable direct child
    os._exit(125)


def check_launch_contract(argv, mode, name):
    fd = HELD["contract"]["fd"]
    size = PINS["contract"][1]
    raw = os.pread(fd, size + 1, 0)
    require(len(raw) == size and size <= 262144, "complete held contract read required")
    def bad_constant(value):
        raise Refusal("nonfinite contract JSON")
    contract = json.loads(raw.decode("ascii"), object_pairs_hook=pairs, parse_constant=bad_constant)
    template = [
        "prlimit", "--as=117440512:117440512", "--cpu=120:120",
        "--fsize=32768:32768", "--nofile=128:128", "--core=0:0", "--",
        "/proc/self/fd/4", "-I", "-S", "-B", "/proc/self/fd/5",
        "--reviewed-source-sha256", SOURCE_SHA, "--reviewed-contract-sha256", CONTRACT_HASH_BINDING,
        "--deadline-monotonic-ns", DEADLINE_BINDING, "--private-home-name", HOME_BINDING,
        "--child-bootstrap-read-debit", str(CHILD_READ_DEBIT)]
    require(contract["assignment"] == ASSIGNMENT and contract["generation"] == 1
            and contract["disposition"] == "INERT_CONNECTED_SOURCE_PENDING_WHOLE_REVIEW_AND_NEW_ROOT_ADMISSION"
            and contract["source"]["path"] == PINS["source"][0]
            and contract["source"]["sha256"] == SOURCE_SHA
            and contract["source"]["bytes"] == PINS["source"][1]
            and contract["own_runtime_fd"] == OWN_RUNTIME_FD
            and contract["resource_receipt_schemas"] == {
                "supervisor": "friday.e4.node.supervisor-resources.a172.v1",
                "verifier": "friday.e4.node.verifier-resources.a172.v1"}
            and contract["readonly_keyring_policy"]["schema"] == "friday.e4.node.readonly-keyring.a172.v1"
            and contract["parent_exec"]["argv_template"] == template,
            "complete current contract/source/argv fixed linkage mismatch")
    resolved = [CONTRACT_SHA if value == CONTRACT_HASH_BINDING else
                str(INNER_DEADLINE) if value == DEADLINE_BINDING else
                name if value == HOME_BINDING else value for value in template]
    if mode == "startup-as-negative":
        resolved[1] = "--as=67108864:67108864"
    require(argv == resolved, "actual launcher vector differs from acyclic fixed contract")
    R["contract_argv_linkage"] = {
        "source_sha256": SOURCE_SHA, "complete_contract_sha256": CONTRACT_SHA,
        "source_value_argv_index": 13, "contract_flag_argv_index": 14,
        "contract_value_argv_index": 15, "verifier_source_value_index": 2,
        "verifier_contract_flag_index": 3, "verifier_contract_value_index": 4,
        "caller_overrides": False, "whole_contract_checked": True}


def launch(mode):
    global CHILD, FORK_STATE, EMPTY_PRE_FORK, STOP
    # Allocate the complete record and all reserve slots BEFORE any fork.
    evidence = {"pid": None, "start_ticks": None, "creation_origin": None,
                "exact_generation_registered": False, "gate_release_written": False,
                "pre_fork_children_empty": None, "signal_mask_during_registration": None,
                "signal_mask_restored": False, "exit_confirmed": False, "reaped": False,
                "sample": None, "missing_reason": "owned parent not created"}
    CHILD = {"pid": None, "pidfd": None, "procfd": None, "start_ticks": None,
             "creation_origin": None, "gate_release_written": False, "reaped": False,
             "samples": 0, "sample": None, "missing_reason": "no complete live sample yet",
             "as_bytes": 67108864 if mode == "startup-as-negative" else PARENT_AS,
             "wait_status": None, "wait4_rusage": None, "evidence": evidence}
    R["fork_registration"] = evidence
    R["child_read_carry"] = {"kind": "FIXED_CONSERVATIVE_UPPER_BOUND_NOT_MEASURED",
                             "reserved_bytes": CHILD_READ_DEBIT,
                             "observed_child_bytes": None, "observation": "UNKNOWN_NOT_ZERO",
                             "copied_parent_prefix_included": False,
                             "delivery": "literal reviewed argv and pinned capsule contract"}
    pfd = component("/var/tmp")
    name = "friday-a048-g1-" + os.urandom(16).hex()
    HOME.update(state="PLANNED", name=name, path="/var/tmp/" + name,
                parent_identity=ident(os.fstat(pfd), True), parent_fd=pfd)
    try:
        os.stat(name, dir_fd=pfd, follow_symlinks=False)
    except FileNotFoundError:
        HOME["prelaunch_absent"] = True
    else:
        raise Refusal("supervisor private name already exists; never launch/delete")
    for label in ("stdout", "stderr"):
        item = {"label": label, "fd": None, "offset": 0, "data": bytearray(),
                "pending_chunk": None, "retain_error_object": None, "retain_error": None,
                "produced_bytes": None, "partial": True}
        TRANSPORT.append(item)
        item["fd"] = acquire_owned(os.memfd_create, "memfd", "A048-supervised-" + label, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    devparent = component("/dev")
    LAUNCH_RESERVE["null"] = acquire_owned(os.open, "open", "null", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=devparent)
    nullfd = LAUNCH_RESERVE["null"]
    nullstat = os.fstat(nullfd)
    require(stat.S_ISCHR(nullstat.st_mode) and nullstat.st_rdev == os.makedev(1, 3)
            and nullstat.st_uid == 0, "trusted /dev/null device mismatch")
    for index, tool in enumerate(("prlimit", "python", "source", "contract")):
        LAUNCH_RESERVE["copies"][index] = acquire_owned(fcntl.fcntl, "launch_copy", HELD[tool]["fd"], fcntl.F_DUPFD_CLOEXEC, 16)
    LAUNCH_RESERVE["gate"][:] = acquire_pipe("launch_gate")
    gate = os.fstat(LAUNCH_RESERVE["gate"][0])
    LAUNCH_RESERVE["gate_identity"] = {"dev": gate.st_dev, "ino": gate.st_ino, "mode": gate.st_mode}
    as_bytes = CHILD["as_bytes"]
    env = dict(ENV)
    if mode == "startup-env-negative":
        del env["LC_ALL"]
    argv = ["prlimit", "--as=" + str(as_bytes) + ":" + str(as_bytes), "--cpu=120:120",
            "--fsize=32768:32768", "--nofile=128:128", "--core=0:0", "--",
            "/proc/self/fd/4", "-I", "-S", "-B", "/proc/self/fd/5",
            "--reviewed-source-sha256", SOURCE_SHA, "--reviewed-contract-sha256", CONTRACT_SHA,
            "--deadline-monotonic-ns", str(INNER_DEADLINE), "--private-home-name", name,
            "--child-bootstrap-read-debit", str(CHILD_READ_DEBIT)]
    check_launch_contract(argv, mode, name)
    R["actual_launch"] = {"argv": argv, "env": env, "shell": False,
                           "as_soft_hard": [as_bytes, as_bytes], "fd_map": [3, 4, 5, 6]}
    bind_runtime_collision()
    custody("immediately_before_fork")
    due(25)
    require(children(os.getpid()) == [], "unknown child at exact fork boundary")
    EMPTY_PRE_FORK = True
    evidence["pre_fork_children_empty"] = True
    gate_end = min(INNER_DEADLINE, time.monotonic_ns() + 5000000000)
    saved_mask = signal.pthread_sigmask(signal.SIG_BLOCK, REGISTRATION_SIGNALS)
    try:
        evidence["signal_mask_during_registration"] = sorted(
            int(value) for value in signal.pthread_sigmask(signal.SIG_BLOCK, set()))
        require(signal.SIGALRM in evidence["signal_mask_during_registration"]
                and signal.SIGINT in evidence["signal_mask_during_registration"],
                "installed asynchronous handlers not blocked at fork boundary")
        FORK_STATE = "PENDING"
        try:
            # Existing dict slot receives the syscall result immediately:
            # no new record/container is constructed after a successful fork.
            require(commit_outside_body(), "outside body commit before fork")
            CHILD["pid"] = os.fork()
        except OSError:
            if children(os.getpid()) == []:
                FORK_STATE = "DEFINITELY_NOT_CREATED"
            else:
                FORK_STATE = "UNKNOWN_CREATION"
                recover_pending_fork(gate_end)
                unknown("fork OS error disagrees with actual child set; exact child retained")
            raise
        except BaseException:
            FORK_STATE = "UNKNOWN_CREATION"
            recover_pending_fork(gate_end)
            unknown("fork return failed; recovered exact child retained for bounded cleanup")
        if CHILD["pid"] == 0:
            try:
                child_startup_gate(gate_end, saved_mask)
                resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
                resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
                resource.setrlimit(resource.RLIMIT_FSIZE, (STREAM, STREAM))
                resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                os.dup2(nullfd, 0, inheritable=True)
                os.dup2(TRANSPORT[0]["fd"], 1, inheritable=True)
                os.dup2(TRANSPORT[1]["fd"], 2, inheritable=True)
                role_meta = [runtime_metadata(fd) for fd in LAUNCH_RESERVE["copies"]]
                for old, new in zip(LAUNCH_RESERVE["copies"], (3, 4, 5, 6)):
                    os.dup2(old, new, inheritable=True)
                os.closerange(7, 8)
                os.closerange(9, 128)
                child_canonical_closed(role_meta)
                # Deliberate child policy, restored only after the startup gate.
                signal.setitimer(signal.ITIMER_REAL, 0)
                for signum in (signal.SIGALRM, signal.SIGINT, signal.SIGPIPE):
                    signal.signal(signum, signal.SIG_DFL)
                signal.pthread_sigmask(signal.SIG_SETMASK, saved_mask)
                os.execve(3, argv, env)
            except BaseException:
                os._exit(125)
        FORK_STATE = "CONFIRMED_CREATED"
        CHILD["creation_origin"] = "os.fork exact return"
        evidence["pid"] = CHILD["pid"]
        register_parent("os.fork exact return")
        observe_owned()  # actual held generation, still behind the bounded gate
        require(time.monotonic_ns() < gate_end, "exact registration missed bounded startup gate")
        require(os.write(LAUNCH_RESERVE["gate"][1], b"\x01") == 1, "startup gate release failed")
        CHILD["gate_release_written"] = evidence["gate_release_written"] = True
    except BaseException:
        if FORK_STATE in ("PENDING", "UNKNOWN_CREATION", "CONFIRMED_CREATED"):
            # Even a failed narrow registration leaves the exact reserved record
            # and gate in custody. Closing the gate prevents an unadmitted exec.
            evidence["registration_failed"] = True
            STOP = True
        raise
    finally:
        try:
            close_launch_reserve()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, saved_mask)
            evidence["signal_mask_restored"] = True
    custody("immediately_after_launch")


def wait_exact(pid, end):
    while time.monotonic_ns() < end:
        try:
            got, status, usage = os.wait4(pid, os.WNOHANG)
        except ChildProcessError:
            unknown("exact owned/adopted child wait ownership unconfirmed")
        if got == pid:
            return status, usage
        select.select([], [], [], min(0.02, max(0, (end - time.monotonic_ns()) / 1e9)))
    unknown("finite exact owned child reap failed")


def finish_parent():
    require(CHILD["pidfd"] is not None and exited(CHILD["pidfd"]), "parent exit not pidfd-confirmed")
    status, usage = wait_exact(CHILD["pid"], min(DEADLINE - 5000000000, time.monotonic_ns() + 5000000000))
    CHILD.update(reaped=True, wait_status=status, wait4_rusage=usage)
    CHILD["evidence"].update(reaped=True, exit_confirmed=True)
    R["parent_exit"] = {"exit_code": os.waitstatus_to_exitcode(status),
                        "raw_wait4_ru_maxrss_kib": usage.ru_maxrss,
                        "meaning": "Linux maximum resident high-water of waited process including its accounted descendants; not a synthetic sum",
                        "complete_live_samples": CHILD["samples"]}
    require(usage.ru_maxrss >= 0 and usage.ru_maxrss <= PARENT_AS // 1024,
            "noncompliant actual supervised high-water")
    require(CHILD["samples"] > 0, "mandatory actual live verifier sample never obtained")


def cleanup_problem(why, original=None):
    global STOP
    STOP = True
    R.setdefault("owned_cleanup_problems", []).append(str(why))
    if original is not None:
        R.setdefault("owned_cleanup_original_errors", []).append(deferred_error(original))


def terminate_owned():
    # One absolute <=5s reserve, shared by normal/exception/final calls. Handler
    # deferral protects known exact stop/reap bookkeeping, not a wider runtime.
    global CLEANUP_END
    if CLEANUP_END is None:
        CLEANUP_END = min(DEADLINE - 5000000000, time.monotonic_ns() + 5000000000)
    saved = signal.pthread_sigmask(signal.SIG_BLOCK, REGISTRATION_SIGNALS)
    try:
        terminate_owned_masked(CLEANUP_END)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, saved)


def terminate_owned_masked(end):
    if CHILD is None:
        require(FORK_STATE == "NOT_ATTEMPTED", "created process has no reserved ownership record")
        return
    if CHILD["pid"] is None:
        if FORK_STATE in ("PENDING", "UNKNOWN_CREATION"):
            try:
                recover_pending_fork(end)
            except BaseException as exc:
                cleanup_problem("exact fork recovery failed: " + type(exc).__name__ + ": " + str(exc), exc)
        elif FORK_STATE not in ("NOT_ATTEMPTED", "DEFINITELY_NOT_CREATED"):
            cleanup_problem("fork state has no exact retained child PID")
        close_launch_reserve()  # refused gate cannot admit child execution
        if CHILD["pid"] is None:
            if FORK_STATE in ("PENDING", "UNKNOWN_CREATION"):
                unknown("fork creation remains unknown; finite cleanup not confirmed")
            require(children(os.getpid()) == [], "no-creation state disagrees with actual child set")
            return
    close_launch_reserve()
    if not CHILD["reaped"]:
        require(time.monotonic_ns() < end, "finite owned-cleanup reserve exhausted")
        if CHILD["pidfd"] is None:
            # The only numeric signal fallback is the exact unreaped direct PID
            # returned by this fork (or proven empty/exclusive held-gate recovery).
            # The child never received gate release. waitid WNOWAIT proves actual
            # current wait ownership; no reaper can recycle it before our wait4.
            require(not CHILD["gate_release_written"]
                    and CHILD["creation_origin"] in (
                        "os.fork exact return", "confirmed held-gate direct-child recovery",
                        "confirmed exclusive-launch direct-child recovery"),
                    "unregistered process lacks exact unreleased direct ownership")
            try:
                os.waitid(os.P_PID, CHILD["pid"], os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                unknown("unreleased direct-child wait ownership is not confirmed")
            try:
                os.kill(CHILD["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
            status, usage = wait_exact(CHILD["pid"], end)
            CHILD.update(reaped=True, wait_status=status, wait4_rusage=usage)
            CHILD["evidence"].update(reaped=True, exit_confirmed=True,
                                      cleanup_basis="exact unreleased direct-child wait ownership")
        else:
            require(pidfd_number(CHILD["pidfd"]) == CHILD["pid"],
                    "unreaped parent pidfd no longer has its exact kernel PID")
            try:
                if not exited(CHILD["pidfd"]):
                    signal.pidfd_send_signal(CHILD["pidfd"], signal.SIGSTOP)
                    stopped = False
                    while time.monotonic_ns() < end:
                        if exited(CHILD["pidfd"]):
                            break
                        values = kernel_at(CHILD["procfd"], "status")
                        require(starttime(CHILD["pid"], CHILD["procfd"]) ==
                                (os.getpid(), CHILD["start_ticks"]), "stop observation changed generation")
                        if re.search(rb"(?m)^State:\s+[Tt]\b", values):
                            stopped = True
                            break
                        select.select([], [], [], min(0.01, max(0, (end - time.monotonic_ns()) / 1e9)))
                    if not stopped and not exited(CHILD["pidfd"]):
                        cleanup_problem("exact parent stop not confirmed")
                    if stopped:
                        for record in enumerate_owned(CHILD["pid"], CHILD["procfd"]):
                            if time.monotonic_ns() >= end:
                                cleanup_problem("finite child-stop reserve exhausted")
                                break
                            if verify_generation(record, CHILD["pid"]):
                                signal.pidfd_send_signal(record["pidfd"], signal.SIGKILL)
            except BaseException as exc:
                # Preserve the problem and still perform safe known-parent
                # shutdown. Adopted enumeration below can establish newly seen
                # exact generations, but never erases sticky uncertainty.
                cleanup_problem("frozen child registration/stop failed: " + type(exc).__name__ + ": " + str(exc), exc)
            finally:
                if not exited(CHILD["pidfd"]) and time.monotonic_ns() < end:
                    signal.pidfd_send_signal(CHILD["pidfd"], signal.SIGKILL)
            status, usage = wait_exact(CHILD["pid"], end)
            CHILD.update(reaped=True, wait_status=status, wait4_rusage=usage)
            CHILD["evidence"].update(reaped=True, exit_confirmed=True)
        R.setdefault("parent_exit", {
            "exit_code": os.waitstatus_to_exitcode(CHILD["wait_status"]),
            "raw_wait4_ru_maxrss_kib": CHILD["wait4_rusage"].ru_maxrss,
            "complete_live_samples": CHILD["samples"], "cleanup_path": True})
    # Verified subreaper + empty baseline + sole exclusive launch establish the
    # actual adopted lineage. New/old same-number generations are reconciled
    # separately before any signal or wait, including previously unseen adoptees.
    require(EMPTY_PRE_FORK and R["kernel_policy"]["child_subreaper"] is True,
            "adopted lineage trust/baseline missing")
    adopted = enumerate_owned(os.getpid(), None, adopted=True)
    for record in adopted:
        try:
            require(time.monotonic_ns() < end, "finite adopted-child cleanup reserve exhausted")
            if verify_generation(record, os.getpid()):
                signal.pidfd_send_signal(record["pidfd"], signal.SIGKILL)
            status, usage = wait_exact(record["pid"], end)
            record["reaped"] = True
            record_exit(record, "exact adopted generation waited by supervisor")
            record["evidence"].update(
                reaped_by_supervisor=True, reap_owner="confirmed subreaper adoption and exact wait4",
                exit_code=os.waitstatus_to_exitcode(status), raw_wait4_ru_maxrss_kib=usage.ru_maxrss)
        except BaseException as exc:
            cleanup_problem("actual adopted generation cleanup failed: " + type(exc).__name__ + ": " + str(exc), exc)
    for record in TRACKED:
        if record["pidfd"] is None:
            cleanup_problem("enumerated generation lacks its exact held pidfd")
            continue
        if not exited(record["pidfd"]):
            cleanup_problem("held owned generation exit remains unconfirmed")
        else:
            record_exit(record, "that exact held generation terminal exit confirmed")
            if pidfd_number(record["pidfd"]) == -1 and not record["reaped"]:
                record["evidence"]["reap_owner"] = (
                    "verifier-owned generation no longer associated with kernel PID; inner call reap checked separately")
    actual_empty = children(os.getpid()) == []
    R["all_generation_registration_bound_to_actual_enumeration"] = (
        all(record["pidfd"] is not None and record["start_ticks"] is not None for record in TRACKED))
    R["owned_cleanup"] = {
        "exact_verifier_reaped": CHILD["reaped"], "actual_supervisor_children_empty": actual_empty,
        "all_held_generation_exits_confirmed": all(
            record["pidfd"] is not None and exited(record["pidfd"]) for record in TRACKED),
        "adopted_generations_exactly_waited": all(record["reaped"] for record in adopted),
        "no_unowned_pid_kill": True, "single_absolute_cleanup_end_ns": end}
    if not actual_empty:
        cleanup_problem("actual owned/adopted child set remains nonempty")
    if STOP:
        unknown("one or more exact-owned cleanup/registration observations remain unconfirmed")


def post_home():
    if HOME["name"] is None:
        HOME["postexit_absent"] = None
        return
    pfd = HOME["parent_fd"]
    require(ident(os.fstat(pfd), True) == HOME["parent_identity"], "home parent identity changed")
    try:
        os.stat(HOME["name"], dir_fd=pfd, follow_symlinks=False)
    except FileNotFoundError:
        HOME.update(state="POSTEXIT_ABSENT_CONFIRMED", postexit_absent=True)
    else:
        HOME.update(state="PRESENT_OWNERSHIP_NOT_ADMITTED", postexit_absent=False)
        unknown("exact planned home remains; supervisor never deletes unknown/EEXIST target")


def pairs(rows):
    answer = {}
    for key, value in rows:
        require(key not in answer, "duplicate JSON key")
        answer[key] = value
    return answer


def strict_receipt(data):
    require(0 < len(data) <= STREAM and data.endswith(b"\n") and not data.endswith(b"\n\n"),
            "complete bounded single JSON plus one newline required")
    text = data.decode("ascii", "strict")
    def bad_constant(value):
        raise Refusal("nonfinite JSON number")
    decoder = json.JSONDecoder(object_pairs_hook=pairs, parse_constant=bad_constant)
    value, end = decoder.raw_decode(text)
    require(end == len(text) - 1 and isinstance(value, dict), "trailing/incomplete JSON")
    def finite(item):
        if type(item) is float:
            require(math.isfinite(item), "nonfinite decoded JSON number")
        elif type(item) is dict:
            for child in item.values():
                finite(child)
        elif type(item) is list:
            for child in item:
                finite(child)
    finite(value)
    validate_received_error_graphs(value)
    return value


def validate_common_inner(inner, mode):
    require(inner["schema"] == "friday.e4.node.bounded-verifier.sol062.v1"
            and type(inner["generation"]) is int and inner["generation"] == 1,
            "complete current inner schema/generation required")
    require(not any(key in inner for key in (
        "resource_failure", "custody_failure", "cleanup_failure")),
        "inner terminal resource/custody/cleanup failure cannot qualify a control or positive")
    invocation = inner["reviewed_invocation"]
    require(invocation["argv"] == R["actual_launch"]["argv"][11:]
            and invocation["source_sha256"] == SOURCE_SHA
            and invocation["contract_sha256"] == CONTRACT_SHA
            and invocation["deadline_monotonic_ns"] == INNER_DEADLINE
            and invocation["private_home_name"] == HOME["name"],
            "complete current inner invocation/hash/home/deadline mismatch")
    startup = inner["startup_observations"]
    actual_env = startup["env"]
    if mode == "startup-env-negative":
        # Missing LC_ALL may make trusted Python add LC_CTYPE during startup.
        # Preserve that actual observation without calling it injection or
        # replacing it with the intended pre-exec mapping. The intended guard
        # remains exact ENV refusal with absent LC_ALL and zero GPG calls.
        require(type(actual_env) is dict and actual_env.get("PATH") == ENV["PATH"]
                and actual_env.get("LANG") == ENV["LANG"] and "LC_ALL" not in actual_env
                and set(actual_env) <= {"PATH", "LANG", "LC_CTYPE"},
                "actual omitted-LC_ALL startup environment cause mismatch")
        if "LC_CTYPE" in actual_env:
            require(type(actual_env["LC_CTYPE"]) is str and bool(actual_env["LC_CTYPE"]),
                    "actual startup locale observation missing")
    else:
        require(actual_env == R["actual_launch"]["env"],
                "actual positive/startup-AS environment mismatch")
    require(startup["resuid"] == [1000, 1000, 1000]
            and startup["resgid"] == [1000, 1000, 1000]
            and startup["limits"] == {
                "AS": [CHILD["as_bytes"], CHILD["as_bytes"]], "CPU": [120, 120],
                "FSIZE": [STREAM, STREAM], "NOFILE": [128, 128], "CORE": [0, 0]},
            "complete actual startup environment/credential/limit observations mismatch")
    phases = {"startup", "before_cleanup"}
    if mode == "verify-node":
        phases.update("before_call_" + str(index) for index in range(1, 7))
        phases.add("verification_end")
    require(inner["resource_schema"] == "friday.e4.node.verifier-resources.a172.v1" and
            type(inner["resources"]) is dict and set(inner["resources"]) == phases,
            "complete original verifier resource phase schema required")
    for observation in inner["resources"].values():
        require(type(observation) is dict and set(observation) == {
            "ru_maxrss_self_kib", "ru_maxrss_largest_child_kib", "current_proc_self_status", "elapsed_seconds"},
            "complete verifier self-phase resource fields required")
        own, child = observation["ru_maxrss_self_kib"], observation["ru_maxrss_largest_child_kib"]
        require(type(own) in (int, float) and type(child) in (int, float)
                and own >= 0 and child >= 0, "missing/nonnumeric actual inner high-water")
        current = observation["current_proc_self_status"]
        require(set(current) == {"VmSize_kib", "VmRSS_kib", "VmHWM_kib", "VmPeak_kib"}
                and all(type(value) is int and 0 <= value <= CHILD["as_bytes"] // 1024
                        for value in current.values()), "inner complete numeric memory sample required")
        elapsed = observation["elapsed_seconds"]
        require(type(elapsed) in (int, float) and 0 <= elapsed <= 900,
                "actual inner observation elapsed bound missing")
        sup = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        require(own <= CHILD["as_bytes"] // 1024 and child <= PARENT_AS // 1024
                and sup + own + child <= 262144,
                "actual aggregate supervisor+inner high-water exceeds256MiB")
    output, scratch = (inner["aggregate_gpg_output_bytes_including_written_key_and_body"],
                       inner["own_scratch_bytes_final_before_cleanup"])
    require(inner.get("aggregate_gpg_output_charge_unknown") is False,
            "unobserved produced output is not an observed zero")
    total = 0
    for call_item in inner["calls"]:
        require(call_item.get("capture_complete") is True and
                call_item.get("output_charge_unknown") is False and
                call_item.get("failure") is None and not call_item.get("secondary_errors"),
                "complete terminal raw/error-capture schema required")
        cells = [call_item[name] for name in ("stdout", "stderr", "status")] + call_item["written_data"]
        for cell in cells:
            if cell["encoding"] == "utf-8":
                raw = cell["text"].encode("utf-8", "strict")
            else:
                require(cell["encoding"] == "base64" and cell["text"] is None,
                        "lossless original raw encoding required")
                raw = base64.b64decode(cell["raw_base64"], validate=True)
            require(cell["retained"] is True and cell["partial"] is False and
                    cell["bytes"] == cell["produced_bytes"] == cell["output_charge_bytes"] == len(raw)
                    and hashlib.sha256(raw).hexdigest() == cell["sha256"] and
                    cell["identity_before"] == cell["identity_after"],
                    "complete fullraw/count/SHA/custody/charge required")
            total += len(raw)
    require(total == output, "produced output aggregate differs from complete raw cells")
    require(type(output) is int and 0 <= output <= OUTPUT
            and type(scratch) is int and 0 <= scratch <= 16515072,
            "actual inner aggregate output/scratch observation missing or noncompliant")
    require(R["custody_postexit"] is True and HOME["postexit_absent"] is True
            and R["owned_cleanup"]["exact_verifier_reaped"] is True
            and R["owned_cleanup"]["actual_supervisor_children_empty"] is True
            and R["owned_cleanup"]["all_held_generation_exits_confirmed"] is True
            and R["all_generation_registration_bound_to_actual_enumeration"] is True,
            "independent final custody/home/exact generation cleanup observations incomplete")
    validate_inner_read_accounting(inner)
    R["inner_evidence_validation"]["common_observations_validated"] = True


def validate_inner_read_accounting(inner):
    account = inner["explicit_read_accounting"]
    spec = OWN_RUNTIME_FD["read_accounting"]
    require(account["schema"] == spec["schema"]
            and account["child_bootstrap_upper_debit_bytes"] == CHILD_READ_DEBIT
            and account["child_debit_kind"] == spec["child_debit_kind"]
            and account["child_observed_bytes"] is None
            and account["child_observation"] == "UNKNOWN_NOT_ZERO"
            and account["copied_parent_prefix_included"] is False
            and account["implicit_io"] == "UNKNOWN_NOT_ZERO"
            and account["reset_or_subtraction"] is False
            and account["budget_bytes"] == spec["verifier_whole_content_ceiling_bytes"],
            "child/runtime read-accounting provenance mismatch")
    local = account["performing_verifier_content_bytes"]
    runtime = inner["runtime_explicit_read_bytes"]
    whole = account["whole_content_charge_bytes"]
    final_read_upper = inner["publication_transport_costs"]["final_read_upper_bytes"]
    require(final_read_upper == STREAM and whole + final_read_upper <= account["budget_bytes"],
            "known verifier prefix plus ONE future readback fits original whole room")
    require(type(local) is int and type(runtime) is int and type(whole) is int
            and 0 < runtime <= local and runtime <= spec["runtime_content_ceiling_bytes"]
            and whole == CHILD_READ_DEBIT + local and whole <= account["budget_bytes"]
            and type(account["runtime_directory_scans"]) is int
            and account["runtime_directory_scans"] >= 2
            and inner["runtime_read_reset_or_subtracted"] is False
            and account["canonical_inherited_before_imports"] == [0, 1, 2, 3, 4, 5, 6]
            and inner["reviewed_invocation"]["child_bootstrap_read_debit"] == CHILD_READ_DEBIT,
            "performing-process cumulative whole read evidence incomplete")
    role = inner["runtime_fd"]
    require(role["role"] == "own_stock_runtime" and role["ambient_input"] is False
            and role["image_proof"] is False and role["canonical_roles_unchanged"] is True
            and role["sha256_pin"] == FFI_SHA and type(role["present"]) is bool,
            "inner runtime role/provenance mismatch")
    if role["present"]:
        require(type(role["fd"]) is int and 7 <= role["fd"] < 128
                and role["hash_check"] == "FULL_OBSERVED" and role["path"] == FFI_PATH
                and role["sha256"] == FFI_SHA and role["size"] == FFI_SIZE
                and role["provenance"] ==
                "ABSENT_AT_STRICT_INHERITED_ENTRY_NEW_DURING_VERIFIER_IMPORTS"
                and type(role["status_flags"]) is int
                and role["status_flags"] & os.O_ACCMODE == os.O_RDONLY
                and not role["status_flags"] & (os.O_PATH | os.O_APPEND)
                and role["descriptor_flags"] == fcntl.FD_CLOEXEC
                and len(role["metadata"]) == 9 and role["metadata"][2] == FFI_SIZE
                and role["metadata"][5:7] == [0, 0] and role["metadata"][8] == 1
                and stat.S_ISREG(role["metadata"][7])
                and stat.S_IMODE(role["metadata"][7]) == 0o644
                and inner["own_runtime_descriptor_closed"] is True,
                "new stock runtime full pin/metadata/flags/final closure incomplete")
    else:
        require(role["hash_check"] == "ABSENT"
                and inner["own_runtime_descriptor_closed"] is None,
                "absent runtime role has invented hash/closure")
    R["inner_evidence_validation"]["read_accounting_validated"] = True



def partial_preowned_bytes():
    # Verifier bytes already read from the parent-held memfd. They are placed
    # on the caller pipe before this process encodes its own receipt.
    chunks = []
    items = TRANSPORT if type(globals().get("TRANSPORT")) is list else []
    for item in items:
        data = item.get("data")
        if data:
            chunks.append(bytes(data))
        pending = item.get("pending_chunk")
        if pending:
            chunks.append(bytes(pending))
    return b"".join(chunks)


def capture_inner_evidence():
    outside = consume_outside_body()
    R["outside_body_custody"] = {"custody": OUTSIDE_BODY["custody"] if outside is not None else None,
        "po880_is_not_this_body": outside is not None, "fault": OUTSIDE_BODY["fault"]}
    drain()
    raw = bytes(TRANSPORT[0]["data"])
    observed = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if "inner_complete_evidence" in R:
        require(R["inner_complete_transport"] == observed,
                "complete inner transport changed after exit capture")
        return R["inner_complete_evidence"]
    bodies, frame, complete = peel_preowned(raw)
    R["preowned_verifier_bodies"] = [body.decode("ascii") for body in bodies]
    if not complete or not frame:
        R["inner_complete_transport"] = observed
        R["inner_preowned_only"] = True
        R["inner_receiver_full_bytes_admitted"] = False
        R["inner_evidence_validation"] = {
            "common_observations_validated": False, "read_accounting_validated": False,
            "intended_cause_validated": False,
            "positive_chain_validated": False, "complete_receipt_preserved": True}
        return None
    inner = decode_channel_frame(frame, STREAM)
    validate_publication_transport(inner, STREAM, len(raw) - len(frame))
    require(encode_channel_frame(inner, STREAM) == frame, "inner channel frame roundtrip")
    require(raw == b"".join(preowned_record(body) for body in bodies) + frame,
            "inner stream is the preowned image plus the frame")
    # Retain the complete parsed evidence BEFORE any consumer may return or
    # refuse, including independent postcheck failures. A digest is additional
    # custody evidence and never a substitute for this complete parsed receipt.
    R["inner_complete_evidence"] = inner
    R["inner_complete_transport"] = observed
    R["inner_receiver_full_bytes_admitted"] = True
    R["inner_evidence_validation"] = {
        "common_observations_validated": False, "read_accounting_validated": False,
        "intended_cause_validated": False,
        "positive_chain_validated": False, "complete_receipt_preserved": True}
    return inner


def check_inner(mode):
    inner = capture_inner_evidence()
    require(not TRANSPORT[1]["data"], "stderr present; transport not clean")
    require(inner["assignment"] == ASSIGNMENT and inner["generation"] == 1,
            "inner assignment binding mismatch")
    require(inner.get("status") != "STOP_UNCONFIRMED", "inner sticky unknown")
    require(inner.get("final_custody_confirmed") is True
            and inner["cleanup"]["exact_owned_child_reaped"] is True
            and inner["cleanup"]["all_own_and_input_and_component_descriptors_closed"] is True,
            "inner final custody/cleanup incomplete")
    validate_common_inner(inner, mode)
    code = os.waitstatus_to_exitcode(CHILD["wait_status"])
    if mode != "verify-node":
        expected = ("nonroot exact three-variable startup environment required"
                    if mode == "startup-env-negative" else "pre-interpreter inherited limit mismatch")
        cause = original_stock_cause(inner["failure"])
        require(code == 2 and inner["status"] == "NOT_PROVEN" and inner["calls"] == []
                and cause == {"type": "Refusal", "message": expected},
                "startup negative did not produce exact before-GPG cause")
        intent = inner["private_home_intent"]
        require(intent["state"] == "NOT_ATTEMPTED" and intent["path"] is None
                and intent["name"] is None and intent["absence"] is None
                and inner["cleanup"]["private_home_state"] == "NOT_ATTEMPTED"
                and inner["cleanup"]["private_home_absent"] is None
                and "private_home" not in inner
                and inner["aggregate_gpg_output_bytes_including_written_key_and_body"] == 0
                and inner["own_scratch_bytes_final_before_cleanup"] == 0,
                "startup control exact no-home/no-GPG intent evidence incomplete")
        R["inner_evidence_validation"]["intended_cause_validated"] = True
        R["startup_control"] = {"mode": mode, "actual_cause": cause,
                                "actual_gpg_calls": 0, "passed": True}
        return "STARTUP_CONTROL_CAUSE_CONFIRMED"
    require(code == 0 and inner["status"] == "QUALIFIED_NODE_RAW_PUBLISHER_CHAIN_PROVEN",
            "inner exit/status not complete success")
    require(inner["cleanup"]["private_home_absent"] is True
            and inner["cleanup"]["private_home_state"] == "CONFIRMED_REMOVED"
            and inner["private_home_intent"]["state"] == "CONFIRMED_REMOVED"
            and inner["private_home"]["path"] == HOME["path"], "inner exact home accounting mismatch")
    labels = ["show-raw-key", "dearmor", "check-self-signatures-and-bindings", "positive-gpg",
              "positive-gpgv-body", "negative-same-path-changed-row"]
    require([x["label"] for x in inner["calls"]] == labels
            and all(x["reaped"] is True and x["elapsed_seconds"] <= 30 for x in inner["calls"]),
            "planned trusted call/reap/time mismatch")
    require(inner["readonly_keyring_policy"] == "friday.e4.node.readonly-keyring.a172.v1",
            "current readonly keyring recipe required")
    for index, call in enumerate(inner["calls"]):
        argv = call["argv"][9:]
        require(argv.count("--keyring") == (1 if index >= 2 else 0) and
                argv.count("--no-keyring") == (1 if index < 2 else 0) and
                argv.count("--no-default-keyring") == (0 if index == 4 else 1),
                "original six-call keyless or explicit sealed keyring recipe mismatch")
    for call in inner["calls"]:
        sample = call["child_proc_status_sample"]
        if sample is None:
            require(type(call["child_proc_status_missing_reason"]) is str
                    and bool(call["child_proc_status_missing_reason"]), "absent child sample requires actual reason")
        else:
            require(set(sample) == {"VmSize_kib", "VmRSS_kib", "VmHWM_kib", "VmPeak_kib"}
                    and all(type(v) is int and 0 <= v <= PARENT_AS // 1024 for v in sample.values()),
                    "complete actual child sample must satisfy numeric bounds")
    require(inner["aggregate_gpg_output_bytes_including_written_key_and_body"] <= OUTPUT
            and inner["own_scratch_bytes_final_before_cleanup"] <= 16515072,
            "inner aggregate output/scratch mismatch")
    require(inner["authority"]["independently_expected_primary"] == EXPECTED
            and inner["positive_gpg"]["accepted"] is True
            and inner["positive_gpg"]["primary"] == EXPECTED
            and inner["positive_gpgv"]["accepted"] is True
            and inner["positive_gpgv"]["signer"] == inner["positive_gpg"]["signer"]
            and inner["gpgv_body_equals_strict_signed_body"] is True
            and inner["positive_archive"]["actual_sha256"] == ARCHIVE_SHA
            and inner["positive_archive"]["actual_bytes"] == 31058332
            and inner["negative_signed_row"]["passed"] is True
            and inner["negative_archive"]["passed"] is True,
            "mandatory actual Node positives/negatives absent")
    R["inner_evidence_validation"]["positive_chain_validated"] = True
    return "QUALIFIED_SUPERVISED_NODE_RAW_PUBLISHER_CHAIN_PROVEN"



FFI_PATH = "/usr/lib/x86_64-linux-gnu/libffi.so.8.2.0"
FFI_SHA = "1a0dc86f787f73e025a6e521056360afcbe70f2a82cd808132fefc2b4ee95daa"
FFI_SIZE = 64184
INHERITED_OWNED = []
RUNTIME = None
RUNTIME_READS = 0
CHILD_RUNTIME_READS = 0  # distinct future child scope; never a copied parent debit
CHILD_READ_DEBIT = 32768
RUNTIME_READ_LIMIT = 262144
RUNTIME_CALLBACK = None
PRE_IMPORT_META = None
PRE_IMPORT_FLAGS = None
OWN_RUNTIME_FD = json.loads("""{"role":"own_stock_runtime","ambient_input":false,"path":"/usr/lib/x86_64-linux-gnu/libffi.so.8.2.0","sha256":"1a0dc86f787f73e025a6e521056360afcbe70f2a82cd808132fefc2b4ee95daa","size":64184,"mode":"0644","uid":0,"gid":0,"nlink":1,"flags":"O_RDONLY|FD_CLOEXEC","authority":"Explicit ordinary owned-source stock dependency assumption only, NOT immutable whole-root image proof","provenance":"ABSENT_AT_STRICT_INHERITED_ENTRY_NEW_DURING_STOCK_CALLBACK_PREPARATION","inherited_bootstrap_before_ctypes":[0,1,2,3,4,5,8],"child_canonical_roles":{"3":"prlimit","4":"python","5":"source","6":"capsule_contract"},"collision":"fd6 dup2 replaces own runtime with capsule contract before exec; fd 8 stays the outside body; other fd>=7 use closerange(7,8) then closerange(9,128); runtime role is not source, capsule, fd 8, or an ambient input; unknown inherited fds are refused and not closed or reclassified","supervisor_hash_check":"FULL_OBSERVED","verifier_hash_check":"FULL_OBSERVED_ONLY_IF_NEW_ELSE_ABSENT","image_proof":false,"acquisition":{"operation":"ctypes.CFUNCTYPE(ctypes.c_int)(lambda: 0)","retained_role":"RUNTIME_CALLBACK","callback_invoked":false,"window":"exact inherited 0..5 and fd 8 before ctypes import; retain the benign callback; exact one new pinned stock descriptor immediately afterward before kernel policy"},"read_accounting":{"schema":"friday.e4.node.explicit-read-ledger.a073.v1","runtime_content_ceiling_bytes":262144,"child_bootstrap_upper_debit_bytes":32768,"verifier_whole_content_ceiling_bytes":4294967296,"child_debit_kind":"FIXED_CONSERVATIVE_UPPER_BOUND_NOT_MEASURED","child_debit_scope":"gate token, post-remap directory scan and four bounded role readlinks; no copied supervisor prefix","local_scope":"all explicit content reads in this verifier including both import-window scans, files, proc memory, readlinks and own directory scans","implicit_io":"UNKNOWN_NOT_ZERO","parent_prefix_carried":false,"reset_or_subtraction":false,"execution_credit":false}}""")


def charge_child_runtime(n):
    global CHILD_RUNTIME_READS
    require(type(n) is int and n >= 0, "child runtime read charge")
    CHILD_RUNTIME_READS += n
    require(CHILD_RUNTIME_READS <= CHILD_READ_DEBIT, "child bootstrap upper read debit exceeded")


def charge_runtime(n):
    global RUNTIME_READS
    require(type(n) is int and n >= 0, "runtime read charge")
    if CHILD is not None and CHILD["pid"] == 0:
        charge_child_runtime(n)
    else:
        RUNTIME_READS += n
        require(RUNTIME_READS <= RUNTIME_READ_LIMIT, "supervisor runtime read ceiling exceeded")


def descriptor_numbers():
    names = os.listdir("/proc/self/fd")
    charge_runtime(sum(len(name.encode("utf-8")) + 1 for name in names))
    require(len(names) <= 129 and all(name.isascii() and name.isdecimal() for name in names),
            "malformed/bounded descriptor sample")
    found = []
    for name in names:
        fd = int(name)
        try:
            os.fstat(fd)
        except OSError as exc:
            if exc.errno != 9:  # only the already-closed listdir descriptor
                raise
            continue
        found.append(fd)
    return found


def runtime_metadata(fd):
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_uid, info.st_gid, info.st_mode, info.st_nlink]


def runtime_link(fd):
    value = os.readlink("/proc/self/fd/" + str(fd))
    charge_runtime(len(value.encode("utf-8")))
    require(len(value.encode("utf-8")) < 4096, "runtime readlink content cap")
    return value


def prove_strict_inherited_before_ctypes():
    global ctypes, PRE_IMPORT_META, PRE_IMPORT_FLAGS, RUNTIME_CALLBACK
    found = descriptor_numbers()
    require(set(found) == {0, 1, 2, 3, 4, 5, 8}, "unknown bootstrap inherited descriptor")
    PRE_IMPORT_META = {fd: runtime_metadata(fd) for fd in found}
    PRE_IMPORT_FLAGS = {fd: [fcntl.fcntl(fd, fcntl.F_GETFD), fcntl.fcntl(fd, fcntl.F_GETFL)]
                        for fd in found}
    import ctypes as loaded_ctypes
    ctypes = loaded_ctypes
    # Root-selected benign no-op preparation, as pinned A067. Never invoke it.
    RUNTIME_CALLBACK = ctypes.CFUNCTYPE(ctypes.c_int)(lambda: 0)


def prove_own_stock_ffi():
    global RUNTIME
    require(PRE_IMPORT_META is not None and RUNTIME_CALLBACK is not None,
            "stock callback window was not bounded/prepared")
    found = descriptor_numbers()
    current = set(found)
    inherited = set(PRE_IMPORT_META)
    require(inherited <= current, "inherited descriptor closed during ctypes window")
    for fd, before in PRE_IMPORT_META.items():
        require(runtime_metadata(fd) == before
                and [fcntl.fcntl(fd, fcntl.F_GETFD), fcntl.fcntl(fd, fcntl.F_GETFL)]
                == PRE_IMPORT_FLAGS[fd], "inherited descriptor identity/flags changed during stock window")
    extra = sorted(current - inherited)
    require(extra, "own stock ffi descriptor missing after ctypes window")
    require(len(extra) == 1, "unknown descriptor opened beside own stock ffi")
    fd = extra[0]
    require(fd not in inherited and fd != 8 and 6 <= fd < 128,
            "new runtime descriptor collides with trusted bootstrap or escapes close window")
    RUNTIME = {"fd": adopt_exact_fd(fd, "runtime"), "own_new": True, "closed": False, "proven": False,
               "provenance": "ABSENT_AT_STRICT_INHERITED_ENTRY_NEW_DURING_STOCK_CALLBACK_PREPARATION"}
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    require(fcntl.fcntl(fd, fcntl.F_GETFD) == fcntl.FD_CLOEXEC, "own runtime descriptor is not CLOEXEC")
    require(flags & os.O_ACCMODE == os.O_RDONLY and not flags & (os.O_PATH | os.O_APPEND),
            "own runtime descriptor is not ordinary readonly")
    info = os.fstat(fd)
    named = os.stat(FFI_PATH, follow_symlinks=False)
    require(runtime_link(fd) == FFI_PATH and stat.S_ISREG(info.st_mode) and stat.S_ISREG(named.st_mode),
            "own ffi path is not the Root-selected regular file")
    require((info.st_dev, info.st_ino) == (named.st_dev, named.st_ino)
            and info.st_size == FFI_SIZE and info.st_uid == 0 and info.st_gid == 0
            and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o644,
            "Root-selected ffi identity/mode mismatch")
    before = runtime_metadata(fd)
    named_before = ident(named)
    require(ident(os.fstat(fd)) == named_before, "own ffi before/opened metadata mismatch")
    digest = hashlib.sha256()
    offset = 0
    while offset < info.st_size:
        amount = min(4096, info.st_size - offset)
        data = os.pread(fd, amount, offset)
        charge_runtime(len(data))
        require(len(data) == amount, "own ffi read short")
        digest.update(data)
        offset += len(data)
    require(offset == FFI_SIZE and digest.hexdigest() == FFI_SHA and runtime_metadata(fd) == before
            and ident(os.stat(FFI_PATH, follow_symlinks=False)) == ident(os.fstat(fd)) == named_before
            and fcntl.fcntl(fd, fcntl.F_GETFL) == flags
            and fcntl.fcntl(fd, fcntl.F_GETFD) == fcntl.FD_CLOEXEC
            and runtime_link(fd) == FFI_PATH, "own ffi Root-selected SHA/path/metadata/flags drift")
    RUNTIME.update(metadata=before, path_identity=named_before, status_flags=flags,
                   descriptor_flags=fcntl.FD_CLOEXEC, proven=True)


def bind_runtime_collision():
    fd = RUNTIME["fd"]
    require(RUNTIME.get("proven") is True, "unproven runtime descriptor cannot be launched")
    held = {item["fd"] for item in HELD.values()}
    copies = {item for item in LAUNCH_RESERVE["copies"] if item is not None}
    require(fd not in {0, 1, 2, 3, 4, 5, 8} and fd not in held and fd not in copies,
            "runtime role entered a held source or bootstrap map")
    require(R["actual_launch"]["fd_map"] == [3, 4, 5, 6], "canonical child map changed")
    require(runtime_metadata(fd) == RUNTIME["metadata"] and runtime_link(fd) == FFI_PATH
            and ident(os.stat(FFI_PATH, follow_symlinks=False)) == RUNTIME["path_identity"]
            and fcntl.fcntl(fd, fcntl.F_GETFL) == RUNTIME["status_flags"]
            and fcntl.fcntl(fd, fcntl.F_GETFD) == RUNTIME["descriptor_flags"],
            "runtime identity/path/flags drift before launch")
    R["runtime_fd"] = {
        "role": "own_stock_runtime", "ambient_input": False, "fd": fd, "path": FFI_PATH,
        "sha256": FFI_SHA, "size": FFI_SIZE, "mode": "0644", "uid": 0, "gid": 0, "nlink": 1,
        "flags": "O_RDONLY|FD_CLOEXEC", "metadata": RUNTIME["metadata"],
        "provenance": RUNTIME["provenance"], "hash_check": "FULL_OBSERVED",
        "authority": OWN_RUNTIME_FD["authority"], "image_proof": False,
        "child_collision": "dup2_capsule_contract_onto_6" if fd == 6 else "closerange_7_128",
        "child_canonical_roles": {"3": "prlimit", "4": "python", "5": "source", "6": "capsule_contract"},
        "explicit_read_bytes": RUNTIME_READS, "read_reset_or_subtracted": False,
        "acquisition": OWN_RUNTIME_FD["acquisition"], "callback_retained": RUNTIME_CALLBACK is not None,
        "callback_invoked": False, "status_flags": RUNTIME["status_flags"],
        "descriptor_flags": RUNTIME["descriptor_flags"]}


def child_canonical_closed(role_meta):
    found = set(descriptor_numbers())
    require(found == {0, 1, 2, 3, 4, 5, 6, 8}, "child own descriptor remains open before exec")
    require(os.pread(8, len(OUTSIDE_BODY["marker"]), 0) == OUTSIDE_BODY["marker"],
            "child outside body marker missing")
    for fd, meta in zip((3, 4, 5, 6), role_meta):
        require(runtime_metadata(fd) == meta, "child canonical role identity mismatch")
        require(runtime_link(fd) != FFI_PATH, "own ffi remains mapped on a canonical role")
    if RUNTIME["fd"] == 6:
        require(runtime_metadata(6) == role_meta[3], "fd6 collision was not replaced by the capsule contract")
    else:
        require(RUNTIME["fd"] >= 7 and RUNTIME["fd"] != 8 and RUNTIME["fd"] not in found,
                "high own runtime descriptor survived closerange")

def prove_joined_stock_channels():
    non_graph, graph_budget, raw_room, inner, supervisor_cap, outer, base64_supervisor = derived_stock_bounds()
    require(non_graph + graph_budget + raw_room == inner, "joined inner stock channel")
    require(non_graph > 0 and graph_budget > 0 and raw_room > 0, "derived stock split is positive")
    require(inner + non_graph <= supervisor_cap, "joined supervisor stock channel")
    require(base64_supervisor > supervisor_cap, "base64 of a 65536-byte body exceeds a 65536-byte channel")
    require(FD_SIMULTANEOUS_CAP == 256, "simultaneous NOFILE cap")
    require(derived_catch_capacity() <= ERROR_OBJECT_CAP, "derived catch envelope fits the original journal")
    require(EMERGENCY_CAP == derived_catch_capacity(), "emergency receiver matches the derived envelope")
    require(non_graph == 14592 and graph_budget == 6058 and raw_room == 12118, "derived factory split")

def run():
    global DEADLINE, INNER_DEADLINE
    prove_joined_stock_channels()
    require(len(sys.argv) == 5 and sys.argv[1] == "--host-deadline-monotonic-ns"
            and sys.argv[2].isdigit() and sys.argv[3] == "--mode"
            and sys.argv[4] in ("verify-node", "startup-env-negative", "startup-as-negative"),
            "exact supervisor preset/deadline CLI required")
    DEADLINE = int(sys.argv[2])
    require(60000000000 < DEADLINE - START <= 900000000000,
            "host-fixed pre-bootstrap deadline <=900s with finite reserves required")
    INNER_DEADLINE = DEADLINE - 20000000000
    signal.signal(signal.SIGALRM, deadline_alarm)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, (DEADLINE - time.monotonic_ns()) / 1e9))
    require(os.getresuid() == (1000, 1000, 1000) and os.getresgid() == (1000, 1000, 1000)
            and dict(os.environ) == ENV, "unprivileged exact bootstrap ENV required")
    prove_strict_inherited_before_ctypes()
    adopt_outside_body()
    global INHERITED_OWNED
    INHERITED_OWNED = [adopt_exact_fd(fd, "inherited_low") for fd in (3, 4, 5)]
    for kind, pair in ((resource.RLIMIT_AS, (SUP_AS, PARENT_AS)), (resource.RLIMIT_CPU, (120, 120)),
                       (resource.RLIMIT_FSIZE, (STREAM, STREAM)), (resource.RLIMIT_NOFILE, (64, 128)),
                       (resource.RLIMIT_CORE, (0, 0))):
        require(resource.getrlimit(kind) == pair, "actual pre-interpreter bootstrap limits mismatch")
    prove_own_stock_ffi()  # closes the stock-only acquisition window before kernel effects
    initialize_kernel_policy()
    sample_self("bootstrap_startup")
    require(children(os.getpid()) == [], "supervisor already owns unknown child")
    for name in PINS:
        hold(name)
    require(ident(os.stat("/proc/self/exe")) == HELD["python"]["identity"],
            "actual bootstrap executable differs from independently pinned Python")
    # The bootstrap descriptors supplied by separately trusted host are checked
    # again; this is custody continuation, not proof of their pre-start trust.
    for fd, name in ((3, "prlimit"), (4, "python")):
        require(ident(os.fstat(fd)) == HELD[name]["identity"]
                and fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY,
                "bootstrap inherited tool descriptor mismatch")
    mode = sys.argv[4]
    R["mode"] = mode
    launch(mode)
    while not exited(CHILD["pidfd"]):
        require(time.monotonic_ns() < INNER_DEADLINE, "supervised monotonic cutoff reached")
        observe_owned()
        drain()
        select.select([], [], [], 0.01)
    capture_inner_evidence()  # full exit receipt survives later postcheck refusal
    finish_parent()
    drain()
    # Supervisor must independently settle even source-reported successful exit.
    terminate_owned()
    custody("postexit")
    post_home()
    sample_self("postexit")
    require(HOME["postexit_absent"] is True, "exact external home absence missing")
    return check_inner(mode)



def commit_existing_receiver(frame):
    # Same exact prefix+frame grammar for both original receiver kinds.
    # Successful IO is byte retention only; outside native custody is NOT proven.
    require(type(frame) is bytes and frame.startswith(b"FR875 ") and 0 < len(frame) <= OUTPUT - PARENT_PLACED,
            "complete prefix plus final frame must fit original receiver cap")
    append_parent_bytes(frame, final=True)
    return frame


def encode(record):
    record["runtime_explicit_read_bytes"] = RUNTIME_READS
    if "runtime_fd" in record:
        record["runtime_fd"]["explicit_read_bytes"] = RUNTIME_READS
    publish_errors(record)
    if record is R:
        for item in TRANSPORT:
            require(restore_transport_raw(record, record["output"][item["label"]]) == bytes(item["data"]),
                    "lossless complete raw transport preimage mismatch")
    return encode_channel_frame(record, OUTPUT - PARENT_PLACED)


def emit(data):
    fcntl.fcntl(1, fcntl.F_SETFL, fcntl.fcntl(1, fcntl.F_GETFL) | os.O_NONBLOCK)
    sent = 0
    while sent < len(data):
        due(0)
        try:
            written = os.write(1, data[sent:sent + 4096])
            require(written > 0, "public transport stalled")
            sent += written
        except BlockingIOError:
            select.select([], [1], [], min(0.02, max(0, (DEADLINE - time.monotonic_ns()) / 1e9)))
    due(0)


qualified = False
try:
    outcome = run()
    R["status"] = outcome
    qualified = True
except BaseException as exc:
    R["failure"] = deferred_error(exc)
finally:
    # Every failure takes the same finite exact-owned shutdown and custody path.
    for action, field in ((terminate_owned, "cleanup_failure"),
                          (lambda: custody("terminal"), "custody_failure"),
                          (post_home, "home_failure")):
        try:
            action()
        except BaseException as exc:
            qualified = False
            STOP = True
            R[field] = deferred_error(exc)
    if CHILD is not None and CHILD["reaped"] and len(TRANSPORT) == 2:
        try:
            capture_inner_evidence()
        except BaseException as exc:
            qualified = False
            R["inner_evidence_capture_failure"] = {
                "error": deferred_error(exc)}
    try:
        for item in TRANSPORT:
            if (item.get("partial") or item.get("pending_chunk") is not None
                    or item.get("retain_error_object") is not None
                    or (item.get("produced_bytes") is not None
                        and len(item.get("data") or b"") != item.get("produced_bytes"))):
                hold_unread_prefix(getattr(item.get("fd"), "allocation", None))
        retain_produced_transport()
        for item in TRANSPORT:
            retained = len(item.get("data") or b"")
            produced = item.get("produced_bytes")
            if (item.get("partial") or item.get("pending_chunk") is not None
                    or item.get("retain_error_object") is not None
                    or (produced is not None and retained != produced)):
                hold_unread_prefix(getattr(item.get("fd"), "allocation", None))
        for item in TRANSPORT:
            fd = item["fd"]
            item["fd"] = None
            close_taken(fd, "transport:" + str(item.get("label")))
        close_launch_reserve()
        for item in TRACKED:
            pidfd = item["pidfd"]
            item["pidfd"] = None
            close_taken(pidfd, "tracked_pidfd")
            procfd = item["procfd"]
            item["procfd"] = None
            close_taken(procfd, "tracked_procfd")
        if CHILD is not None:
            pidfd = CHILD["pidfd"]
            CHILD["pidfd"] = None
            close_taken(pidfd, "child_pidfd")
            procfd = CHILD["procfd"]
            CHILD["procfd"] = None
            close_taken(procfd, "child_procfd")
        for item in HELD.values():
            fd = item["fd"]
            item["fd"] = None
            close_taken(fd, "held")
        pending_dirs = []
        for path, pair in list(DIRS.items()):
            pending_dirs.append(pair[0])
            DIRS[path] = (None, pair[1])
        for fd in pending_dirs:
            close_taken(fd, "directory")
        if RUNTIME is not None and RUNTIME.get("own_new") and not RUNTIME["closed"]:
            RUNTIME["closed"] = True
            close_taken(RUNTIME["fd"], "runtime")
        for fd in INHERITED_OWNED:
            close_taken(fd, "inherited_low")
        R["all_supervisor_owned_descriptors_closed"] = FD_UNREAD_HOLDS == 0
        R["own_runtime_descriptor_closed"] = RUNTIME["closed"] if RUNTIME else None
        R["runtime_explicit_read_bytes"] = RUNTIME_READS
        R["runtime_read_reset_or_subtracted"] = False
        if "runtime_fd" in R:
            R["runtime_fd"]["explicit_read_bytes"] = RUNTIME_READS
    except BaseException as exc:
        qualified = False
        STOP = True
        R["descriptor_cleanup_failure"] = deferred_error(exc)

try:
    finish_allocations()
except BaseException as exc:
    STOP = True
    qualified = False
    R["allocation_cleanup_failure"] = deferred_error(exc)

try:
    due(0)
    if not qualified:
        R["status"] = "STOP_UNCONFIRMED" if STOP else "NOT_PROVEN"
        # Preserve observations on refusal too; qualification is never inherited
        # from an inner status. Full evidence fit or emission failure refuses.
        R["qualified"] = False
    R["qualified"] = qualified and not STOP
    R["fork_state"] = FORK_STATE
    R["runtime_explicit_read_bytes"] = RUNTIME_READS
    R["runtime_read_scope"] = "supervisor own runtime observations only; copied child prefix excluded"
    R["implicit_io"] = "UNKNOWN_NOT_ZERO"
    R["elapsed_seconds_from_supervisor_start"] = (time.monotonic_ns() - START) / 1e9
    R["completed_msk"] = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).isoformat()
    require(commit_outside_body(), "outside body commit before publication")
except BaseException as exc:
    qualified = False
    R["qualified"] = False
    R["status"] = "STOP_UNCONFIRMED" if STOP else "NOT_PROVEN"
    # Append's caught primary+unlock secondary were first-stored separately.
    # The Refusal below is additional evidence, never their replacement.
    R["publication_failure"] = deferred_error(exc)
try:
    finish_outside_end()
    # One ordered publication attempt on success OR ordinary append refusal.
    # Both original decoders see the full errors; never re-encode a failed frame.
    public = publish_preowned_then_frame(R, lambda: encode(R))
except BaseException as exc:
    qualified = False
    R["qualified"] = False
    R["status"] = "STOP_UNCONFIRMED" if STOP else "NOT_PROVEN"
    retain_outside_failure("publication", exc)
    R["prepublication_failure"] = deferred_error(exc)
    public = None
if public is not None:
    try:
        commit_existing_receiver(public)
    except BaseException as exc:
        qualified = False
        marker = retain_publication_failure("commit", exc)
        R["status"] = "STOP_UNCONFIRMED" if STOP else "NOT_PROVEN"
        R["qualified"] = False
        R["receiver_failure"] = marker
        # Native preowned original body is still a missing Source edge.
else:
    qualified = False
    R["qualified"] = False
    R["status"] = "STOP_UNCONFIRMED" if STOP else "NOT_PROVEN"
os._exit(0 if qualified and not STOP else 3 if STOP else 2)
