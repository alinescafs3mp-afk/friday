#!/usr/bin/python3.14
"""A061 SOURCE ONLY. Reviewed outer owner; never trust inner success alone.
Future invocation requires external exact source/runtime pins and an already
provisioned exclusive root-controlled cgroup. This source never creates a cgroup.
Private inert controls call the same drain/reap/terminal consumers, without exec.
"""
import errno
import fcntl
import hashlib
import json
import os
import re
import resource
import select
import signal
import stat
import struct
import sys
import time
import types

BASE = "/home/jericho/.jericho/runtime/subagent-lifecycle/"
STEM = "/var/tmp/friday-astra-browser-full-native-control-a066-g1/A066"
EXECUTOR, CONTROLS, BILL = (STEM + s for s in ("-EXECUTOR.py", "-CONTROLS.py", "-BILL.json"))
RUNTIME = STEM + "-TRUSTED-RUNTIME.json"
PYTHON = "/usr/bin/python3.14"
STDLIB = "/usr/lib/python3.14"
CGROUP = "/sys/fs/cgroup/friday-browser3-a061-g1"
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
PIPE_CAP = 1048576
RSS = 268435456
OUTER_AS = 67108864
INNER_MEMORY = RSS - OUTER_AS
SEALS = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
SOURCE_ROLES = {"executor": (101, EXECUTOR), "controls": (102, CONTROLS),
    "supervisor": (103, STEM + "-SUPERVISOR.py"), "bill": (104, BILL),
    "G1": (105, BASE + "ASTRA-E4-MATERIAL-ACQUISITION-EXECUTOR-A025-G1.py"),
    "R4": (106, BASE + "ASTRA-E4-MATERIAL-ACQUISITION-EXECUTOR-A025-G1-R4.py"),
    "owner": (107, "/home/jericho/.jericho/grok-takeover/ASTRA-E4-OWNER-DELEGATED-PROJECT-AUTHORITY-20261001.json"),
    "inventory": (108, BASE + "ASTRA-E4-PARTIAL-ACQUISITION-CUSTODY-BROWSER-SUCCESSOR-A035-G1-PARTIAL-INVENTORY.json"),
    "CA": (109, "/etc/ssl/certs/ca-certificates.crt")}
INCOMPLETE = b'{"state":"OUTER_SUPERVISION_FAILED","body_complete":false,"terminal_completion":false,"acceptance_complete":false}\n'


class Refused(Exception):
    pass


def need(ok, cause):
    if not ok:
        raise Refused(cause)


def ident(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
            st.st_uid, st.st_gid, st.st_mode, st.st_nlink)


def bounded_fd_bytes(fd, sha, cap, *, sealed=False, root=False, deadline=None, collect=True):
    """Hash and consume the SAME descriptor. Sealed memfds cannot be substituted.
    Root custody is separate from a parser fixture; no fixture grants root proof.
    """
    need(type(sha) is str and re.fullmatch(r"[0-9a-f]{64}", sha), "UNKNOWN_PIN")
    before = os.fstat(fd)
    need(stat.S_ISREG(before.st_mode) and 0 <= before.st_size <= cap, "HELD_FD_CUSTODY")
    if root:
        need(before.st_uid == before.st_gid == 0, "HELD_ROOT_AUTHORITY")
    if sealed:
        need(fcntl.fcntl(fd, fcntl.F_GET_SEALS) & SEALS == SEALS, "HELD_KERNEL_SEALS")
    parts, h, count = [], hashlib.sha256(), 0
    while True:
        if deadline is not None:
            need(time.monotonic() < deadline, "HELD_READ_TIMEOUT")
        block = os.pread(fd, min(65536, cap - count + 1), count)
        if not block:
            break
        count += len(block)
        need(count <= cap, "HELD_FD_SIZE")
        h.update(block)
        if collect: parts.append(block)
    need(count == before.st_size and h.hexdigest() == sha, "HELD_FD_SHA")
    need(ident(before) == ident(os.fstat(fd)), "HELD_FD_DRIFT")
    return b"".join(parts) if collect else ident(before)


