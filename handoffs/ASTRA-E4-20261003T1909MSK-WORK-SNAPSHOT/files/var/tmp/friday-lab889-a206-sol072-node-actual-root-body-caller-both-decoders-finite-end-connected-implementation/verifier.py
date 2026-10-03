"""A048 SOURCE ONLY. Invocation requires separate reviewed execution authority.

Installed Python/GnuPG/prlimit, their loaders/libraries and the host are supplied
trust assumptions. This program proves only the exact Node publisher byte chain.
The caller pins descriptors 3=prlimit, 4=Python, 5=this source, 6=launch contract
and execs descriptor 3 with the contract's limits BEFORE interpreter startup.
No A036 program is imported or executed. No archive code is run or extracted.
"""
import os

# The exact reviewed launcher prefunds a conservative child-process debit.
# It is a bound, not an observed zero and not the supervisor's copied prefix.
CHILD_READ_DEBIT = 32768
WHOLE_READ_LIMIT = 4294967296
RUNTIME_READ_LIMIT = 262144
WHOLE_READS = CHILD_READ_DEBIT
VERIFIER_READS = 0
RUNTIME_READS = 0
RUNTIME_DIRECTORY_SCANS = 0


def _read_room(n):
    if type(n) is not int or n < 0 or WHOLE_READS + n > WHOLE_READ_LIMIT:
        raise RuntimeError("verifier whole explicit-content read ceiling")


def _charge_content(n, runtime=False):
    global WHOLE_READS, VERIFIER_READS, RUNTIME_READS
    if type(n) is not int or n < 0:
        raise RuntimeError("explicit-content read charge")
    # Retain known work even on refusal; there is never a counter reset.
    WHOLE_READS += n
    VERIFIER_READS += n
    if runtime:
        RUNTIME_READS += n
    if WHOLE_READS > WHOLE_READ_LIMIT or RUNTIME_READS > RUNTIME_READ_LIMIT:
        raise RuntimeError("verifier cumulative content read ceiling")


def _directory_names(fd):
    names = os.listdir(fd)
    _charge_content(sum(len(name.encode("utf-8")) + 1 for name in names))
    return names


def _descriptor_meta():
    global RUNTIME_DIRECTORY_SCANS
    names = os.listdir("/proc/self/fd")
    _charge_content(sum(len(name.encode("utf-8")) + 1 for name in names), True)
    RUNTIME_DIRECTORY_SCANS += 1
    if len(names) > 129 or not all(name.isascii() and name.isdecimal() for name in names):
        raise RuntimeError("malformed/bounded descriptor sample")
    found = {}
    for name in names:
        fd = int(name)
        try:
            info = os.fstat(fd)
        except OSError as exc:
            if exc.errno != 9:  # only listdir's already-closed descriptor
                raise
            continue
        found[fd] = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                     info.st_ctime_ns, info.st_uid, info.st_gid, info.st_mode, info.st_nlink]
    return found

_PRE_IMPORT_META = _descriptor_meta()
if set(_PRE_IMPORT_META) != {0, 1, 2, 3, 4, 5, 6, 8}:
    raise RuntimeError("unknown bootstrap inherited descriptor")
_PRE_IMPORT_INHERITABLE = {fd: os.get_inheritable(fd) for fd in _PRE_IMPORT_META}
# Unknown inherited descriptors refuse before general imports and remain open;
# they are never classified as our runtime or deliberately closed.

import base64
import datetime
import fcntl
import hashlib
import json
import os
import resource
import select
import signal
import stat
import subprocess
import sys
import time
import re

_POST_IMPORT_META = _descriptor_meta()
_POST_IMPORT_INHERITABLE = {fd: os.get_inheritable(fd) for fd in _POST_IMPORT_META}
VERIFIER_RUNTIME = None

ASSIGNMENT = "ASTRA-E4-NODE-LAUNCH-OWNERSHIP-EVIDENCE-CLOSURE-A048"
PREFIX = "/var/tmp/friday-lab889-a206-sol072-node-actual-root-body-caller-both-decoders-finite-end-connected-implementation"
BASE = "/var/tmp/friday-astra-material-acquisition-20261001-a023-g1"
EXPECTED = "CC68F5A3106FF448322E48ED27F5E38D5B0A215F"
TARGET = "node-v22.23.2-linux-x64.tar.xz"
AHASH = "d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307"
AS_PARENT, AS_CHILD = 117440512, 67108864
AS_SUPERVISOR = 33554432
INNER_MEMORY_KIB = 229376
RAW, ARCH, OUTPUT, SCRATCH = 262144, 33554432, 65536, 16515072
PUBLIC_OUTPUT = 32768
CONTRACT_HASH_BINDING = {"fixed_binding": "sealed_contract_full_sha256",
                         "provider": "supervisor.CONTRACT_SHA", "caller_override": False}
DEADLINE_BINDING = {"parameter": "supervisor_inner_deadline_monotonic_ns"}
HOME_BINDING = {"parameter": "supervisor_created_private_home_name"}
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
START_NS = time.monotonic_ns()
DEADLINE_NS = START_NS + 900000000000
MAIN_END_NS = DEADLINE_NS - 20000000000
DIRECTORIES, FILES, OWN = {}, {}, set()
HOME = None
HOME_FD = None
HOME_INTENT = {"state": "NOT_ATTEMPTED", "path": None, "name": None,
               "parent_identity": None, "created_identity": None, "absence": None}
ACTIVE = None
STOP = False
TOTAL_OUTPUT = 0
OUTPUT_UNKNOWN = False
CALL_OWNERS = []
R = {"schema": "friday.e4.node.bounded-verifier.sol062.v1",
     "assignment": ASSIGNMENT, "generation": 1, "status": "NOT_PROVEN",
     "calls": [], "custody": {}, "resources": {}, "cleanup": {},
     "resource_schema": "friday.e4.node.verifier-resources.a172.v1",
     "readonly_keyring_policy": "friday.e4.node.readonly-keyring.a172.v1",
     "private_home_intent": HOME_INTENT,
     "authority": {"independently_expected_primary": EXPECTED,
                   "origin": "Pinned A048 contract and A032 bill; A036's recorded official documentary corroboration is historical authority evidence, not a new fetch."},
     "qualification": ["Exact Node archive publisher chain only.",
                       "Installed verifier/dependencies/kernel/host trust is assumed, not reconstructed.",
                       "No generic toolchain, host, root, execution admission, live, gate or GO acceptance.",
                       "No latest global revocation search or network syscall observation.",
                       "A036 remains terminal NOT_PROVEN with zero GPG calls; its refusal and historical labels are not changed."]}


class Refusal(Exception):
    pass


def require(ok, why):
    if not ok:
        raise Refusal(why)


def stop(why):
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


def outside_body_fault(reason):
    OUTSIDE_BODY["fault"] = True
    OUTSIDE_BODY["fault_reason"] = reason
    return False


def outside_body_number(number):
    if OUTSIDE_BODY.get("retire_allowed"):
        return False
    if number == 8:
        return True
    held = OUTSIDE_BODY.get("held")
    return type(held) is int and number == held


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
    return b"".join(parts)


