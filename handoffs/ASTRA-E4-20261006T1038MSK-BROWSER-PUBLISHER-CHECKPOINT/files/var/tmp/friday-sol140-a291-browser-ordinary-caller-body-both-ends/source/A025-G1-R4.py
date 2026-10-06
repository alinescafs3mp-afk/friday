#!/usr/bin/python3.14
"""A025 R4: fresh A032 raw-input bill; unchanged sticky acquisition accounting.

Launch using env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C python3.14 -I -S -B.
Only sealed, SHA-checked G1 helpers are reused; G1 execute/run_wave are never called.
Default CLI is read-only. Offline capability is an in-process private test seam,
never a CLI selector. Downloaded content is never imported, executed or extracted.
"""
import hashlib
import json
import os
import re
import resource
import select
import signal
import ssl
import stat
import sys
import time
import types

BASE = "/home/jericho/.jericho/runtime/subagent-lifecycle/"
G1 = BASE + "ASTRA-E4-MATERIAL-ACQUISITION-EXECUTOR-A025-G1.py"
G1_SHA = "8c8382979b1047777f0141d21e6d76c22032aef7d59d83739e7bebc0d69ec40a"
ACTIVE_RUN = None
UNCERTAIN_RECEIPT = b'{"event":"RUN_STATE","state":"STOP_UNCONFIRMED","terminal_completion":false}\n'


def checked_source(path, expected):
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        parts = path.split("/")[1:]
        if not parts or any(p in ("", ".", "..") for p in parts):
            raise RuntimeError("SOURCE_PATH")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600:
            raise RuntimeError("SOURCE_CUSTODY")
        data = bytearray()
        while len(data) <= 1048576:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > 1048576 or hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError("SOURCE_SHA")
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RuntimeError("SOURCE_DRIFT")
        return bytes(data)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


OWNED_G1 = "/var/tmp/friday-sol140-a291-browser-ordinary-caller-body-both-ends/source/A025-G1.py"
_g1_bytes = checked_source(OWNED_G1, G1_SHA)
m = types.ModuleType("a025_sealed_g1_helpers")
m.__file__ = G1
exec(compile(_g1_bytes, G1, "exec"), m.__dict__)
del _g1_bytes

# Narrow download-only admission for three exact, non-executable source documents.
# Never alter their permissions or treat their bytes as code. All other input and
# every output retain G1's original strict mode/type/owner/nlink validation.
_DECLARED_0664_DATA = (
    (BASE + "ASTRA-E4-PROTECTED-TOOLCHAIN-MATERIAL-AVAILABILITY-A006-G2-RESULT.md",
     "130be08ee01bccf1e905ad24160f35be9d80353a43762a44185af704f833e6fb", 21942),
    ("/home/jericho/jericho/requirements.lock",
     "617dc008ff40c1c9e79f902e248417c157ac38d667b662d96e2e579897139984", 1627),
    ("/home/jericho/jericho/requirements-dev.lock",
     "6e72cfabfe6083970c248a5fb2d59a08d78acfb5b45011c8671a038df2d33063", 957),
)
_DATA_IDENTITIES = set()
_DATA_HOLDS = []
_original_file_stat = m.file_stat
try:
    for _path, _sha, _size in _DECLARED_0664_DATA:
        _fd = m.absolute_open(_path)
        _DATA_HOLDS.append(_fd)
        _before = os.fstat(_fd)
        m.require(stat.S_ISREG(_before.st_mode) and stat.S_IMODE(_before.st_mode) == 0o664
                  and _before.st_uid == 1000 and _before.st_gid == 1000
                  and _before.st_nlink == 1 and _before.st_size == _size,
                  "DECLARED_DATA_MODE_OR_IDENTITY")
        _bytes = os.pread(_fd, _size + 1, 0)
        m.require(len(_bytes) == _size and hashlib.sha256(_bytes).hexdigest() == _sha
                  and m.identity(_before) == m.identity(os.fstat(_fd)),
                  "DECLARED_DATA_SHA_OR_DRIFT")
        _DATA_IDENTITIES.add(m.identity(_before))
    del _bytes
except BaseException:
    for _fd in _DATA_HOLDS:
        os.close(_fd)
    _DATA_HOLDS.clear()
    raise


def _declared_data_file_stat(fd, output=False):
    if not output:
        current = os.fstat(fd)
        if m.identity(current) in _DATA_IDENTITIES:
            return current
    return _original_file_stat(fd, output)


m.file_stat = _declared_data_file_stat


# A032 successor is a fresh bill identity; A023 remains immutable historical evidence.
_HISTORICAL_PLAN, _HISTORICAL_PLAN_SHA = m.PLAN, m.PLAN_SHA
_ORIGINAL_COMPILE_PLAN = m.compile_plan
m.PLAN = BASE + "ASTRA-E4-ACQUISITION-INPUT-DRIFT-SUCCESSOR-A032-G1-BILL.json"
m.PLAN_SHA = "cd973b83717015b985055482c9835e7ac1cf8311120c71461d4c0f17ad2d50a2"
_RAW_SUCCESSORS = (
    ("evidence/ubuntu/resolute-updates-main-Packages",
     "1b9ae59e399a944396ae0abf0616787d7e4a8b97edcb2e7795dfb1365d56a02c", 3619968,
     "6b8ff983cfce9b528f33f5c0fa49d92dd9f2ce33a7918dcb24b3188702a54a2f", 3643486),
    ("evidence/ubuntu/resolute-updates-universe-Packages",
     "bb9bcce69a0f764f5c88c9ce31846c2b5c20b214518e1225043ff0e1d08ccff7", 1518230,
     "2c4d97bcb7737bcff9c1331fe38bc52bb9c77342d45f2473b2e4c0815da2f57d", 1542434),
    ("evidence/ubuntu/resolute-updates-InRelease",
     "c73c04e538b29eedbb569e62179477edaef4ff3751abf243dc58784c340b2f18", 137401,
     "802e675dd9de4c7f3916434a95e7c1d8eec0e82886622d7805ab19a2c6fe0365", 137401),
)
_RAW_ADMISSION = {
    "current_raw3": "NOT_PROVEN",
    "original_signed_evidence": "HISTORICAL_ONLY_NOT_TRANSFERRED",
    "signature_verification_performed": False,
    "member_chain_verification_performed": False,
    "download_authority_only": True,
}