class HeldSource:
    """Freeze an approved source/tool/data file into a real sealed memfd.
    The original path is checked during capture only. Later consumers use this
    exact sealed fd, never a new named-path open. No runtime execution here.
    """
    def __init__(self, path, sha, cap=PIPE_CAP, *, system=False, deadline=None):
        self.path, self.sha, self.fd = path, sha, None
        original = nf(path)
        try:
            before = os.fstat(original)
            need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
                 ((before.st_uid == before.st_gid == 0 and not before.st_mode & 0o022) if system else
                  (before.st_uid == before.st_gid == os.getuid() and stat.S_IMODE(before.st_mode) == 0o600)),
                 "PIN_CUSTODY")
            raw = bounded_fd_bytes(original, sha, cap, deadline=deadline)
            linked = nf(path)
            try: need(ident(os.fstat(linked)) == ident(before), "PIN_PATH_DRIFT")
            finally: os.close(linked)
            self.fd = os.memfd_create("friday-a061-approved-bytes", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            left = raw
            while left:
                if deadline is not None:
                    need(time.monotonic() < deadline, "HELD_READ_TIMEOUT")
                n = os.write(self.fd, left)
                need(n > 0, "HELD_SHORT_WRITE"); left = left[n:]
            fcntl.fcntl(self.fd, fcntl.F_ADD_SEALS, SEALS)
            need(bounded_fd_bytes(self.fd, sha, cap, sealed=True, deadline=deadline) == raw, "HELD_COPY_DRIFT")
        except BaseException:
            self.close(); raise
        finally:
            os.close(original)

    def bytes(self, cap=PIPE_CAP, deadline=None):
        return bounded_fd_bytes(self.fd, self.sha, cap, sealed=True, deadline=deadline)

    def close(self):
        if self.fd is not None:
            value, self.fd = self.fd, None
            os.close(value)


def source_bundle(pins, deadline):
    need(type(pins) is dict and set(pins) == set(SOURCE_ROLES), "HELD_SOURCE_SET")
    held = {}
    try:
        for role, (number, path) in SOURCE_ROLES.items():
            held[role] = HeldSource(path, pins[role], system=role == "CA", deadline=deadline)
        return held
    except BaseException:
        for blob in held.values(): blob.close()
        raise


class NativeLaunchAdapter:
    """Functional fd-only handoff to a SEPARATELY approved STATIC native launcher.
    Legacy structural-control adapter, NOT the A061 public production entry.
    The actual A061 implementation is BOOTSTRAP/COMMON/OWNED.c. This legacy
    signature is retained only for the inherited216 fixture obligations and cannot
    approve that native implementation. Separately admitted runtime must implement
    the declared pre-interpreter runtime/mount/limits/clone-registry contract.
    Its exact binary SHA and runtime closure come from independent root review.
    No arbitrary command, path, URL, environment or weaker interpreter route.
    """
    def __init__(self, launcher, runtime, *, launcher_sha, runtime_sha, deadline):
        self.launcher, self.runtime, self.deadline = launcher, runtime, deadline
        raw = bounded_fd_bytes(launcher, launcher_sha, 16777216, sealed=True, root=True, deadline=deadline)
        need(raw[:4] == b"\x7fELF", "NATIVE_LAUNCHER_ELF")
        dependencies, loader = elf_needed_fd(launcher)
        need(not dependencies and loader is None, "NATIVE_LAUNCHER_NOT_STATIC")
        bounded_fd_bytes(runtime, runtime_sha, 134217728, sealed=True, root=True, deadline=deadline, collect=False)
        self.pins = {"launcher_sha256": launcher_sha, "runtime_sha256": runtime_sha}

    def handoff(self, held, capsule_fd, out, err, group, attach):
        # Called in the exact owned child, after authoritative intention registration
        # in its parent. execve(fd) consumes the already approved static ELF bytes.
        need(time.monotonic() < self.deadline, "NATIVE_LAUNCHER_TIMEOUT")
        need(set(held) == set(SOURCE_ROLES), "HELD_SOURCE_SET")
        needed = {100: capsule_fd, 110: self.launcher, 111: self.runtime, 120: group, 121: attach}
        needed.update({SOURCE_ROLES[role][0]: blob.fd for role, blob in held.items()})
        # Duplicate above all fixed destinations BEFORE dup2, avoiding fd permutation.
        temporary = {}
        try:
            for number, fd in needed.items():
                temporary[number] = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 256)
            for number, fd in temporary.items(): os.dup2(fd, number, inheritable=True)
        finally:
            for fd in temporary.values(): os.close(fd)
        os.dup2(out, 1); os.dup2(err, 2)
        keep = {0, 1, 2} | set(needed)
        for name in os.listdir("/proc/self/fd"):
            fd = int(name)
            if fd not in keep:
                try: os.close(fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF: raise
        os.execve(110, ["friday-approved-native-browser3", "--inner-held-a061", "100", "111"], ENV)


def nf(path):
    need(type(path) is str and path.startswith("/") and
         all(p not in ("", ".", "..") for p in path.split("/")[1:]), "ABSOLUTE_PATH")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.split("/")[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        return os.open(path.split("/")[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
    finally:
        os.close(fd)


def seal(path, sha, cap, system=False, collect=True):
    need(type(sha) is str and re.fullmatch(r"[0-9a-f]{64}", sha), "UNKNOWN_PIN")
    fd = nf(path)
    try:
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= cap,
             "PIN_CUSTODY")
        need((before.st_uid == before.st_gid == 0 and not before.st_mode & 0o022) if system else
             (before.st_uid == before.st_gid == os.getuid() and stat.S_IMODE(before.st_mode) == 0o600),
             "PIN_CUSTODY")
        h, data, count = hashlib.sha256(), [], 0
        while True:
            block = os.read(fd, min(65536, cap - count + 1))
            if not block: break
            count += len(block); need(count <= cap, "PIN_SIZE")
            h.update(block)
            if collect: data.append(block)
        need(count == before.st_size and h.hexdigest() == sha and ident(before) == ident(os.fstat(fd)), "PIN_SHA_OR_DRIFT")
        linked = nf(path)
        try: need(ident(os.fstat(linked)) == ident(before), "PIN_PATH_DRIFT")
        finally: os.close(linked)
        return b"".join(data) if collect else ident(before)
    finally: os.close(fd)


def inert(raw):
    need(len(raw) <= PIPE_CAP, "JSON_CAP")
    def pairs(rows):
        result = {}
        for k, v in rows:
            need(k not in result, "JSON_DUPLICATE")
            result[k] = v
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(Refused("JSON_CONSTANT")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Refused("JSON_PARSE") from exc


def elf_needed_fd(fd):
    """Read inert ELF64 headers only; never run a loader/ldd/native probe."""
    before = os.fstat(fd)
    def read(count, offset, reason):
        need(type(offset) is int and offset >= 0 and count >= 0 and
             offset + count <= before.st_size, reason)
        raw = os.pread(fd, count, offset)
        need(len(raw) == count, reason)
        return raw
    if True:
        header = os.pread(fd, 64, 0)
        if header[:4] != b"\x7fELF": return (), None
        need(len(header) == 64 and header[4:7] == b"\x02\x01\x01" and
             struct.unpack_from("<HHI", header, 16) in ((2, 62, 1), (3, 62, 1)), "RUNTIME_ELF_FORMAT")
        phoff = struct.unpack_from("<Q", header, 32)[0]
        phsize, phcount = struct.unpack_from("<HH", header, 54)
        need(phsize == 56 and 0 < phcount <= 128, "RUNTIME_ELF_HEADERS")
        loads, dynamic, interpreter = [], None, None
        for i in range(phcount):
            ph = read(56, phoff + i * phsize, "RUNTIME_ELF_HEADERS")
            typ, flags, off, addr, physical, filesz, memsz, alignment = struct.unpack("<IIQQQQQQ", ph)
            if typ == 1: loads.append((addr, filesz, off))
            if typ == 2: dynamic = (off, filesz)
            if typ == 3:
                need(interpreter is None and 0 < filesz <= 512, "RUNTIME_LOADER_CAP")
                raw = read(filesz, off, "RUNTIME_LOADER_CAP")
                need(raw.endswith(b"\0") and b"\0" not in raw[:-1], "RUNTIME_LOADER_CAP")
                interpreter = raw[:-1].decode("ascii", "strict")
        if dynamic is None: return (), interpreter
        need(dynamic[1] <= 65536 and dynamic[1] % 16 == 0, "RUNTIME_DYNAMIC_CAP")
        entries = [struct.unpack("<qQ", read(16, dynamic[0] + i, "RUNTIME_DYNAMIC_CAP"))
                   for i in range(0, dynamic[1], 16)]
        needed = [v for k, v in entries if k == 1]
        strings = [v for k, v in entries if k == 5]
        sizes = [v for k, v in entries if k == 10]
        need(len(strings) == len(sizes) == 1 and sizes[0] <= PIPE_CAP and
             not any(k in (15, 29) for k, v in entries), "RUNTIME_DYNAMIC_SEARCH")
        locations = [off + strings[0] - addr for addr, size, off in loads
                     if addr <= strings[0] and strings[0] + sizes[0] <= addr + size]
        need(len(locations) == 1, "RUNTIME_STRTAB")
        table = read(sizes[0], locations[0], "RUNTIME_STRTAB")
        result = []
        for off in needed:
            need(off < len(table) and b"\0" in table[off:], "RUNTIME_SONAME")
            name = table[off:].split(b"\0", 1)[0].decode("ascii", "strict")
            need(re.fullmatch(r"[A-Za-z0-9_.+-]+", name), "RUNTIME_SONAME")
            result.append(name)
        need(ident(before) == ident(os.fstat(fd)), "RUNTIME_ELF_DRIFT")
        return tuple(sorted(result)), interpreter


def elf_needed(path):
    fd = nf(path)
    try: return elf_needed_fd(fd)
    finally: os.close(fd)


class RuntimeView:
    """Actual filesystem boundary; private owned fixture mapping only changes I/O.
    Production always constructs RuntimeView() for the real fixed OS namespace.
    No public flag or runtime manifest can choose another physical root/owner.
    """
    def __init__(self, root="/", *, fixture=False):
        self.root = root.rstrip("/")
        self.fixture = fixture
        if fixture:
            parent = os.path.dirname(root)
            need(parent.startswith("/var/tmp/astra-e4-browser3-a061-offline-") and
                 os.path.dirname(parent) == "/var/tmp" and root == parent + "/runtime",
                 "RUNTIME_FIXTURE_SCOPE")
            st = os.lstat(root)
            need(stat.S_ISDIR(st.st_mode) and st.st_uid == st.st_gid == os.getuid() and
                 stat.S_IMODE(st.st_mode) == 0o700, "RUNTIME_DIRECTORY_CUSTODY")
        else:
            need(root == "/", "RUNTIME_PATH")
        self.owner = os.getuid() if fixture else 0

    def path(self, logical):
        need(type(logical) is str and logical.startswith("/") and
             all(p not in ("", ".", "..") for p in logical.split("/")[1:]), "RUNTIME_LIBRARY_PATHS")
        return self.root + logical

    def exists(self, logical):
        return os.path.lexists(self.path(logical))

    def stat(self, logical):
        return os.lstat(self.path(logical))

    def realpath(self, logical):
        actual = os.path.realpath(self.path(logical))
        need(not self.root or actual.startswith(self.root + "/"), "RUNTIME_ALIAS_ESCAPE")
        return actual[len(self.root):]

    def directories(self):
        physical = self.path(STDLIB)
        need(os.path.isdir(physical), "RUNTIME_STDLIB_ABSENT")
        for base, dirs, leaves in os.walk(physical, followlinks=False):
            logical = base[len(self.root):]
            yield logical, dirs, leaves

    def held(self, logical, row, deadline):
        return HeldSource(self.path(logical), row["sha256"], row["bytes"],
                          system=not self.fixture, deadline=deadline)


class RuntimeClosure:
    def __init__(self, manifest, holds, view):
        self.manifest, self.holds, self.view = manifest, holds, view
        # A parser fixture can prove byte consumers, never real root/OS authority.
        self.authority = "NOT_PROVEN" if view.fixture else "ROOT_FILE_CUSTODY_ONLY_NOT_PREINTERPRETER_PROOF"

    def __getitem__(self, key):
        return self.manifest[key]

    def close(self):
        values, self.holds = self.holds, {}
        for blob in values.values(): blob.close()

    def check_held(self, deadline):
        for path, blob in self.holds.items():
            row = self.manifest["files"][path]
            bounded_fd_bytes(blob.fd, row["sha256"], row["bytes"], sealed=True,
                             deadline=deadline, collect=False)


def cache_resolutions(cache, view):
    need(cache[:20] == b"glibc-ld.so.cache1.1" and len(cache) >= 48, "RUNTIME_CACHE_FORMAT")
    count = struct.unpack_from("<I", cache, 20)[0]
    need(count <= 8192 and 48 + 24 * count <= len(cache), "RUNTIME_CACHE_CAP")
    resolutions = {}
    for i in range(count):
        flags, key, value, version, hwcap = struct.unpack_from("<iIIIQ", cache, 48 + 24 * i)
        if flags & 0xff00 != 0x300:
            continue
        def string(offset):
            need(48 + 24 * count <= offset < len(cache) and b"\0" in cache[offset:],
                 "RUNTIME_CACHE_STRING")
            return cache[offset:].split(b"\0", 1)[0].decode("ascii", "strict")
        resolutions.setdefault(string(key), set()).add(view.realpath(string(value)))
    return resolutions


def runtime_preflight(path, sha, deadline, *, view=None):
    # Fixed production path and schema. Only an in-process owned I/O fixture view
    # is permitted; it cannot escape to the CLI or carry OS-authority credit.
    view = RuntimeView() if view is None else view
    need(type(view) is RuntimeView, "RUNTIME_VIEW")
    need(path == RUNTIME, "RUNTIME_PATH")
    raw = seal(view.path(path), sha, PIPE_CAP)
    return runtime_preflight_bytes(raw, deadline, view=view)


def runtime_preflight_bytes(raw, deadline, *, view=None):
    """Same complete held-file/ELF/cache consumer; image producer consumes held manifest bytes."""
    view = RuntimeView() if view is None else view
    need(type(view) is RuntimeView, "RUNTIME_VIEW")
    manifest = inert(raw)
    need(type(manifest) is dict and set(manifest) ==
         {"schema", "interpreter", "stdlib", "files", "sonames", "loader_alias"} and
         manifest["schema"] == "friday.browser3.trusted-runtime.v1" and
         manifest["interpreter"] == PYTHON and manifest["stdlib"] == STDLIB, "RUNTIME_SCHEMA")
    files, names = manifest["files"], manifest["sonames"]
    need(type(files) is dict and 0 < len(files) <= 8192 and PYTHON in files and
         type(names) is dict, "RUNTIME_FILE_SET")
    actual = {PYTHON}
    for root, dirs, leaves in view.directories():
        need(time.monotonic() < deadline, "PREFLIGHT_TIMEOUT")
        st = view.stat(root)
        need(stat.S_ISDIR(st.st_mode) and st.st_uid == st.st_gid == view.owner and
             not st.st_mode & 0o022, "RUNTIME_DIRECTORY_CUSTODY")
        for name in dirs:
            need(not stat.S_ISLNK(view.stat(root + "/" + name).st_mode), "RUNTIME_SYMLINK")
        for leaf in leaves: actual.add(root + "/" + leaf)
    need(not view.exists("/usr/lib/python314.zip"), "UNKNOWN_RUNTIME_ZIP")
    extras = set(files) - actual
    need(extras and "/etc/ld.so.cache" in extras and not view.exists("/etc/ld.so.preload") and
         all(p == "/etc/ld.so.cache" or p.startswith("/usr/lib/x86_64-linux-gnu/") or
             p.startswith("/usr/lib64/") for p in extras), "RUNTIME_LIBRARY_PATHS")
    need(actual <= set(files), "RUNTIME_MEMBERSHIP")
    holds, total = {}, 0
    try:
        for p, row in files.items():
            need(time.monotonic() < deadline, "PREFLIGHT_TIMEOUT")
            need(type(row) is dict and set(row) == {"sha256", "bytes"} and
                 type(row["bytes"]) is int and row["bytes"] >= 0, "RUNTIME_PIN_ROW")
            total += row["bytes"]
            need(total <= 134217728, "RUNTIME_READ_CAP")
            holds[p] = view.held(p, row, deadline)
        need(all(type(k) is str and re.fullmatch(r"[A-Za-z0-9_.+-]+", k) and
                 type(v) is str and v in extras for k, v in names.items()), "RUNTIME_SONAME_MAP")
        cache = holds["/etc/ld.so.cache"].bytes(deadline=deadline)
        resolutions = cache_resolutions(cache, view)
        need(all(resolutions.get(name) == {target} for name, target in names.items()),
             "RUNTIME_CACHE_RESOLUTION")
        need(holds[PYTHON].bytes(16777216, deadline)[:4] == b"\x7fELF", "RUNTIME_INTERPRETER_ELF")
        for p, blob in holds.items():
            need(time.monotonic() < deadline, "PREFLIGHT_TIMEOUT")
            dependencies, loader = elf_needed_fd(blob.fd)
            need(all(name in names and names[name] in files for name in dependencies),
                 "UNKNOWN_RUNTIME_DEPENDENCY")
            if loader is not None:
                alias = manifest["loader_alias"]
                need(type(alias) is dict and set(alias) == {"path", "target"} and
                     loader == alias["path"] and alias["target"] in extras and
                     view.realpath(loader) == alias["target"], "RUNTIME_LOADER_PIN")
                for prefix in ("/lib", "/lib64", "/usr/lib64"):
                    if view.exists(prefix):
                        st = view.stat(prefix)
                        need(st.st_uid == st.st_gid == view.owner and
                             (stat.S_ISLNK(st.st_mode) or not st.st_mode & 0o022),
                             "RUNTIME_LOADER_CUSTODY")
        closure = RuntimeClosure(manifest, holds, view)
        closure.check_held(deadline)
        return closure
    except BaseException:
        for blob in holds.values(): blob.close()
        raise


def cgroup_values(read):
    expected = {"memory.max": str(INNER_MEMORY), "memory.swap.max": "0", "memory.oom.group": "1",
                "pids.max": "4", "cpu.max": "max 100000"}
    need(all(read(key) == value for key, value in expected.items()), "CGROUP_BOUND_UNKNOWN")
    need(read("cgroup.procs") == "" and read("cgroup.events") == "populated 0\nfrozen 0",
         "CGROUP_NOT_EXCLUSIVE_EMPTY")
    return expected


def cgroup_preflight(directory_fd=None, attach_fd=None):
    fd = nf(CGROUP) if directory_fd is None else os.dup(directory_fd)
    st = os.fstat(fd)
    need(stat.S_ISDIR(st.st_mode) and st.st_uid == st.st_gid == 0 and not st.st_mode & 0o022, "CGROUP_CUSTODY")
    expected = {"memory.max": str(INNER_MEMORY), "memory.swap.max": "0", "memory.oom.group": "1", "pids.max": "4", "cpu.max": "max 100000"}
    def read(key):
        item = os.open(key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            s = os.fstat(item)
            need(s.st_uid == s.st_gid == 0 and not s.st_mode & 0o002, "CGROUP_LIMIT_CUSTODY")
            if key in expected:
                need(not s.st_mode & 0o022 and not any("acl" in x for x in os.listxattr(item)), "CGROUP_LIMIT_CUSTODY")
            raw = os.read(item, 65537); need(len(raw) <= 65536, "CGROUP_READ_CAP")
            return raw.decode("ascii", "strict").strip()
        finally: os.close(item)
    try:
        cgroup_values(read)
        attach = os.open("cgroup.procs", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=fd) if attach_fd is None else os.dup(attach_fd)
        attached = os.fstat(attach)
        original = os.stat("cgroup.procs", dir_fd=fd, follow_symlinks=False)
        need((attached.st_dev, attached.st_ino) == (original.st_dev, original.st_ino), "CGROUP_ATTACH_CUSTODY")
        return fd, attach, read, expected
    except BaseException:
        if "attach" in locals(): os.close(attach)
        os.close(fd); raise


def terminal_check(raw, mode, expected_controls):
    need(raw.endswith(b"\n") and raw.count(b"\n") == 1, "PARTIAL_OR_MULTIPLE_TERMINAL")
    obj = inert(raw)
    need(type(obj) is dict, "TERMINAL_TYPE")
    if mode == "--controls":
        need(obj.get("state") == "CONTROLS_FINISHED_REQUIRES_INDEPENDENT_CAUSAL_RECEIPT_REVIEW" and
             obj.get("network_effects") == 0 and obj.get("production_target_created") is False and
             obj.get("production_admission") is False, "CONTROL_TERMINAL")
        rows = obj.get("controls")
        need(type(rows) is list and len(rows) == len(expected_controls) and
             {v.get("control") for v in rows if type(v) is dict} == set(expected_controls), "CONTROL_COVERAGE")
        for row in rows:
            meta = expected_controls[row["control"]]
            need(row.get("passed") is True and row.get("coverage") == meta and
                 row.get("forbidden_effects") == {"network": 0, "exec": 0, "original_write": 0}, "CONTROL_RECEIPT")
    else:
        need(obj.get("acceptance_complete") is False and obj.get("reason") is None and
             obj.get("state") != "STOP_UNCONFIRMED" and obj.get("uncertainty_sticky", False) is False,
             "INNER_FAILURE")
        if mode == "--preflight":
            need(obj.get("state") == "REAL_RETAINED_METADATA_PATH_PREFLIGHT_PASSED" and
                 obj.get("network") is False and obj.get("target_created") is False, "PREFLIGHT_TERMINAL")
        else:
            need(obj.get("state") == "BROWSER3_FETCH_FINISHED_WITH_EXPLICIT_ACCEPTANCE_GAPS" and
                 obj.get("body_complete") is True and obj.get("started_routes") == 3 and
                 obj.get("charged_body_bytes", RSS * 9) <= 956301312 and obj.get("peak_workers") <= 3 and
                 obj.get("execution_install_root_gate_credit") is False and
                 len(obj.get("children", [])) == 3 and all(c.get("lifecycle") == "REAPED" for c in obj["children"]), "ACQUISITION_TERMINAL")
    return obj


def proc(pid):
    with open("/proc/%d/stat" % pid, "rb") as f: raw = f.read(65537)
    need(len(raw) <= 65536, "PROC_CAP")
    fields = raw.rsplit(b")", 1)[1].split()
    return int(fields[1]), int(fields[19])


def supervise_owned(spawn, *, wall, reserve, mode, expected_controls, members=None, rss=None,
                    stop=None, cancel=None, now=time.monotonic, postcustody=None, terminal_reserve=0):
    """Actual bounded pipes, kernel waits, owned pidfds and no implicit success.
    spawn writes only to passed pipe fds. Production spawn imposes kernel limits
    and cgroup membership before exec. No public fixture route invokes this seam.
    """
    started = now(); hard = started + wall; work = hard - reserve
    need(0 <= terminal_reserve < reserve < wall, "OUTER_TIME_BOUNDS")
    cleanup_cutoff = hard - terminal_reserve
    readout, writeout = os.pipe(); readerr, writeerr = os.pipe()
    # Allocate ownership intention BEFORE a trusted fork. The trusted spawn writes
    # its actual direct-child PID into this exact object immediately after fork,
    # before it performs any /proc, pidfd, pipe, resource or observation operation.
    # This independent record survives registration/validation failure.
    direct = {"pid": None, "fd": None, "birth": None, "parent": os.getpid(),
              "direct": True, "reaped": False, "status": None, "stop_attempted": False}
    pid, records, buffers, openpipes = None, {}, [bytearray(), bytearray()], {readout: 0, readerr: 1}
    reason, uncertain, status, errors, terminal = None, False, None, [], None
    def fail(value, sticky=False):
        nonlocal reason, uncertain
        if reason is None: reason = value
        uncertain = uncertain or sticky
        if len(errors) < 128: errors.append(value)
    def track_direct(p):
        # No namespace/PID scan establishes ownership. Only the actual fork
        # intention authorizes this exact unreaped direct-child PID fallback.
        need(type(p) is int and p > 0 and direct["pid"] == p, "SPAWN_NOT_OWNED")
        records[p] = direct
        parent, birth = proc(p)
        need(parent == direct["parent"], "SPAWN_NOT_OWNED")
        direct["birth"] = birth
        try: direct["fd"] = os.pidfd_open(p)
        except OSError as exc: raise Refused("OUTER_PIDFD:" + str(exc.errno)) from exc
        need(proc(p) == (parent, birth), "OWNERSHIP_RACE")
    def dispose(p, rec):
        if rec["reaped"] or rec["stop_attempted"]:
            return
        rec["stop_attempted"] = True
        try:
            # Establish wait ownership before numeric signalling. WNOHANG never
            # waits, and a still unreaped direct child cannot recycle its PID.
            done, value = os.waitpid(p, os.WNOHANG)
            if done:
                rec.update(reaped=True, status=value)
                return
            if stop is not None:
                need(stop(p, rec), "OUTER_STOP_UNCONFIRMED")
            elif rec["fd"] is not None:
                signal.pidfd_send_signal(rec["fd"], signal.SIGKILL)
            else:
                need(rec is direct and direct["pid"] == p, "OUTER_OWNERSHIP_UNCONFIRMED")
                os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            # ESRCH does not establish reaping; a later wait still must confirm.
            pass
        except BaseException as exc:
            fail(str(exc) if isinstance(exc, Refused) else "OUTER_STOP_EXCEPTION:" + type(exc).__name__, True)
    def reap(p, rec):
        if rec["reaped"]:
            return
        try:
            done, value = os.waitpid(p, os.WNOHANG)
            if done:
                rec.update(reaped=True, status=value)
        except ChildProcessError:
            # No disappeared /proc entry, guessed parent or inner JSON grants
            # ownership/reap credit. Losing exact wait custody stays sticky.
            fail("OUTER_REAP_CUSTODY_LOST", True)
    try:
        pid = spawn(writeout, writeerr, direct)
        need(pid == direct["pid"], "SPAWN_INTENTION_MISMATCH")
        track_direct(pid)
        os.close(writeout); writeout = None; os.close(writeerr); writeerr = None
        for fd in openpipes: os.set_blocking(fd, False)
        while openpipes or any(not v["reaped"] for v in records.values()):
            if cancel is not None and cancel(): fail("OUTER_OWNER_STOP")
            if now() >= work: fail("OUTER_WORK_TIMEOUT")
            if rss is not None:
                try: need(rss() <= RSS, "OUTER_RSS_CAP")
                except BaseException as exc: fail(str(exc), True)
            if members is not None:
                try:
                    # A cgroup proves containment, not exact clone ownership.
                    # Unknown members are neither adopted nor signalled. A future
                    # authenticated native clone registry is still required for
                    # acquisition workers; this source grants no guessed credit.
                    need(all(p in records for p in members()), "UNKNOWN_CGROUP_MEMBER")
                except BaseException as exc: fail(str(exc), True)
            if reason:
                for p, rec in records.items():
                    dispose(p, rec)
            ready = select.select(list(openpipes), [], [], min(0.01, max(0, hard - now())))[0]
            for fd in ready:
                try: data = os.read(fd, 65536)
                except BlockingIOError: continue
                if data:
                    target = buffers[openpipes[fd]]
                    if len(target) + len(data) > PIPE_CAP: fail("OUTER_PIPE_CAP")
                    else: target.extend(data)
                else:
                    os.close(fd); del openpipes[fd]
            for p, rec in records.items():
                reap(p, rec)
                if p == pid and rec["reaped"]: status = rec["status"]
            if now() >= cleanup_cutoff:
                fail("OUTER_HARD_TIMEOUT", any(not v["reaped"] for v in records.values()) or bool(openpipes)); break
        need(reason is None, reason or "OUTER_FAILURE")
        need(status is not None and os.waitstatus_to_exitcode(status) == 0, "INNER_EXIT")
        need(not buffers[1], "INNER_STDERR")
        need(not openpipes and all(v["reaped"] for v in records.values()), "OUTER_REAP_OR_DRAIN_UNKNOWN")
        need(members is None or members() == [], "OUTER_CGROUP_NOT_EMPTY")
        terminal=terminal_check(bytes(buffers[0]), mode, expected_controls)
        if postcustody is not None:
            postcustody()
        if mode=="--execute":
            need({c["pid"] for c in terminal["children"]} == set(records)-{pid}, "INNER_OWNERSHIP_CLAIM")
    except BaseException as exc:
        fail(str(exc) if isinstance(exc, Refused) else type(exc).__name__)
    finally:
        # A failure during spawn/track/drain must still stop only the actual direct child.
        # Never retry the operation that failed during tracking. Use the actual
        # spawn intention, even if spawn itself raised after recording its PID.
        if direct["pid"] is not None and direct["pid"] not in records:
            records[direct["pid"]] = direct
        if pid is None:
            pid = direct["pid"]
        end = min(cleanup_cutoff, now() + reserve)
        for p, rec in records.items():
            dispose(p, rec)
        # Close the parent's write ends before draining exceptional startup.
        for key, value in (("stdout", writeout), ("stderr", writeerr)):
            if value is not None:
                try: os.close(value)
                except OSError: fail("OUTER_CLOSE_FAILED", True)
        writeout = writeerr = None
        for fd in openpipes:
            try: os.set_blocking(fd, False)
            except OSError: fail("OUTER_PIPE_STATE_UNKNOWN", True)
        while (openpipes or any(not v["reaped"] for v in records.values())) and now() < end:
            try: ready = select.select(list(openpipes), [], [], 0)[0]
            except BaseException:
                fail("OUTER_DRAIN_UNCONFIRMED", True); ready = []
            for fd in ready:
                try: data = os.read(fd, 65536)
                except BlockingIOError: continue
                except OSError:
                    fail("OUTER_DRAIN_UNCONFIRMED", True)
                    try: os.close(fd)
                    except OSError: pass
                    del openpipes[fd]; continue
                if not data:
                    try: os.close(fd)
                    except OSError: fail("OUTER_CLOSE_FAILED", True)
                    del openpipes[fd]
                elif len(buffers[openpipes[fd]]) + len(data) <= PIPE_CAP: buffers[openpipes[fd]].extend(data)
                else: fail("OUTER_PIPE_CAP")
            for p, rec in records.items():
                reap(p, rec)
            try: select.select([], [], [], min(0.01, max(0, end - now())))
            except BaseException: fail("OUTER_CLEANUP_WAIT_FAILED", True)
        if any(not v["reaped"] for v in records.values()): fail("OUTER_REAP_UNCONFIRMED", True)
        if openpipes: fail("OUTER_DRAIN_UNCONFIRMED", True)
        for rec in records.values():
            if rec["fd"] is not None:
                try: os.close(rec["fd"])
                except OSError: fail("OUTER_CLOSE_FAILED", True)
        for fd in list(openpipes) + [v for v in (writeout, writeerr) if v is not None]:
            try: os.close(fd)
            except OSError: fail("OUTER_CLOSE_FAILED", True)
    return {"state": "STOP_UNCONFIRMED" if uncertain else ("OUTER_FAILED" if reason else "OUTER_BOUNDED_DRAINED_FINISHED"),
        "reason": reason, "body_complete": False, "acceptance_complete": False,
        "errors": errors,
        "terminal_completion": reason is None and not uncertain, "uncertainty_sticky": uncertain,
        "elapsed_sec": now() - started, "stdout_bytes": len(buffers[0]), "stderr_bytes": len(buffers[1]),
        "inner_terminal_sha256":hashlib.sha256(buffers[0]).hexdigest(),
        "inner_terminal":terminal if reason is None and not uncertain else None,
        "owned": [{"pid": p, "reaped": r["reaped"], "status": r["status"]} for p, r in records.items()]}


def write_terminal(result, fd=1, trace=None, deadline=None):
    """Outer emission is itself a finite drained-pipe requirement."""
    try:
        data = json.dumps(result, separators=(",", ":")).encode() + b"\n"
        need(len(data) <= PIPE_CAP and stat.S_ISFIFO(os.fstat(fd).st_mode), "OUTER_TERMINAL_SINK")
        os.set_blocking(fd, False); end = min(time.monotonic() + 1, deadline) if deadline is not None else time.monotonic() + 1
        while data:
            need(time.monotonic() < end, "OUTER_TERMINAL_TIMEOUT")
            try: n = os.write(fd, data)
            except BlockingIOError:
                select.select([], [fd], [], min(0.01, max(0, end - time.monotonic()))); continue
            need(n > 0, "OUTER_TERMINAL_SHORT_WRITE"); data = data[n:]
        return True
    except BaseException as exc:
        if trace is not None: trace["cause"] = str(exc) if isinstance(exc, Refused) else type(exc).__name__ + ":" + str(getattr(exc,"errno",None))
        return False


class RootHeldBlob:
    def __init__(self, fd, sha):
        self.fd, self.sha = fd, sha
        bounded_fd_bytes(fd, sha, PIPE_CAP, sealed=True, root=True)


def main():
    # The native root launcher must establish outer limits/watchdog and consume
    # this exact held source BEFORE starting Python. This Python check cannot
    # retroactively certify startup. Its root capsule is mandatory, never minted.
    need(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode and
         dict(os.environ) == ENV, "ISOLATED_ENV")
    need(len(sys.argv) == 7 and sys.argv[1] in ("--controls", "--preflight", "--execute"),
         "OUTER_PUBLIC_ARGS")
    mode, supervisor_sha, executor_sha, controls_sha, bill_sha, capsule_sha = sys.argv[1:]
    try:
        capsule_raw = bounded_fd_bytes(100, capsule_sha, 65536, sealed=True, root=True)
    except OSError as exc:
        raise Refused("EXTERNAL_PREINTERPRETER_LAUNCH_REQUIRED") from exc
    capsule = inert(capsule_raw)
    need(type(capsule) is dict and type(capsule.get("sources")) is dict, "HELD_CAPSULE_IDENTITY")
    rows = capsule["sources"]
    for role, (fd, path) in SOURCE_ROLES.items():
        need(role in rows and type(rows[role]) is dict and rows[role].get("fd") == fd and
             rows[role].get("path") == path, "HELD_SOURCE_ROW")
    for role, wanted in (("supervisor", supervisor_sha), ("executor", executor_sha),
                          ("controls", controls_sha), ("bill", bill_sha)):
        need(rows[role].get("sha256") == wanted, "EXTERNAL_SOURCE_PIN")
    executor_bytes = bounded_fd_bytes(101, executor_sha, PIPE_CAP, sealed=True, root=True)
    bounded_fd_bytes(103, supervisor_sha, PIPE_CAP, sealed=True, root=True)
    x = types.ModuleType("a061_held_root_executor"); x.__file__ = EXECUTOR
    exec(compile(executor_bytes, EXECUTOR, "exec"), x.__dict__)
    admitted = x.install_held_capsule()
    need(admitted["mode"] == mode, "HELD_CAPSULE_MODE")
    hard, work = admitted["hard"], admitted["work"]
    bounds_seen={kind:resource.getrlimit(kind) for kind in
                 (resource.RLIMIT_AS,resource.RLIMIT_CPU,resource.RLIMIT_NOFILE)}
    need(all(0 <= soft <= hard_bound and hard_bound != resource.RLIM_INFINITY for soft,hard_bound in bounds_seen.values()) and
         bounds_seen[resource.RLIMIT_AS][1] <= OUTER_AS and
         bounds_seen[resource.RLIMIT_CPU][1] <= (180 if mode == "--controls" else 1200) and
         bounds_seen[resource.RLIMIT_NOFILE][1] <= 1024,
         "OUTER_PREINTERPRETER_LIMITS")
    need(stat.S_ISFIFO(os.fstat(1).st_mode), "OUTER_TERMINAL_SINK")
    plan = inert(x.HELD_BYTES[BILL])
    x.compile_bill(plan)
    adapter = NativeLaunchAdapter(110, 111, launcher_sha=admitted["native_launcher_sha256"],
                                  runtime_sha=admitted["runtime_sha256"], deadline=work)
    held = {role: RootHeldBlob(fd, rows[role]["sha256"]) for role, (fd, path) in SOURCE_ROLES.items()}
    # Root passes these actual descriptors; no reopen of cgroup attachment or
    # immutable code/runtime is used to launch the generated child.
    group, attach, read, bounds = cgroup_preflight(120, 121)
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    filesystem = ctypes.create_string_buffer(256)
    need(libc.fstatfs(group, filesystem) == 0 and
         struct.unpack_from("<q", filesystem.raw)[0] == 0x63677270, "CGROUP_KERNEL_FILESYSTEM")
    need(libc.prctl(36, 1, 0, 0, 0) == 0, "SUBREAPER_REFUSED")
    cancelled, previous, result = [False], {}, None
    try:
        def owner_stop(signum, frame): cancelled[0] = True
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, owner_stop)
        def spawn(out, err, intention):
            need(time.monotonic() < work and all(read(k) == v for k, v in bounds.items()) and
                 read("cgroup.procs") == "", "CGROUP_PREEXEC_DRIFT")
            child = os.fork()
            if child:
                intention["pid"] = child
                return child
            try:
                # Kernel envelope applies even to native launcher startup.
                resource.setrlimit(resource.RLIMIT_AS, (INNER_MEMORY, INNER_MEMORY))
                resource.setrlimit(resource.RLIMIT_CPU, (180 if mode == "--controls" else 1200,) * 2)
                resource.setrlimit(resource.RLIMIT_FSIZE, (2147483648,) * 2)
                resource.setrlimit(resource.RLIMIT_NOFILE, (512,) * 2)
                os.umask(0o077)
                payload = str(os.getpid()).encode()
                need(os.write(attach, payload) == len(payload), "CGROUP_ATTACH_SHORT")
                need(str(os.getpid()) in read("cgroup.procs").split(), "CGROUP_ATTACH_UNKNOWN")
                null = os.open("/dev/null", os.O_RDONLY | os.O_NOFOLLOW); os.dup2(null, 0); os.close(null)
                adapter.handoff(held, 100, out, err, group, attach)
            except BaseException:
                os._exit(125)
        def members():
            raw = read("cgroup.procs")
            need(all(re.fullmatch("[1-9][0-9]*", p) for p in raw.split()), "CGROUP_PID_FORMAT")
            return sorted(int(p) for p in raw.split())
        def rss():
            total = 0
            for p in [os.getpid()] + members():
                try:
                    with open("/proc/%d/status" % p, "rt", encoding="ascii") as stream:
                        text = stream.read(65537)
                except FileNotFoundError:
                    continue
                need(len(text) <= 65536, "PROC_RSS_CAP")
                match = re.search(r"^VmRSS:\s+(\d+) kB$", text, re.M)
                if match: total += int(match[1]) * 1024
                else: need(re.search(r"^State:\s+Z", text, re.M), "PROC_RSS_UNKNOWN")
            return total
        def postcustody():
            need(read("cgroup.events") == "populated 0\nfrozen 0" and
                 all(read(k) == v for k, v in bounds.items()), "CGROUP_FINAL_UNKNOWN")
            for role, blob in held.items():
                bounded_fd_bytes(blob.fd, blob.sha, PIPE_CAP, sealed=True, root=True, deadline=hard - 1)
        result = supervise_owned(spawn, wall=hard-time.monotonic(),
            reserve=10 if mode == "--controls" else 60, terminal_reserve=1,
            mode=mode, expected_controls=plan["control_map"], members=members, rss=rss,
            cancel=lambda: cancelled[0], postcustody=postcustody)
    finally:
        for sig, handler in previous.items():
            try: signal.signal(sig, handler)
            except BaseException:
                if result is not None:
                    result.update(state="STOP_UNCONFIRMED", terminal_completion=False,
                                  uncertainty_sticky=True, reason=result["reason"] or "OUTER_SIGNAL_RESTORE")
        for fd in (attach, group):
            try: os.close(fd)
            except OSError:
                if result is not None:
                    result.update(state="STOP_UNCONFIRMED", terminal_completion=False,
                                  uncertainty_sticky=True, reason=result["reason"] or "OUTER_CLOSE_FAILED")
    emitted = write_terminal(result, deadline=hard)
    return 0 if emitted and result["terminal_completion"] else 2


legacy_outer_main = main


def load_held_module(name, fd, sha, path):
    raw = bounded_fd_bytes(fd, sha, 1048576, sealed=True, root=True)
    module = types.ModuleType(name); module.__file__ = path
    sys.modules[name] = module
    exec(compile(raw, path, "exec"), module.__dict__)
    return module


def main():
    """The actual C bootstrap has already bounded/verified this interpreter.
    Public startup uses its single capsule pin and exact held source descriptors.
    No fixture flag, path override, native-method adapter or named source load.
    """
    need(len(sys.argv) == 2 and re.fullmatch("[0-9a-f]{64}", sys.argv[1]), "A061_PUBLIC_ARGS")
    raw = bounded_fd_bytes(100, sys.argv[1], 4096, sealed=True, root=True)
    # Fixed schema: first source pin is byte240; contract role16 is byte752.
    need(len(raw) == 848 and raw[:8] == b"FRA061C1", "A061_CAPSULE_SIZE")
    contract_sha = raw[752:784].hex()
    c = load_held_module("a061_contract", 118, contract_sha, STEM + "-CONTRACT.py")
    cap = c.startup(sys.argv[1])
    native = c.native_bridge(cap)
    x = load_held_module("a061_executor", 101, cap["pins"]["executor"], EXECUTOR)
    admitted = x.install_held_capsule()
    x.load_helpers()
    plan_raw = x.HELD_BYTES[BILL]
    plan = x.m.inert_json(plan_raw, 262144); x.compile_bill(plan)
    result, restore = None, None
    try:
        if cap["mode_id"] == 1:
            restore = c.enable_fixture_forks(cap, native)
            ctr = load_held_module("a061_controls", 102, cap["pins"]["controls"], CONTROLS)
            ctr.x, ctr.s = x, sys.modules[__name__]
            ctr.PINS.update(executor=cap["pins"]["executor"], controls=cap["pins"]["controls"], supervisor=cap["pins"]["supervisor"])
            ctr.HELD_BYTES.update(x.HELD_BYTES)
            def deny(event, args):
                if event.startswith(("socket.", "subprocess.")) or event in ("os.exec", "os.posix_spawn", "ctypes.dlopen"):
                    raise x.m.Refused("OFFLINE_NETWORK_EXEC_DENIED")
            sys.addaudithook(deny)
            result = ctr.controls(plan, plan_raw)
            c.command(cap, 12, 13)
        elif cap["mode_id"] == 2:
            result = x.preflight(plan)
            if result.get("reason") is None: c.command(cap, 12, 13)
        else:
            owned = load_held_module("a061_owner", 127, cap["pins"]["owned_consumer"], STEM + "-OWNER.py")
            run = x.adopt_held_deadline(x.make_run()); x.ACTIVE_RUN = run
            run.native_owner = owned.NativeOwner(x, cap, native)
            observed = c.production_resources(cap)
            context = x.ca_context(run)
            if cap["mode_id"] == 4:
                fixture = x.m.inert_json(c.held(119, cap["pins"]["fixture"], 1048576), 1048576)
                need(type(fixture) is dict and fixture.get("schema") == "friday.a061.benign-fixture.v1" and
                     set(fixture) == {"schema", "held_root", "inventory", "target", "responses"}, "A061_BENIGN_FIXTURE_SCHEMA")
                parent = os.path.dirname(fixture["held_root"])
                need(parent.startswith("/var/tmp/astra-e4-browser3-a061-offline-") and
                     os.path.dirname(parent) == "/var/tmp" and fixture["held_root"] == parent + "/held" and
                     fixture["target"] == parent + "/fresh-three" and len(fixture["responses"]) == 3,
                     "A061_BENIGN_OWNED_SCOPE")
                held = x.RetainedTree(fixture["held_root"], fixture["inventory"], run)
                target = fixture["target"]
            else:
                _, _, _, held = x.prepare(plan, run); target = x.ROOT
            result = x.execute_core(plan, plan_raw, target, run, held, context, observed, x.m.worker)
        if cap["mode_id"]==4:
            # A fully reaped benign negative is a completed control contour.
            # The separate driver owns its independent exact cause/byte oracle;
            # an acquisition failure is never transformed into body success.
            need(type(result) is dict and result.get("acceptance_complete") is False and
                 result.get("execution_install_root_gate_credit") is False and
                 result.get("started_routes")==3 and len(result.get("children",[]))==3 and
                 all(v.get("lifecycle")=="REAPED" for v in result["children"]) and
                 not result.get("uncertainty_sticky") and
                 result.get("charged_body_bytes",956301313)<=956301312,
                 "BENIGN_OWNED_TERMINAL")
        else:
            terminal_check(json.dumps(result,separators=(",",":")).encode()+b"\n",cap["mode"],plan["control_map"])
        need(x.ACTIVE_RUN is None or not x.ACTIVE_RUN.uncertain, "A061_UNKNOWN_INNER_RESULT")
        c.check_loaded_runtime(cap)
        c.command(cap, 9, 10)
        # Root native owner drains this exact line and validates all registration,
        # reap/pipe/exit/resource conditions before its separate finite terminal.
        return 0 if x.emit_terminal(result, x.ACTIVE_RUN) else 2
    finally:
        if restore is not None: restore()


if __name__ == "__main__":
    try: sys.exit(main())
    except SystemExit: raise
    except BaseException as exc:
        write_terminal({"state": "OUTER_SUPERVISION_FAILED", "body_complete": False,
                        "terminal_completion": False, "acceptance_complete": False,
                        "reason": str(exc) if isinstance(exc, Refused) else type(exc).__name__})
        sys.exit(2)