def open_caller_outside_body():
    opener = globals().get("allocate_owned")
    require(opener is not None and OUTSIDE_BODY["held"] is None, "outside body opener missing")
    lease = opener(os.memfd_create, "outside-body", "A206-outside-body",
                   os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    held = int(lease)
    marker = OUTSIDE_BODY["marker"]
    wrote = os.pwrite(held, marker, 0)
    require(wrote == len(marker), "outside body header short")
    os.dup2(held, 8, inheritable=True)
    flags = fcntl.fcntl(8, fcntl.F_GETFD)
    fcntl.fcntl(8, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
    OUTSIDE_BODY["held"] = held
    OUTSIDE_BODY["end"] = len(marker)
    OUTSIDE_BODY["inherited"] = False
    return True


def adopt_outside_body():
    info = os.fstat(8)
    require(stat.S_ISREG(info.st_mode), "outside body alias is not regular")
    size = info.st_size
    require(0 < size <= OUTSIDE_BODY["cap"], "outside body alias extent")
    head = os.pread(8, len(OUTSIDE_BODY["marker"]), 0)
    require(head == OUTSIDE_BODY["marker"], "outside body marker missing")
    OUTSIDE_BODY["held"] = 8
    OUTSIDE_BODY["end"] = size
    OUTSIDE_BODY["inherited"] = True
    return True


def commit_outside_body():
    if OUTSIDE_BODY["fault"] or OUTSIDE_BODY["sealed"] or OUTSIDE_BODY["held"] is None:
        return outside_body_fault("commit refused")
    fd = OUTSIDE_BODY["held"]
    try:
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
    except BaseException:
        return outside_body_fault("commit io")


def remember_emergency_body(rendered):
    if type(rendered) is not bytes or OUTSIDE_BODY["sealed"] or OUTSIDE_BODY["held"] is None:
        return outside_body_fault("emergency body unavailable")
    return commit_outside_body()


def seal_outside_body():
    if OUTSIDE_BODY["inherited"] or OUTSIDE_BODY["held"] is None:
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
    except BaseException:
        return outside_body_fault("seal failed")


def consume_outside_body():
    fd = OUTSIDE_BODY["held"] if type(OUTSIDE_BODY["held"]) is int else 8
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
    except BaseException:
        outside_body_fault("consume io")
        return None


def finish_outside_end():
    disposition = "NOT_RETIRED"
    try:
        if OUTSIDE_BODY["inherited"]:
            if OUTSIDE_BODY["end"] > 0 and not OUTSIDE_BODY["fault"]:
                signal.setitimer(signal.ITIMER_REAL, 0)
                disposition = "TRANSFERRED_PARENT_HOLDS_BODY"
        else:
            receipt = globals().get("R")
            success = (OUTSIDE_BODY["sealed"] and OUTSIDE_BODY["consumed"]
                       and not OUTSIDE_BODY["fault"] and not globals().get("STOP")
                       and type(receipt) is dict and receipt.get("qualified_node_only"))
            if success:
                signal.setitimer(signal.ITIMER_REAL, 0)
                OUTSIDE_BODY["retire_allowed"] = True
                closed = []
                for number in (OUTSIDE_BODY["held"], 8):
                    if type(number) is int and number not in closed:
                        os.close(number)
                        closed.append(number)
                disposition = "RETIRED_AFTER_CONSUME"
            elif type(receipt) is dict:
                receipt["qualified_node_only"] = False
    except BaseException:
        disposition = "NOT_RETIRED"
        OUTSIDE_BODY["retire_allowed"] = False
        outside_body_fault("finite end")
        receipt = globals().get("R")
        if type(receipt) is dict and "qualified_node_only" in receipt:
            receipt["qualified_node_only"] = False
    OUTSIDE_BODY["disposition"] = disposition
    OUTSIDE_BODY["outside_timer_refreshed"] = False
    receipt = globals().get("R")
    if type(receipt) is dict:
        receipt["outside_finite_end"] = {
            "disposition": disposition,
            "outside_timer_refreshed": False,
            "os_exit_is_not_custody": True,
        }
    return disposition

def preowned_record(body):
    return b"PO880 %d\n" % len(body) + body


def publication_due():
    due(True)


def publication_read_room(size):
    _read_room(size)


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
    _charge_content(len(body))
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
    objects, nodes, values, cells = [exc], [], [], []
    def ref(value):
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
        if isinstance(value, BaseException):
            return {"error_ref": ref(value)}
        if type(value) in (type(None), bool, int, str):
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
            while tb is not None:
                require(len(frames) < 128, "original traceback capacity exhausted")
                frames.append({"file": tb.tb_frame.f_code.co_filename,
                               "function": tb.tb_frame.f_code.co_name,
                               "line": tb.tb_lineno, "lasti": tb.tb_lasti})
                tb = tb.tb_next
            nodes.append({"id": error_index, "module": type(obj).__module__,
                "type": type(obj).__name__, "message": str(obj), "args": val(obj.args),
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
            if type(obj) in (bytes, bytearray):
                cell = {"id": value_index, "kind": type(obj).__name__, "bytes": len(obj),
                        "sha256": hashlib.sha256(obj).hexdigest(),
                        "base64": base64.b64encode(obj).decode("ascii")}
            elif type(obj) in (list, tuple):
                cell = {"id": value_index, "kind": type(obj).__name__, "items": [val(v) for v in obj]}
            else:
                cell = {"id": value_index, "kind": "dict", "pairs": [[val(k), val(v)] for k,v in obj.items()]}
            cells.append(cell)
            value_index += 1
    graph = {"schema": "friday.sol060.error-graph.v2", "root": 0,
             "nodes": nodes, "values": cells,
             "stock_cause": {"type": nodes[0]["type"], "message": nodes[0]["message"]},
             "native_object_reconstruction": "NOT_QUALIFIED"}
    return seal_stock_graph(graph)


def validate_received_error_graphs(record):
    # Whole decoded receipt is bounded by its unchanged original channel cap.
    if type(record) is dict:
        require("$private_pending_error" not in record, "private error owner is not durable public evidence")
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
    for record in FD_HISTORY[:FD_HISTORY_USED]:
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
                  ("PLANNED", "CONFIRMED_CLOSED")]
    R["fd_lifetime_audit"] = fd_lifetime_audit()
    R["descriptor_allocation_history"] = [{k: r[k] for k in
        ("generation", "where", "number", "state", "attempted_once", "acquire_error", "close_error")}
        for r in FD_HISTORY[:FD_HISTORY_USED]]
    if unresolved:
        STOP = True
    if first is not None:
        raise first
    require(not unresolved, "same-owner acquisition/retirement outcomes unresolved")


def due(cleanup=False):
    require(time.monotonic_ns() < (DEADLINE_NS if cleanup else MAIN_END_NS),
            "whole-launch deadline or reserved cleanup boundary exceeded")


def alarm_handler(signum, frame):
    raise Refusal("whole-launch deadline alarm")


def identity(s, directory=False):
    d = {"dev": s.st_dev, "ino": s.st_ino, "uid": s.st_uid,
         "gid": s.st_gid, "mode": s.st_mode}
    if not directory:
        d.update(size=s.st_size, nlink=s.st_nlink,
                 mtime_ns=s.st_mtime_ns, ctime_ns=s.st_ctime_ns)
    return d


def directory(path):
    """Every component is held and opened relative to its held parent NOFOLLOW.

    Directory custody records identity/owner/mode, not mutable child-list times.
    File custody additionally records size/link count/times and full byte hash.
    """
    require(path.startswith("/") and "//" not in path, "noncanonical directory")
    if path in DIRECTORIES:
        return DIRECTORIES[path][0]
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    if path == "/":
        fd = acquire_owned(os.open, "open", "/", flags)
    else:
        parent, name = os.path.split(path)
        require(name not in ("", ".", ".."), "bad directory component")
        pfd = directory(parent or "/")
        fd = acquire_owned(os.open, "open", name, flags, dir_fd=pfd)
        try:
            require(identity(os.stat(name, dir_fd=pfd, follow_symlinks=False), True)
                    == identity(os.fstat(fd), True), "directory open race")
        except BaseException:
            try:
                close_once(fd, "directory_unpublished")
            except BaseException:
                globals()["STOP"] = True
            raise
    s = os.fstat(fd)
    require(stat.S_ISDIR(s.st_mode), "component is not a directory")
    DIRECTORIES[path] = (fd, identity(s, True))
    return fd


def components():
    for path, (fd, before) in DIRECTORIES.items():
        require(identity(os.fstat(fd), True) == before, "held directory drift: " + path)
        if path == "/":
            current = os.stat("/", follow_symlinks=False)
        else:
            parent, name = os.path.split(path)
            current = os.stat(name, dir_fd=DIRECTORIES[parent or "/"][0],
                              follow_symlinks=False)
        require(identity(current, True) == before, "directory path drift: " + path)


def open_regular(path):
    parent, name = os.path.split(path)
    require(path.startswith("/") and name not in ("", ".", ".."), "bad file path")
    pfd = directory(parent)
    fd = acquire_owned(os.open, "open", name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=pfd)
    try:
        s = os.fstat(fd)
        require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1, "not single-link regular file")
        require(identity(s) == identity(os.stat(name, dir_fd=pfd, follow_symlinks=False)),
                "file open race")
    except BaseException:
        try:
            close_once(fd, "regular_unpublished")
        except BaseException:
            STOP = True
        raise
    return fd


def read_fd(fd, cap, retain=True, cleanup=False):
    before = identity(os.fstat(fd))
    require(stat.S_ISREG(before["mode"]) and 0 <= before["size"] <= cap,
            "file size/type exceeds bound")
    pos, parts, digest = 0, [], hashlib.sha256()
    while pos < before["size"]:
        due(cleanup)
        amount = min(65536, before["size"] - pos)
        _read_room(amount)
        chunk = os.pread(fd, amount, pos)
        _charge_content(len(chunk))
        require(bool(chunk), "short input")
        pos += len(chunk)
        digest.update(chunk)
        if retain:
            parts.append(chunk)
    _read_room(1)
    tail = os.pread(fd, 1, pos)
    _charge_content(len(tail))
    require(not tail and identity(os.fstat(fd)) == before,
            "input length/identity changed during read")
    return digest.hexdigest(), pos, b"".join(parts) if retain else None


def hold(name, path, cap, digest, size, tool=False, inherited=None):
    fd = open_regular(path)
    # Register before hashing so partial startup failure still closes our fd.
    FILES[name] = {"fd": fd, "path": path, "cap": cap, "expected": digest,
                   "before": identity(os.fstat(fd)), "tool": tool}
    h, n, _ = read_fd(fd, cap, False)
    require(h == digest and (size is None or n == size), "pin mismatch: " + name)
    if tool:
        s = os.fstat(fd)
        require(s.st_uid == 0 and stat.S_IMODE(s.st_mode) == 0o755,
                "trusted executable ownership/mode mismatch")
    else:
        require(os.fstat(fd).st_uid == os.getuid()
                and stat.S_IMODE(os.fstat(fd).st_mode) == 0o600,
                "protected input ownership/mode mismatch")
    if inherited is not None:
        require(identity(os.fstat(inherited)) == FILES[name]["before"],
                "inherited descriptor differs from reviewed path: " + name)
        require(fcntl.fcntl(inherited, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY,
                "inherited descriptor is not read-only")
    R["custody"][name] = {"path": path, "sha256": h, "bytes": n,
                           "identity": FILES[name]["before"]}
    return fd


def custody(cleanup=False):
    components()
    for name, item in FILES.items():
        due(cleanup)
        require(identity(os.fstat(item["fd"])) == item["before"],
                "held input drift: " + name)
        reopened = open_regular(item["path"])
        try:
            require(identity(os.fstat(reopened)) == item["before"],
                    "path/fd drift: " + name)
            hh, hn, _ = read_fd(item["fd"], item["cap"], False, cleanup)
            ph, pn, _ = read_fd(reopened, item["cap"], False, cleanup)
            require(hh == ph == item["expected"] and hn == pn,
                    "input byte drift: " + name)
        finally:
            close_once(reopened, "custody_reopen")
    components()


class MissingMemoryObservation(Refusal):
    pass


def mm(pid="self"):
    # /proc is kernel telemetry, not source/authority input. Missing child samples
    # remain None; they are never reported as a zero measurement.
    fd = acquire_owned(os.open, "open", "/proc/" + str(pid) + "/status", os.O_RDONLY | os.O_CLOEXEC)
    try:
        _read_room(16385)
        data = os.read(fd, 16385)
        _charge_content(len(data))
        require(len(data) <= 16384, "oversize /proc status")
    finally:
        close_allocation(fd, "lexical_close")
    answer = {}
    for line in data.splitlines():
        m = re.fullmatch(rb"(VmSize|VmRSS|VmHWM|VmPeak):\s+([0-9]+) kB", line)
        if m:
            answer[m[1].decode("ascii") + "_kib"] = int(m[2])
    fields = {"VmSize_kib", "VmRSS_kib", "VmHWM_kib", "VmPeak_kib"}
    if set(answer) != fields:
        raise MissingMemoryObservation("complete four-field /proc memory sample unavailable")
    require(all(type(v) is int and v >= 0 for v in answer.values()),
            "nonnumeric/negative /proc memory observation")
    return answer


def resources(phase):
    own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    child = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    current = mm()
    require(type(own) in (int, float) and type(child) in (int, float)
            and own >= 0 and child >= 0, "actual numeric high-water required")
    R["resources"][phase] = {"ru_maxrss_self_kib": own,
                              "ru_maxrss_largest_child_kib": child,
                              "current_proc_self_status": current,
                              "elapsed_seconds": (time.monotonic_ns() - START_NS) / 1e9}
    require(own <= AS_PARENT // 1024 and child <= AS_PARENT // 1024,
            "noncompliant observed high-water memory; origin not inferred")
    require(own + child <= INNER_MEMORY_KIB, "inner high-water plus reserved32MiB supervisor exceeds256MiB")
    require(all(value <= AS_PARENT // 1024 for value in current.values()),
            "noncompliant/missing current self memory observation")


def scratch():
    total = sum(os.fstat(fd).st_size for fd in OWN)
    require(total <= SCRATCH, "own memfd scratch cap exceeded")
    if HOME_FD is not None:
        require(_directory_names(HOME_FD) == [], "private homedir ceased to be empty")
    return total


def new_mem(name, data=b"", sealed=False):
    require(len(data) <= RAW, "own raw/body cap")
    fd = acquire_owned(os.memfd_create, "memfd", "A048-" + name, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    OWN.add(fd)
    pos = 0
    while pos < len(data):
        due()
        pos += os.pwrite(fd, data[pos:pos + 65536], pos)
    if sealed:
        seal(fd)
    scratch()
    return fd


def seal(fd):
    fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW
                | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)


def close_once(fd, where):
    return close_allocation(fd, where)

def close_own(fd):
    require(isinstance(fd, AllocationFD), "unregistered own close")
    if fd.allocation["attempted_once"]:
        return close_once(fd, "own")
    require(fd in OWN, "close of an unowned allocation refused")
    OWN.remove(fd)
    return close_once(fd, "own")

def raw_cell(name, fd=None):
    return {"name": name, "fd": fd, "attempted_once": False,
            "produced_bytes": None, "bytes": None, "sha256": None,
            "partial": True, "retained": False, "encoding": None, "text": None,
            "raw_base64": None, "output_charge_bytes": None,
            "identity_before": None, "identity_after": None,
            "capture_error": None, "decode_error": None,
            "raw_object": {"parts": [], "pending": None, "tail": None,
                           "capture_error_object": None, "decode_error_object": None}}


def retain_cell(cell, quota, settled, written=False):
    global TOTAL_OUTPUT, OUTPUT_UNKNOWN, CHANNEL_TAIL_USED
    if cell["attempted_once"]:
        return  # no second physical read/charge, not a retry after an error
    cell["attempted_once"] = True
    owner = cell["raw_object"]
    if cell["fd"] is None:
        cell["capture_error"] = {"not_created_or_no_returned_fd": True}
        OUTPUT_UNKNOWN = True
        return
    try:
        before = identity(os.fstat(cell["fd"]))
        cell["identity_before"] = before
        produced = before["size"]
        require(type(produced) is int and produced >= 0, "unknown produced size")
        cell["produced_bytes"] = produced
        cell["output_charge_bytes"] = produced
        TOTAL_OUTPUT += produced  # actual observed production, not retained-prefix count
        require(produced <= quota and TOTAL_OUTPUT <= OUTPUT,
                "produced output exceeds original per-file/aggregate quota")
        require(settled, "unsettled producer cannot supply terminal fullraw")
        pos = 0
        while pos < produced:
            amount = min(65536, produced - pos)
            _read_room(amount)
            owner["pending"] = os.pread(cell["fd"], amount, pos)
            _charge_content(len(owner["pending"]))
            require(bool(owner["pending"]), "short produced output read")
            # Compute allocating arithmetic BEFORE commit. Mask only the exact
            # append/offset/pending commit, not reads, deadlines or whole cleanup.
            next_pos = pos + len(owner["pending"])
            saved = signal.pthread_sigmask(signal.SIG_BLOCK, FD_SIGNALS)
            try:
                owner["parts"].append(owner["pending"])
                pos = next_pos
                owner["pending"] = None
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, saved)
        _read_room(1)
        owner["tail"] = os.pread(cell["fd"], 1, pos)
        _charge_content(len(owner["tail"]))
        cell["identity_after"] = identity(os.fstat(cell["fd"]))
        require(not owner["tail"] and cell["identity_after"] == before,
                "produced output EOF/fullidentity drift")
        cell["partial"] = False
    except BaseException as exc:
        owner["capture_error_object"] = exc  # native object held before projection
        cell["capture_error"] = deferred_error(exc)
        OUTPUT_UNKNOWN = OUTPUT_UNKNOWN or cell["produced_bytes"] is None or not settled
    # Every successful/partial returned chunk remains owned even if join/encode fails.
    parts = owner["parts"] + ([owner["pending"]] if owner["pending"] is not None else [])
    data = b"".join(parts)
    cell["bytes"] = len(data)
    cell["sha256"] = hashlib.sha256(data).hexdigest()
    cell["retained"] = not cell["partial"] and len(data) == cell["produced_bytes"]
    # One lossless representation, never a hash-only writable snapshot.
    if written:
        cell["encoding"] = "base64"
        cell["raw_base64"] = base64.b64encode(data).decode("ascii")
    else:
        try:
            text = data.decode("utf-8", "strict")
            escaped = len(json.dumps(text, ensure_ascii=True, separators=(",", ":")))
            if escaped <= 2 * len(data) + 2:
                cell["text"], cell["encoding"] = text, "utf-8"
            else:
                cell["raw_base64"], cell["encoding"] = base64.b64encode(data).decode("ascii"), "base64"
        except UnicodeError as exc:
            owner["decode_error_object"] = exc
            cell["decode_error"] = deferred_error(exc)
            cell["raw_base64"], cell["encoding"] = base64.b64encode(data).decode("ascii"), "base64"
    CHANNEL_TAIL_USED += len(data)


def published_cell(cell):
    # Raw object and original errors stay in this owner until native process end.
    # JSON carries full reversible bytes + complete projected causal errors.
    return {k: v for k, v in cell.items() if k != "raw_object"}


def armor(raw, label):
    require(0 < len(raw) <= RAW and b"\r" not in raw, "armor length/line endings")
    begin = ("-----BEGIN " + label + "-----\n").encode("ascii")
    end = ("-----END " + label + "-----\n").encode("ascii")
    require(raw.startswith(begin + b"\n") and raw.endswith(end)
            and raw.count(begin) == raw.count(end) == 1, "armor framing/trailer")
    lines = raw[len(begin) + 1:-len(end)].split(b"\n")
    require(lines[-1] == b"", "armor final line")
    lines.pop()
    require(len(lines) >= 2 and re.fullmatch(rb"=[A-Za-z0-9+/]{4}", lines[-1]),
            "armor CRC framing")
    require(all(re.fullmatch(rb"[A-Za-z0-9+/]{1,76}={0,2}", s) for s in lines[:-1]),
            "armor base64 rows")
    text = b"".join(lines[:-1])
    binary = base64.b64decode(text, validate=True)
    require(base64.b64encode(binary) == text, "noncanonical armor base64")
    crc = 0xB704CE
    for byte in binary:
        due()
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
        crc &= 0xFFFFFF
    require(crc.to_bytes(3, "big") == base64.b64decode(lines[-1][1:], validate=True),
            "armor CRC mismatch")
    return binary


def parse_raw(raw):
    header = b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\n"
    marker = b"-----BEGIN PGP SIGNATURE-----\n"
    require(len(raw) <= RAW and raw.startswith(header) and b"\r" not in raw,
            "strict clearsign header/line endings")
    rest = raw[len(header):]
    require(rest.count(marker) == 1, "exactly one signature armor required")
    body, tail = rest.split(marker)
    packet = armor(marker + tail, "PGP SIGNATURE")
    require(body.endswith(b"\n") and 0 < len(body) <= RAW, "signed body framing")
    seen, selected = set(), []
    for index, line in enumerate(body[:-1].split(b"\n")):
        m = re.fullmatch(rb"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._/-]{0,200})", line)
        require(m is not None, "malformed/dash-escaped checksum row")
        name = m[2].decode("ascii")
        require(name not in seen and all(x not in ("", ".", "..") for x in name.split("/")),
                "duplicate or unsafe checksum filename")
        seen.add(name)
        if name == TARGET:
            selected.append((index, m[1].decode("ascii"), line))
    require(len(selected) == 1, "selected filename must have exactly one row")
    return body, marker + tail, packet, selected[0], len(seen)


def child_limits(file_cap):
    # Single-threaded parent. These limits precede exec of trusted prlimit and
    # GPG, then prlimit independently repeats the exact lower hard/soft limits.
    resource.setrlimit(resource.RLIMIT_AS, (AS_CHILD, AS_CHILD))
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_cap, file_cap))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))


