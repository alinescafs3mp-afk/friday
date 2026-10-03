"""Closed stdlib unittest driver. All effects/logs are retained under its run root."""
from __future__ import annotations

import argparse
import builtins
import ctypes  # stdlib only; loaders are fenced before generated code imports
import _ctypes
import _strptime  # datetime.strptime lazy stdlib dependency, loaded before fencing
from datetime import datetime, timezone, timedelta
import hashlib
import fcntl
import importlib.util
import json
import os
import posix
import _posixsubprocess
from pathlib import Path
import resource
import signal
import socket
import stat
import sys
import subprocess
import time
import types
import unittest
from unittest import mock  # finish stdlib asyncio/ssl class definitions before fencing socket

PACKAGE = Path(__file__).resolve().parents[1]
SOURCES = ("canonical", "pinned_fs", "provenance", "archive", "manifest",
           "assemble", "ledger", "custody_linux", "broker_runtime",
           "broker_bootstrap", "install_bootstrap", "install", "recover", "uninstall")
TESTS = ("test_canonical", "test_manifest", "test_provenance", "test_archive",
         "test_broker", "test_ledger", "test_custody", "test_install_recovery",
         "test_uninstall", "test_effect_bills")
FOUNDATION = ("canonical", "pinned_fs", "provenance", "archive", "manifest", "assemble")
DEPENDENCIES = {
    "test_canonical": ("canonical",),
    "test_manifest": FOUNDATION,
    "test_provenance": ("canonical", "provenance"),
    "test_archive": FOUNDATION,
    "test_broker": FOUNDATION + ("ledger", "custody_linux", "broker_runtime", "broker_bootstrap", "install_bootstrap", "install"),
    "test_ledger": ("canonical", "ledger"),
    "test_custody": FOUNDATION + ("ledger", "custody_linux", "broker_runtime", "install_bootstrap", "install"),
    "test_install_recovery": FOUNDATION + ("ledger", "broker_runtime", "install_bootstrap", "install", "recover", "uninstall"),
    "test_uninstall": FOUNDATION + ("ledger", "broker_runtime", "install_bootstrap", "install", "recover", "uninstall"),
    "test_effect_bills": ("canonical",),
}
ATTEMPTS = []
WORKER_AS_LIMIT = 1_610_612_736
TEST_DEPENDENCIES = {"test_archive": ("test_provenance", "test_manifest"),
    "test_uninstall": ("test_install_recovery",)}
CAPABILITY_BOUNDARY = "approved-own-source/inert-adapter; arbitrary-Python/kernel/custody NOT_PROVEN"
APPROVED_IMPORTS = frozenset(SOURCES + TESTS + ("support", "install_controls",
    "__future__", "_strptime", "builtins", "bz2", "compression", "contextlib", "copy", "ctypes",
    "dataclasses", "datetime", "errno", "fcntl", "gzip", "hashlib", "hmac", "hmac", "importlib.util",
    "io", "json", "lzma", "marshal", "math", "os", "pathlib", "platform", "posixpath",
    "re", "resource", "select", "signal", "stat", "struct", "subprocess", "sys", "tarfile",
    "time", "types", "unittest", "unittest.mock", "weakref", "zipfile", "zlib"))