def _compile_successor_plan(plan):
    m.require(plan["schema"] == "friday.astra.e4.acquisition-input-drift-successor.v1" and
              plan["assignment"] == "ASTRA-E4-ACQUISITION-INPUT-DRIFT-SUCCESSOR-A032" and
              type(plan["generation"]) is int and plan["generation"] == 1, "SUCCESSOR_IDENTITY")
    m.require(plan["supersedes"]["bill_pin"] ==
              {"path": _HISTORICAL_PLAN, "sha256": _HISTORICAL_PLAN_SHA}, "HISTORICAL_BILL_PIN")
    m.require(plan["raw_input_receipt_admission"] == _RAW_ADMISSION, "RAW_ADMISSION_NOT_PROVEN")
    historical, _ = m.pinned_json(_HISTORICAL_PLAN, _HISTORICAL_PLAN_SHA)
    remote, local, paths, dirs = _ORIGINAL_COMPILE_PLAN(historical)
    m.require(plan["authority"]["owner_authority_pin"] ==
              historical["authority"]["owner_authority_pin"], "OWNER_PIN")
    updates = {row[0]: row[1:] for row in _RAW_SUCCESSORS}
    expected_bill = dict(historical["acquisition_bill"])
    provenance = dict(expected_bill["local_provenance_copies"])
    changed = set()
    for group in ("selected_packages", "selected_inrelease"):
        fresh = []
        for original in provenance[group]:
            row = dict(original)
            if row["relative_path"] in updates:
                old_sha, old_size, current_sha, current_size = updates[row["relative_path"]]
                m.require(row["sha256"] == old_sha and row["size"] == old_size,
                          "HISTORICAL_RAW_IDENTITY")
                row.update(sha256=current_sha, size=current_size,
                           historical_sha256=old_sha, historical_size=old_size,
                           input_purpose="DIAGNOSTIC_UNACCEPTED_VERIFICATION_INPUT",
                           publisher_verification="NOT_PROVEN")
                if group == "selected_packages":
                    row["historical_inrelease_sha256"] = row["inrelease_sha256"]
                    row["inrelease_sha256"] = _RAW_SUCCESSORS[2][3]
                changed.add(row["relative_path"])
            fresh.append(row)
        provenance[group] = fresh
    expected_bill["local_provenance_copies"] = provenance
    m.require(changed == set(updates) and plan["acquisition_bill"] == expected_bill and
              all(type(value) is int for value in plan["acquisition_bill"]["limits"].values()),
              "SUCCESSOR_EXACT_CONTRACT")
    # Compile only the genuine pinned historical A023 identity, never forge its schema.
    # All network routes/archive pins/limits/transport/owner/output paths remain exact.
    for row in local:
        if row["relative_path"] in updates:
            old_sha, old_size, current_sha, current_size = updates[row["relative_path"]]
            row.update(sha256=current_sha, size=current_size, cap=current_size,
                       historical_sha256=old_sha, historical_size=old_size,
                       input_purpose="DIAGNOSTIC_UNACCEPTED_VERIFICATION_INPUT",
                       publisher_verification="NOT_PROVEN")
    return remote, local, paths, dirs


m.compile_plan = _compile_successor_plan


class Run:
    def __init__(self, clock=time.monotonic, wall=1800, reserve=60, hook=None, stop=None):
        self.clock = clock
        self.started = clock()
        self.hard = self.started + wall
        self.work = self.hard - reserve
        self.hook = hook or (lambda stage, run: None)
        self.stop_override = stop
        self.uncertain = False
        self.reason = None
        self.original_errors = []
        self.errors = []
        self.children = []
        self.results = {}
        self.started_paths = set()
        self.peak = 0
        self.charged = 0
        self.hashes = []
        self.cancel = False
        self.receipt = None
        self.uncertainty_persisted = False

    def fail(self, reason, uncertainty=False):
        if uncertainty and not self.uncertain:
            # Set before any allocation, serialization, hook or cleanup can fail.
            self.uncertain = True
            if self.receipt is not None:
                try:
                    self.uncertainty_persisted = os.write(self.receipt, UNCERTAIN_RECEIPT) == len(UNCERTAIN_RECEIPT)
                except BaseException:
                    pass  # sticky in memory and outermost fixed stdout fallback remain
        if self.reason is None:
            self.reason = str(reason)
        self.errors.append(str(reason))

    def guard(self, stage, terminal=False):
        self.hook(stage, self)
        if self.cancel or m.STOP:
            raise m.Refused("OWNER_STOP")
        if self.clock() >= (self.hard if terminal else self.work):
            raise m.Refused("WALL_TIMEOUT:" + stage)
        if self.uncertain:
            raise m.Refused("STOP_UNCONFIRMED")

    def status(self):
        return "STOP_UNCONFIRMED" if self.uncertain else ("CONTOUR_ABORTED" if self.reason else "ACQUISITION_FINISHED_WITH_EXPLICIT_ACCEPTANCE_GAPS")


def error_name(exc):
    m.remember_error(exc,'R4.error_name.before_legacy_formatting')
    try:return str(exc)[:512] if isinstance(exc, m.Refused) else type(exc).__name__
    except BaseException as secondary:
        m.remember_error(secondary,'R4.legacy_format_secondary')
        return 'FORMAT_FAILED_NO_BODY_CREDIT'


def close_fd(fd, run):
    if fd is None:
        return True
    observed = True
    try:
        run.hook("cleanup_descriptor", run)
    except BaseException as exc:
        observed = False
        run.original_errors.append(exc)
        run.fail("CLEANUP:" + error_name(exc))
    try:
        os.close(fd)  # Once; a failed observation never suppresses the syscall.
    except BaseException as exc:
        run.original_errors.append(exc)
        run.fail("CLEANUP:" + error_name(exc), True)
        return False
    return observed