def reap(p):
    global ACTIVE
    # Only this exact unreaped Popen child; never process-group or broad PID kill.
    if p.poll() is None:
        try:
            os.kill(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        p.wait(timeout=min(5.0, max(0.001, (DEADLINE_NS - time.monotonic_ns()) / 1e9)))
    except BaseException:
        stop("exact owned child could not be finitely reaped")
    ACTIVE = None


ORIGINAL_STOCK_CALLS = (
    ("show-raw-key", "gpg", 1, 0),
    ("dearmor", "gpg", 1, 1),
    ("check-self-signatures-and-bindings", "gpg", 1, 0),
    ("positive-gpg", "gpg", 2, 0),
    ("positive-gpgv-body", "gpgv", 2, 1),
    ("negative-same-path-changed-row", "gpg", 2, 0),
)


def call(label, tool, args, inputs, writable=()):
    global ACTIVE
    # Complete owner graph/slots before ANY call prefix, including constructor.
    stream_cells = [raw_cell(name) for name in ("stdout", "stderr", "status")]
    write_cells = [raw_cell("written", fd) for fd in writable]
    streams = [None, None, None]
    # One reap + every raw cell + every stream close + every public-cell copy.
    cells = stream_cells + write_cells
    secondary_capacity = 2 + 2 * len(cells) + len(streams)
    secondary_objects = [None] * secondary_capacity
    secondary_tracebacks = [None] * secondary_capacity
    secondary_public = [None] * secondary_capacity
    secondary_used = 0
    def note_secondary(exc):
        nonlocal secondary_used
        # Store native object first, before the private marker/public copy.
        require(secondary_used < secondary_capacity, "finite call secondary slot overflow")
        secondary_objects[secondary_used] = exc
        secondary_tracebacks[secondary_used] = exc.__traceback__
        secondary_used += 1
    item = {"label": label, "rc": None, "reaped": False, "pid": None,
            "spawn_state": "NOT_ATTEMPTED", "failure": None,
            "secondary_errors": [], "capture_complete": False,
            "stdout": None, "stderr": None, "status": None, "written_data": [],
            "output_charge_unknown": True, "elapsed_seconds": None,
            "child_proc_status_sample": None,
            "child_proc_status_missing_reason": "child not yet observed alive",
            "complete_child_samples": 0}
    start, p, primary, primary_tb, quota = time.monotonic_ns(), None, None, None, None
    R["calls"].append(item)
    # Nonserialized exact objects are owned by a separate same-lifetime graph.
    owner = {"record": item, "streams": stream_cells, "written": write_cells,
             "primary": None, "primary_tb": None, "secondary": secondary_objects,
             "secondary_tb": secondary_tracebacks,
             "popen": None, "secondary_used": 0, "publication_error": None,
             "publication_tb": None}
    CALL_OWNERS.append(owner)
    try:
        due()
        require(ACTIVE is None and 1 <= len(R["calls"]) <= len(ORIGINAL_STOCK_CALLS),
                "original six actual factories; no extra producer birth")
        require((label, tool, len(inputs), len(writable)) == ORIGINAL_STOCK_CALLS[len(R["calls"]) - 1],
                "original ordered factory/tool/input/writable domain before spawn")
        custody()
        resources("before_call_" + str(len(R["calls"])))
        scratch()
        remaining = OUTPUT - TOTAL_OUTPUT
        slots = 3 + len(writable)
        room = channel_raw_room()
        require(room > 0, "derived raw room exhausted before spawn")
        quota = min(remaining // slots, room // slots)
        require(quota > 0 and quota * slots <= room,
                "call raw exceeds the derived frame room before spawn")
        item.update(per_file_output_cap=quota, all_output_slots=3 + len(writable),
                    timeout_seconds=30, preexec_hard_and_soft_as=AS_CHILD)
        for i, suffix in enumerate(("-stdout", "-stderr", "-status")):
            streams[i] = new_mem(label + suffix)
            stream_cells[i]["fd"] = streams[i]
        out, err, statusfd = streams
        expanded = [str(statusfd) if value == "{STATUS}" else value for value in args]
        pfd, tfd = FILES["prlimit"]["fd"], FILES[tool]["fd"]
        limits = ["--as=67108864:67108864", "--cpu=20:20",
                  "--fsize=" + str(quota) + ":" + str(quota),
                  "--nofile=128:128", "--core=0:0", "--nproc=0:0"]
        command = ["prlimit"] + limits + ["--", "/proc/self/fd/" + str(tfd)] + expanded
        passed = tuple(sorted(set([pfd, tfd, statusfd] + list(inputs) + list(writable))))
        item.update(executable="/proc/self/fd/" + str(pfd), argv=command,
                    tool_fd=tfd, shell=False, env=dict(ENV, HOME=HOME, GNUPGHOME=HOME),
                    pass_fds=list(passed))
        argv_bytes = len(json.dumps(command, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        require(argv_bytes <= ARGV_CEILING, "derived call argv ceiling before spawn")
        item["spawn_state"] = "UNKNOWN_BEFORE_POPEN"
        p = subprocess.Popen(command, executable="/proc/self/fd/" + str(pfd),
                             env=item["env"], stdin=subprocess.DEVNULL,
                             stdout=out, stderr=err, pass_fds=passed,
                             close_fds=True, shell=False,
                             preexec_fn=lambda: child_limits(quota))
        owner["popen"] = p
        ACTIVE = p
        item["pid"], item["spawn_state"] = p.pid, "RETURNED_EXACT_POPEN"
        while p.poll() is None:
            due()
            require(time.monotonic_ns() - start < 30000000000, "GPG child wall timeout")
            try:
                sample = mm(p.pid)
                item["child_proc_status_sample"] = sample
                item["child_proc_status_missing_reason"] = None
                item["complete_child_samples"] += 1
                require(all(sample[k] <= AS_PARENT // 1024 for k in
                            ("VmSize_kib", "VmRSS_kib", "VmHWM_kib", "VmPeak_kib")),
                        "child transient memory budget exceeded")
            except (FileNotFoundError, ProcessLookupError, MissingMemoryObservation) as exc:
                item["child_proc_status_sample"] = None
                item["child_proc_status_missing_reason"] = type(exc).__name__ + ": " + str(exc)
                if p.poll() is None:
                    raise Refusal("mandatory live-child complete memory sample unavailable")
            p.wait(timeout=0.05) if p.poll() is not None else select.select([], [], [], 0.02)
        if item["complete_child_samples"] == 0 and item["child_proc_status_missing_reason"] == "child not yet observed alive":
            item["child_proc_status_missing_reason"] = "child exited before any complete live sample; kernel caps and actual child high-water remain separate"
        item["rc"], item["reaped"] = p.returncode, True
        ACTIVE = None
    except BaseException as exc:
        primary, primary_tb = exc, exc.__traceback__
        owner["primary"], owner["primary_tb"] = primary, primary_tb
    finally:
        if p is not None and not item["reaped"]:
            try:
                reap(p)
                item["rc"], item["reaped"] = p.returncode, True
            except BaseException as exc:
                note_secondary(exc)
        elif item["spawn_state"] == "NOT_ATTEMPTED":
            item["reaped"] = True  # no creation attempt, not a fabricated child rc
        for cell in cells:
            raw_owner = cell.get("raw_object") or {}
            incomplete = (raw_owner.get("pending") is not None
                          or raw_owner.get("capture_error_object") is not None
                          or (cell.get("attempted_once") and not cell.get("retained"))
                          or (cell.get("produced_bytes") is not None
                              and cell.get("bytes") not in (None, cell.get("produced_bytes"))))
            if cell.get("fd") is not None and incomplete:
                hold_unread_prefix(getattr(cell.get("fd"), "allocation", None))
        # EVERY prefix captures ALL existing buffers before ANY stream firstclose.
        for cell in cells:
            try:
                if cell["fd"] is not None:
                    retain_cell(cell, quota if quota is not None else 0,
                                item["reaped"], written=cell["name"] == "written")
            except BaseException as exc:
                if cell.get("fd") is not None and not cell.get("retained"):
                    hold_unread_prefix(getattr(cell.get("fd"), "allocation", None))
                note_secondary(exc)
        for cell in cells:
            raw_owner = cell.get("raw_object") or {}
            produced = cell.get("produced_bytes")
            retained = cell.get("bytes")
            if (cell.get("partial") or raw_owner.get("pending") is not None
                    or raw_owner.get("capture_error_object") is not None
                    or not cell.get("retained")
                    or (produced is not None and retained != produced)):
                hold_unread_prefix(getattr(cell.get("fd"), "allocation", None))
        for fd in streams:
            if fd is None:
                continue
            try:
                close_own(fd)
            except BaseException as exc:
                note_secondary(exc)
        # Fallible public-cell copying happens only AFTER all stream firstcloses.
        for name, cell in zip(("stdout", "stderr", "status"), stream_cells):
            try:
                item[name] = published_cell(cell)
            except BaseException as exc:
                note_secondary(exc)
        try:
            item["written_data"] = [published_cell(cell) for cell in write_cells]
            item["capture_complete"] = all(c["retained"] and not c["partial"] for c in cells)
            item["output_charge_unknown"] = any(c["output_charge_bytes"] is None for c in cells)
            item["elapsed_seconds"] = (time.monotonic_ns() - start) / 1e9
        except BaseException as exc:
            note_secondary(exc)
        owner["secondary_used"] = secondary_used
        try:
            if primary is not None:
                item["failure"] = deferred_error(primary)
            for i in range(secondary_used):
                secondary_public[i] = deferred_error(secondary_objects[i])
            item["secondary_errors"] = secondary_public[:secondary_used]
        except BaseException as exc:
            owner["publication_error"] = exc
            owner["publication_tb"] = exc.__traceback__
            # Never replace the original first fault with a publication fault.
    if primary is not None:
        raise primary.with_traceback(primary_tb)  # original firstfault, not cleanup replacement
    if secondary_used:
        raise secondary_objects[0].with_traceback(secondary_tracebacks[0])
    if owner["publication_error"] is not None:
        raise owner["publication_error"].with_traceback(owner["publication_tb"])
    require(item["capture_complete"] and not item["output_charge_unknown"] and not OUTPUT_UNKNOWN,
            "complete actual output/count/charge required")
    require(all(item[name]["encoding"] == "utf-8" for name in ("stdout", "stderr", "status")),
            "complete utf-8 stream required on the normal return")
    scratch()
    custody()
    return item


def statuses(c):
    rows = []
    for line in c["status"]["text"].splitlines():
        require(line.startswith("[GNUPG:] ") and len(line) <= 4096,
                "malformed trusted status channel")
        rows.append(line[9:].split(" "))
    require(len(rows) <= 256, "status row cap")
    return rows


def signature(c, keys):
    rows = statuses(c)
    bad_names = {"BADSIG", "ERRSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG",
                 "KEYEXPIRED", "SIGEXPIRED", "KEYREVOKED", "NO_PUBKEY",
                 "NODATA", "FAILURE", "ERROR", "BADARMOR"}
    goods = [x for x in rows if x[0] == "GOODSIG"]
    valids = [x for x in rows if x[0] == "VALIDSIG"]
    bad = [x for x in rows if x[0] in bad_names]
    accepted = c["rc"] == 0 and len(goods) == len(valids) == 1 and not bad
    signer, primary = None, None
    if accepted:
        v = valids[0]
        require(len(v) == 11 and re.fullmatch(r"[0-9A-F]{40}", v[1]),
                "VALIDSIG primary/signer fields required")
        signer, primary = v[1], v[10]
        now = int(time.time())
        require(v[3].isdigit() and v[4].isdigit(), "signature time syntax")
        key = keys.get(signer)
        accepted = (primary == EXPECTED and key is not None and len(goods[0]) >= 2
                    and goods[0][1] == signer[-16:] and v[5] == "4"
                    and v[7] == key["algorithm"] and v[8] == "8" and v[9] == "01"
                    and key["created"] <= int(v[3]) <= now
                    and (int(v[4]) == 0 or int(v[4]) > now)
                    and (key["expires"] == 0 or key["expires"] > now)
                    and "s" in key["caps"] and "D" not in key["caps"])
    return {"accepted": accepted, "signer": signer, "primary": primary,
            "bad_statuses": bad, "status_rows": rows, "rc": c["rc"]}


def key_listing(c, checked=False):
    require(c["rc"] == 0, "key listing/check failed")
    require(not any(x[0] in {"ERROR", "FAILURE", "NODATA", "BADARMOR"}
                    for x in statuses(c)), "key listing status refusal")
    rows = [x.split(":") for x in c["stdout"]["text"].splitlines()]
    require(len(rows) <= 256, "key colon row cap")
    keys, pub_count, current, section = {}, 0, None, None
    certs, bindings = 0, set()
    now = int(time.time())
    for row in rows:
        kind = row[0]
        if kind in ("pub", "sub"):
            require(len(row) >= 12 and row[1] not in ("r", "e", "d", "i")
                    and "D" not in row[11] and row[5].isdigit()
                    and (not row[6] or row[6].isdigit()), "invalid/revoked/expired key")
            created, expires = int(row[5]), int(row[6] or "0")
            require(created <= now and (expires == 0 or expires > now), "key time validity")
            current = {"kind": kind, "created": created, "expires": expires,
                       "algorithm": row[3], "keyid": row[4], "caps": row[11]}
            section = kind
            if kind == "pub":
                pub_count += 1
        elif kind == "fpr":
            require(current is not None and "fingerprint" not in current and len(row) >= 10
                    and re.fullmatch(r"[0-9A-F]{40}", row[9]), "key fingerprint association")
            fp = row[9]
            require(fp not in keys and current["keyid"] == fp[-16:], "duplicate/keyid fingerprint")
            current["fingerprint"] = fp
            if current["kind"] == "pub":
                require(fp == EXPECTED, "independent primary mismatch")
            keys[fp] = current
        elif kind in ("uid", "uat"):
            section = kind
            require(len(row) > 1 and row[1] not in ("r", "e", "d", "i"), "invalid UID")
        elif kind in ("rev", "rvs", "sec", "ssb"):
            raise Refusal("revocation or secret key record in raw key")
        elif kind == "sig" and checked:
            require(len(row) >= 13 and row[1] == "!" and row[12] == EXPECTED
                    and row[4] == EXPECTED[-16:] and row[5].isdigit()
                    and int(row[5]) <= now
                    and (not row[6] or (row[6].isdigit() and int(row[6]) > now)),
                    "unverified/non-primary/current-time key certification")
            cls = row[10][:2]
            if section == "sub":
                require(cls == "18" and current is not None
                        and current["kind"] == "sub", "actual subkey binding association")
                bindings.add(current["fingerprint"])
            elif section in ("uid", "uat"):
                require(cls in ("10", "11", "12", "13"), "UID certification class")
                certs += 1
            else:
                require(cls == "1f", "primary direct-key certification class")
                certs += 1
    require(pub_count == 1 and EXPECTED in keys
            and all("fingerprint" in value for value in keys.values()), "one primary required")
    if checked:
        require(certs > 0 and all(k in bindings for k, v in keys.items() if v["kind"] == "sub"),
                "missing checked primary certification or actual subkey binding")
    return keys, {"keys": keys, "checked": checked,
                  "verified_primary_certifications": certs if checked else None,
                  "verified_subkey_bindings": sorted(bindings) if checked else None}


def compare(fd, expected_digest, expected_size):
    digest, size, _ = read_fd(fd, ARCH, False)
    return {"accepted": digest == expected_digest and size == expected_size,
            "actual_sha256": digest, "expected_sha256": expected_digest,
            "actual_bytes": size, "expected_bytes": expected_size,
            "decision_function": "compare", "held_archive_fd": fd}


def make_home(name):
    global HOME, HOME_FD
    pfd = directory("/var/tmp")
    require(re.fullmatch(r"friday-a048-g1-[0-9a-f]{32}", name) is not None,
            "exact supervisor-owned private-home name required")
    # Record exact held parent/name BEFORE mkdir. No exception can erase intent.
    HOME = "/var/tmp/" + name
    HOME_INTENT.update(state="PENDING", path=HOME, name=name,
                       parent_identity=identity(os.fstat(pfd), True),
                       parent_fd=pfd, absence=None)
    R["private_home_intent"] = HOME_INTENT
    try:
        os.mkdir(name, 0o700, dir_fd=pfd)
    except FileExistsError:
        HOME_INTENT.update(state="DEFINITELY_NOT_CREATED_EEXIST", absence=False)
        # Never own/delete the colliding target, even if it happens to be empty.
        raise Refusal("private name EEXIST; target remains unowned")
    except OSError as exc:
        # Only EEXIST has the explicitly unowned branch. Other errors, including
        # interrupted/IO states, are conservative unknown outcomes, not absence.
        HOME_INTENT.update(state="UNKNOWN",
                           mkdir_errno=exc.errno, absence=None)
        stop("private mkdir OS error; creation outcome unconfirmed")
    except BaseException:
        HOME_INTENT["state"] = "UNKNOWN"
        stop("private mkdir outcome unknown at exact held parent/name")
    HOME_INTENT["state"] = "CONFIRMED_CREATED"
    try:
        HOME_FD = acquire_owned(os.open, "open", name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                          dir_fd=pfd)
        before = identity(os.fstat(HOME_FD), True)
        HOME_INTENT["created_identity"] = before
        require(identity(os.stat(name, dir_fd=pfd, follow_symlinks=False), True) == before
                and os.fstat(HOME_FD).st_uid == os.getuid()
                and stat.S_IMODE(os.fstat(HOME_FD).st_mode) == 0o700
                and _directory_names(HOME_FD) == [], "new private directory identity/owner/mode/contents")
        os.fchmod(HOME_FD, 0o500)
        HOME_INTENT["created_identity"] = identity(os.fstat(HOME_FD), True)
        R["private_home"] = {"path": HOME, "identity": HOME_INTENT["created_identity"],
                             "mode": "0500", "initially_empty": True,
                             "file_writes_disallowed_by_directory_mode": True}
    except BaseException:
        stop("confirmed-created directory custody/setup incomplete; retain exact target")


def cleanup():
    global HOME_FD, ACTIVE
    if ACTIVE is not None:
        reap(ACTIVE)
    require(all(c["reaped"] for c in R["calls"]), "owned child reap incomplete")
    state = HOME_INTENT["state"]
    if state in ("PENDING", "UNKNOWN"):
        stop("pending/unknown mkdir outcome; absence and ownership unconfirmed")
    if state == "CONFIRMED_CREATED":
        if HOME_FD is None or HOME_INTENT["created_identity"] is None:
            stop("confirmed-created directory lacks exact held identity")
        pfd, name = HOME_INTENT["parent_fd"], HOME_INTENT["name"]
        require(identity(os.fstat(pfd), True) == HOME_INTENT["parent_identity"],
                "private parent held identity drift")
        require(identity(os.stat(name, dir_fd=pfd, follow_symlinks=False), True)
                == identity(os.fstat(HOME_FD), True) == HOME_INTENT["created_identity"],
                "private home custody failure")
        if _directory_names(HOME_FD):
            stop("private home unexpected entries; no traversal/deletion")
        # A completed rmdir and later lookup are separate confirmed outcomes.
        HOME_INTENT["state"] = "REMOVE_PENDING"
        os.rmdir(name, dir_fd=pfd)
        try:
            os.stat(name, dir_fd=pfd, follow_symlinks=False)
        except FileNotFoundError:
            HOME_INTENT.update(state="CONFIRMED_REMOVED", absence=True)
        else:
            stop("private removal absence not confirmed")
        fd = HOME_FD
        HOME_FD = None
        close_once(fd, "home")
    elif state.startswith("DEFINITELY_NOT_CREATED"):
        # No deletion of EEXIST/unowned target. Observation is about exact name,
        # and never about HOME being None. Presence may belong to someone else.
        try:
            os.stat(HOME_INTENT["name"], dir_fd=HOME_INTENT["parent_fd"],
                    follow_symlinks=False)
        except FileNotFoundError:
            HOME_INTENT["absence"] = True
        else:
            HOME_INTENT["absence"] = False
    elif state == "NOT_ATTEMPTED":
        HOME_INTENT["absence"] = None  # no name exists to observe, never fake True
    elif state != "CONFIRMED_REMOVED":
        stop("private directory terminal state unconfirmed: " + state)
    for fd in list(OWN):
        close_own(fd)
    for item in FILES.values():
        fd = item["fd"]
        item["fd"] = None
        close_once(fd, "file")
    pending_dirs = []
    for path, pair in list(DIRECTORIES.items()):
        pending_dirs.append(pair[0])
        DIRECTORIES[path] = (None, pair[1])
    for fd in pending_dirs:
        close_once(fd, "directory")
    if VERIFIER_RUNTIME is not None and VERIFIER_RUNTIME.get("own_new") and not VERIFIER_RUNTIME["closed"]:
        VERIFIER_RUNTIME["closed"] = True
        close_once(VERIFIER_RUNTIME["fd"], "runtime")
    for fd in INHERITED_OWNED:
        close_once(fd, "inherited_role")
    R["own_runtime_descriptor_closed"] = VERIFIER_RUNTIME["closed"] if VERIFIER_RUNTIME else None
    R["cleanup"] = {"exact_owned_child_reaped": True,
                    "private_home_absent": HOME_INTENT["absence"],
                    "private_home_state": HOME_INTENT["state"],
                    "all_own_and_input_and_component_descriptors_closed": FD_UNREAD_HOLDS == 0}



def commit_existing_receiver(frame):
    # Same exact prefix+frame grammar for both original receiver kinds.
    # Successful IO is byte retention only; outside native custody is NOT proven.
    require(type(frame) is bytes and frame.startswith(b"FR875 ") and 0 < len(frame) <= PUBLIC_OUTPUT - PARENT_PLACED,
            "complete prefix plus final frame must fit original receiver cap")
    append_parent_bytes(frame, final=True)
    return frame


def encode_bounded(record):
    finalize_read_accounting()  # includes actual prefix readback, not future IO
    publish_errors(record)
    return encode_channel_frame(record, PUBLIC_OUTPUT - PARENT_PLACED)


def emit(data):
    # Bounded whole serialization precedes the first byte. A partial receipt is
    # never acceptance; caller also requires complete JSON and successful exit.
    flags = fcntl.fcntl(1, fcntl.F_GETFL)
    fcntl.fcntl(1, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    sent = 0
    while sent < len(data):
        due(True)
        try:
            n = os.write(1, data[sent:sent + 4096])
            require(n > 0, "public output stalled")
            sent += n
        except BlockingIOError:
            select.select([], [1], [], min(0.05, max(0.0, (DEADLINE_NS - time.monotonic_ns()) / 1e9)))



FFI_PATH = "/usr/lib/x86_64-linux-gnu/libffi.so.8.2.0"
FFI_SHA = "1a0dc86f787f73e025a6e521056360afcbe70f2a82cd808132fefc2b4ee95daa"
FFI_SIZE = 64184
OWN_RUNTIME_FD = json.loads("""{"role":"own_stock_runtime","ambient_input":false,"path":"/usr/lib/x86_64-linux-gnu/libffi.so.8.2.0","sha256":"1a0dc86f787f73e025a6e521056360afcbe70f2a82cd808132fefc2b4ee95daa","size":64184,"mode":"0644","uid":0,"gid":0,"nlink":1,"flags":"O_RDONLY|FD_CLOEXEC","authority":"Explicit ordinary owned-source stock dependency assumption only, NOT immutable whole-root image proof","provenance":"ABSENT_AT_STRICT_INHERITED_ENTRY_NEW_DURING_STOCK_CALLBACK_PREPARATION","inherited_bootstrap_before_ctypes":[0,1,2,3,4,5,8],"child_canonical_roles":{"3":"prlimit","4":"python","5":"source","6":"capsule_contract"},"collision":"fd6 dup2 replaces own runtime with capsule contract before exec; fd 8 stays the outside body; other fd>=7 use closerange(7,8) then closerange(9,128); runtime role is not source, capsule, fd 8, or an ambient input; unknown inherited fds are refused and not closed or reclassified","supervisor_hash_check":"FULL_OBSERVED","verifier_hash_check":"FULL_OBSERVED_ONLY_IF_NEW_ELSE_ABSENT","image_proof":false,"acquisition":{"operation":"ctypes.CFUNCTYPE(ctypes.c_int)(lambda: 0)","retained_role":"RUNTIME_CALLBACK","callback_invoked":false,"window":"exact inherited 0..5 and fd 8 before ctypes import; retain the benign callback; exact one new pinned stock descriptor immediately afterward before kernel policy"},"read_accounting":{"schema":"friday.e4.node.explicit-read-ledger.a073.v1","runtime_content_ceiling_bytes":262144,"child_bootstrap_upper_debit_bytes":32768,"verifier_whole_content_ceiling_bytes":4294967296,"child_debit_kind":"FIXED_CONSERVATIVE_UPPER_BOUND_NOT_MEASURED","child_debit_scope":"gate token, post-remap directory scan and four bounded role readlinks; no copied supervisor prefix","local_scope":"all explicit content reads in this verifier including both import-window scans, files, proc memory, readlinks and own directory scans","implicit_io":"UNKNOWN_NOT_ZERO","parent_prefix_carried":false,"reset_or_subtraction":false,"execution_credit":false}}""")


def charge_runtime(n):
    _charge_content(n, True)


def verifier_link(fd):
    value = os.readlink("/proc/self/fd/" + str(fd))
    charge_runtime(len(value.encode("utf-8")))
    require(len(value.encode("utf-8")) < 4096, "runtime readlink content cap")
    return value


def prove_verifier_runtime_window():
    global VERIFIER_RUNTIME
    require(set(_PRE_IMPORT_META) == {0, 1, 2, 3, 4, 5, 6, 8},
            "unknown bootstrap inherited descriptor")
    pre = set(_PRE_IMPORT_META)
    post = set(_POST_IMPORT_META)
    require(pre <= post, "inherited descriptor closed during verifier imports")
    for fd, before in _PRE_IMPORT_META.items():
        require(_POST_IMPORT_META[fd] == before
                and _PRE_IMPORT_INHERITABLE[fd] == _POST_IMPORT_INHERITABLE[fd],
                "inherited descriptor identity/flags changed during verifier imports")
    extra = sorted(post - pre)
    record = {"present": False, "ambient_input": False, "image_proof": False,
              "sha256_pin": FFI_SHA, "hash_check": "ABSENT",
              "role": "own_stock_runtime", "canonical_roles_unchanged": True}
    if not extra:
        VERIFIER_RUNTIME = None
        R["runtime_fd"] = record
        R["runtime_explicit_read_bytes"] = RUNTIME_READS
        R["runtime_read_reset_or_subtracted"] = False
        return
    require(len(extra) == 1, "unknown descriptor opened beside own stock ffi")
    fd = extra[0]
    require(fd not in pre and fd != 8 and 7 <= fd < 128, "verifier runtime descriptor collides with canonical roles")
    VERIFIER_RUNTIME = {"fd": adopt_exact_fd(fd, "runtime"), "own_new": True, "closed": False}
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    require(fcntl.fcntl(fd, fcntl.F_GETFD) == fcntl.FD_CLOEXEC, "own runtime descriptor is not CLOEXEC")
    require(flags & os.O_ACCMODE == os.O_RDONLY and not flags & (os.O_PATH | os.O_APPEND),
            "own runtime descriptor is not ordinary readonly")
    info = os.fstat(fd)
    named = os.stat(FFI_PATH, follow_symlinks=False)
    require(verifier_link(fd) == FFI_PATH and stat.S_ISREG(info.st_mode) and stat.S_ISREG(named.st_mode),
            "own ffi path is not the Root-selected regular file")
    require((info.st_dev, info.st_ino) == (named.st_dev, named.st_ino)
            and info.st_size == FFI_SIZE and info.st_uid == 0 and info.st_gid == 0
            and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o644,
            "Root-selected ffi identity/mode mismatch")
    before = _POST_IMPORT_META[fd]
    named_before = identity(named)
    require(identity(os.fstat(fd)) == named_before, "own ffi before/opened metadata mismatch")
    digest = hashlib.sha256()
    offset = 0
    while offset < info.st_size:
        amount = min(4096, info.st_size - offset)
        _read_room(amount)
        data = os.pread(fd, amount, offset)
        charge_runtime(len(data))
        require(len(data) == amount, "own ffi read short")
        digest.update(data)
        offset += len(data)
    final_map = _descriptor_meta()
    require(set(final_map) == post and final_map[fd] == before
            and all(final_map[number] == meta for number, meta in _PRE_IMPORT_META.items())
            and offset == FFI_SIZE and digest.hexdigest() == FFI_SHA
            and identity(os.stat(FFI_PATH, follow_symlinks=False)) ==
                identity(os.fstat(fd)) == named_before
            and fcntl.fcntl(fd, fcntl.F_GETFL) == flags
            and fcntl.fcntl(fd, fcntl.F_GETFD) == fcntl.FD_CLOEXEC
            and verifier_link(fd) == FFI_PATH, "own ffi Root-selected SHA/path/metadata/flags drift")
    VERIFIER_RUNTIME.update(metadata=before, status_flags=flags,
                            descriptor_flags=fcntl.FD_CLOEXEC, proven=True)
    record.update(present=True, fd=fd, hash_check="FULL_OBSERVED", path=FFI_PATH,
                  sha256=FFI_SHA, size=FFI_SIZE, metadata=before,
                  status_flags=flags, descriptor_flags=fcntl.FD_CLOEXEC,
                  provenance="ABSENT_AT_STRICT_INHERITED_ENTRY_NEW_DURING_VERIFIER_IMPORTS")
    R["runtime_fd"] = record
    R["runtime_explicit_read_bytes"] = RUNTIME_READS
    R["runtime_read_reset_or_subtracted"] = False

INHERITED_OWNED = [adopt_exact_fd(fd, "inherited_role") for fd in (3, 4, 5, 6)]


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

def main():
    global DEADLINE_NS, MAIN_END_NS
    prove_joined_stock_channels()
    prove_verifier_runtime_window()
    adopt_outside_body()
    require(len(sys.argv) == 11 and sys.argv[7] == "--private-home-name"
            and sys.argv[9] == "--child-bootstrap-read-debit"
            and sys.argv[10] == str(CHILD_READ_DEBIT)
            and sys.argv[1] == "--reviewed-source-sha256"
            and sys.argv[3] == "--reviewed-contract-sha256"
            and sys.argv[5] == "--deadline-monotonic-ns", "exact reviewed CLI required")
    sh, ch = sys.argv[2], sys.argv[4]
    require(re.fullmatch(r"[0-9a-f]{64}", sh) and re.fullmatch(r"[0-9a-f]{64}", ch)
            and sys.argv[6].isdigit(), "reviewed pins/deadline syntax")
    DEADLINE_NS = int(sys.argv[6])
    require(20000000000 < DEADLINE_NS - START_NS <= 900000000000,
            "deadline must be externally established before launch, <=900s")
    MAIN_END_NS = DEADLINE_NS - 20000000000
    signal.signal(signal.SIGALRM, alarm_handler)
    signal.setitimer(signal.ITIMER_REAL, (DEADLINE_NS - time.monotonic_ns()) / 1e9)
    R["reviewed_invocation"] = {"argv": list(sys.argv), "source_sha256": sh,
                                "contract_sha256": ch, "deadline_monotonic_ns": DEADLINE_NS,
                                "private_home_name": sys.argv[8],
                                "child_bootstrap_read_debit": CHILD_READ_DEBIT}
    # Actual observations precede both intended startup guards. The complete
    # evidence survives the before-GPG refusals and the common final cleanup.
    R["startup_observations"] = {
        "env": dict(os.environ), "resuid": list(os.getresuid()), "resgid": list(os.getresgid()),
        "limits": {label: list(resource.getrlimit(kind)) for label, kind in (
            ("AS", resource.RLIMIT_AS), ("CPU", resource.RLIMIT_CPU),
            ("FSIZE", resource.RLIMIT_FSIZE), ("NOFILE", resource.RLIMIT_NOFILE),
            ("CORE", resource.RLIMIT_CORE))}}
    resources("startup")
    require(os.getresuid() == (1000, 1000, 1000)
            and os.getresgid() == (1000, 1000, 1000) and dict(os.environ) == ENV, "nonroot exact three-variable startup environment required")
    for kind, expected in ((resource.RLIMIT_AS, AS_PARENT), (resource.RLIMIT_CPU, 120),
                           (resource.RLIMIT_FSIZE, PUBLIC_OUTPUT), (resource.RLIMIT_NOFILE, 128),
                           (resource.RLIMIT_CORE, 0)):
        require(resource.getrlimit(kind) == (expected, expected), "pre-interpreter inherited limit mismatch")
    hold("prlimit", "/usr/bin/prlimit", 65536,
         "cb28811cb3902773c1a0f3ac0ea554c7a1e232724e9d4cae055ede54b65a7bc4", 27616, True, 3)
    py = hold("python", "/usr/bin/python3.14", 8388608,
              "52e0a13e60a981d8c4b6478be2ba5176f69da07948a056bf49cf6f077e30cb41", 7477160, True, 4)
    require(identity(os.stat("/proc/self/exe")) == identity(os.fstat(py)), "interpreter fd-executable pin mismatch")
    hold("source", PREFIX + "/verifier.py", RAW, sh, None, inherited=5)
    contractfd = hold("launch_contract", PREFIX + "/launch-contract.json", RAW, ch, None, inherited=6)
    contract = json.loads(read_fd(contractfd, RAW)[2])
    require(contract["assignment"] == ASSIGNMENT and contract["generation"] == 1
            and contract["source"]["sha256"] == sh
            and contract["disposition"] == "INERT_CONNECTED_SOURCE_PENDING_WHOLE_REVIEW_AND_NEW_ROOT_ADMISSION"
            and contract["resource_receipt_schemas"] == {
                "supervisor": "friday.e4.node.supervisor-resources.a172.v1",
                "verifier": "friday.e4.node.verifier-resources.a172.v1"}
            and contract["readonly_keyring_policy"]["schema"] == "friday.e4.node.readonly-keyring.a172.v1"
            and contract["own_runtime_fd"] == OWN_RUNTIME_FD,
            "reviewed source/contract linkage")
    expected_contract_argv = [
        "prlimit", "--as=117440512:117440512", "--cpu=120:120",
        "--fsize=32768:32768", "--nofile=128:128", "--core=0:0", "--",
        "/proc/self/fd/4", "-I", "-S", "-B", "/proc/self/fd/5",
        "--reviewed-source-sha256", sh, "--reviewed-contract-sha256", CONTRACT_HASH_BINDING,
        "--deadline-monotonic-ns", DEADLINE_BINDING, "--private-home-name", HOME_BINDING,
        "--child-bootstrap-read-debit", str(CHILD_READ_DEBIT)]
    require(contract["parent_exec"]["argv_template"] == expected_contract_argv,
            "complete contract/launcher/parser positional linkage mismatch")
    hold("gpg", "/usr/bin/gpg", 2097152,
         "c8f3dc07a34ab0a0a081368b482724b79a2a6d6b7aa856741c09edf7af854754", 1186456, True)
    hold("gpgv", "/usr/bin/gpgv", 1048576,
         "95ecd00d02b79d091f103af175b1dbf95b5b0c66503104bfcea447cca7e3829d", 302296, True)
    billfd = hold("bill", "/home/jericho/.jericho/runtime/subagent-lifecycle/ASTRA-E4-ACQUISITION-INPUT-DRIFT-SUCCESSOR-A032-G1-BILL.json",
                  1048576, "cd973b83717015b985055482c9835e7ac1cf8311120c71461d4c0f17ad2d50a2", 466956)
    bill = json.loads(read_fd(billfd, 1048576)[2])
    node = bill["acquisition_bill"]["node"]
    require(node["filename"] == TARGET and node["version"] == "22.23.2"
            and node["size"] == 31058332 and node["sha256"] == AHASH
            and node["signer_primary_fingerprint"] == EXPECTED
            and node["local_path"] == "/home/jericho/.jericho/runtime/friday-toolchain22.bSt8Uu/archives/" + TARGET,
            "pinned bill row mismatch")
    R["historical_bill_labels_unchanged"] = bill["raw_input_receipt_admission"]
    hold("receipts", BASE + "/transport-receipts.ndjson", 4194304,
         "180c44ad4439a96f89335a62e35a5668327a228ce164d1f31b503b867b05fe9b", 400979)
    rawfd = hold("raw", BASE + "/metadata/node/SHASUMS256.txt.asc", RAW,
                 "cfcf12eb3146d641be185d08ca018166b98495b1d8e5f7f94ab77f9965b651d2", 4696)
    keyfd = hold("key", BASE + "/metadata/node/publisher-key.asc", RAW,
                 "e31e1aa40a8331f01d753cef475f7b9eab934fc25f5f0b36995bfd80bd66ad27", 3163)
    archivefd = hold("archive", BASE + "/archives/node/" + TARGET, ARCH, AHASH, 31058332)
    hold("original_archive", node["local_path"], ARCH, AHASH, 31058332)
    raw, keyraw = read_fd(rawfd, RAW)[2], read_fd(keyfd, RAW)[2]
    body, sigarmor, packet, selected, rows = parse_raw(raw)
    index, digest, row = selected
    require(digest == AHASH, "actual selected signed row digest mismatch")
    keypacket = armor(keyraw, "PGP PUBLIC KEY BLOCK")
    R["raw_parse"] = {"body_sha256": hashlib.sha256(body).hexdigest(), "body_bytes": len(body),
                      "row_count": rows, "selected_count": 1, "selected_index": index,
                      "selected_exact_row": row.decode("ascii"), "signature_crc_valid": True,
                      "key_crc_valid": True, "no_unsigned_trailer": True,
                      "no_dash_escaping_required": True}
    make_home(sys.argv[8])
    common = ["--no-options", "--homedir", HOME, "--batch", "--no-tty", "--no-autostart",
              "--no-auto-key-retrieve", "--no-auto-key-import", "--auto-key-locate", "clear",
              "--no-default-keyring", "--trust-model", "always", "--lock-never",
              "--no-auto-check-trustdb", "--no-sig-cache", "--require-cross-certification"]
    cols = ["--with-colons", "--fixed-list-mode", "--with-fingerprint", "--with-subkey-fingerprint"]
    # Stock GPG documents that --no-default-keyring WITHOUT an explicit ring
    # still opens the default ring. The two input-only commands must instead
    # suppress every ring. Never propagate --no-keyring to authenticated checks:
    # it overrides even an explicit sealed binary ring. The home stays0500.
    keyless = common + ["--no-keyring"]
    shown = call("show-raw-key", "gpg", keyless + ["--status-fd", "{STATUS}"] + cols
                 + ["--show-keys", "/proc/self/fd/" + str(keyfd)], [keyfd])
    keys, showproof = key_listing(shown)
    binaryfd = new_mem("binary-key")
    dearmor = call("dearmor", "gpg", keyless + ["--status-fd", "{STATUS}", "--yes", "--output",
                   "/proc/self/fd/" + str(binaryfd), "--dearmor", "/proc/self/fd/" + str(keyfd)],
                   [keyfd], [binaryfd])
    require(dearmor["rc"] == 0 and read_fd(binaryfd, RAW)[2] == keypacket,
            "trusted dearmor differs from strict raw armor")
    seal(binaryfd)
    keyed = common + ["--keyring", "/proc/self/fd/" + str(binaryfd)]
    checked = call("check-self-signatures-and-bindings", "gpg",
                   keyed + ["--status-fd", "{STATUS}"] + cols + ["--check-sigs", EXPECTED], [binaryfd])
    checkedkeys, checkproof = key_listing(checked, True)
    require(checkedkeys == keys, "raw-key/check-key correspondence differs")
    R["key_proof"] = checkproof
    # Reader path remains literally the same for both positive and mutation
    # negative. Only our retained writer can change this owned same-size memfd.
    writer = new_mem("same-path-signed-input", raw)
    fcntl.fcntl(writer, fcntl.F_ADD_SEALS, fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK)
    reader = acquire_owned(os.open, "open", "/proc/self/fd/" + str(writer), os.O_RDONLY | os.O_CLOEXEC)
    OWN.add(reader)
    require(identity(os.fstat(reader)) == identity(os.fstat(writer)), "owned memfd reader custody")
    verify_args = keyed + ["--status-fd", "{STATUS}", "--verify", "/proc/self/fd/" + str(reader)]
    positive = call("positive-gpg", "gpg", verify_args, [binaryfd, reader])
    pd = signature(positive, keys)
    R["positive_gpg"] = pd
    require(pd["accepted"], "trusted GPG positive refusal")
    bodyfd = new_mem("verified-body")
    # gpgv has NO options/configuration files and no retrieval/import/agent
    # interface. Do not pass unsupported gpg --no-options flags to gpgv.
    v = call("positive-gpgv-body", "gpgv", ["--homedir", HOME, "--keyring",
             "/proc/self/fd/" + str(binaryfd), "--status-fd", "{STATUS}", "--output",
             "/proc/self/fd/" + str(bodyfd), "/proc/self/fd/" + str(reader)],
             [binaryfd, reader], [bodyfd])
    vd = signature(v, keys)
    require(vd["accepted"] and vd["signer"] == pd["signer"]
            and read_fd(bodyfd, RAW)[2] == body, "gpgv signature/body correspondence refusal")
    seal(bodyfd)
    R["positive_gpgv"] = vd
    R["gpgv_body_equals_strict_signed_body"] = True
    match = compare(archivefd, digest, 31058332)
    R["positive_archive"] = match
    require(match["accepted"], "actual archive digest/size refusal")
    changed_digest = ("0" if digest[0] != "0" else "1") + digest[1:]
    offset = raw.index(row)
    require(raw.count(row) == 1 and os.pwrite(writer, changed_digest[:1].encode("ascii"), offset) == 1,
            "precise owned signed-row mutation")
    changed = read_fd(reader, RAW)[2]
    cb, ca, cp, cs, cr = parse_raw(changed)
    require(len(changed) == len(raw) and changed[:offset] == raw[:offset]
            and changed[offset + 1:] == raw[offset + 1:] and ca == sigarmor
            and cs[1] == changed_digest and cs[2][64:] == row[64:] and cr == rows,
            "signed-row negative changed unrelated bytes")
    negative = call("negative-same-path-changed-row", "gpg", verify_args, [binaryfd, reader])
    nd = signature(negative, keys)
    ns = statuses(negative)
    require(negative["rc"] != 0 and not nd["accepted"]
            and sum(x[0] == "BADSIG" for x in ns) == 1
            and not any(x[0] in ("VALIDSIG", "ERRSIG", "NO_PUBKEY", "NODATA", "FAILURE", "ERROR") for x in ns),
            "same-path mutation did not produce actual BADSIG")
    require(os.pwrite(writer, raw[offset:offset + 1], offset) == 1
            and read_fd(reader, RAW)[2] == raw, "owned signed input restoration")
    seal(writer)
    R["negative_signed_row"] = {"passed": True, "same_exact_input_argument": verify_args[-1],
                                "same_gpg_argument_template": True, "same_decision_function": "signature",
                                "changed_raw_sha256": hashlib.sha256(changed).hexdigest(),
                                "changed_offset": offset, "same_signature_armor": True, "decision": nd}
    mismatch = compare(archivefd, changed_digest, 31058332)
    require(not mismatch["accepted"] and mismatch["actual_sha256"] == AHASH
            and mismatch["actual_bytes"] == 31058332, "inert digest negative refusal missing")
    R["negative_archive"] = {"passed": True, "same_decision_function": "compare",
                             "archive_bytes_unchanged": True, "altered_digest_is_not_claimed_signed": True,
                             "decision": mismatch}
    custody()
    R["component_nofollow_before_after_stable"] = True
    R["own_scratch_bytes_final_before_cleanup"] = scratch()
    resources("verification_end")
    due()
    return True


def finalize_read_accounting():
    # Known local content and conservative child debit stay separately visible.
    # Exact syscall/implicit runtime/GPG IO is not measured or treated as zero.
    R["runtime_explicit_read_bytes"] = RUNTIME_READS
    R["runtime_read_reset_or_subtracted"] = False
    R["explicit_read_accounting"] = {
        "schema": OWN_RUNTIME_FD["read_accounting"]["schema"],
        "child_bootstrap_upper_debit_bytes": CHILD_READ_DEBIT,
        "child_debit_kind": "FIXED_CONSERVATIVE_UPPER_BOUND_NOT_MEASURED",
        "child_observed_bytes": None, "child_observation": "UNKNOWN_NOT_ZERO",
        "copied_parent_prefix_included": False,
        "performing_verifier_content_bytes": VERIFIER_READS,
        "whole_content_charge_bytes": WHOLE_READS, "budget_bytes": WHOLE_READ_LIMIT,
        "runtime_directory_scans": RUNTIME_DIRECTORY_SCANS,
        "canonical_inherited_before_imports": sorted(_PRE_IMPORT_META),
        "reset_or_subtraction": False, "implicit_io": "UNKNOWN_NOT_ZERO"}


verified = False
try:
    verified = main()
except BaseException as exc:
    R["failure"] = deferred_error(exc)
finally:
    try:
        # Failure also checks every held byte/path before closing. Reserve 20s
        # and never relax the external wall bound to obtain a cleanup pass.
        custody(True)
        R["final_custody_confirmed"] = True
    except BaseException as exc:
        verified = False
        R["custody_failure"] = deferred_error(exc)
    try:
        R["own_scratch_bytes_final_before_cleanup"] = scratch()
        resources("before_cleanup")
    except BaseException as exc:
        verified = False
        R["resource_failure"] = deferred_error(exc)
    try:
        cleanup()
    except BaseException as exc:
        STOP = True
        verified = False
        R["cleanup_failure"] = deferred_error(exc)

try:
    finish_allocations()
except BaseException as exc:
    STOP = True
    verified = False
    R["allocation_cleanup_failure"] = deferred_error(exc)

try:
    due(True)
    finalize_read_accounting()
    R["aggregate_gpg_output_bytes_including_written_key_and_body"] = TOTAL_OUTPUT
    R["aggregate_gpg_output_charge_unknown"] = OUTPUT_UNKNOWN
    R["completed_msk"] = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).isoformat()
    R["status"] = ("STOP_UNCONFIRMED" if STOP else
                   "QUALIFIED_NODE_RAW_PUBLISHER_CHAIN_PROVEN" if verified else "NOT_PROVEN")
    require(commit_outside_body(), "outside body commit before publication")
    data = publish_preowned_then_frame(R, lambda: encode_bounded(R))
except BaseException as exc:
    verified = False
    # Original R/fullraw/errors remain owned. No hash-only smaller replacement.
    try:
        retain_error_object(exc)
    except BaseException:
        pass
    R["publication_failure"] = deferred_error(exc)
    data = None
if data is not None:
    try:
        commit_existing_receiver(data)
    except BaseException as exc:
        verified = False
        marker = retain_publication_failure("commit", exc)
        R["status"] = "STOP_UNCONFIRMED" if STOP else "NOT_PROVEN"
        R["receiver_failure"] = marker
else:
    verified = False
    R["status"] = "STOP_UNCONFIRMED" if STOP else "NOT_PROVEN"
# Finite end records a transfer. The timer is disarmed and the deadline number stays.
# os._exit is process death and is not custody of the outside body.
finish_outside_end()
os._exit(0 if verified and not STOP else 3 if STOP else 2)
