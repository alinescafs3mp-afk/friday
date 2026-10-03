import os
from pathlib import Path
import types
import hashlib
import contextlib
import io
import unittest
from unittest import mock

from support import retained_root, observe, matrix, PACKAGE, control_semantics
from canonical import ContractError, canonical_bytes, digest
import custody_linux as custody
from pinned_fs import PinnedRoot
from ledger import descriptor_identity, descriptor_mount


class Halt(BaseException):
    pass


class RecordingSyscalls:
    def __init__(self):
        self.calls = []
    def mount(self, *args, **kwargs):
        self.calls.append(("mount", args, kwargs))
    def unshare(self):
        self.calls.append(("unshare",))
    def readonly(self, path):
        self.calls.append(("readonly", path))
    def pivot(self, new, old):
        self.calls.append(("pivot", new, old))
    def detach(self, path):
        self.calls.append(("detach", path))
    def prctl(self, option, value):
        self.calls.append(("prctl", option, value))
    def publish_noreplace(self, fd, source, target):
        self.calls.append(("publish-noreplace-recording", fd, source, target))
        try:
            os.stat(target, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            os.rename(source, target, src_dir_fd=fd, dst_dir_fd=fd)
        else:
            raise FileExistsError(target)


class FakePinned:
    def __init__(self, path):
        self.path = Path(path)
    def open_beneath(self, name, *, directory=False):
        return os.open(self.path / name, os.O_RDONLY | os.O_CLOEXEC | (os.O_DIRECTORY if directory else 0))
    def check(self):
        return True
    def close_beneath(self, fd):
        os.close(fd)


NAMESPACE_RECORDED = ("namespace_unshare", "propagation_private", "snapshot_bind", "snapshot_readonly", "runtime_mounts", "proc_mount", "device_mounts", "candidate_bind", "golden_bind", "root_pivot", "old_root_detach")
DROP_RECORDED = ("namespace_guard", "groups_clear", "gid_drop", "uid_drop", "no_new_privileges", "dumpable_clear", "signals_normalize", "limits_set", "descriptor_close")
LEGACY_SOURCE_POINTS = sorted([side + ":custody:" + effect for effect in NAMESPACE_RECORDED + DROP_RECORDED for side in ("pre", "post")] + ["pre:custody:fixed_exec"])
PUBLICATION_NEGATIVES = ["collision", "mount", "named-identity", "owner-mode"]
ORIGINAL_CUSTODY_POINTS = sorted(side + ":custody:" + effect for effect in custody.CUSTODY_EFFECTS[:39] for side in ("pre", "post"))
ORIGINAL_MISSING_35 = sorted(set(ORIGINAL_CUSTODY_POINTS) - set(LEGACY_SOURCE_POINTS) - {"pre:custody:evidence_read", "post:custody:evidence_read"})
NEW_CUSTODY_POINTS = sorted(set(custody.CUSTODY_FAULTS) - set(ORIGINAL_CUSTODY_POINTS))


def recorded_json(value):
    if isinstance(value, (tuple, list)):
        return [recorded_json(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [recorded_json(item) for item in sorted(value)]
    if isinstance(value, dict):
        return {str(key): recorded_json(item) for key, item in value.items()}
    if callable(value):
        return {"callback_symbol": value.__module__ + "." + value.__qualname__}
    return value


def declare_control_contract():
    all_faults_owner = __name__ + ".CustodyTests.test_all_original_and_new_custody_faults_actual_source_methods"
    owners = {"custody-implemented-namespace-drop": __name__ + ".CustodyTests.test_namespace_and_drop_before_after_faults_actual_methods",
        "custody-evidence-read": __name__ + ".CustodyTests.test_actual_evidence_parser_and_held_descriptor_negatives",
        "custody-sealed-publication-negatives": __name__ + ".CustodyTests.test_sealed_publication_refuses_metadata_identity_mount_and_collision",
        **{name: all_faults_owner for name in ("custody-original-78", "custody-former-missing-35", "custody-new-lifecycle-10", "custody-all-source-method-faults")}}
    semantics = control_semantics(__name__, "CustodyTests", [
        ("test_actual_live_bill_capability_constraints", "PASS", ["custody-bill-negative:" + key for key in ("allowed_effects", "forbidden_effects", "scope", "required_authority", "bill_id")]),
        ("test_real_source_namespace_order_recorded_without_privileged_calls", "PASS", ["custody:actual-namespace-source-recording"]),
        ("test_namespace_and_drop_before_after_faults_actual_methods", "PASS", ["custody-fault:" + point for point in LEGACY_SOURCE_POINTS]),
        ("test_namespace_and_drop_before_after_faults_actual_methods", "INCOMPLETE", ["custody-gap:post-exec"]),
        ("test_privilege_drop_fixed_exec_records_dumpability_reset_honestly", "PASS", ["custody:drop-before-exec-is-insufficient"]),
        ("test_production_start_child_refuses_before_GO", "PASS", ["custody:production-exec-fail-closed"]),
        ("test_real_capability_and_recovered_token_refusal", "PASS", ["custody:explicit-production-capability"]),
        ("test_actual_startup_inventory_rejects_hooks_and_executable_substitution", "PASS", ["startup-tree-negative:" + key for key in ("injection.pth", "sitecustomize.py", "usercustomize.py", "__editable___finder.py", "editable.egg-link")]
            + ["startup-tree-negative:" + key + suffix for key in ("interpreter", "node", "unrar", "browser", "headless_browser", "driver") for suffix in ("-missing", "-writable")]),
        ("test_actual_evidence_parser_and_held_descriptor_negatives", "PASS", ["custody-evidence:" + key for key in ("positive", "extra-member", "startup-drift", "console-drift", "noncanonical", "parent-swap", "exit-bool", "exit-float", "artifact-bool-size", "pre:custody:evidence_read", "post:custody:evidence_read")]
            + ["custody-fault:pre:custody:evidence_read", "custody-fault:post:custody:evidence_read", "custody:evidence-exact-exit-types"]),
        ("test_all_original_and_new_custody_faults_actual_source_methods", "PASS", ["custody-fault:" + point for point in custody.CUSTODY_FAULTS]),
        ("test_source_publication_nested_directories", "PASS", ["custody:evidence-nested-directory-seal"]),
        ("test_sealed_publication_refuses_metadata_identity_mount_and_collision", "PASS", ["custody-publication-negative:" + key for key in PUBLICATION_NEGATIVES]),
        ("test_fork_and_pidfd_failures_retain_owned_cleanup", "PASS", ["custody:fork-pidfd-owned-cleanup"]),
        ("test_child_fork_prefix_failure_exits_before_host_recovery", "PASS", ["custody:child-fork-handoff-refusal"]),
        ("test_abort_has_deadline_and_preserves_unconfirmed_owner", "PASS", ["custody:abort-bounded-stop-unconfirmed"]),
        ("test_actual_guard_rejects_cancelled_identity_capability_and_parent", "PASS", ["custody:actual-supervisor-guard-negatives"]),
        ("test_late_signal_during_final_handler_restore_fails_close", "PASS", ["custody:late-signal-refusal"]),
        ("test_parent_death_race_and_unfinished_finalization_refuse", "PASS", ["custody:parent-death-race-refusal", "custody:unfinished-finalization-refusal"]),
    ])
    return {"matrices": {"custody-implemented-namespace-drop": LEGACY_SOURCE_POINTS,
        "custody-evidence-read": ["post:custody:evidence_read", "pre:custody:evidence_read"],
        "custody-sealed-publication-negatives": PUBLICATION_NEGATIVES,
        "custody-original-78": ORIGINAL_CUSTODY_POINTS,
        "custody-former-missing-35": ORIGINAL_MISSING_35,
        "custody-new-lifecycle-10": NEW_CUSTODY_POINTS,
        "custody-all-source-method-faults": sorted(custody.CUSTODY_FAULTS)},
        "required_observations": sorted(["custody-fault:" + point for point in custody.CUSTODY_FAULTS] + [
            "custody:production-exec-fail-closed", "custody:fork-pidfd-owned-cleanup", "custody:abort-bounded-stop-unconfirmed",
            "custody:child-fork-handoff-refusal",
            "custody:evidence-nested-directory-seal", "custody:evidence-exact-exit-types", "custody:late-signal-refusal",
            "custody:actual-supervisor-guard-negatives", "custody:parent-death-race-refusal", "custody:unfinished-finalization-refusal"]),
        "observation_semantics": semantics, "matrix_owners": owners}


class RecordingTree:
    """Only fresh ordinary retained files; owner/mount syscalls remain modeled."""
    instances = []
    def __init__(self, fd, **unused):
        self.fd = os.dup(fd)
        self.closed = False
        self.instances.append(self)
    def check(self):
        return True
    def handoff(self, fd, **unused):
        return RecordingTree(fd)
    def close_beneath(self, fd):
        os.close(fd)
    def open_beneath(self, path, *, directory=False):
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_DIRECTORY if directory else 0), dir_fd=self.fd)
    def mkdir_new(self, path, mode=0o700):
        os.mkdir(path, mode, dir_fd=self.fd)
    def write_new(self, path, raw, mode=0o400):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.fd)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
    def read_exact(self, path, **unused):
        fd = self.open_beneath(path)
        try:
            return os.read(fd, 2097152)
        finally:
            os.close(fd)
    def publish_noreplace(self, source, target):
        try:
            os.stat(target, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            os.rename(source, target, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            raise FileExistsError(target)
    def close(self):
        if not self.closed:
            os.close(self.fd)
            self.closed = True


class CustodyTests(unittest.TestCase):
    def test_actual_live_bill_capability_constraints(self):
        raw = (PACKAGE / "effects/live-one-attempt.v1.json").read_bytes()
        bill = custody.validate_live_effect_bill(raw, hashlib.sha256(raw).hexdigest())
        for name in ("allowed_effects", "forbidden_effects", "scope", "required_authority", "bill_id"):
            changed = {**bill, name: ["arbitrary"] if isinstance(bill[name], list) else "arbitrary"}
            with self.assertRaises(ContractError):
                custody.validate_live_effect_bill(canonical_bytes(changed), digest(changed))
            observe("custody-bill-negative", name, "PASS", actual_canonical_parser=True)

    def backend(self, root, fault=None):
        backend = object.__new__(custody.LinuxCustodyBackend)
        backend._guard = lambda: True
        backend.syscalls = RecordingSyscalls()
        backend.fault = fault
        backend.authority = {"gate_uid": 1001, "gate_gid": 1001}
        backend.grant = {"gate_argv": ["/opt/friday/quality-toolchain/venv/bin/python", "-I", "-S", "-B", "/work/candidate/tools/quality_gate.py"],
            "environment": {"LANG": "C.UTF-8"}, "capacity": {"cpus": [0]}}
        backend.held = types.SimpleNamespace(root=FakePinned(root / "stage/snapshot"), fd=0, revalidate=lambda: True)
        backend.device_fds = [0, 0]
        backend.lock_fd = 77
        backend.lock_identity = None
        backend.worker = None
        backend.worker_pidfd = None
        backend.pending_snapshot_fd = None
        backend.init_pid = None
        backend.control = backend.ready = backend.namespace_fd = backend.init_pidfd = None
        backend.gate_evidence_fd = None
        backend.gate_evidence_root = None
        backend.capture = {}
        backend.output = {"stdout": bytearray(), "stderr": bytearray()}
        backend.owned_pipe_fds = set()
        backend.old_signals = {}
        backend.cancelled = backend.closed = False
        backend.stage = backend.evidence = backend.evidence_parent = None
        backend.device_fds = []
        return backend

    def namespace_probe(self, point=None):
        root = retained_root("custody-namespace")
        for relative in ("stage", "stage/snapshot", "stage/snapshot/rootfs", "stage/snapshot/rootfs/run",
            "stage/snapshot/rootfs/run/friday-gate", "stage/snapshot/rootfs/proc", "stage/snapshot/rootfs/dev",
            "stage/snapshot/rootfs/work", "stage/snapshot/rootfs/work/candidate", "stage/snapshot/rootfs/inputs",
            "stage/snapshot/rootfs/inputs/golden", "stage/snapshot/rootfs/.oldroot", "evidence"):
            (root / relative).mkdir(mode=0o700)
        trace = []
        def fault(actual):
            trace.append(actual)
            if actual == point:
                raise Halt(actual)
        backend = self.backend(root, fault)
        fds = []
        for name in ("ready", "control", "stdout", "stderr"):
            fd = os.open(root / name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            fds.append(fd)
        try:
            with mock.patch.object(custody.os, "chown", side_effect=lambda *a, **k: backend.syscalls.calls.append(("chown-recording",))), \
                 mock.patch.object(custody.os, "fork", return_value=123), \
                 mock.patch.object(custody.os, "waitpid", return_value=(123, 0)), \
                 mock.patch.object(custody.os, "chdir", return_value=None), \
                 mock.patch.object(custody.os, "_exit", side_effect=Halt("worker-exit")):
                with self.assertRaises(Halt):
                    backend._namespace_worker(str(root / "stage"), str(root / "evidence"), *fds)
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return trace, backend.syscalls.calls

    def drop_probe(self, point=None):
        root = retained_root("custody-drop")
        trace, effects = [], []
        def fault(actual):
            trace.append(actual)
            if actual == point:
                raise Halt(actual)
        backend = self.backend(root, fault)
        def record(name):
            return lambda *args, **kwargs: effects.append((name, args))
        def exec_record(path, argv, environment):
            effects.append(("execve", path, list(argv), dict(environment)))
            # Linux ordinary exec sets dumpability to 1. It is deliberately NOT
            # treated as a protected success by this recording fixture.
            effects.append(("ordinary-exec-dumpable", 1))
            if point == "post:custody:fixed_exec":
                return None
            raise Halt("exec boundary unavailable")
        dev_fds = []
        native_open = os.open
        def open_record(path, *args, **kwargs):
            # Never open a host device in the inert source-method control.
            if path == "/dev/null":
                path = root / "inert-null"
                args = (os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            fd = native_open(path, *args, **kwargs)
            dev_fds.append(fd)
            return fd
        with mock.patch.object(custody.os, "setgroups", side_effect=record("setgroups")), \
             mock.patch.object(custody.os, "setresgid", side_effect=record("setresgid")), \
             mock.patch.object(custody.os, "setresuid", side_effect=record("setresuid")), \
             mock.patch.object(custody.os, "sched_setaffinity", side_effect=record("affinity")), \
             mock.patch.object(custody.os, "dup2", side_effect=record("dup2")), \
             mock.patch.object(custody.os, "closerange", side_effect=record("closerange")), \
             mock.patch.object(custody.os, "chdir", side_effect=record("chdir")), \
             mock.patch.object(custody.os, "open", side_effect=open_record), \
             mock.patch.object(custody.os, "execve", side_effect=exec_record), \
             mock.patch.object(custody.resource, "setrlimit", side_effect=record("rlimit")), \
             mock.patch.object(custody.signal, "pthread_sigmask", side_effect=record("sigmask")), \
             mock.patch.object(custody.signal, "signal", side_effect=record("signal")):
            try:
                with self.assertRaises(Halt):
                    backend._drop_and_exec(1, 2)
            finally:
                for fd in dev_fds:
                    os.close(fd)
        return trace, effects, backend.syscalls.calls

    def test_real_source_namespace_order_recorded_without_privileged_calls(self):
        trace, calls = self.namespace_probe()
        names = [row[0] for row in calls]
        self.assertEqual(names[:4], ["unshare", "mount", "mount", "readonly"])
        self.assertLess(names.index("pivot"), names.index("detach"))
        self.assertIn("pre:custody:old_root_detach", trace)
        observe("custody", "actual-namespace-source-recording", "PASS", trace=trace, host_privileged_calls=0,
                post_exec_boundary_complete=False)

    def test_namespace_and_drop_before_after_faults_actual_methods(self):
        namespace_trace, unused = self.namespace_probe()
        drop_trace, unused, unused = self.drop_probe()
        # Success exec has no returning 'post' hook. Its unavailable boundary is a
        # declared gap, not a synthetic post-exec security proof.
        expected = sorted(set(namespace_trace + drop_trace))
        seen = []
        for point in expected:
            with self.subTest(point=point):
                if point in namespace_trace:
                    trace, unused = self.namespace_probe(point)
                else:
                    trace, unused, unused = self.drop_probe(point)
                self.assertIn(point, trace)
                seen.append(point)
                observe("custody-fault", point, "PASS", actual_source_method=True, privileged_calls=0,
                        implementation_review_only=True)
        matrix("custody-implemented-namespace-drop", expected, seen)
        observe("custody-gap", "post-exec", "INCOMPLETE", missing="host same-UID post-exec kernel custody", dumpable_after_exec=1)

    def test_privilege_drop_fixed_exec_records_dumpability_reset_honestly(self):
        trace, effects, syscalls = self.drop_probe()
        names = [row[0] for row in effects]
        self.assertLess(names.index("setgroups"), names.index("setresgid"))
        self.assertLess(names.index("setresgid"), names.index("setresuid"))
        self.assertLess(names.index("closerange"), names.index("execve"))
        self.assertIn(("prctl", custody.PR_SET_NO_NEW_PRIVS, 1), syscalls)
        self.assertIn(("prctl", custody.PR_SET_DUMPABLE, 0), syscalls)
        self.assertIn(("ordinary-exec-dumpable", 1), effects)
        observe("custody", "drop-before-exec-is-insufficient", "PASS", ordinary_exec_dumpable=1, actual_exec_calls=0)

    def test_production_start_child_refuses_before_GO(self):
        root = retained_root("custody-start-refusal")
        backend = self.backend(root)
        backend._namespace_check = lambda: True
        backend.worker = 123
        backend.control = 12345
        with mock.patch.object(custody.os, "write") as write:
            with self.assertRaisesRegex(ContractError, "post-exec same-UID"):
                backend.start_child()
            write.assert_not_called()
        observe("custody", "production-exec-fail-closed", "PASS", GO_sent=False, gate_executed=False)

    def test_real_capability_and_recovered_token_refusal(self):
        root = retained_root("custody-capability")
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(ContractError):
                custody.LiveCapability.create(fd, authority_sha256="a" * 64, live_grant_sha256="b" * 64,
                    effect_bill_raw=b"{}\n", effect_bill_sha256="c" * 64)
        finally:
            os.close(fd)
        with self.assertRaises(ContractError):
            custody.LinuxCustodyBackend(object())
        backend = self.backend(root)
        backend.bound = False
        backend.capability = types.SimpleNamespace(authority_sha256="a" * 64, live_grant_sha256="b" * 64)
        recovered = types.SimpleNamespace(fresh=False)
        with self.assertRaises(ContractError):
            backend.bind(recovered, {}, {})
        observe("custody", "explicit-production-capability", "PASS", nonroot_rejected=True, recovered_token_rejected=True)

    def test_actual_startup_inventory_rejects_hooks_and_executable_substitution(self):
        startup = {"interpreter": "/opt/friday/quality-toolchain/venv/bin/python", "node": "/opt/friday/quality-toolchain/bin/node",
            "unrar": "/opt/friday/quality-toolchain/bin/unrar", "browser": "/opt/friday/quality-toolchain/browsers/chromium",
            "headless_browser": "/opt/friday/quality-toolchain/browsers/headless", "driver": "/opt/friday/quality-toolchain/venv/driver/node"}
        members = [{"path": "rootfs" + path, "type": "file", "uid": 0, "gid": 0, "mode": 0o555,
                    "executable_class": "executable"} for path in startup.values()]
        grant = {"startup_receipt": startup}
        custody.verify_startup_inventory({"members": members}, grant)
        for hook in ("injection.pth", "sitecustomize.py", "usercustomize.py", "__editable___finder.py", "editable.egg-link"):
            changed = members + [{"path": "rootfs/opt/friday/quality-toolchain/venv/lib/" + hook}]
            with self.assertRaises(ContractError):
                custody.verify_startup_inventory({"members": changed}, grant)
            observe("startup-tree-negative", hook, "PASS", actual_tree_members=True)
        for name in startup:
            path = "rootfs" + startup[name]
            changed = [member for member in members if member["path"] != path]
            with self.assertRaises(ContractError):
                custody.verify_startup_inventory({"members": changed}, grant)
            observe("startup-tree-negative", name + "-missing", "PASS")
            changed = [{**member, "mode": 0o777} if member["path"] == path else member for member in members]
            with self.assertRaises(ContractError):
                custody.verify_startup_inventory({"members": changed}, grant)
            observe("startup-tree-negative", name + "-writable", "PASS")

    def test_actual_evidence_parser_and_held_descriptor_negatives(self):
        cases = ("positive", "extra-member", "startup-drift", "console-drift", "noncanonical", "parent-swap", "exit-bool", "exit-float", "artifact-bool-size",
                 "pre:custody:evidence_read", "post:custody:evidence_read")
        seen = []
        for case in cases:
            with self.subTest(case=case):
                root = retained_root("custody-evidence")
                (root / "gate").mkdir(mode=0o700)
                backend = self.backend(root)
                backend.authority = {"gate_uid": os.getuid(), "gate_gid": os.getgid()}
                backend.worker = None
                backend.output = {"stdout": bytearray(), "stderr": bytearray()}
                receipt = {"synthetic": "externally-bound-receipt"}
                backend.grant = {"attempt_id": "synthetic-attempt", "evidence_allowlist": ["terminal.v1.json"],
                    "output_limit_bytes": 65536, "startup_receipt": receipt}
                terminal = {"schema": "terminal-evidence.v1", "attempt_id": "synthetic-attempt", "verdict": "PASS", "exit_code": 0,
                    "stdout_sha256": hashlib.sha256(b"").hexdigest(), "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                    "startup": receipt, "artifacts": []}
                if case == "startup-drift":
                    terminal["startup"] = {}
                elif case == "console-drift":
                    terminal["stdout_sha256"] = "f" * 64
                elif case == "exit-bool":
                    terminal["exit_code"] = False
                elif case == "exit-float":
                    terminal["exit_code"] = 0.0
                elif case == "artifact-bool-size":
                    terminal["artifacts"] = [{"path": "fake", "size": False, "sha256": "f" * 64}]
                raw = canonical_bytes(terminal)
                if case == "noncanonical":
                    raw += b" "
                node = os.open(root / "gate/terminal.v1.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(node, raw)
                os.close(node)
                if case == "extra-member":
                    node = os.open(root / "gate/extra", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    os.close(node)
                parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                backend.evidence = PinnedRoot(parent_fd, expected_uid=os.getuid(), expected_gid=os.getgid())
                os.close(parent_fd)
                gate_fd = backend.evidence.open_beneath("gate", directory=True)
                try:
                    backend.gate_evidence_root = backend.evidence.handoff(gate_fd)
                finally:
                    backend.evidence.close_beneath(gate_fd)
                backend.gate_evidence_fd = os.dup(backend.gate_evidence_root.fd)
                backend.gate_evidence_identity = descriptor_identity(backend.gate_evidence_fd)
                backend.gate_evidence_mount = descriptor_mount(backend.gate_evidence_fd)
                if case == "parent-swap":
                    os.rename(root / "gate", root / "gate.saved")
                    (root / "gate").mkdir(mode=0o700)
                trace = []
                def fault(point):
                    trace.append(point)
                    if point == case:
                        raise Halt(point)
                backend.fault = fault
                try:
                    if case == "positive":
                        self.assertEqual(backend.validate_evidence(0), terminal)
                    elif case.startswith(("pre:", "post:")):
                        with self.assertRaises(Halt):
                            backend.validate_evidence(0)
                        self.assertIn(case, trace)
                        seen.append(case)
                    else:
                        with self.assertRaises(ContractError):
                            backend.validate_evidence(0)
                    observe("custody-evidence", case, "PASS", actual_parser=True, host_privileged_calls=0)
                    if case in seen:
                        observe("custody-fault", case, "PASS", actual_source_method=True, privileged_calls=0)
                finally:
                    os.close(backend.gate_evidence_fd)
                    backend.gate_evidence_root.close()
                    backend.evidence.close()
        matrix("custody-evidence-read", ("pre:custody:evidence_read", "post:custody:evidence_read"), seen)
        observe("custody", "evidence-exact-exit-types", "PASS", boolean_zero_refused=True, float_zero_refused=True)

    def source_probe(self, effect, point=None, pidfd_error=False):
        if effect in NAMESPACE_RECORDED:
            trace, calls = self.namespace_probe(point)
            return trace, calls, "LinuxCustodyBackend._namespace_worker"
        if effect in DROP_RECORDED or (effect == "fixed_exec" and point != "post:custody:fixed_exec"):
            trace, calls, syscalls = self.drop_probe(point)
            return trace, calls + syscalls, "LinuxCustodyBackend._drop_and_exec"
        root = retained_root("custody-source-method")
        trace, calls, opened, metadata_modes, device_metadata = [], [], [], {}, {}
        for directory in ("scratch", "evidence", "snapshot", "gate"):
            (root / directory).mkdir(mode=0o700)
        for name in ("manifest.v1.json", "provenance.v1.json"):
            (root / "snapshot" / name).write_bytes(b"{}\n")
            os.chmod(root / "snapshot" / name, 0o600)
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        def fault(actual):
            trace.append(actual)
            if actual == point:
                raise Halt(actual)
        backend = self.backend(root, fault)
        backend.device_fds = []
        backend.lock_fd = None
        backend.root = RecordingTree(root_fd)
        backend.held = types.SimpleNamespace(fd=0, revalidate=lambda: True, close=lambda: None)
        backend.authority.update(snapshot_root="/snapshot", scratch_parent="/scratch", evidence_parent="/evidence",
            live_lock_path="/live.lock", snapshot_manifest_sha256="a" * 64, provenance_sha256="b" * 64,
            candidate_commit="c" * 40, candidate_tree="d" * 40, candidate_controller_sha256="e" * 64,
            broker_bundle_sha256="f" * 64, install_identity_sha256="1" * 64, install_grant_sha256="2" * 64)
        backend.grant.update(attempt_id="recording-attempt", timeout_sec=30, output_limit_bytes=65536,
            rootfs_sha256="3" * 64, golden_sha256="4" * 64)
        backend.grant["capacity"].update(memory_min_bytes=1, cpu_max="max 100000", memory_max="max")
        native_open, native_fstat, native_stat = os.open, os.fstat, os.stat
        def inert_fd(label, raw=b""):
            name = "fd-" + label + "-" + str(len(opened))
            fd = native_open(root / name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            if raw:
                os.write(fd, raw)
                os.lseek(fd, 0, os.SEEK_SET)
            opened.append(fd)
            return fd
        def modeled_info(info):
            # Model metadata by the retained inode, not an ephemeral fd number;
            # production reopens the sealed source for named/open verification.
            if effect in ("lock_acquire", "evidence_seal", "evidence_fsync", "evidence_publish", "evidence_parent_fsync"):
                values = {name: getattr(info, name) for name in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns", "st_rdev")}
                key = (info.st_dev, info.st_ino)
                if key in metadata_modes:
                    values["st_mode"] = (info.st_mode & ~0o777) | metadata_modes[key]
                values["st_uid"], values["st_gid"] = 0, backend.authority["gate_gid"] if effect != "lock_acquire" else 0
                return types.SimpleNamespace(**values)
            return info
        def modeled_fstat(fd):
            info = native_fstat(fd)
            if fd in device_metadata:
                return types.SimpleNamespace(st_mode=0o020600, st_rdev=device_metadata[fd], st_uid=0)
            return modeled_info(info)
        def safe_open(path, flags, *args, **kwargs):
            if type(path) is str and path.startswith(("/dev/", "/proc/")):
                fd = inert_fd("host-path")
                if path in ("/dev/null", "/dev/urandom"):
                    device_metadata[fd] = os.makedev(1, 3 if path == "/dev/null" else 9)
                return fd
            return native_open(path, flags, *args, **kwargs)
        method = ""
        instances_start = len(RecordingTree.instances)
        try:
            with contextlib.ExitStack() as stack:
                def patch(target, name, **options):
                    return stack.enter_context(mock.patch.object(target, name, **options))
                patch(custody, "PinnedRoot", side_effect=lambda fd, **kw: RecordingTree(fd, **kw))
                patch(custody.os, "open", side_effect=safe_open)
                patch(custody.os, "fstat", side_effect=modeled_fstat)
                patch(custody.os, "stat", side_effect=lambda *args, **kwargs: modeled_info(native_stat(*args, **kwargs)))
                patch(custody.os, "fork", return_value=123)
                patch(custody.os, "pidfd_open", side_effect=OSError("recorded pidfd failure") if pidfd_error else lambda pid, flags=0: inert_fd("pidfd"))
                patch(custody.os, "kill", side_effect=lambda *args: calls.append(("kill-recorded", args)))
                patch(custody.signal, "pidfd_send_signal", side_effect=lambda *args: calls.append(("pidfd-signal-recorded", args)))
                patch(custody.fcntl, "flock", side_effect=lambda *args: calls.append(("flock-recorded", args)))
                patch(custody.os, "fchown", side_effect=lambda *args: calls.append(("ownership-recorded", args)))
                patch(custody.os, "fchmod", side_effect=lambda fd, mode: metadata_modes.update({(native_fstat(fd).st_dev, native_fstat(fd).st_ino): mode}))
                patch(custody.signal, "signal", side_effect=lambda *args: calls.append(("signal-handler-recorded", args)))
                patch(custody.signal, "getsignal", return_value=custody.signal.SIG_DFL)
                patch(custody.os, "getppid", return_value=999)
                patch(custody.os, "waitpid", return_value=(123, 0))
                patch(custody.select, "select", return_value=([], [], []))
                if effect in ("snapshot_open", "snapshot_verify"):
                    projection = {}
                    provenance = {"creation_tool_sha256": "5" * 64, "owner_approval": {"golden_identity_sha256": backend.grant["golden_sha256"]}}
                    manifest = {"candidate": {"commit": backend.authority["candidate_commit"], "tree": backend.authority["candidate_tree"],
                        "quality_gate_sha256": backend.authority["candidate_controller_sha256"], "broker_package_sha256": backend.authority["broker_bundle_sha256"]},
                        "materials_sha256": digest(projection), "creation_tool_sha256": provenance["creation_tool_sha256"], "platform": {"rootfs_image_sha256": backend.grant["rootfs_sha256"]}}
                    patch(custody, "parse_material_provenance", return_value=provenance)
                    patch(custody, "parse_snapshot_manifest", return_value=manifest)
                    patch(custody, "material_identity_projection", return_value=projection)
                    patch(custody, "verify_startup_inventory", return_value=True)
                    patch(custody, "installation_binding", return_value=object())
                    patch(custody, "verify_snapshot", return_value=backend.held)
                    backend.root.read_exact = lambda *a, **kw: b"{}\n"
                    method, action = "LinuxCustodyBackend.verify_snapshot", backend.verify_snapshot
                elif effect in ("scratch_create", "evidence_stage_create", "supervisor_guard", "namespace_fork", "worker_pidfd"):
                    patch(custody.os, "pipe2", side_effect=lambda flags: (inert_fd("pipe-read", b"PID 456\nREADY\n"), inert_fd("pipe-write")))
                    native_read = os.read
                    patch(custody.select, "select", side_effect=lambda reads, *a: (reads, [], []))
                    patch(custody.os, "read", side_effect=lambda fd, count: b"PID 456\nREADY\n" if fd == backend.ready else native_read(fd, count))
                    method, action = "LinuxCustodyBackend.seal_namespace", lambda: backend.seal_namespace(backend.held)
                elif effect == "proc_remount_private":
                    # Execute the real namespace-init branch with inert fork/PIDs.
                    # No process or GO write is created; control read is fixture data.
                    patch(custody.os, "fork", side_effect=[0, 123])
                    patch(custody.os, "getppid", return_value=0)
                    patch(custody.os, "chown", return_value=None)
                    patch(custody.os, "chdir", return_value=None)
                    patch(custody.os, "read", return_value=b"GO")
                    patch(custody.os, "_exit", side_effect=Halt("init-exit"))
                    for relative in ("stage/snapshot/rootfs/run/friday-gate", "stage/snapshot/rootfs/proc", "stage/snapshot/rootfs/dev", "stage/snapshot/rootfs/work/candidate", "stage/snapshot/rootfs/inputs/golden", "stage/snapshot/rootfs/.oldroot"):
                        (root / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
                    backend.held = types.SimpleNamespace(root=FakePinned(root / "stage/snapshot"), fd=0, revalidate=lambda: True)
                    fds = [inert_fd(name) for name in ("ready", "control", "stdout", "stderr")]
                    method, action = "LinuxCustodyBackend._namespace_worker", lambda: backend._namespace_worker(str(root / "stage"), str(root / "evidence"), *fds)
                elif effect == "parent_death_guard":
                    method, action = "LinuxCustodyBackend._parent_death_guard", lambda: backend._parent_death_guard(999)
                elif effect == "capacity_sample":
                    backend._namespace_check = lambda: True
                    patch(custody.os, "sched_getaffinity", return_value={0})
                    records = {"/proc/meminfo": "MemAvailable: 100 kB\n", "/proc/self/cgroup": "0::/fixture\n", "/sys/fs/cgroup/fixture/cpu.max": "max 100000\n", "/sys/fs/cgroup/fixture/memory.max": "max\n"}
                    patch(custody, "open", create=True, side_effect=lambda path, *a, **kw: io.StringIO(records[path]))
                    method, action = "LinuxCustodyBackend.admit", backend.admit
                elif effect == "lock_acquire":
                    fd = native_open(root / "live.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    os.close(fd)
                    method, action = "LinuxCustodyBackend.acquire_lock", backend.acquire_lock
                elif effect in ("child_wait", "child_signal", "child_timeout", "fixed_exec"):
                    backend.worker = 123
                    if effect == "child_signal":
                        patch(custody.os, "waitpid", return_value=(123, custody.signal.SIGTERM))
                    elif effect == "child_timeout":
                        patch(custody.time, "monotonic", side_effect=[0, 100, 100, 100, 100])
                    method, action = "LinuxCustodyBackend.wait_child", lambda: backend.wait_child(123)
                elif effect == "evidence_read":
                    terminal = {"schema": "terminal-evidence.v1", "attempt_id": backend.grant["attempt_id"], "verdict": "PASS", "exit_code": 0,
                        "stdout_sha256": hashlib.sha256(b"").hexdigest(), "stderr_sha256": hashlib.sha256(b"").hexdigest(), "startup": {}, "artifacts": []}
                    backend.grant.update(startup_receipt={}, evidence_allowlist=["terminal.v1.json"])
                    (root / "gate/terminal.v1.json").write_bytes(canonical_bytes(terminal))
                    os.chmod(root / "gate/terminal.v1.json", 0o600)
                    backend.authority.update(gate_uid=os.getuid(), gate_gid=os.getgid())
                    backend.evidence = PinnedRoot(root_fd, expected_uid=os.getuid(), expected_gid=os.getgid())
                    gate_fd = backend.evidence.open_beneath("gate", directory=True)
                    try:
                        backend.gate_evidence_root = backend.evidence.handoff(gate_fd)
                    finally:
                        backend.evidence.close_beneath(gate_fd)
                    backend.gate_evidence_fd = os.dup(backend.gate_evidence_root.fd)
                    backend.gate_evidence_identity = descriptor_identity(backend.gate_evidence_fd)
                    backend.gate_evidence_mount = descriptor_mount(backend.gate_evidence_fd)
                    patch(custody, "PinnedRoot", side_effect=lambda fd, **kw: PinnedRoot(fd, expected_uid=os.getuid(), expected_gid=os.getgid()))
                    method, action = "LinuxCustodyBackend.validate_evidence", lambda: backend.validate_evidence(0)
                elif effect in ("evidence_seal", "evidence_fsync", "evidence_publish", "evidence_parent_fsync"):
                    backend.stage_name = ".recording"
                    backend.evidence_parent = RecordingTree(root_fd)
                    terminal = {"fixture": "terminal"}
                    backend.evidence_bytes = {"terminal.v1.json": canonical_bytes(terminal), "nested/deep/artifact": b"inert"}
                    method, action = "LinuxCustodyBackend.publish_evidence", lambda: backend.publish_evidence(terminal)
                elif effect in ("abort_signal", "abort_reap"):
                    backend.worker = 123
                    backend.worker_pidfd = inert_fd("pidfd")
                    method, action = "LinuxCustodyBackend.abort", backend.abort
                elif effect == "finalization_guard":
                    backend.lock_fd = inert_fd("lock")
                    method, action = "LinuxCustodyBackend.finalization_guard", backend.finalization_guard
                elif effect == "lock_release":
                    backend.lock_fd = inert_fd("lock")
                    method, action = "LinuxCustodyBackend.close", backend.close
                else:
                    raise AssertionError("unmapped mandatory source effect: " + effect)
                try:
                    action()
                except (Halt, ContractError, OSError):
                    if point is not None and point not in trace:
                        raise
                if point is not None:
                    self.assertIn(point, trace)
                calls.append(("owned-before-cleanup", backend.worker, len(backend.owned_pipe_fds)))
                backend.fault = None
                if backend.worker is not None:
                    backend.abort()
                calls.append(("owned-after-cleanup", backend.worker, len(backend.owned_pipe_fds)))
        finally:
            for obj in RecordingTree.instances[instances_start:] + [backend.root]:
                obj.close()
            for obj in (backend.gate_evidence_root, backend.evidence):
                if obj is not None:
                    obj.close()
            for fd in set(opened + list(backend.owned_pipe_fds) + list(backend.capture) + backend.device_fds + [value for value in (backend.lock_fd, backend.worker_pidfd, backend.init_pidfd, backend.namespace_fd, backend.gate_evidence_fd, backend.pending_snapshot_fd) if value is not None]):
                try:
                    os.close(fd)
                except OSError:
                    pass
            os.close(root_fd)
        return trace, calls + backend.syscalls.calls, method

    def test_all_original_and_new_custody_faults_actual_source_methods(self):
        expected = sorted(custody.CUSTODY_FAULTS)
        self.assertEqual(len(custody.CUSTODY_EFFECTS[:39]), 39)
        seen = []
        for point in expected:
            effect = point.split(":", 2)[2]
            with self.subTest(point=point):
                trace, calls, method = self.source_probe(effect, point)
                self.assertIn(point, trace)
                seen.append(point)
                observe("custody-fault", point, "PASS", actual_source_method=method, trace=trace, calls=recorded_json(calls),
                    original_78_point=effect in custody.CUSTODY_EFFECTS[:39], privileged_calls=0, kernel_proof=False,
                    post_fixed_exec_means="supervisor-observed-successful-worker-completion" if point == "post:custody:fixed_exec" else None)
        matrix("custody-all-source-method-faults", expected, seen)
        self.assertEqual(len(ORIGINAL_CUSTODY_POINTS), 78)
        self.assertEqual(len(ORIGINAL_MISSING_35), 35)
        self.assertEqual(len(NEW_CUSTODY_POINTS), 10)
        for name, rows in (("custody-original-78", ORIGINAL_CUSTODY_POINTS), ("custody-former-missing-35", ORIGINAL_MISSING_35), ("custody-new-lifecycle-10", NEW_CUSTODY_POINTS)):
            matrix(name, rows, sorted(set(rows) & set(seen)))

    def test_source_publication_nested_directories(self):
        trace, calls, method = self.source_probe("evidence_parent_fsync")
        self.assertIn("post:custody:evidence_parent_fsync", trace)
        ownership_calls = [row for row in calls if row[0] == "ownership-recorded"]
        self.assertEqual(len(ownership_calls), 5)  # two files, two ancestors, final root
        self.assertTrue(any(row[0] == "publish-noreplace-recording" for row in calls))
        observe("custody", "evidence-nested-directory-seal", "PASS", actual_source_method=method,
            ownership_transitions=len(ownership_calls), nested_directories_sealed=2, group_traversal_mode=0o750, kernel_metadata_proof=False)

    def test_sealed_publication_refuses_metadata_identity_mount_and_collision(self):
        seen = []
        for case in PUBLICATION_NEGATIVES:
            with self.subTest(case=case):
                root = retained_root("custody-publish-negative")
                (root / ".stage").mkdir(mode=0o700)
                if case == "collision":
                    (root / "attempt").mkdir(mode=0o700)
                parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                stage_fd = os.open(root / ".stage", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                backend = self.backend(root)
                backend.grant["attempt_id"] = "attempt"
                backend.evidence_parent = RecordingTree(parent_fd)
                expected = descriptor_identity(stage_fd)[:2]
                expected_mount = descriptor_mount(stage_fd)
                original_fstat, original_stat = os.fstat, os.stat
                def metadata(info, named=False):
                    values = {name: getattr(info, name) for name in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink", "st_size", "st_mtime_ns")}
                    values.update(st_mode=(info.st_mode & ~0o777) | (0o700 if case == "owner-mode" else 0o750), st_uid=0, st_gid=1001)
                    if named and case == "named-identity":
                        values["st_ino"] += 1
                    return types.SimpleNamespace(**values)
                try:
                    with mock.patch.object(custody.os, "fstat", side_effect=lambda fd: metadata(original_fstat(fd))), \
                         mock.patch.object(custody.os, "stat", side_effect=lambda *a, **k: metadata(original_stat(*a, **k), named=True)), \
                         mock.patch.object(custody, "descriptor_mount", return_value=expected_mount + (case == "mount")):
                        with self.assertRaises((ContractError, FileExistsError)):
                            backend._publish_evidence_directory(".stage", expected, expected_mount)
                    if case != "collision":
                        self.assertEqual(backend.syscalls.calls, [])
                    self.assertTrue((root / ".stage").is_dir())
                    seen.append(case)
                    observe("custody-publication-negative", case, "PASS", actual_source_method="LinuxCustodyBackend._publish_evidence_directory",
                        native_publish_called=False, modeled_metadata=True, collision_target_preserved=case == "collision")
                finally:
                    backend.evidence_parent.close()
                    os.close(stage_fd)
                    os.close(parent_fd)
        matrix("custody-sealed-publication-negatives", PUBLICATION_NEGATIVES, seen)

    def test_fork_and_pidfd_failures_retain_owned_cleanup(self):
        for point, pidfd_error in (("post:custody:namespace_fork", False), (None, True)):
            trace, calls, method = self.source_probe("namespace_fork", point, pidfd_error=pidfd_error)
            before = [row for row in calls if row[0] == "owned-before-cleanup"][0]
            after = [row for row in calls if row[0] == "owned-after-cleanup"][0]
            self.assertEqual(before[1:], (123, 8))
            self.assertEqual(after[1:], (None, 0))
            self.assertTrue(any(row[0] == "kill-recorded" for row in calls))
        observe("custody", "fork-pidfd-owned-cleanup", "PASS", actual_source_method=method,
            child_registered_before_post_hook=True, pipe_descriptors_registered_before_fork=True,
            pidfd_failure_exact_unreaped_child_fallback=True, kernel_child_proof=False)

    def test_child_fork_prefix_failure_exits_before_host_recovery(self):
        root = retained_root("custody-child-fork-prefix")
        backend = self.backend(root)
        backend._new_stage = backend._new_evidence = lambda: str(root)
        backend._protect_supervisor = lambda: None
        trace, files = [], []
        def fault(point):
            trace.append(point)
            if point == "post:custody:namespace_fork":
                raise ContractError("inert child handoff interruption")
        backend.fault = fault
        def pipe(unused_flags):
            pair = []
            for unused in range(2):
                fd = os.open(root / ("pipe-" + str(len(files))), os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
                pair.append(fd)
                files.append(fd)
            return tuple(pair)
        try:
            # Isolate PID/fork observations from the host audit's own os module.
            adapter = types.SimpleNamespace(**vars(os))
            adapter.pipe2, adapter.fork = pipe, lambda: 0
            adapter.getpid = mock.Mock(side_effect=[999, 1000])
            adapter.getppid = lambda: 999
            adapter._exit = mock.Mock(side_effect=Halt("child-exit-125"))
            with mock.patch.object(custody, "DEVICE_CONTOUR", ()), \
                 mock.patch.object(custody, "os", adapter):
                with self.assertRaises(Halt):
                    backend.seal_namespace(backend.held)
                adapter._exit.assert_called_once_with(125)
            self.assertLess(trace.index("post:custody:parent_death_guard"), trace.index("post:custody:namespace_fork"))
            self.assertIsNone(backend.worker)
            observe("custody", "child-fork-handoff-refusal", "PASS", actual_source_method="LinuxCustodyBackend.seal_namespace",
                parent_death_before_post_fork_callback=True, failed_child_exits_125=True, host_journal_recovery_entered=False, real_children=0)
        finally:
            backend.fault = None
            backend.abort()

    def test_abort_has_deadline_and_preserves_unconfirmed_owner(self):
        root = retained_root("custody-abort-deadline")
        backend = self.backend(root)
        backend.lock_fd = None
        backend.device_fds = []
        backend.worker = 123
        backend.cancelled = True
        with mock.patch.object(custody.os, "kill", return_value=None) as kill, \
                mock.patch.object(custody.os, "waitpid", return_value=(0, 0)), \
                mock.patch.object(custody.time, "monotonic", side_effect=[0, 3]), \
                mock.patch.object(custody.select, "select", return_value=([], [], [])):
            with self.assertRaisesRegex(ContractError, "STOP_UNCONFIRMED"):
                backend.abort()
            kill.assert_called_once_with(123, custody.signal.SIGKILL)
            self.assertEqual(backend.worker, 123)
        with mock.patch.object(custody.os, "kill", return_value=None), mock.patch.object(custody.os, "waitpid", return_value=(123, 0)):
            backend.abort()
        self.assertIsNone(backend.worker)
        observe("custody", "abort-bounded-stop-unconfirmed", "PASS", uses_WNOHANG=True,
            deadline_seconds=2, timeout_retains_child_owner=True, cancelled_cleanup_still_signals=True, real_children=0)

    def test_actual_guard_rejects_cancelled_identity_capability_and_parent(self):
        root = retained_root("custody-real-guard-recording")
        backend = self.backend(root)
        backend.bound = True
        backend.lock_fd = None
        backend.capability = types.SimpleNamespace(validate=lambda: True)
        backend.consumed = types.SimpleNamespace(validate=lambda: True)
        backend.root = types.SimpleNamespace(check=lambda: True)
        backend._guard = custody.LinuxCustodyBackend._guard.__get__(backend)
        self.assertTrue(backend._guard() is None)
        def refuse():
            raise ContractError("inert identity refusal")
        for obj, name in ((backend.capability, "validate"), (backend.consumed, "validate"), (backend.root, "check")):
            with mock.patch.object(obj, name, side_effect=refuse):
                with self.assertRaises(ContractError):
                    backend._guard()
        backend.cancelled = True
        with self.assertRaisesRegex(ContractError, "signal ambiguity"):
            backend._guard()
        backend.cancelled = False
        fd = os.open(root / "lock", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            backend.lock_fd, backend.lock_identity = fd, (0,) * 6
            with self.assertRaisesRegex(ContractError, "lock descriptor drift"):
                backend._guard()
        finally:
            os.close(fd)
        observe("custody", "actual-supervisor-guard-negatives", "PASS", actual_source_method="LinuxCustodyBackend._guard",
            cancelled_capability_consumed_parent_lock_refused=True, modeled_authority=True, kernel_proof=False)

    def test_late_signal_during_final_handler_restore_fails_close(self):
        root = retained_root("custody-late-signal")
        backend = self.backend(root)
        backend.root = types.SimpleNamespace(close=lambda: None)
        backend.held = types.SimpleNamespace(close=lambda: None)
        backend.device_fds = []
        backend.lock_fd = None
        backend.old_signals = {custody.signal.SIGTERM: custody.signal.SIG_DFL}
        with mock.patch.object(custody.signal, "signal", side_effect=lambda *a: setattr(backend, "cancelled", True)):
            with self.assertRaisesRegex(ContractError, "cleanup ambiguity"):
                backend.close()
        self.assertTrue(backend.closed)
        with self.assertRaisesRegex(ContractError, "late supervisor signal"):
            backend.close()
        observe("custody", "late-signal-refusal", "PASS", actual_source_method="LinuxCustodyBackend.close",
            cancellation_after_fence_checked=True, model_signal_delivery=True, kernel_proof=False)

    def test_parent_death_race_and_unfinished_finalization_refuse(self):
        root = retained_root("custody-liveness-race")
        backend = self.backend(root)
        with mock.patch.object(custody.os, "getppid", return_value=456):
            with self.assertRaisesRegex(ContractError, "parent died"):
                backend._parent_death_guard(123)
        self.assertIn(("prctl", custody.PR_SET_PDEATHSIG, custody.signal.SIGKILL), backend.syscalls.calls)
        observe("custody", "parent-death-race-refusal", "PASS", actual_source_method="LinuxCustodyBackend._parent_death_guard",
            liveness_identity_drift_refused=True, pdeath_signal_recorded_only=True, kernel_proof=False)
        for lock, worker, capture in ((None, None, {}), (77, 123, {}), (77, None, {99: "stdout"})):
            backend.lock_fd, backend.worker, backend.capture = lock, worker, capture
            with self.assertRaisesRegex(ContractError, "unfinished custody"):
                backend.finalization_guard()
        observe("custody", "unfinished-finalization-refusal", "PASS", actual_source_method="LinuxCustodyBackend.finalization_guard",
            missing_fence_owned_child_and_pending_capture_refused=True, kernel_proof=False)