def stop_child(child, run):
    """One cleanup decision per direct child; reaped/unconfirmed are sticky."""
    if child['pid'] is None:
        if child['lifecycle'] == 'BIRTH_UNCONFIRMED':
            run.fail('STOP_UNCONFIRMED:WRAPPED_FORK_BIRTH_UNKNOWN', True)
            return False
        child['lifecycle'] = 'NOT_CREATED'
        return True  # No child to signal/reap; not retirement evidence.
    if child["lifecycle"] != "LIVE":
        return child["lifecycle"] == "REAPED"
    child["stop_attempted"] = True
    try:
        if run.stop_override:
            confirmed, status = run.stop_override(child, run)
        else:
            try:
                if child["pidfd"] is not None:
                    signal.pidfd_send_signal(child["pidfd"], signal.SIGKILL)
                else:
                    # Direct child not yet reaped: this PID cannot be recycled.
                    os.kill(child["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
            confirmed, status = False, None
            end = min(time.monotonic() + 1, run.hard)
            while time.monotonic() < end:
                done, status = os.waitpid(child["pid"], os.WNOHANG)
                if done:
                    confirmed = True
                    break
                select.select([], [], [], 0.01)
        if confirmed:
            child.update(lifecycle="REAPED", wait_status=status)
            return True
        child["lifecycle"] = "STOP_UNCONFIRMED"
        run.fail("STOP_UNCONFIRMED", True)
    except BaseException as exc:
        child["lifecycle"] = "STOP_UNCONFIRMED"
        run.fail("STOP_EXCEPTION:" + error_name(exc), True)
    return False


def guarded_digest(fd, cap, run, stage, expected=None, size=None, collect=False, terminal=False):
    run.guard(stage, terminal)
    before = m.file_stat(fd)
    m.require(before.st_size <= cap and (size is None or before.st_size == size), "INPUT_SIZE")
    os.lseek(fd, 0, os.SEEK_SET)
    sha, count, chunks = hashlib.sha256(), 0, []
    while True:
        run.guard(stage, terminal)
        data = os.read(fd, min(m.CHUNK, cap - count + 1))
        run.guard(stage, terminal)
        if not data:
            break
        count += len(data)
        m.require(count <= cap, "FILE_GROWTH")
        sha.update(data)
        if collect:
            chunks.append(data)
    m.require(m.identity(before) == m.identity(m.file_stat(fd)), "FILE_DRIFT")
    m.require(expected is None or sha.hexdigest() == expected, "INPUT_SHA")
    m.require(size is None or count == size, "INPUT_SIZE")
    os.lseek(fd, 0, os.SEEK_SET)
    run.guard(stage, terminal)
    return count, sha.hexdigest(), b"".join(chunks) if collect else None


def guarded_write(fd, data, run, stage, terminal=False):
    while data:
        run.guard(stage, terminal)
        block = data[:m.CHUNK]
        count = os.write(fd, block)
        m.require(count > 0, "SHORT_WRITE")
        data = data[count:]
        run.guard(stage, terminal)


def ordinary_failure(record):
    failure = record.get("failure", "")
    return bool(re.fullmatch(r"HTTP_STATUS_[0-9]{3}", failure)) or failure in {
        "TimeoutError", "gaierror", "ConnectionRefusedError", "ConnectionResetError",
        "ConnectionAbortedError", "RemoteDisconnected"}


def unknown_record(child, reason):
    e = child["entry"]
    try:
        st = os.fstat(child["body"])
        file = {"mode": oct(stat.S_IMODE(st.st_mode)), "uid": st.st_uid, "gid": st.st_gid,
                "nlink": st.st_nlink, "device": st.st_dev, "inode": st.st_ino,
                "size_observed_unsealed": st.st_size}
    except BaseException:
        file = None
    return {"state": "LAUNCH_FAILED" if child['pid'] is None else "UNKNOWN_PARTIAL", "url": e["url"], "relative_path": e["relative_path"],
            "body_complete": False, "failure": reason, "accounting_charged_bytes": e["cap"],
            "observed_retained_bytes": None, "observed_retained_sha256": None,
            "wire_bytes": None, "expected_bytes": e["size"], "expected_sha256": e["sha256"],
            "worker_lifecycle": child["lifecycle"], "stop_confirmed": child["lifecycle"] == "REAPED", "file": file}


def launch(entry, out, context, run, worker):
    run.guard("launch")
    m.require(entry["relative_path"] not in run.started_paths, "ROUTE_REUSED")
    run.started_paths.add(entry["relative_path"])
    child = {"entry": entry, "body": None, "pipe": None, "write_pipe": None, "pidfd": None,
             "pid": None, "lifecycle": "LIVE", "stop_attempted": False, "wait_status": None,
             "events": [], "buffer": b"", "eof": False, "connected": False,
             "started": run.clock(), "started_msk": m.now(), "recorded": False}
    m.sol076_launch_intent(child)
    run.children.append(child)  # Before any ordinary body/pipe/plane/fork acquisition.
    child['preowned_receiver'] = {'pipe_accepted': False, 'receipt_accepted': False, 'both_accepted': False}
    try:
        acquire = getattr(run, 'sol076_acquire', None)
        pipe_pair = getattr(run, 'sol076_pipe_pair', None)
        body = m.sol076_own_fd(child, 'body', out.create(entry['relative_path']) if acquire is None
                             else acquire(lambda: out.create(entry['relative_path'])))
        read_fd, write_fd = os.pipe() if pipe_pair is None else pipe_pair()
        m.sol076_own_fd(child, 'pipe', read_fd)
        m.sol076_own_fd(child, 'write_pipe', write_fd)
        receiver_stat = os.fstat(read_fd)
        child["preowned_receiver"] = {
            "fd": read_fd, "bound_before_birth": True, "readonly_parent": True,
            "identity9": [str(v) for v in (
                receiver_stat.st_dev, receiver_stat.st_ino, receiver_stat.st_mode,
                receiver_stat.st_uid, receiver_stat.st_gid, receiver_stat.st_nlink,
                receiver_stat.st_size, receiver_stat.st_mtime_ns, receiver_stat.st_ctime_ns)],
            "pipe_accepted": False, "receipt_accepted": False, "both_accepted": False,
            "exit_is_handover": False, "eof_is_handover": False,
            "digest_is_handover": False, "pending_is_handover": False}
        plane = m.a201_fork_plane(entry)
        fork_operation = os.fork
        child['fork_is_stock'] = (type(fork_operation) is type(time.monotonic) and
                                  getattr(fork_operation, '__module__', None) == 'posix')
        child['fork_attempted'] = True
        pid = fork_operation()
    except BaseException as exc:
        m.sol076_launch_error(child, exc, 'R4.acquire_plane_or_fork')
        m.sol076_finish_record(child, {}, run, close_fd)
        raise
    if pid == 0:
        try:
            keep = m.a201_bind_fork_plane(plane, body, write_fd)
            for descriptor in os.listdir("/proc/self/fd"):
                fd = int(descriptor)
                if fd not in keep:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            worker(entry, body, write_fd, context)
            os._exit(0)
        except BaseException:
            os._exit(70)
    child["pid"] = pid
    m.sol076_close_slot(child, 'write_pipe', run, close_fd)
    child["pidfd"] = m.sol076_own_fd(child, 'pidfd', os.pidfd_open(pid) if acquire is None
                                    else acquire(lambda: os.pidfd_open(pid)))
    os.set_blocking(read_fd, False)
    return child


def drain(child, run):
    if child["eof"]:
        return
    for _ in range(3):
        run.guard("event_drain", True)
        try:
            data = os.read(child["pipe"], 131073)
        except BlockingIOError:
            break
        if not data:
            child["eof"] = True
            break
        child["buffer"] += data
        m.require(len(child["buffer"]) <= 131072, "EVENT_PIPE_CAP")
        events,child["buffer"]=m.sol069_events(child["buffer"],child["eof"])
        for event in events:
            child["events"].append(event)
            m.require(len(child["events"])<=2,"EVENT_COUNT")
            if event.get("event")=="CONNECTED":
                m.require(not child["connected"],"DUPLICATE_CONNECTED")
                child["connected"]=True
    if child["eof"] and not child["buffer"] and child["events"]:
        child["preowned_receiver"]["pipe_accepted"] = True
        child["preowned_receiver"]["eof_is_handover"] = False



def sol081_decode_receipt_stream(raw, require, expected=None):
    """Bounded physical candidate pairs. No physical line is final qualification."""
    require(type(raw) is str, 'SOL081_STREAM_TEXT')
    data = raw.encode('utf-8', 'strict')
    require(0 < len(data) <= 1048576 and data.endswith(b'\n') and
            data.count(b'\n') == 6, 'SOL081_STREAM_COMPLETE_THREE_PAIRS')

    def unique(items):
        row = {}
        for key, value in items:
            require(key not in row, 'SOL081_STREAM_DUPLICATE_KEY')
            row[key] = value
        return row

    def constant(value):
        require(False, 'SOL081_STREAM_NONFINITE')

    def canon(row):
        return json.dumps(row, sort_keys=True, separators=(',', ':'), allow_nan=False)

    lines = data.split(b'\n')[:-1]
    rows = [json.loads(line, object_pairs_hook=unique, parse_constant=constant)
            for line in lines]
    require(all(canon(row).encode() == line for row, line in zip(rows, lines)),
            'SOL081_CANONICAL_PHYSICAL_LINES')
    routes, pids, candidates = set(), set(), {}
    for index in range(0, 6, 2):
        provisional, candidate = rows[index:index + 2]
        require(type(provisional) is dict and type(candidate) is dict,
                'SOL081_STREAM_ROW')
        require(provisional.get('receipt_protocol') == 'friday.sol081.browser.v1' and
                candidate.get('receipt_protocol') == 'friday.sol081.browser.v1' and
                provisional.get('receipt_commitment') == 'PROVISIONAL' and
                provisional.get('receipt_qualified') is False and
                candidate.get('receipt_commitment') == 'CANDIDATE' and
                candidate.get('receipt_qualified') is False,
                'SOL081_STREAM_NOT_A_SELF_QUALIFIED_TERMINAL')
        witness = hashlib.sha256(lines[index] + b'\n').hexdigest()
        require(candidate.get('provisional_sha256') == witness,
                'SOL081_EXACT_PROVISIONAL_PREIMAGE')
        comparable = dict(provisional, receipt_commitment='CANDIDATE',
                          receipt_qualified=False, provisional_sha256=witness)
        require(canon(comparable) == canon(candidate),
                'SOL081_EXACT_RECORD_CORRESPONDENCE')
        route = candidate.get('relative_path')
        launch = candidate.get('launch')
        require(type(route) is str and 0 < len(route) <= 256 and
                not route.startswith('/') and
                all(part not in ('', '.', '..') for part in route.split('/')) and
                type(launch) is dict and launch.get('relative_path') == route,
                'SOL081_STREAM_ROUTE')
        pid = launch.get('pid')
        require(type(pid) is int and pid > 0 and route not in routes and
                pid not in pids, 'SOL081_STREAM_OWNED_PID_ROUTE')
        routes.add(route); pids.add(pid); candidates[route] = candidate
        require(type(candidate.get('bytes')) is int and candidate['bytes'] >= 0 and
                type(candidate.get('observed_retained_bytes')) is int and
                candidate['observed_retained_bytes'] == candidate['bytes'] and
                type(candidate.get('sha256')) is str and len(candidate['sha256']) == 64 and
                all(ch in '0123456789abcdef' for ch in candidate['sha256']) and
                candidate.get('observed_retained_sha256') == candidate['sha256'],
                'SOL081_EXACT_BODY_BYTES_SHA')
        require(candidate.get('body_complete') is True and
                candidate.get('worker_lifecycle') == 'REAPED' and
                candidate.get('stop_confirmed') is True and
                candidate.get('pipe_accepted') is True,
                'SOL081_STREAM_GENUINE_COMPLETE_MATERIAL')
    if expected is not None:
        require(type(expected) is dict and set(expected) == routes,
                'SOL081_STREAM_MATERIAL_SET')
        for route, material in expected.items():
            require(type(material) is dict, 'SOL081_STREAM_MATERIAL')
            comparable = dict(material, receipt_commitment='CANDIDATE',
                              receipt_qualified=False)
            require(canon(comparable) == canon(candidates[route]),
                    'SOL081_STREAM_ACTUAL_MATERIAL_JOIN')
    return candidates


def sol081_rebuild_receipt_stream(materials, pairs, require):
    """Exact canonical metadata preimage, not native/body or retirement credit."""
    require(type(materials) is dict and len(materials) == 3 and
            type(pairs) is list and len(pairs) == 3, 'SOL081_PROJECTION_CARDINALITY')
    chunks, routes, offset = [], set(), 0
    for pair in pairs:
        require(type(pair) is dict and set(pair) ==
                {'relative_path', 'pid', 'offset', 'provisional_bytes',
                 'candidate_bytes', 'provisional_sha256'},
                'SOL081_PROJECTION_FIELDS')
        route = pair['relative_path']
        require(type(route) is str and route in materials and route not in routes,
                'SOL081_PROJECTION_ROUTE')
        routes.add(route)
        material = materials[route]
        require(type(material) is dict and type(material.get('launch')) is dict and
                type(pair['pid']) is int and pair['pid'] > 0 and
                material['launch'].get('pid') == pair['pid'] and
                type(material['launch'].get('pid')) is int,
                'SOL081_PROJECTION_EXACT_PID')
        for key in ('offset', 'provisional_bytes', 'candidate_bytes'):
            require(type(pair[key]) is int and 0 <= pair[key] <= 1048576,
                    'SOL081_PROJECTION_EXACT_BOUNDS')
        require(pair['offset'] == offset, 'SOL081_PROJECTION_GAPLESS')
        candidate = dict(material, receipt_commitment='CANDIDATE', receipt_qualified=False)
        provisional = dict(candidate, receipt_commitment='PROVISIONAL')
        provisional.pop('provisional_sha256', None)
        p = json.dumps(provisional, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        witness = hashlib.sha256(p).hexdigest()
        require(material.get('provisional_sha256') == witness ==
                pair['provisional_sha256'], 'SOL081_PROJECTION_EXACT_WITNESS')
        q = json.dumps(candidate, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        require(len(p) == pair['provisional_bytes'] and len(q) == pair['candidate_bytes'] and
                0 < offset + len(p) + len(q) <= 1048576,
                'SOL081_PROJECTION_EXACT_PHYSICAL_BYTES')
        chunks.extend((p, q)); offset += len(p) + len(q)
    require(routes == set(materials), 'SOL081_PROJECTION_COMPLETE_ROUTES')
    return b''.join(chunks)


def sol081_receipt_snapshot(receipt, run):
    """Read the actual fsynced descriptor. Final finite qualification is later."""
    run.guard('receipt_stream_readback', True)
    before = m.file_stat(receipt, True)
    m.require(0 < before.st_size <= 1048576, 'SOL081_RECEIPT_SNAPSHOT_CAP')
    raw = os.pread(receipt, before.st_size, 0)
    m.require(len(raw) == before.st_size and
              m.identity(os.fstat(receipt)) == m.identity(before),
              'SOL081_RECEIPT_SNAPSHOT_DRIFT')
    text = raw.decode('utf-8', 'strict')
    candidates = sol081_decode_receipt_stream(text, m.require, run.results)
    lines = raw.split(b'\n')[:-1]; pairs = []; offset = 0
    for index, (route, row) in enumerate(candidates.items()):
        p, q = lines[index * 2:index * 2 + 2]
        pairs.append({'relative_path': route, 'pid': row['launch']['pid'],
            'offset': offset, 'provisional_bytes': len(p) + 1, 'candidate_bytes': len(q) + 1,
            'provisional_sha256': hashlib.sha256(p + b'\n').hexdigest()})
        offset += len(p) + len(q) + 2
    m.require(sol081_rebuild_receipt_stream(run.results, pairs, m.require) == raw,
              'SOL081_EXACT_PHYSICAL_PROJECTION')
    run.guard('receipt_stream_readback', True)
    m.require(m.identity(os.fstat(receipt)) == m.identity(before),
              'SOL081_RECEIPT_SNAPSHOT_POST_GUARD_DRIFT')
    return {'schema': 'friday.sol081.browser-receipt-stream.v1',
            'pairs': pairs, 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'identity9_decimal_strings': [str(v) for v in
                (before.st_dev, before.st_ino, before.st_mode, before.st_uid,
                 before.st_gid, before.st_nlink, before.st_size,
                 before.st_mtime_ns, before.st_ctime_ns)],
            'finite_end_confirmed': False}


def sol076_publish_caller_receipt(receipt, record, child, run):
    """Browser writes canonical nonfinal candidates; finite caller qualifies. Other R4 callers retain the historical local protocol, not whole/native credit."""
    browser = getattr(run, 'sol081_browser_stream', False)
    m.require(not browser or not any(key in record for key in
              ('receipt_commitment', 'receipt_qualified', 'provisional_sha256', 'receipt_protocol')),
              'SOL081_CALLER_OWNS_PROTOCOL_FIELDS')
    if browser:
        record['receipt_protocol'] = 'friday.sol081.browser.v1'
    provisional = dict(record)
    provisional['receipt_commitment'] = 'PROVISIONAL'
    provisional['receipt_qualified'] = False
    blob = json.dumps(provisional, sort_keys=browser, separators=(',', ':')).encode() + b'\n'
    digest = hashlib.sha256(blob).hexdigest()

    def mark():
        child['sol076']['cleanup_fault'] = True
        record['receipt_commitment'] = 'PUBLICATION_FAULT'
        record['receipt_qualified'] = False
        record['body_complete'] = False
        record['terminal_completion'] = False
        record['acceptance_complete'] = False
        record['provisional_sha256'] = digest
        record['launch'] = m.sol076_launch_DATA(child)
        run.fail('RECEIPT_PUBLICATION_UNCONFIRMED')

    def end(cause):
        try:
            size = os.lseek(receipt, 0, os.SEEK_END)
            if size and os.pread(receipt, 1, size - 1) != b'\n':
                guarded_write(receipt, b'\n', run, 'receipt_end', True)
            inv = {'schema': 'friday.sol076.receipt-end.v1', 'receipt_commitment': 'END_UNCONFIRMED',
                   'receipt_qualified': False, 'provisional_sha256': digest, 'finite_end': cause,
                   'relative_path': record.get('relative_path')}
            guarded_write(receipt, json.dumps(inv, separators=(',', ':')).encode() + b'\n', run, 'receipt_end', True)
            os.fsync(receipt)
        except BaseException as exc:
            run.original_errors.append(exc)

    def durable(payload, stage):
        offset = os.lseek(receipt, 0, os.SEEK_CUR)
        # Both physical writes retain the original exact-stage negative.
        # Finer labels observe the same operation; original clock/end unchanged.
        run.guard(stage, True)
        guarded_write(receipt, payload, run, 'receipt_write', True)
        run.guard(stage, True)
        held = os.pread(receipt, len(payload), offset)
        m.require(held == payload, 'RECEIPT_RECEIVER_READBACK')
        os.fsync(receipt)
        held = os.pread(receipt, len(payload), offset)
        m.require(held == payload, 'RECEIPT_FSYNC_READBACK')

    try:
        durable(blob, 'receipt_provisional')
    except BaseException as exc:
        m.sol076_launch_error(child, exc, 'provisional_receipt')
        mark()
        end('RECEIPT_PROVISIONAL_UNCONFIRMED')
        raise
    qualified = dict(record)
    qualified['receipt_commitment'] = 'CANDIDATE' if browser else 'QUALIFIED'
    qualified['receipt_qualified'] = not browser
    qualified['provisional_sha256'] = digest
    qblob = json.dumps(qualified, sort_keys=browser, separators=(',', ':')).encode() + b'\n'
    try:
        durable(qblob, 'receipt_commit')
    except BaseException as exc:
        m.sol076_launch_error(child, exc, 'receipt_commit')
        mark()
        end('RECEIPT_COMMIT_UNCONFIRMED')
        raise
    record.update(qualified)
    # Browser candidates stay unqualified until actual caller readback and ALL
    # finite ordinary closes succeed; a failed end append is never the proof.
    record['provisional_sha256'] = digest
    return record


def sol076_append_receipt_end(out, run, cause):
    if out is None or getattr(out, 'root', None) is None:
        return
    fd = os.open('transport-receipts.ndjson', os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, dir_fd=out.root)
    try:
        line = json.dumps({'schema': 'friday.sol076.receipt-end.v1', 'receipt_commitment': 'END_UNCONFIRMED',
                           'receipt_qualified': False, 'finite_end': cause}, separators=(',', ':')).encode() + b'\n'
        guarded_write(fd, line, run, 'receipt_end', True)
        os.fsync(fd)
    finally:
        os.close(fd)


def collect(child, out, run, receipt):
    e = child["entry"]
    # Never inspect/hash a body while stop remains unconfirmed, even after exceptions.
    if child["lifecycle"] != "REAPED" or run.uncertain:
        record = unknown_record(child, "STOP_UNCONFIRMED")
    else:
        drain(child, run)
        finals = [x for x in child["events"] if x.get("event") == "FINAL"]
        success_exit = os.waitstatus_to_exitcode(child["wait_status"]) == 0
        if not success_exit or len(finals) != 1 or child["buffer"]:
            record = unknown_record(child, "WORKER_RECEIPT_INCOMPLETE")
            run.fail("WORKER_RECEIPT_INCOMPLETE")
        else:
            run.hashes.append(e["relative_path"])
            count, sha, _ = guarded_digest(child["body"], e["cap"], run, "hash_network", terminal=True)
            record = dict(finals[0])
            m.require(record.get("bytes") == count and record.get("sha256") == sha, "WORKER_ACCOUNTING_DRIFT")
            complete = record.get("body_complete") is True
            if complete:
                tls = record.get("tls") or {}
                m.require(record.get("http_status") == 200 and tls.get("hostname") == e["host"] and
                    tls.get("certificate_and_hostname_verified") is True and tls.get("verify_mode") == "CERT_REQUIRED" and
                    tls.get("protocol") in ("TLSv1.2", "TLSv1.3") and
                    re.fullmatch(r"[0-9a-f]{64}", tls.get("peer_certificate_sha256", "")), "TLS_EVIDENCE")
                m.validate_headers(types.SimpleNamespace(status=200, getheaders=lambda: record["headers"]), e)
                if e["sha256"]:
                    m.require(count == e["size"] and sha == e["sha256"] and record["state"] == "HASH_MATCH", "BODY_SHA_OR_SIZE")
                else:
                    m.require(record["state"] == "UNACCEPTED_UNPINNED_BODY", "UNPINNED_ACCEPTANCE")
            elif not ordinary_failure(record):
                run.fail("SECURITY_INTEGRITY_PROTOCOL:" + record.get("failure", "UNKNOWN_WORKER_FAILURE"))
            record.update(url=e["url"], relative_path=e["relative_path"], expected_bytes=e["size"],
                expected_sha256=e["sha256"], observed_retained_bytes=count, observed_retained_sha256=sha,
                accounting_charged_bytes=count if complete else e["cap"], wire_bytes=None,
                worker_lifecycle="REAPED", stop_confirmed=True)
            st = m.file_stat(child["body"], True)
            record["file"] = {"mode": "0600", "uid": st.st_uid, "gid": st.st_gid, "nlink": st.st_nlink,
                              "device": st.st_dev, "inode": st.st_ino}
    record.update(started_msk=child["started_msk"], completed_msk=m.now(),
                  elapsed_sec=round(run.clock() - child["started"], 6))
    run.results[e["relative_path"]] = record
    child["recorded"] = True
    child["preowned_receiver"]["exit_is_handover"] = False
    child["preowned_receiver"]["digest_is_handover"] = False
    child["preowned_receiver"]["eof_is_handover"] = False
    child["preowned_receiver"]["pending_is_handover"] = False
    record["pipe_accepted"] = bool(child["preowned_receiver"]["pipe_accepted"])
    record["exit_is_handover"] = False
    record["eof_is_handover"] = False
    record["digest_is_handover"] = False
    record["pending_is_handover"] = False
    record["receipt_readback"] = "REQUIRED_AFTER_WRITE"
    if record.get("body_complete") is True and not record["pipe_accepted"]:
        record["body_complete"] = False
        record["custody"] = "LOCAL_UNTIL_BOTH_RECEIVERS"
    # Actual ordinary FD cleanup precedes the receipt/terminal publication.
    m.sol076_finish_record(child, record, run, close_fd)
    run.charged += record['accounting_charged_bytes']
    if not run.uncertain:
        child["preowned_receiver"]["both_accepted"] = (
            False if getattr(run, 'sol081_browser_stream', False) else bool(record["pipe_accepted"]))
        record["both_receivers_accepted"] = child["preowned_receiver"]["both_accepted"]
        sol076_publish_caller_receipt(receipt, record, child, run)
        child["preowned_receiver"]["receipt_accepted"] = record.get("receipt_qualified") is True
    out.check()


def run_wave(entries, out, context, run, receipt, worker):
    active = []
    next_index = 0
    try:
        while active or next_index < len(entries):
            run.guard("network_schedule")
            out.check()
            # Drain/collect every existing child before admitting replacement work.
            for child in list(active):
                drain(child, run)
                done, status = os.waitpid(child["pid"], os.WNOHANG)
                expired = run.clock() >= min(child["started"] + child["entry"]["seconds"], run.work) or (
                    not child["connected"] and run.clock() >= child["started"] + 15)
                if done:
                    child.update(lifecycle="REAPED", wait_status=status)
                elif expired:
                    run.fail("REQUEST_TIMEOUT")
                    stop_child(child, run)
                else:
                    continue
                collect(child, out, run, receipt)
                active.remove(child)
                for key in ("body", "pipe", "pidfd"):
                    close_fd(child[key], run)
                    child[key] = None
            if run.reason or run.uncertain:
                break
            while len(active) < 4 and next_index < len(entries):
                active.append(launch(entries[next_index], out, context, run, worker))
                next_index += 1
                run.peak = max(run.peak, len(active))
            if active:
                resident = m.process_status(os.getpid())
                for child in active:
                    try:
                        resident += m.process_status(child["pid"])
                    except FileNotFoundError:
                        pass
                m.require(resident <= m.LIMITS["rss_bytes_max"], "RSS_CAP")
                select.select([x["pipe"] for x in active], [], [], min(0.02, max(0, run.work - run.clock())))
    except BaseException as exc:
        run.original_errors.append(exc)
        run.fail(error_name(exc))
    finally:
        # Includes children whose launch failed after fork but before active.append.
        for child in run.children:
            if not child["recorded"]:
                stop_child(child, run)
                record = unknown_record(child, "STOP_UNCONFIRMED" if run.uncertain else run.reason or "CONTOUR_ABORTED")
                run.results[child["entry"]["relative_path"]] = record
                run.charged += child["entry"]["cap"]
                child["recorded"] = True
            m.sol076_finish_record(child, run.results[child['entry']['relative_path']], run, close_fd)
        for entry in entries:
            run.results.setdefault(entry["relative_path"], {"state": "NOT_RUN", "url": entry["url"],
                "failure": run.reason, "body_complete": False, "accounting_charged_bytes": 0})


class OfflineCapability:
    """Private explicit offline-only dependency seam; no production CLI access."""
    def __init__(self, root, plan, raw, remote, local, paths, dirs, worker,
                 clock=time.monotonic, wall=1800, reserve=60, hook=None, stop=None):
        parent, name = os.path.split(root)
        st = os.stat(parent, follow_symlinks=False)
        m.require(parent.startswith("/var/tmp/astra-e4-material-fetcher-a025-offline-") and
                  os.path.dirname(parent) == "/var/tmp" and
                  stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o700 and
                  root != m.ROOT and not os.path.lexists(root) and name not in ("", ".", ".."), "OFFLINE_PRIVATE_TARGET")
        m.require(len(remote) == 210 and len(local) == 107 and len(paths) == len(set(paths)) == 320 and
                  len(dirs) == 12 and set(paths) == {r["relative_path"] for r in remote + local} |
                  {"bill.json", "transport-receipts.ndjson", "inventory.json"}, "OFFLINE_EXACT_BILL_SHAPE")
        for p in paths + dirs:
            m.relpath(p)
        def deny(event, args):
            if event.startswith(("socket.", "subprocess.")) or event in ("os.exec", "os.posix_spawn"):
                raise RuntimeError("OFFLINE_CAPABILITY_NETWORK_EXEC_DENIED")
        sys.addaudithook(deny)
        self.values = root, plan, raw, remote, local, paths, dirs, worker
        self.run = Run(clock, wall, reserve, hook, stop)
        self.context = types.SimpleNamespace(check_hostname=True, verify_mode=ssl.CERT_REQUIRED)


def preflight_inputs(local, run, inputs):
    """Same all107 no-effect held-input preflight for production, CLI and controls."""
    m.require(len(local) == 107, "INPUT_COUNT_107")
    observations = []
    for row in local:
        run.guard("input_preflight")
        fd = m.absolute_open(row["source"])
        inputs.append((row, fd, None))
        count, sha, _ = guarded_digest(fd, row["cap"], run, "input_hash",
                                      row["sha256"], row["size"])
        current = m.file_stat(fd)
        inputs[-1] = row, fd, m.identity(current)
        observations.append({"source": row["source"], "relative_path": row["relative_path"],
                             "bytes": count, "sha256": sha, "cap": row["cap"],
                             "identity": list(m.identity(current)),
                             "publisher_verification": row.get("publisher_verification", "NO_NEW_CLAIM")})
    run.guard("all_107_inputs_preflight_complete")
    return observations


def read_only_preflight():
    m.require(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode and
              dict(os.environ) == {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
              "ISOLATED_EMPTY_ENV_REQUIRED")
    plan, _ = m.pinned_json(m.PLAN, m.PLAN_SHA)
    owner, _ = m.pinned_json(m.OWNER, m.OWNER_SHA)
    m.require(owner["schema"] == "friday.astra.e4.owner-delegated-project-authority.v1", "OWNER_SCHEMA")
    _, local, _, _ = m.compile_plan(plan)
    m.require(not os.path.lexists(m.ROOT), "EXISTING_QUARANTINE")
    run, inputs = Run(), []
    try:
        observations = preflight_inputs(local, run, inputs)
    finally:
        for _, fd, _ in inputs:
            close_fd(fd, run)
    m.require(not run.reason and not run.uncertain, "PREFLIGHT_CLEANUP")
    run.guard("read_only_preflight_terminal", True)
    m.require(not os.path.lexists(m.ROOT), "EXISTING_QUARANTINE")
    return {"state": "ALL_107_PRODUCTION_INPUTS_PREFLIGHT_PASSED",
            "input_count": len(observations), "observations": observations,
            "network_effects": False, "quarantine_effects": False,
            "acquisition_complete": False, "publisher_admission": "NOT_PROVEN",
            "bill_sha256": m.PLAN_SHA, "historical_bill_sha256": _HISTORICAL_PLAN_SHA,
            "completed_msk": m.now(), "elapsed_sec": round(run.clock() - run.started, 6)}


def execute(*, capability=None):
    global ACTIVE_RUN
    if capability is None:
        m.require(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode and
                  dict(os.environ) == {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}, "ISOLATED_EMPTY_ENV_REQUIRED")
        run = Run()
        plan, raw = m.pinned_json(m.PLAN, m.PLAN_SHA)
        owner, _ = m.pinned_json(m.OWNER, m.OWNER_SHA)
        m.require(owner["schema"] == "friday.astra.e4.owner-delegated-project-authority.v1", "OWNER_SCHEMA")
        remote, local, paths, dirs = m.compile_plan(plan)
        root, worker = m.ROOT, m.worker
        observed = m.resources()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        ca = m.absolute_open(m.CA)
        try:
            guarded_digest(ca, 1048576, run, "ca_preflight", m.CA_SHA)
            context.load_verify_locations(cafile="/proc/self/fd/" + str(ca))
        finally:
            close_fd(ca, run)
        resource.setrlimit(resource.RLIMIT_AS, (67108864, 67108864))
    else:
        m.require(type(capability) is OfflineCapability, "OFFLINE_CAPABILITY_TYPE")
        root, plan, raw, remote, local, paths, dirs, worker = capability.values
        run, context, observed = capability.run, capability.context, {"offline": True}
    ACTIVE_RUN = run
    m.require(not os.path.lexists(root), "EXISTING_QUARANTINE")
    m.require(sum(r["cap"] for r in remote) <= m.LIMITS["transfer_body_bytes_max"] and
              sum(r["cap"] for r in remote + local) + len(raw) + 67108864 < m.LIMITS["quarantine_bytes_max"], "RESERVATION")
    inputs, out, receipt = [], None, None
    def owner_stop(signum, frame):
        run.cancel = True
    prior_signals = {s: signal.signal(s, owner_stop) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        preflight_inputs(local, run, inputs)
        run.guard("create_quarantine")
        os.umask(0o077)
        out = m.Output(root, paths, dirs)
        fd = out.create("bill.json")
        try:
            guarded_write(fd, raw, run, "bill_write")
            os.fsync(fd)
            run.guard("bill_seal")
        finally:
            close_fd(fd, run)
        receipt = out.create("transport-receipts.ndjson")
        run.receipt = receipt
        for row, fd, checkpoint in inputs:
            run.guard("local_copy")
            m.require(m.identity(m.file_stat(fd)) == checkpoint, "INPUT_CHANGED_AFTER_PREFLIGHT")
            target = out.create(row["relative_path"])
            try:
                count, sha, _ = guarded_digest(fd, row["cap"], run, "copy_input_hash", row["sha256"], row["size"])
                os.lseek(fd, 0, os.SEEK_SET)
                left = count
                while left:
                    run.guard("local_copy_chunk")
                    data = os.read(fd, min(m.CHUNK, left))
                    m.require(data, "COPY_TRUNCATED")
                    guarded_write(target, data, run, "local_copy_write")
                    left -= len(data)
                m.require(m.identity(m.file_stat(fd)) == checkpoint, "COPY_INPUT_DRIFT")
                guarded_digest(target, row["cap"], run, "copy_output_hash", sha, count)
                os.fsync(target)
                run.guard("local_copy_seal")
                record = {"state": "LOCAL_HASH_MATCH", "bytes": count, "sha256": sha}
                if row.get("publisher_verification") == "NOT_PROVEN":
                    record.update(publisher_verification="NOT_PROVEN",
                                  input_purpose="DIAGNOSTIC_UNACCEPTED_VERIFICATION_INPUT",
                                  historical_sha256=row["historical_sha256"],
                                  historical_size=row["historical_size"],
                                  signature_and_member_chain_verified=False)
                run.results[row["relative_path"]] = record
            finally:
                close_fd(target, run)
        run_wave([r for r in remote if r["kind"] != "browser"], out, context, run, receipt, worker)
        if not run.reason:
            run.guard("mapping")
            m.mapping_checks(out, plan["acquisition_bill"], run.results)
            run.guard("mapping_complete")
            run_wave([r for r in remote if r["kind"] == "browser"], out, context, run, receipt, worker)
    except BaseException as exc:
        run.fail(error_name(exc))
    finally:
        for _, fd, _ in inputs:
            close_fd(fd, run)
        receipt_closed = close_fd(receipt, run) if receipt is not None else True
        if receipt is not None and receipt_closed is False:
            try:
                sol076_append_receipt_end(out, run, 'RECEIPT_CLOSE_UNCONFIRMED')
            except BaseException as exc:
                run.original_errors.append(exc)
                run.fail('RECEIPT_END:' + error_name(exc))
        run.receipt = None
        for signum, previous in prior_signals.items():
            try: signal.signal(signum, previous)
            except BaseException as exc:
                run.original_errors.append(exc)
                run.fail('SIGNAL_RESTORE:' + error_name(exc))
    for row in remote + local:
        run.results.setdefault(row["relative_path"], {"state": "NOT_RUN", "failure": run.reason,
                                                    "body_complete": False, "accounting_charged_bytes": 0})
    result = {"state": run.status(), "quarantine": root if out else None, "inventory": None,
              "body_complete": False, "uncertainty_sticky": run.uncertain, "reason": run.reason,
              "uncertainty_receipt_persisted": run.uncertainty_persisted,
              "errors": run.errors, "materials": run.results, "peak_workers": run.peak,
              "started_routes": len(run.started_paths), "charged_body_bytes": run.charged,
              "wire_bytes": None, "acceptance_complete": False,
              "children": [{"pid": c["pid"], "lifecycle": c["lifecycle"],
                            "stop_attempted": c["stop_attempted"],
                            "relative_path": c["entry"]["relative_path"]} for c in run.children]}
    if out is None:
        return result
    try:
        if run.reason or run.uncertain:
            return result
        run.guard("finalization_begin", True)
        out.check()
        for path in paths:
            run.guard("final_slot", True)
            if path not in out.created:
                close_fd(out.create(path), run)
        retained = []
        for path in paths:
            run.guard("finalization_hash", True)
            parts = m.relpath(path)
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW,
                         dir_fd=out.fds["/".join(parts[:-1])])
            try:
                st = m.file_stat(fd, True)
                count, sha, _ = guarded_digest(fd, m.LIMITS["quarantine_bytes_max"], run, "finalization_hash_chunk", terminal=True)
                retained.append({"path": path, "type": "regular", "mode": "0600", "uid": st.st_uid,
                    "gid": st.st_gid, "nlink": st.st_nlink, "device": st.st_dev, "inode": st.st_ino,
                    "bytes": count if path != "inventory.json" else None,
                    "sha256": sha if path != "inventory.json" else None})
            finally:
                close_fd(fd, run)
        run.guard("inventory_serialize", True)
        manifest = {"schema": "friday.astra.e4.a025-r2.inventory.v1",
            "state": "PREPARED_REQUIRES_EXTERNAL_ON_TIME_SEAL_RECEIPT", "materials": run.results,
            "retained_files": retained, "plan_sha256": m.PLAN_SHA, "owner_sha256": m.OWNER_SHA,
            "offline": capability is not None, "resources": observed, "peak_workers": run.peak,
            "charged_body_bytes": run.charged, "wire_bytes": None,
            "directories": [{"path": key or ".", "type": "directory", "mode": "0700",
                "uid": os.fstat(fd).st_uid, "gid": os.fstat(fd).st_gid, "nlink": os.fstat(fd).st_nlink,
                "device": os.fstat(fd).st_dev, "inode": os.fstat(fd).st_ino} for key, fd in out.fds.items()],
            "execution_install_root_live_gate_release_credit": False}
        data = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        run.guard("inventory_serialized", True)
        m.require(len(data) <= 33554432 and sum(r["bytes"] or 0 for r in retained) + len(data) <=
                  m.LIMITS["quarantine_bytes_max"] and run.charged <= m.LIMITS["transfer_body_bytes_max"], "FINAL_CAPS")
        fd = os.open("inventory.json", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=out.root)
        try:
            m.require(os.fstat(fd).st_size == 0, "INVENTORY_COLLISION")
            guarded_write(fd, data, run, "inventory_write", True)
            os.fsync(fd)
            run.guard("inventory_fsynced", True)
        finally:
            close_fd(fd, run)
        run.guard("inventory_final_custody", True)
        out.check()
        run.guard("terminal_seal", True)
        m.require(not run.reason and not run.uncertain, "SEAL_FAILURE")
        result.update(state=run.status(), inventory=root + "/inventory.json", bytes=len(data),
                      sha256=hashlib.sha256(data).hexdigest(), body_complete=True,
                      completed_msk=m.now(), elapsed_sec=round(run.clock() - run.started, 6))
        return result
    except BaseException as exc:
        run.fail(error_name(exc))
        result.update(state=run.status(), reason=run.reason, inventory=None, body_complete=False)
        return result
    finally:
        try:
            out.close()
            if result.get("body_complete"):
                run.guard("output_closed", True)
                result.update(completed_msk=m.now(), elapsed_sec=round(run.clock() - run.started, 6))
        except BaseException as exc:
            run.fail("OUTPUT_CLOSE:" + error_name(exc))
            result.update(state=run.status(), reason=run.reason, uncertainty_sticky=run.uncertain,
                          inventory=None, body_complete=False)
        if run.reason or run.uncertain:
            result.update(state=run.status(), reason=run.reason, uncertainty_sticky=run.uncertain,
                          inventory=None, body_complete=False, terminal_completion=False,
                          acceptance_complete=False)


def emit_terminal(result):
    try:
        if ACTIVE_RUN is not None:
            result['launch_journal'] = [m.sol076_launch_DATA(child) for child in ACTIVE_RUN.children
                                        if 'sol076' in child]
            if ACTIVE_RUN.reason or ACTIVE_RUN.uncertain:
                result.update(state=ACTIVE_RUN.status(), body_complete=False,
                              terminal_completion=False, acceptance_complete=False, inventory=None)
        if ACTIVE_RUN is not None and not ACTIVE_RUN.uncertain:
            ACTIVE_RUN.guard("terminal_serialization", True)
            if ACTIVE_RUN.reason:
                result.update(state=ACTIVE_RUN.status(), body_complete=False,
                              terminal_completion=False, acceptance_complete=False, inventory=None)
        data = json.dumps(result, separators=(",", ":")).encode() + b"\n"
        if ACTIVE_RUN is not None and not ACTIVE_RUN.uncertain:
            ACTIVE_RUN.guard("terminal_serialized", True)
            if ACTIVE_RUN.reason:
                result.update(state=ACTIVE_RUN.status(), body_complete=False,
                              terminal_completion=False, acceptance_complete=False, inventory=None)
                data = json.dumps(result, separators=(',', ':')).encode() + b'\n'
        preowned = m.sol069_preowned_backing(data)
        m.require(bytes(preowned["view"]) == data and preowned["bound_before_channel_write"]
                  and preowned["receiver_accepted"] is False and preowned["exit_is_handover"] is False,
                  "TERMINAL_PREOWNED_BEFORE_EXISTING_FD")
        preowned["commit"] = m.sol069_commit_existing(
            1, preowned["backing"], len(data), os.write, os.pread, os.pwrite, os.fstat, stat.S_ISREG)
        preowned["receiver_accepted"] = False
    except BaseException:
        if ACTIVE_RUN is not None and ACTIVE_RUN.uncertain:
            os.write(1, UNCERTAIN_RECEIPT)
        else:
            raise


def main():
    m.require(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode, "ISOLATED_PYTHON_REQUIRED")
    if sys.argv[1:] in ([], ["--describe"]):
        plan, _ = m.pinned_json(m.PLAN, m.PLAN_SHA)
        remote, local, paths, dirs = m.compile_plan(plan)
        print(json.dumps({"network_effects": False, "quarantine_effects": False, "remote_routes": len(remote),
                          "local_copies": len(local), "files": len(paths), "directories": len(dirs) + 1}))
        return
    m.require(len(sys.argv) == 4 and sys.argv[1] in
              ("--execute-reviewed-a032", "--preflight-reviewed-a032"), "CLI_REVIEW_REQUIRED")
    checked_source(os.path.abspath(__file__), sys.argv[2])
    m.require(sys.argv[3] == m.PLAN_SHA, "EXTERNAL_SUCCESSOR_BILL_PIN")
    checked_source(m.PLAN, sys.argv[3])
    if sys.argv[1] == "--preflight-reviewed-a032":
        emit_terminal(read_only_preflight())
    else:
        emit_terminal(execute())


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        if ACTIVE_RUN is not None and ACTIVE_RUN.uncertain:
            os.write(1, UNCERTAIN_RECEIPT)  # no JSON serializer/cleanup can erase uncertainty
        else:
            print(json.dumps({"state": "REFUSED", "failure": error_name(exc), "body_complete": False}))
        sys.exit(2)