def install_effect_fence():
    """An audit hook alone does not cover all direct Linux/Python effects."""
    def deny(name):
        def unavailable(*args, **kwargs):
            ATTEMPTS.append({"event": "blocked-host-capability", "capability": name})
            raise PermissionError("host capability unavailable in recording controls: " + name)
        return unavailable
    names = ("fork", "forkpty", "execv", "execve", "execl", "execle", "execlp",
        "execlpe", "execvp", "execvpe", "posix_spawn", "posix_spawnp", "system",
        "setuid", "seteuid", "setreuid", "setresuid", "setgid", "setegid",
        "setregid", "setresgid", "setgroups", "unshare", "setns", "chroot",
        "chown", "fchown", "lchown", "kill", "killpg", "pidfd_open",
        "wait", "waitpid", "waitid", "symlink", "link", "mkfifo", "mknod")
    for name in names:
        if hasattr(os, name):
            blocked = deny("os/posix." + name)
            setattr(os, name, blocked)
            if hasattr(posix, name):
                setattr(posix, name, blocked)
    for module, names in ((fcntl, ("flock", "lockf", "ioctl")),
        (signal, ("signal", "siginterrupt", "pthread_kill", "pidfd_send_signal")),
        (resource, ("setrlimit", "prlimit")),
        (subprocess, ("Popen",)), (socket, ("socket", "socketpair")),
        (ctypes, ("CDLL", "PyDLL", "WinDLL", "OleDLL", "CFUNCTYPE", "PYFUNCTYPE",
                  "WINFUNCTYPE", "cast", "memmove", "memset", "string_at", "wstring_at",
                  "POINTER", "pointer", "byref", "addressof", "resize", "sizeof", "get_errno", "set_errno")),
        (_ctypes, ("dlopen", "dlsym", "call_function", "call_cdeclfunction"))):
        for name in names:
            if hasattr(module, name):
                setattr(module, name, deny(module.__name__ + "." + name))
    _posixsubprocess.fork_exec = deny("_posixsubprocess.fork_exec")
    if hasattr(subprocess, "_fork_exec"):
        subprocess._fork_exec = deny("subprocess._fork_exec")
    for loader in (ctypes.cdll, ctypes.pydll):
        loader.LoadLibrary = deny("ctypes.LibraryLoader.LoadLibrary")
        loader._dlltype = deny("ctypes.LibraryLoader._dlltype")
    class UnavailableNativeHandle:
        def __getattribute__(self, name):
            return deny("ctypes.pythonapi." + name)()
    ctypes.pythonapi = UnavailableNativeHandle()
    # Revoke cached/private factories statically. Approved source receives plain
    # Python data types; no native callable, address, loader or prototype cache.
    for name in ("_CFuncPtr", "_dlopen", "_dlsym", "_cast", "_memmove_addr", "_memset_addr",
                 "_string_at_addr", "_wstring_at_addr", "_get_errno", "_set_errno", "_reset_cache", "_PointerTypeCache"):
        if hasattr(ctypes, name):
            setattr(ctypes, name, deny("ctypes." + name))
    for name in ("_c_functype_cache", "_win_functype_cache", "_pointer_type_cache", "_pointer_type_cache_fallback"):
        cache = getattr(ctypes, name, None)
        if isinstance(cache, dict):
            cache.clear()
        if cache is not None:
            setattr(ctypes, name, types.MappingProxyType({}))
    for name in ("CFuncPtr", "_CFuncPtr", "POINTER", "pointer", "byref", "addressof",
                 "resize", "sizeof", "Structure", "Union", "Array", "_SimpleCData"):
        if hasattr(_ctypes, name):
            setattr(_ctypes, name, deny("_ctypes." + name))
    class InertScalar:
        __slots__ = ("value",)
        def __init__(self, value=0):
            if type(value) not in (int, bytes) and value is not None:
                raise TypeError("inert scalar is plain data only")
            self.value = value
    class InertStructure:
        def __init__(self, *values):
            fields = getattr(type(self), "_fields_", ())
            if len(fields) != len(values):
                raise ValueError("inert structure field count")
            for (name, kind), value in zip(fields, values):
                if kind is not InertScalar or type(value) is not int:
                    raise TypeError("inert structure plain integer fields only")
                setattr(self, name, value)
    class InertNativeModule(types.ModuleType):
        def __getattribute__(self, name):
            value = types.ModuleType.__getattribute__(self, name)
            return types.MappingProxyType(value) if name == "__dict__" else value
        def __setattr__(self, name, value):
            if types.ModuleType.__getattribute__(self, "__dict__").get("_sealed"):
                return deny(self.__name__ + ".adapter-mutation")()
            types.ModuleType.__setattr__(self, name, value)
        def __getattr__(self, name):
            return deny(self.__name__ + "." + name)()
    global INERT_CTYPES
    INERT_CTYPES = InertNativeModule("ctypes")
    INERT_CTYPES.Structure = InertStructure
    for name in ("c_int", "c_uint", "c_uint64", "c_long", "c_ulong", "c_char_p", "c_size_t"):
        setattr(INERT_CTYPES, name, InertScalar)
    INERT_CTYPES._sealed = True
    sys.modules["ctypes"] = INERT_CTYPES
    raw_adapter = InertNativeModule("_ctypes")
    raw_adapter._sealed = True
    sys.modules["_ctypes"] = raw_adapter
    native_import = builtins.__import__
    def approved_import(name, globals=None, locals=None, fromlist=(), level=0):
        caller = (globals or {}).get("__file__", "")
        owned = caller.startswith(str(PACKAGE / "src") + "/") or (
            caller.startswith(str(PACKAGE / "tests") + "/") and Path(caller).stem in TESTS + ("support", "install_controls"))
        if owned and (level or name not in APPROVED_IMPORTS):
            return deny("unapproved-import:" + name)()
        if owned and name == "ctypes":
            if any(member not in INERT_CTYPES.__dict__ for member in (fromlist or ())):
                return deny("unapproved-ctypes-member")()
            return INERT_CTYPES
        return native_import(name, globals, locals, fromlist, level)
    builtins.__import__ = approved_import


def canonical(value):
    return (json.dumps(value, allow_nan=False, ensure_ascii=True,
                       sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def _target(value, dir_fd=None):
    if isinstance(value, int):
        value = os.readlink(f"/proc/self/fd/{value}")
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    path = Path(value)
    if not path.is_absolute():
        prefix = Path(os.readlink(f"/proc/self/fd/{dir_fd}")) if dir_fd not in (None, -1) else Path.cwd()
        path = prefix / path
    # Actual fixture links/specials are forbidden and hostile nodes are modeled.
    # The initial package/run graph is checked below; all later rename targets
    # remain in the run and link creation is audited off. Lexical normalization
    # therefore retains this observation boundary without resolving ~15 parent
    # components on every one of the exhaustive journal/member events. Runtime
    # descriptor/no-follow/identity security is separately exercised in source.
    return Path(os.path.normpath(str(path)))


def install_audit(run):
    native_lstat = os.lstat  # source tests may model stat, not this host fence
    # Check the closed source/config graph, not all other retained diagnostic
    # fixtures on every worker. Dynamic open targets get their own full component
    # no-link check below; new run writes cannot manufacture special/link nodes.
    for graph in tuple(PACKAGE / name for name in ("src", "tests", "schemas", "effects", "templates")) + (run,):
        for parent, directories, files in os.walk(graph, followlinks=False):
            for name in directories + files:
                info = (Path(parent) / name).lstat()
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise ValueError("source/run graph must contain only regular files/directories")
    forbidden = {"subprocess.Popen", "os.system", "os.posix_spawn", "os.posix_spawnp",
                 "socket.__new__", "socket.connect", "socket.bind", "os.chown",
                 "os.fchown", "os.lchown", "os.symlink", "os.link", "os.mkfifo", "os.mknod"}
    writing = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    open_context = []

    def require_inside(path):
        if path != run and run not in path.parents:
            ATTEMPTS.append({"event": "write-outside-run", "path": str(path)})
            raise PermissionError("test write outside retained run root")

    def require_no_links(path):
        if not path.is_absolute():
            raise PermissionError("absolute resolved target required")
        if path == Path("/dev/null") or (path.parent in (Path("/proc/self/fdinfo"),
                Path(f"/proc/{os.getpid()}/fdinfo")) and path.name.isdecimal()):
            return  # fixed read-only metadata/null paths; write policy still denies
        for member in reversed((path, *path.parents)):
            try:
                mode = native_lstat(member).st_mode
            except FileNotFoundError:
                continue
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise PermissionError("actual special/link open target forbidden")

    def require_read(path, flags=0):
        admitted = (PACKAGE, Path("/usr/lib"), Path("/usr/local/lib"),
                    Path(f"/proc/{os.getpid()}/fdinfo"), Path("/proc/self/fdinfo"))
        pinned_ancestor = (flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW and
                           (path in PACKAGE.parents or path in run.parents))
        if not pinned_ancestor and not any(path == p or p in path.parents for p in admitted) and path != Path("/dev/null"):
            raise PermissionError("source tests read outside source/stdlib/held-fd metadata")

    # CPython's open audit event omits dir_fd. Bind it before the syscall, and
    # feed its actual resolved target to the audit consumer. Descriptor mutators
    # are not all audited by CPython; check their actual named fd target too.
    original_open = os.open
    def bounded_open(path, flags, mode=0o777, *, dir_fd=None):
        resolved = _target(path, dir_fd)
        require_no_links(resolved)
        (require_inside if flags & writing else lambda p: require_read(p, flags))(resolved)
        open_context.append(resolved)
        try:
            return original_open(path, flags, mode, dir_fd=dir_fd)
        finally:
            open_context.pop()
    os.open = bounded_open
    posix.open = bounded_open
    def descriptor_mutator(original):
        def bounded(fd, *args, **kwargs):
            require_inside(_target(fd))
            return original(fd, *args, **kwargs)
        return bounded
    for name in ("write", "writev", "pwrite", "pwritev", "ftruncate", "fchmod", "fsync", "fdatasync"):
        if hasattr(os, name):
            bounded = descriptor_mutator(getattr(os, name))
            setattr(os, name, bounded)
            if hasattr(posix, name):
                setattr(posix, name, bounded)
    original_truncate = os.truncate
    def bounded_truncate(path, length):
        require_inside(_target(path))
        return original_truncate(path, length)
    os.truncate = bounded_truncate
    posix.truncate = bounded_truncate

    def audit(event, args):
        if event in forbidden:
            ATTEMPTS.append({"event": event})
            raise PermissionError(f"host effect forbidden in official source tests: {event}")
        if event == "open":
            path, mode, flags = args
            resolved = open_context[-1] if open_context else _target(path)
            if not open_context and not isinstance(path, int):
                require_no_links(resolved)
            if flags & writing or (isinstance(mode, str) and any(x in mode for x in "wax+")):
                require_inside(resolved)
            else:
                require_read(resolved, flags)
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod"}:
            descriptor = args[-1] if isinstance(args[-1], int) else None
            require_inside(_target(args[0], descriptor))
        elif event in {"os.rename", "os.replace"}:
            require_inside(_target(args[0], args[2]))
            require_inside(_target(args[1], args[3]))
        elif event in {"ctypes.dlopen", "ctypes.dlsym", "ctypes.dlsym/handle", "ctypes.call_function"}:
            ATTEMPTS.append({"event": event})
            raise PermissionError("no live syscall capability loading in fake-root controls")
        elif event == "os.chdir":
            require_inside(_target(args[0]))

    sys.addaudithook(audit)


def exact_load(name, path):
    owner=sys.modules.get("_friday_receipt_owner")
    if owner is not None:return owner.observed_load(name,path)
    if name in sys.modules:
        raise ValueError(f"foreign module preloaded: {name}")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("unsafe source member")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.controls = []

    def startTest(self, test):
        sys.modules["support"].CURRENT_TEST_ID = test.id()
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        sys.modules["support"].CURRENT_TEST_ID = None

    def addSuccess(self, test):
        self.controls.append({"id": test.id(), "status": "PASS"})
        super().addSuccess(test)

    def addFailure(self, test, err):
        self.controls.append({"id": test.id(), "status": "FAIL"})
        super().addFailure(test, err)

    def addError(self, test, err):
        self.controls.append({"id": test.id(), "status": "ERROR"})
        super().addError(test, err)

    def addSkip(self, test, reason):
        self.controls.append({"id": test.id(), "status": "SKIP", "reason": reason})
        super().addSkip(test, reason)

    def addSubTest(self, test, subtest, err):
        self.controls.append({"id": subtest.id(), "parent_test_id": test.id(), "status": "PASS" if err is None else "FAIL"})
        super().addSubTest(test, subtest, err)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--only", nargs="+", choices=TESTS)
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--case-id", nargs="+", help="exact precollected partial case IDs; never full credit alone")
    parser.add_argument("--diagnostic-lane", choices=("sol019_foundation", "sol019_install", "sol019_runtime", "sol019_handoff", "sol020_controls", "sol020_install", "sol020_harness"))
    parser.add_argument("--assignment-deadline-utc", required=True)
    parser.add_argument("--seal-reserve-seconds", type=int, default=600)
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise PermissionError("recording test harness refuses EUID0")
    receipts = _A132_WORKER_RECEIPTS
    deadline = receipts.load_run_context(PACKAGE)
    receipts.validate_mode_admission("collect" if args.collect_only else "affected" if args.only else "full-pair")
    os.umask(0o077)
    # Four concurrent bounded workers together may not exceed the 8GiB bill.
    resource.setrlimit(resource.RLIMIT_AS, (WORKER_AS_LIMIT, WORKER_AS_LIMIT))
    resource.setrlimit(resource.RLIMIT_CPU, (300, 300))
    usage_start = list(resource.getrusage(resource.RUSAGE_SELF))
    if (args.assignment_deadline_utc != deadline["assignment_deadline_utc"] or
            args.seal_reserve_seconds != deadline["seal_reserve_seconds"]):
        raise ValueError("worker cannot replace authenticated RUN deadline/reserve")
    deadline_end = receipts.instant(deadline["assignment_deadline_utc"])
    wall_timeout = min(300, (deadline_end - datetime.now(timezone.utc)).total_seconds() - args.seal_reserve_seconds - 1)
    receipts.validate_deadline(deadline, datetime.now(timezone.utc).isoformat(), wall_timeout)
    def timed_out(signum, frame):
        raise TimeoutError("absolute assignment child deadline reached; no successful credit")
    signal.signal(signal.SIGALRM, timed_out)
    signal.setitimer(signal.ITIMER_REAL, wall_timeout)
    run = Path(args.run_root)
    if not run.is_absolute() or run.is_symlink():
        raise ValueError("run root must be explicit absolute non-symlink")
    parent = run.parent.resolve(strict=True)
    if args.diagnostic_lane and not args.only:
        raise ValueError("lane diagnostic must be an explicit subset, never official credit")
    allowed = (PACKAGE / ("fixtures/coordination/" + args.diagnostic_lane + "/diagnostic-runs")
        if args.diagnostic_lane else PACKAGE / "fixtures/official-runs").resolve(strict=True)
    if parent != allowed:
        raise ValueError("fresh run must be immediately below official-runs")
    run.mkdir(mode=0o700)
    (run / "home").mkdir(mode=0o700)
    (run / "tmp").mkdir(mode=0o700)
    os.environ["SOL017_RUN_ROOT"] = str(run)
    if args.diagnostic_lane:
        os.environ["SOL019_DIAGNOSTIC_LANE"] = args.diagnostic_lane
    os.environ["HOME"] = str(run / "home")
    os.environ["TMPDIR"] = str(run / "tmp")
    os.chdir(run)
    started = datetime.now(timezone.utc).isoformat()
    monotonic = time.monotonic()
    selected = args.only or TESTS
    required_sources = set(SOURCES)
    helpers = tuple(dict.fromkeys(dependency for name in selected
        for dependency in TEST_DEPENDENCIES.get(name, ()) if dependency not in selected))
    adapter_helpers = ("install_controls",) if any(name in tuple(selected) + helpers
        for name in ("test_install_recovery", "test_uninstall")) else ()
    # Use the same complete mandatory inventory as admission and every receipt
    # consumer. A disjoint execution subset never narrows the source snapshot.
    inventory = receipts.source_inventory(PACKAGE)
    (run / "source-inventory.json").write_bytes(canonical(inventory))
    (run / "invocation.json").write_bytes(canonical({"argv": [sys.executable, "-I", "-S", "-B"] + sys.argv,
        "environment": dict(sorted(os.environ.items())), "initial_sys_path": list(sys.path),
        "started_at_utc": started, "isolation": {"isolated": sys.flags.isolated,
        "no_site": sys.flags.no_site, "dont_write_bytecode": sys.flags.dont_write_bytecode},
        "deadline": deadline, "resources": {"run_context": receipts.run_binding(), "address_space_bytes_max": WORKER_AS_LIMIT, "cpu_seconds_max": 300,
            "wall_timeout_seconds": wall_timeout,
            "actual_rlimit_as": list(resource.getrlimit(resource.RLIMIT_AS)),
            "actual_rlimit_cpu": list(resource.getrlimit(resource.RLIMIT_CPU)),
            "usage_start": usage_start, "usage": list(resource.getrusage(resource.RUSAGE_SELF))}}))
    install_audit(run)
    install_effect_fence()
    exact_load("support", PACKAGE / "tests/support.py")
    receipts.validate_current_binding(deadline["run_context"])
    for name in SOURCES:
        if name not in required_sources:
            continue
        path = PACKAGE / "src" / (name + ".py")
        if path.exists():
            exact_load(name, path)
        else:
            raise ValueError("missing required source")
    suite = unittest.TestSuite()
    collection = {}
    module_contracts = {}
    support = sys.modules["support"]
    for name in adapter_helpers:
        exact_load(name, PACKAGE / "tests" / (name + ".py"))
    for name in helpers:
        exact_load(name, PACKAGE / "tests" / (name + ".py"))
    for name in selected:
        module = exact_load(name, PACKAGE / "tests" / (name + ".py"))
        declaration = getattr(module, "declare_control_contract", None)
        if not callable(declaration):
            raise ValueError("mandatory pre-execution control declaration missing: " + name)
        declared = declaration()
        support.declare_control_contract(name, declared)
        module_contracts[name] = declared
        group = unittest.defaultTestLoader.loadTestsFromModule(module)
        if not group.countTestCases():
            raise ValueError(f"empty mandatory test inventory: {name}")
        collection[name] = group.countTestCases()
        suite.addTests(group)
    def cases(group):
        for item in group:
            if isinstance(item, unittest.TestSuite):
                yield from cases(item)
            else:
                yield item
    all_cases = list(cases(suite))
    all_ids = sorted(item.id() for item in all_cases)
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("duplicate exact ordinary case collection")
    independent_collection = {"collection": collection, "source_hashes": inventory, "full_inventory": args.only is None,
        "selected_test_modules": list(selected), "test_ids": all_ids,
        "control_contract": support.control_contract(), "module_contracts": module_contracts, "executed_tests": 0}
    receipts.validate_collection(independent_collection, inventory, selected)
    receipts.validate_expected_union(PACKAGE, independent_collection)
    if args.case_id:
        if args.collect_only or not args.only or args.case_id != sorted(set(args.case_id)) or not set(args.case_id) <= set(all_ids):
            raise ValueError("partial cases must be unique exact precollected module IDs")
        suite = unittest.TestSuite(item for item in all_cases if item.id() in set(args.case_id))
    selected_ids = sorted(item.id() for item in cases(suite))
    if args.collect_only:
        def test_ids(group):
            for item in group:
                if isinstance(item, unittest.TestSuite):
                    yield from test_ids(item)
                else:
                    yield item.id()
        independent_collection.update(started_at_utc=started, completed_at_utc=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=time.monotonic() - monotonic, deadline=deadline,
            resources={"run_context": receipts.run_binding(), "address_space_bytes_max": WORKER_AS_LIMIT, "cpu_seconds_max": 300, "wall_timeout_seconds": wall_timeout,
                "actual_rlimit_as": list(resource.getrlimit(resource.RLIMIT_AS)),
                "actual_rlimit_cpu": list(resource.getrlimit(resource.RLIMIT_CPU)),
                "usage_start": usage_start, "usage": list(resource.getrusage(resource.RUSAGE_SELF))})
        (run / "collection.json").write_bytes(canonical(independent_collection))
        print(json.dumps({"collected": sum(collection.values()), "executed_tests": 0}, sort_keys=True))
        return 0
    with (run / "unittest.log").open("x", encoding="ascii") as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=EvidenceResult).run(suite)
    for relative, before in inventory.items():
        if hashlib.sha256((PACKAGE / relative).read_bytes()).hexdigest() != before:
            raise ValueError("source drift during controls")
    closure = support.control_closure()
    projection = {"run_context": receipts.run_binding(), "collection": collection, "controls": result.controls,
                  "observations": support.OBSERVATIONS, "matrices": support.MATRICES,
                  "source_hashes": inventory, "forbidden_effect_attempts": ATTEMPTS,
                  "control_contract": support.control_contract(), "control_closure": closure}
    projection["module_contracts"] = module_contracts
    projection["matrix_test_ids"] = dict(support.MATRIX_TEST_IDS)
    executed = {row["id"] for row in result.controls if row["status"] == "PASS"}
    if any(row["test_id"] not in executed for row in support.OBSERVATIONS) or any(
            test_id not in executed for test_id in support.MATRIX_TEST_IDS.values()):
        closure["complete"] = False
        closure["unattributed_observations_or_matrices"] = True
    raw_projection = canonical(projection)
    (run / "raw-projection.json").write_bytes(raw_projection)
    projection = receipts.canonical_run_projection(projection, run)
    (run / "semantic-projection.json").write_bytes(canonical(projection))
    completed = datetime.now(timezone.utc).isoformat()
    partial = bool(args.case_id)
    partial_valid = (not closure["unexpected_matrices"] and not closure["mismatched_matrices"]
        and not closure.get("unattributed_observations_or_matrices") and
        sorted(row["id"] for row in result.controls if row["id"] in all_ids) == selected_ids)
    success = result.wasSuccessful() and not result.skipped and not ATTEMPTS and (partial_valid if partial else closure["complete"])
    record = {"schema": "friday.a049.official-test-run.v1", "started_at_utc": started,
              "completed_at_utc": completed, "elapsed_seconds": time.monotonic() - monotonic,
              "rc": 0 if success else 1, "success": success,
              "test_count": result.testsRun, "subcontrol_count": len(result.controls),
              "failures": len(result.failures), "errors": len(result.errors), "skips": len(result.skipped),
              "semantic_sha256": hashlib.sha256(canonical(projection)).hexdigest(),
              "raw_projection_sha256": hashlib.sha256(raw_projection).hexdigest(),
              "selected_test_modules": list(selected), "full_inventory": args.only is None,
              "diagnostic_lane": args.diagnostic_lane, "official_credit": not args.diagnostic_lane and args.only is None,
              "selected_test_ids": selected_ids, "partial_case_subset": partial,
              "control_closure": closure, "host_effect_fence_installed": True,
              "source_hashes": inventory, "deadline": deadline, "capability_boundary": CAPABILITY_BOUNDARY,
              "resources": {"run_context": receipts.run_binding(), "address_space_bytes_max": WORKER_AS_LIMIT,
                  "cpu_seconds_max": 300,
                  "wall_timeout_seconds": wall_timeout,
                  "actual_rlimit_as": list(resource.getrlimit(resource.RLIMIT_AS)),
                  "actual_rlimit_cpu": list(resource.getrlimit(resource.RLIMIT_CPU)),
                  "usage_start": usage_start, "usage": list(resource.getrusage(resource.RUSAGE_SELF))}}
    (run / "run-result.json").write_bytes(canonical(record))
    print(json.dumps(record, sort_keys=True))
    return record["rc"]


if __name__ == "__main__":
    _A132_WORKER_RECEIPTS=exact_load("a049_worker_receipts",PACKAGE / "tests/receipt_contract.py")
    _wall,_mono=time.time(),time.monotonic()
    _A132_WORKER_RECEIPTS.install_root_observer("worker",(_wall + 30,_mono + 30))
    _A132_WORKER_RECEIPTS.load_run_context(PACKAGE)
    _context=_A132_WORKER_RECEIPTS.run_context()
    _end=_A132_WORKER_RECEIPTS.instant(_context["deadline_at_utc"]).timestamp()
    _A132_WORKER_RECEIPTS.raw_client().ends=(_end,_mono + _end - _wall)
    _A132_WORKER_RECEIPTS.instrument_root_module(globals(),"worker")
    _native_exit=125
    try:
        _native_exit=_A132_WORKER_RECEIPTS.observe_root_call("worker.main",main)
    finally:
        _A132_WORKER_RECEIPTS.observer_terminal(_native_exit)
    raise SystemExit(_native_exit)
