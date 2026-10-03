"""Linux root supervisor with explicit grant/bill capability; no fake fallback.

Production entry points are never called by source-package tests. A namespace/PID
worker holds the read-only snapshot; a host root supervisor keeps ledger, lock,
capture pipes and hidden evidence directory descriptors until final publication.
"""
import ctypes
import errno
import fcntl
import hashlib
import os
import platform
import resource
import select
import signal
import stat
import time
from dataclasses import dataclass

from canonical import ContractError, canonical_bytes, digest, exact_keys, parse_canonical_object, parse_effect_bill, validate_digest, validate_integer, validate_path
from ledger import descriptor_identity, descriptor_mount
from pinned_fs import PinnedRoot, identity as filesystem_identity
from manifest import parse_snapshot_manifest, verify_snapshot, installation_binding
from provenance import parse_material_provenance, material_identity_projection

CUSTODY_EFFECTS = ("snapshot_open", "snapshot_verify", "scratch_create", "evidence_stage_create", "supervisor_guard",
    "namespace_fork", "namespace_unshare", "propagation_private", "snapshot_bind", "snapshot_readonly",
    "runtime_mounts", "proc_mount", "proc_remount_private", "device_mounts", "candidate_bind", "golden_bind", "root_pivot",
    "old_root_detach", "namespace_guard", "capacity_sample", "lock_acquire", "groups_clear", "gid_drop",
    "uid_drop", "no_new_privileges", "dumpable_clear", "signals_normalize", "limits_set", "descriptor_close",
    "fixed_exec", "child_wait", "child_signal", "child_timeout", "evidence_read", "evidence_seal", "evidence_fsync",
    "evidence_publish", "evidence_parent_fsync", "lock_release", "worker_pidfd", "parent_death_guard",
    "abort_signal", "abort_reap", "finalization_guard")
CUSTODY_FAULTS = tuple(side + ":custody:" + effect for effect in CUSTODY_EFFECTS for side in ("pre", "post"))
TERMINAL_KEYS = frozenset("schema attempt_id verdict exit_code stdout_sha256 stderr_sha256 startup artifacts".split())
RUNTIME_MOUNTS = ("run/friday-gate",)
DEVICE_CONTOUR = (("null", 1, 3), ("urandom", 1, 9))
LIVE_ALLOWED_EFFECTS = ("canonical-lock", "capacity-admission", "consume-permanent-ledger", "fixed-gate", "namespace-custody", "publish-live-grant", "terminal-evidence")
LIVE_FORBIDDEN_EFFECTS = ("alternate-argv", "alternate-candidate", "alternate-environment", "arbitrary-cleanup", "ledger-deletion", "reset", "retry")
MS_BIND, MS_REC, MS_PRIVATE = 4096, 16384, 1 << 18
MS_NOSUID, MS_NODEV, MS_NOEXEC = 2, 4, 8
CLONE_NEWNS, CLONE_NEWPID = 0x00020000, 0x20000000
PR_SET_DUMPABLE, PR_SET_NO_NEW_PRIVS = 4, 38
PR_SET_PDEATHSIG = 1
AT_RECURSIVE, MOUNT_ATTR_RDONLY, MOUNT_ATTR_NOSUID, MOUNT_ATTR_NODEV = 0x8000, 1, 2, 4


def validate_live_effect_bill(raw, expected_sha256):
    bill = parse_effect_bill(raw, expected_sha256, "live-one-attempt")
    if bill["allowed_effects"] != list(LIVE_ALLOWED_EFFECTS) or bill["forbidden_effects"] != list(LIVE_FORBIDDEN_EFFECTS) or bill["scope"] != ["fixed-authority", "fixed-candidate-generation", "held-snapshot"] or bill["required_authority"] != "separate-one-terminal-live-grant":
        raise ContractError("live capability effect limits are not exact")
    return bill


def verify_startup_inventory(manifest, grant):
    """Check authenticated tree startup surfaces, beyond environment receipts."""
    members = {member["path"]: member for member in manifest["members"]}
    for path in members:
        basename = path.rsplit("/", 1)[-1]
        if path.startswith("rootfs/opt/friday/quality-toolchain/venv/") and (
                basename.endswith((".pth", ".egg-link")) or basename in ("sitecustomize.py", "usercustomize.py") or basename.startswith("__editable__")):
            raise ContractError("forbidden protected startup member: " + basename)
    startup = grant["startup_receipt"]
    for name in ("interpreter", "node", "unrar", "browser", "headless_browser", "driver"):
        path = "rootfs" + startup[name]
        member = members.get(path)
        if member is None or member["type"] not in ("file", "symlink") or member["uid"] != 0 or member["gid"] != 0 or member["mode"] & 0o7222:
            raise ContractError("protected startup executable missing/unsafe: " + name)
        if member["type"] == "file" and (member["mode"] & 0o111 != 0o111 or member["executable_class"] != "executable"):
            raise ContractError("protected startup executable classification drift: " + name)
    if startup["node"] != "/opt/friday/quality-toolchain/bin/node" or startup["unrar"] != "/opt/friday/quality-toolchain/bin/unrar":
        raise ContractError("PATH Node/UnRAR resolution drift")
    return True


class _MountAttr(ctypes.Structure):
    _fields_ = [("attr_set", ctypes.c_uint64), ("attr_clr", ctypes.c_uint64),
                ("propagation", ctypes.c_uint64), ("userns_fd", ctypes.c_uint64)]


class LinuxSyscalls:
    """No shells or programs; fixed Linux x86-64 syscall surface."""
    def __init__(self):
        if os.name != "posix" or platform.system() != "Linux" or platform.machine() != "x86_64":
            raise ContractError("reviewed Linux x86-64 backend unavailable")
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.mount.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p)
        self.libc.mount.restype = ctypes.c_int
        self.libc.unshare.argtypes = (ctypes.c_int,)
        self.libc.unshare.restype = ctypes.c_int
        self.libc.umount2.argtypes = (ctypes.c_char_p, ctypes.c_int)
        self.libc.umount2.restype = ctypes.c_int
        self.libc.prctl.restype = ctypes.c_int
        self.libc.syscall.restype = ctypes.c_long
        self.rename = getattr(self.libc, "renameat2", None)
        if self.rename is None:
            raise ContractError("atomic evidence no-replace publication unavailable")
        self.rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        self.rename.restype = ctypes.c_int

    def _check(self, result, operation):
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, operation + ": " + os.strerror(error))

    def mount(self, source, target, fs=None, flags=0, data=None):
        def encoded(value):
            return None if value is None else os.fsencode(value)
        self._check(self.libc.mount(encoded(source), encoded(target), encoded(fs), flags, encoded(data)), "mount")

    def unshare(self):
        self._check(self.libc.unshare(CLONE_NEWNS | CLONE_NEWPID), "unshare")

    def readonly(self, path):
        attributes = _MountAttr(MOUNT_ATTR_RDONLY | MOUNT_ATTR_NOSUID | MOUNT_ATTR_NODEV, 0, 0, 0)
        self._check(self.libc.syscall(ctypes.c_long(442), ctypes.c_int(-100), ctypes.c_char_p(os.fsencode(path)),
                    ctypes.c_uint(AT_RECURSIVE), ctypes.byref(attributes), ctypes.c_size_t(ctypes.sizeof(attributes))), "mount_setattr")

    def pivot(self, new_root, old_root):
        self._check(self.libc.syscall(ctypes.c_long(155), ctypes.c_char_p(os.fsencode(new_root)), ctypes.c_char_p(os.fsencode(old_root))), "pivot_root")

    def detach(self, path):
        self._check(self.libc.umount2(os.fsencode(path), 2), "umount2")

    def prctl(self, option, value):
        self._check(self.libc.prctl(ctypes.c_int(option), ctypes.c_ulong(value), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)), "prctl")

    def publish_noreplace(self, directory_fd, source, target):
        self._check(self.rename(directory_fd, os.fsencode(source), directory_fd, os.fsencode(target), 1), "renameat2 evidence")


@dataclass(frozen=True)
class LiveCapability:
    root_fd: int
    root_identity: tuple
    authority_sha256: str
    live_grant_sha256: str
    effect_bill_sha256: str
    effect_bill: dict

    @classmethod
    def create(cls, root_fd, *, authority_sha256, live_grant_sha256, effect_bill_raw, effect_bill_sha256):
        if os.geteuid() != 0 or os.getegid() != 0:
            raise ContractError("production custody requires root; no recording fallback")
        info = os.fstat(root_fd)
        slash = os.stat("/", follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (slash.st_dev, slash.st_ino) or (info.st_uid, info.st_gid) != (0, 0):
            raise ContractError("real root capability must pin /")
        for value in (authority_sha256, live_grant_sha256, effect_bill_sha256):
            validate_digest(value)
        bill = validate_live_effect_bill(effect_bill_raw, effect_bill_sha256)
        return cls(os.dup(root_fd), descriptor_identity(root_fd), authority_sha256, live_grant_sha256, effect_bill_sha256, bill)

    def validate(self):
        if os.geteuid() != 0 or descriptor_identity(self.root_fd) != self.root_identity or digest(self.effect_bill) != self.effect_bill_sha256:
            raise ContractError("root custody capability lost")

    def close(self):
        os.close(self.root_fd)


class LinuxCustodyBackend:
    """Owns only the fixed authority's one namespace worker and descriptors."""
    def __init__(self, capability, *, fault=None):
        if not isinstance(capability, LiveCapability):
            raise ContractError("explicit production capability required")
        capability.validate()
        self.capability = capability
        self.syscalls = LinuxSyscalls()
        self.fault = fault
        self.bound = False
        self.lock_fd = None
        self.worker = None
        self.worker_pidfd = None
        self.pending_snapshot_fd = None
        self.init_pid = None
        self.held = None
        self.stage = None
        self.evidence = None
        self.gate_evidence_fd = None
        self.gate_evidence_root = None
        self.control = None
        self.capture = {}
        self.output = {"stdout": bytearray(), "stderr": bytearray()}
        self.old_signals = {}
        self.cancelled = False
        self.closed = False
        self.namespace_identity = None
        self.device_fds = []
        self.owned_pipe_fds = set()
        self.prepared_finalization = False
        self.root = PinnedRoot(capability.root_fd, expected_uid=0, expected_gid=0)

    def _guard(self):
        if not self.bound:
            raise ContractError("custody cannot precede durable consumption")
        self.capability.validate()
        self.consumed.validate()
        self.root.check()
        if self.gate_evidence_root is not None:
            self.gate_evidence_root.check()
        if self.held is not None:
            self.held.root.check()
        if self.cancelled:
            raise ContractError("supervisor signal ambiguity")
        if self.lock_fd is not None and descriptor_identity(self.lock_fd) != self.lock_identity:
            raise ContractError("live lock descriptor drift")

    def _effect(self, name, action):
        self._guard()
        if name not in CUSTODY_EFFECTS:
            raise ContractError("unknown custody effect")
        if self.fault is not None:
            self.fault("pre:custody:" + name)
        result = action()
        if self.fault is not None:
            self.fault("post:custody:" + name)
        self._guard()
        return result

    def _cleanup_effect(self, name, action):
        # Cancellation or a broken evidence guard must never suppress killing an
        # exact owned child. Cleanup uses the already retained ownership record.
        if name not in ("abort_signal", "abort_reap"):
            raise ContractError("unknown cleanup effect")
        if self.fault is not None:
            self.fault("pre:custody:" + name)
        result = action()
        if self.fault is not None:
            self.fault("post:custody:" + name)
        return result

    def bind(self, consumed, authority, grant):
        if not consumed.fresh or self.bound or digest(authority) != self.capability.authority_sha256 or digest(grant) != self.capability.live_grant_sha256:
            raise ContractError("capability/authority/grant substitution")
        consumed.validate()
        if consumed.record["authority_sha256"] != self.capability.authority_sha256 or consumed.record["live_grant_sha256"] != self.capability.live_grant_sha256:
            raise ContractError("capability consumption binding drift")
        self.consumed, self.authority, self.grant = consumed, dict(authority), dict(grant)
        self.bound = True

    def verify_snapshot(self):
        path = self.authority["snapshot_root"].lstrip("/")
        def open_owned():
            self.pending_snapshot_fd = self.root.open_beneath(path, directory=True)
            return self.pending_snapshot_fd
        fd = self._effect("snapshot_open", open_owned)
        try:
            snapshot = self.root.handoff(fd, expected_uid=0, expected_gid=0)
        finally:
            self.root.close_beneath(fd)
            self.pending_snapshot_fd = None
        try:
            raw = snapshot.read_exact("manifest.v1.json", expected_sha256=self.authority["snapshot_manifest_sha256"])
            provenance_raw = snapshot.read_exact("provenance.v1.json", expected_sha256=self.authority["provenance_sha256"])
            provenance = parse_material_provenance(provenance_raw, self.authority["provenance_sha256"])
            manifest = parse_snapshot_manifest(raw, self.authority["snapshot_manifest_sha256"])
            if manifest["candidate"]["commit"] != self.authority["candidate_commit"] or manifest["candidate"]["tree"] != self.authority["candidate_tree"]:
                raise ContractError("snapshot candidate drift")
            if manifest["candidate"]["quality_gate_sha256"] != self.authority["candidate_controller_sha256"] or manifest["candidate"]["broker_package_sha256"] != self.authority["broker_bundle_sha256"]:
                raise ContractError("snapshot controller/broker drift")
            if manifest["materials_sha256"] != digest(material_identity_projection(provenance)) or manifest["creation_tool_sha256"] != provenance["creation_tool_sha256"]:
                raise ContractError("snapshot provenance/creation-tool closure drift")
            if manifest["platform"]["rootfs_image_sha256"] != self.grant["rootfs_sha256"] or provenance["owner_approval"]["golden_identity_sha256"] != self.grant["golden_sha256"]:
                raise ContractError("live grant rootfs/golden identity drift")
            verify_startup_inventory(manifest, self.grant)
            journal_raw = self.root.read_exact("var/lib/friday/quality-gate-v1/install/journal.v1.json")
            binding = installation_binding(journal_raw,
                expected_install_identity_sha256=self.authority["install_identity_sha256"],
                expected_install_grant_sha256=self.authority["install_grant_sha256"],
                expected_manifest_sha256=self.authority["snapshot_manifest_sha256"], snapshot_path=path, purpose="installed")
            def verify_owned():
                self.held = verify_snapshot(snapshot, manifest, expected_uid=0,
                    expected_gid=0, expected_provenance_sha256=self.authority["provenance_sha256"], installation_binding=binding)
                return self.held
            self._effect("snapshot_verify", verify_owned)
            return self.held
        finally:
            snapshot.close()

    def _new_stage(self):
        parent_fd = self.root.open_beneath(self.authority["scratch_parent"].lstrip("/"), directory=True)
        try:
            parent = self.root.handoff(parent_fd, expected_uid=0, expected_gid=0)
        finally:
            self.root.close_beneath(parent_fd)
        name = self.grant["attempt_id"]
        try:
            parent.mkdir_new(name, mode=0o700)
            stage_fd = parent.open_beneath(name, directory=True)
            try:
                self.stage = parent.handoff(stage_fd, expected_uid=0, expected_gid=0)
            finally:
                parent.close_beneath(stage_fd)
        finally:
            parent.close()
        self.stage.mkdir_new("snapshot", mode=0o700)
        return self.authority["scratch_parent"] + "/" + name

    def _new_evidence(self):
        parent_fd = self.root.open_beneath(self.authority["evidence_parent"].lstrip("/"), directory=True)
        try:
            self.evidence_parent = self.root.handoff(parent_fd, expected_uid=0, expected_gid=0)
        finally:
            self.root.close_beneath(parent_fd)
        self.stage_name = "." + self.grant["attempt_id"] + ".stage"
        self.evidence_parent.mkdir_new(self.stage_name, mode=0o700)
        stage_fd = self.evidence_parent.open_beneath(self.stage_name, directory=True)
        try:
            self.evidence = self.evidence_parent.handoff(stage_fd, expected_uid=0, expected_gid=0)
        finally:
            self.evidence_parent.close_beneath(stage_fd)
        self.evidence.mkdir_new("gate", mode=0o700)
        gate_fd = self.evidence.open_beneath("gate", directory=True)
        try:
            os.fchown(gate_fd, self.authority["gate_uid"], self.authority["gate_gid"])
            os.fsync(gate_fd)
            self.gate_evidence_root = self.evidence.handoff(gate_fd,
                expected_uid=self.authority["gate_uid"], expected_gid=self.authority["gate_gid"],
                expected_identity=filesystem_identity(os.fstat(gate_fd)))
            self.gate_evidence_fd = os.dup(gate_fd)
            self.gate_evidence_identity = descriptor_identity(gate_fd)
            self.gate_evidence_mount = descriptor_mount(gate_fd)
        finally:
            self.evidence.close_beneath(gate_fd)
        os.fsync(self.evidence.fd)
        return self.authority["evidence_parent"] + "/" + self.stage_name + "/gate"

    def _protect_supervisor(self):
        self.syscalls.prctl(PR_SET_DUMPABLE, 0)
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
            self.old_signals[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda unused_signum, unused_frame: setattr(self, "cancelled", True))

    def _pipe_owned(self):
        pair = os.pipe2(os.O_CLOEXEC)
        self.owned_pipe_fds.update(pair)
        return pair

    def _close_pipe(self, fd):
        if fd in self.owned_pipe_fds:
            os.close(fd)
            self.owned_pipe_fds.remove(fd)

    def _fork_owned(self, expected_parent):
        # Transfer ownership inside the action, before post-fault/guard callbacks.
        # A direct unreaped child cannot have its PID recycled under this parent.
        pid = os.fork()
        if type(pid) is not int or pid < 0:
            raise ContractError("fork returned invalid child identity")
        if pid:
            self.worker = pid
        else:
            # Establish child liveness inside the fork action, before either
            # post-fork callback or a guard can interrupt the branch dispatch.
            self._parent_death_guard(expected_parent)
        return pid

    def _parent_death_guard(self, expected_parent):
        def protect():
            self.syscalls.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
            if os.getppid() != expected_parent:
                raise ContractError("parent died during liveness handoff")
        return self._effect("parent_death_guard", protect)

    def seal_namespace(self, held):
        if held is not self.held:
            raise ContractError("snapshot descriptor substitution")
        held.revalidate()
        stage_path = self._effect("scratch_create", self._new_stage)
        evidence_path = self._effect("evidence_stage_create", self._new_evidence)
        self._effect("supervisor_guard", self._protect_supervisor)
        for name, major, minor in DEVICE_CONTOUR:
            fd = os.open("/dev/" + name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            info = os.fstat(fd)
            if not stat.S_ISCHR(info.st_mode) or info.st_rdev != os.makedev(major, minor) or info.st_uid != 0:
                os.close(fd)
                raise ContractError("minimal device capability invalid")
            self.device_fds.append(fd)
        ready_read, ready_write = self._pipe_owned()
        control_read, control_write = self._pipe_owned()
        stdout_read, stdout_write = self._pipe_owned()
        stderr_read, stderr_write = self._pipe_owned()
        # All descriptors are tracked before fork, including partial pipe failure.
        self.control, self.ready = control_write, ready_read
        self.capture = {stdout_read: "stdout", stderr_read: "stderr"}
        supervisor_pid = os.getpid()
        try:
            pid = self._effect("namespace_fork", lambda: self._fork_owned(supervisor_pid))
        except BaseException:
            if os.getpid() != supervisor_pid:
                # A failed child handoff must not fall into host runtime journal
                # recovery. Parent still owns and boundedly reaps this child.
                os._exit(125)
            raise
        if pid == 0:
            try:
                self._close_pipe(ready_read)
                self._close_pipe(control_write)
                self._close_pipe(stdout_read)
                self._close_pipe(stderr_read)
                self._namespace_worker(stage_path, evidence_path, ready_write, control_read, stdout_write, stderr_write)
            except BaseException:
                try:
                    os.write(ready_write, b"ERROR\n")
                except OSError:
                    pass
            os._exit(125)
        def own_pidfd():
            self.worker_pidfd = os.pidfd_open(pid, 0)
            return self.worker_pidfd
        self._effect("worker_pidfd", own_pidfd)
        for fd in (ready_write, control_read, stdout_write, stderr_write):
            self._close_pipe(fd)
        response = bytearray()
        deadline = time.monotonic() + min(30, self.grant["timeout_sec"])
        while b"READY\n" not in response or self.init_pid is None:
            self._guard()
            if time.monotonic() >= deadline:
                raise ContractError("namespace sealing timeout")
            if select.select([ready_read], [], [], 0.1)[0]:
                chunk = os.read(ready_read, 512)
                if not chunk or b"ERROR" in chunk or len(response) + len(chunk) > 2048:
                    raise ContractError("namespace sealing failed")
                response.extend(chunk)
                for line in response.splitlines():
                    if line.startswith(b"PID "):
                        self.init_pid = int(line[4:])
        self.namespace_fd = os.open("/proc/" + str(self.init_pid) + "/ns/mnt", os.O_RDONLY | os.O_CLOEXEC)
        self.init_pidfd = os.pidfd_open(self.init_pid, 0)
        self.namespace_identity = descriptor_identity(self.namespace_fd)
        return self.namespace_identity

    def _namespace_worker(self, stage_path, evidence_path, ready, control, stdout, stderr):
        self._effect("namespace_unshare", self.syscalls.unshare)
        self._effect("propagation_private", lambda: self.syscalls.mount(None, "/", flags=MS_REC | MS_PRIVATE))
        snapshot_path = stage_path + "/snapshot"
        self._effect("snapshot_bind", lambda: self.syscalls.mount("/proc/self/fd/" + str(self.held.fd), snapshot_path, flags=MS_BIND | MS_REC))
        self._effect("snapshot_readonly", lambda: self.syscalls.readonly(snapshot_path))
        rootfs = snapshot_path + "/rootfs"
        # All targets must be existing manifested directories; never create inside
        # or weaken the authenticated read-only rootfs.
        targets = ("run/friday-gate", "proc", "dev", "work/candidate", "inputs/golden", ".oldroot")
        for target in targets:
            fd = self.held.root.open_beneath("rootfs/" + target, directory=True)
            self.held.root.close_beneath(fd)
        self._effect("runtime_mounts", lambda: self.syscalls.mount("tmpfs", rootfs + "/run/friday-gate", "tmpfs",
                            MS_NOSUID | MS_NODEV, "mode=0700,size=1G"))
        for relative in ("home", "tmp", "xdg", "xdg/cache", "xdg/config", "xdg/data", "xdg/state", "xdg/runtime", "evidence"):
            path = rootfs + "/run/friday-gate/" + relative
            os.mkdir(path, 0o700)
            os.chown(path, self.authority["gate_uid"], self.authority["gate_gid"], follow_symlinks=False)
        os.chown(rootfs + "/run/friday-gate", self.authority["gate_uid"], self.authority["gate_gid"], follow_symlinks=False)
        self.syscalls.mount(evidence_path, rootfs + "/run/friday-gate/evidence", flags=MS_BIND)
        # Root-only bootstrap proc preserves descriptor metadata access across
        # pivot. It is replaced with the PID-private proc before any UID drop.
        self._effect("proc_mount", lambda: self.syscalls.mount("proc", rootfs + "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC))
        def devices():
            self.syscalls.mount("tmpfs", rootfs + "/dev", "tmpfs", MS_NOSUID | MS_NOEXEC, "mode=0755,size=64k")
            for (name, unused_major, unused_minor), fd in zip(DEVICE_CONTOUR, self.device_fds):
                path = rootfs + "/dev/" + name
                node = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
                os.close(node)
                self.syscalls.mount("/proc/self/fd/" + str(fd), path, flags=MS_BIND)
        self._effect("device_mounts", devices)
        self._effect("candidate_bind", lambda: self.syscalls.mount(snapshot_path + "/candidate", rootfs + "/work/candidate", flags=MS_BIND | MS_REC))
        self.syscalls.readonly(rootfs + "/work/candidate")
        self._effect("golden_bind", lambda: self.syscalls.mount(snapshot_path + "/golden", rootfs + "/inputs/golden", flags=MS_BIND | MS_REC))
        self.syscalls.readonly(rootfs + "/inputs/golden")
        # rootfs must itself be a mount for pivot_root; its bind remains readonly.
        self.syscalls.mount(rootfs, rootfs, flags=MS_BIND)
        self._effect("root_pivot", lambda: self.syscalls.pivot(rootfs, rootfs + "/.oldroot"))
        os.chdir("/")
        self._effect("old_root_detach", lambda: self.syscalls.detach("/.oldroot"))
        init = os.fork()
        if init:
            os.write(ready, b"PID " + str(init).encode("ascii") + b"\n")
            os.close(control)
            os.close(stdout)
            os.close(stderr)
            os.close(ready)
            unused_pid, status = os.waitpid(init, 0)
            os._exit(os.waitstatus_to_exitcode(status) if os.WIFEXITED(status) else 125)
        # PID namespace init ensures orphan/double-fork descendants cannot survive.
        # Its parent is outside the new PID namespace, hence getppid()==0.
        self._parent_death_guard(0)
        self._effect("proc_remount_private", lambda: self.syscalls.mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC))
        os.write(ready, b"READY\n")
        if os.read(control, 2) != b"GO":
            os._exit(125)
        gate = os.fork()
        if gate == 0:
            self._parent_death_guard(1)
            self._drop_and_exec(stdout, stderr)
            os._exit(125)
        for fd in (stdout, stderr, ready):
            os.close(fd)
        status = None
        # Continue observing the held supervisor channel after GO; EOF is fatal.
        # GO remains unconditionally unavailable in start_child.
        while status is None:
            pid, value = os.waitpid(gate, os.WNOHANG)
            if pid:
                status = value
                break
            if select.select([control], [], [], 0.05)[0] and os.read(control, 1) == b"":
                os.kill(-1, signal.SIGKILL)
                os._exit(125)
        # Kill all other processes in this PID namespace; not a host-wide signal.
        try:
            os.kill(-1, signal.SIGKILL)
        except ProcessLookupError:
            pass
        while True:
            try:
                os.waitpid(-1, 0)
            except ChildProcessError:
                break
        os._exit(os.waitstatus_to_exitcode(status) if os.WIFEXITED(status) else 125)

    def _drop_and_exec(self, stdout, stderr):
        # Child guard validates held descriptors locally before privilege changes.
        self._effect("namespace_guard", lambda: self.held.revalidate())
        self._effect("groups_clear", lambda: os.setgroups([]))
        self._effect("gid_drop", lambda: os.setresgid(self.authority["gate_gid"], self.authority["gate_gid"], self.authority["gate_gid"]))
        # _effect's root guard is inappropriate after dropping IDs; these final
        # effects use the child's fixed plan and fault hook directly.
        def final(name, action):
            if self.fault is not None:
                self.fault("pre:custody:" + name)
            result = action()
            if self.fault is not None:
                self.fault("post:custody:" + name)
            return result
        final("uid_drop", lambda: os.setresuid(self.authority["gate_uid"], self.authority["gate_uid"], self.authority["gate_uid"]))
        final("no_new_privileges", lambda: self.syscalls.prctl(PR_SET_NO_NEW_PRIVS, 1))
        final("dumpable_clear", lambda: self.syscalls.prctl(PR_SET_DUMPABLE, 0))
        def normalize():
            signal.pthread_sigmask(signal.SIG_SETMASK, [])
            for signum in signal.valid_signals():
                if signum not in (signal.SIGKILL, signal.SIGSTOP):
                    try:
                        signal.signal(signum, signal.SIG_DFL)
                    except (OSError, ValueError):
                        pass
        final("signals_normalize", normalize)
        final("limits_set", lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))
        os.sched_setaffinity(0, set(self.grant["capacity"]["cpus"]))
        os.dup2(stdout, 1)
        os.dup2(stderr, 2)
        devnull = os.open("/dev/null", os.O_RDONLY | os.O_NOFOLLOW)
        os.dup2(devnull, 0)
        def close_descriptors():
            limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
            if limit == resource.RLIM_INFINITY:
                raise ContractError("unbounded descriptor limit")
            os.closerange(3, min(limit, 2**20))
            if limit > 2**20:
                raise ContractError("descriptor closure bound exceeded")
        final("descriptor_close", close_descriptors)
        os.chdir("/work/candidate")
        if self.fault is not None:
            self.fault("pre:custody:fixed_exec")
        os.execve(self.grant["gate_argv"][0], self.grant["gate_argv"], self.grant["environment"])
        raise ContractError("fixed exec unexpectedly returned")

    def _namespace_check(self):
        self._guard()
        if self.init_pid is None or self.namespace_identity is None:
            raise ContractError("namespace worker missing")
        fd = os.open("/proc/" + str(self.init_pid) + "/ns/mnt", os.O_RDONLY | os.O_CLOEXEC)
        try:
            if descriptor_identity(fd) != self.namespace_identity or descriptor_identity(self.namespace_fd) != self.namespace_identity:
                raise ContractError("namespace identity drift")
        finally:
            os.close(fd)
        self.held.revalidate()

    def admit(self):
        self._effect("namespace_guard", self._namespace_check)
        def sample():
            expected = self.grant["capacity"]
            if sorted(os.sched_getaffinity(0)) != expected["cpus"]:
                raise ContractError("CPU affinity drift")
            memory = {}
            with open("/proc/meminfo", "r", encoding="ascii") as source:
                for line in source:
                    parts = line.split()
                    if parts and parts[0] == "MemAvailable:":
                        memory["available"] = int(parts[1]) * 1024
            if memory.get("available", 0) < expected["memory_min_bytes"]:
                raise ContractError("insufficient admitted memory")
            with open("/proc/self/cgroup", "r", encoding="ascii") as source:
                lines = source.read(4097).splitlines()
            unified = [line[3:] for line in lines if line.startswith("0::")]
            if len(unified) != 1 or ".." in unified[0].split("/"):
                raise ContractError("cgroup identity unavailable")
            cg = "/sys/fs/cgroup" + unified[0].rstrip("/")
            for name in ("cpu.max", "memory.max"):
                with open(cg + "/" + name, "r", encoding="ascii") as source:
                    value = source.read(129).strip()
                if value != expected[name.replace(".", "_")]:
                    raise ContractError("cgroup quota drift")
            return True
        return self._effect("capacity_sample", sample)

    def acquire_lock(self):
        def acquire():
            fd = self.root.open_beneath(self.authority["live_lock_path"].lstrip("/"))
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (info.st_uid, info.st_gid, info.st_nlink, stat.S_IMODE(info.st_mode)) != (0, 0, 1, 0o600):
                self.root.close_beneath(fd)
                raise ContractError("live lock identity invalid")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                self.root.close_beneath(fd)
                raise
            self.lock_fd, self.lock_identity = fd, descriptor_identity(fd)
            return True
        return self._effect("lock_acquire", acquire)

    def start_child(self):
        self._namespace_check()
        if self.lock_fd is None:
            raise ContractError("gate cannot run without held lock")
        # Ordinary execve resets dumpability to 1, exposing /proc/<pid>/root/fd
        # and potentially process memory to a hostile process of the same host UID.
        # No approved post-exec kernel/LSM custody primitive exists in A009's fixed
        # transport. Never release this worker to the unprotected exec boundary.
        raise ContractError("SOURCE_PACKAGE_INCOMPLETE: protected post-exec same-UID custody unavailable; live execution refused")

    def wait_child(self, child):
        if child != self.worker:
            raise ContractError("foreign child identity")
        deadline = time.monotonic() + self.grant["timeout_sec"]
        status = None
        while status is None or self.capture:
            self._guard()
            if time.monotonic() >= deadline:
                self._effect("child_timeout", self.abort)
                raise ContractError("gate timeout")
            if status is None:
                pid, value = os.waitpid(child, os.WNOHANG)
                if pid:
                    status = value
                    self.worker = None
            ready = select.select(list(self.capture), [], [], 0.05)[0] if self.capture else []
            for fd in ready:
                data = os.read(fd, 65536)
                if not data:
                    os.close(fd)
                    self.owned_pipe_fds.discard(fd)
                    del self.capture[fd]
                    continue
                output = self.output[self.capture[fd]]
                if len(output) + len(data) > self.grant["output_limit_bytes"]:
                    raise ContractError("bounded console output exceeded")
                output.extend(data)
        if not os.WIFEXITED(status):
            self._effect("child_signal", lambda: None)
            raise ContractError("gate signal termination")
        if os.WEXITSTATUS(status) == 0:
            # Observable supervisor completion boundary, never a returning exec.
            # This records lifecycle completion only, not post-exec kernel proof.
            self._guard()
            if self.fault is not None:
                self.fault("post:custody:fixed_exec")
            self._guard()
        return self._effect("child_wait", lambda: os.WEXITSTATUS(status))

    def validate_evidence(self, status):
        if type(status) is not int or status != 0 or self.worker is not None:
            raise ContractError("evidence before successful child completion")
        self._guard()
        # Preserve the root-held descriptor across the reviewed owner transition;
        # a root-owner walker must not reopen the gate-UID child as root-owned.
        info = os.fstat(self.gate_evidence_fd)
        actual = descriptor_identity(self.gate_evidence_fd)
        parent = os.stat("gate", dir_fd=self.evidence.fd, follow_symlinks=False)
        if actual[:5] != self.gate_evidence_identity[:5] or (parent.st_dev, parent.st_ino) != actual[:2] or descriptor_mount(self.gate_evidence_fd) != self.gate_evidence_mount:
            raise ContractError("gate evidence root identity/mount substitution")
        gate = self.gate_evidence_root
        if gate is None:
            raise ContractError("named-origin gate evidence capability missing")
        try:
            inventory = gate.walk_exact()
            # Only regular files and necessary parent directories, no link/special.
            file_names = sorted(item["path"] for item in inventory if item["type"] == "file")
            if file_names != self.grant["evidence_allowlist"] or any(item["type"] not in ("file", "directory") for item in inventory):
                raise ContractError("extra/missing/special evidence")
            raw = self._effect("evidence_read", lambda: gate.read_exact("terminal.v1.json", maximum=self.grant["output_limit_bytes"]))
            terminal = parse_canonical_object(raw, TERMINAL_KEYS, schema="terminal-evidence.v1")
            validate_integer(terminal["exit_code"], minimum=0, maximum=255)
            for name in ("stdout_sha256", "stderr_sha256"):
                validate_digest(terminal[name])
            if type(terminal["attempt_id"]) is not str or type(terminal["verdict"]) is not str or type(terminal["startup"]) is not dict:
                raise ContractError("terminal nested types invalid")
            if terminal["attempt_id"] != self.grant["attempt_id"] or terminal["verdict"] != "PASS" or terminal["exit_code"] != 0 or terminal["startup"] != self.grant["startup_receipt"]:
                raise ContractError("terminal/startup evidence mismatch")
            for name in ("stdout", "stderr"):
                if terminal[name + "_sha256"] != hashlib.sha256(self.output[name]).hexdigest():
                    raise ContractError("console evidence digest mismatch")
            if type(terminal["artifacts"]) is not list or len(terminal["artifacts"]) > 1024:
                raise ContractError("artifact evidence invalid")
            artifacts = terminal["artifacts"]
            for record in artifacts:
                exact_keys(record, ("path", "size", "sha256"))
                validate_path(record["path"])
                validate_integer(record["size"], maximum=self.grant["output_limit_bytes"])
                validate_digest(record["sha256"])
            paths = [record["path"] for record in artifacts]
            if paths != sorted(set(paths)):
                raise ContractError("artifact declarations not sorted/unique")
            if sorted(record["path"] for record in artifacts) != sorted(name for name in file_names if name != "terminal.v1.json"):
                raise ContractError("artifact inventory mismatch")
            self.evidence_bytes = {"terminal.v1.json": raw}
            for record in artifacts:
                exact_keys(record, ("path", "size", "sha256"))
                self.evidence_bytes[record["path"]] = gate.read_exact(record["path"], expected_sha256=record["sha256"], expected_size=record["size"], maximum=self.grant["output_limit_bytes"])
            gate.check()
            return terminal
        finally:
            # Backend retains the independent named-origin capability through
            # publication, terminal commit and finalization, not this parser.
            gate.check()

    def publish_evidence(self, terminal):
        self._guard()
        if canonical_bytes(terminal) != self.evidence_bytes["terminal.v1.json"]:
            raise ContractError("publication evidence substitution")
        # A root-only final stage is copied from exact captured bytes. Gate-owned
        # stage never becomes final evidence and cannot mutate published output.
        final_stage = self.stage_name + ".final"
        self.evidence_parent.mkdir_new(final_stage, mode=0o700)
        fd = self.evidence_parent.open_beneath(final_stage, directory=True)
        try:
            final = self.evidence_parent.handoff(fd, expected_uid=0, expected_gid=0)
        finally:
            self.evidence_parent.close_beneath(fd)
        directory_fds = []
        root_fd = None
        try:
            directories = set()
            for path, raw in sorted(self.evidence_bytes.items()):
                components = path.split("/")[:-1]
                built = []
                for component in components:
                    built.append(component)
                    relative = "/".join(built)
                    directories.add(relative)
                    try:
                        final.mkdir_new(relative, mode=0o700)
                    except FileExistsError:
                        pass
                # Create0400 under mandated umask077, then capability-seal exact
                # group-readable metadata; never assume umask preserves0440.
                final.write_new(path, raw, mode=0o400)
                member_fd = final.open_beneath(path)
                try:
                    def seal_member():
                        os.fchown(member_fd, 0, self.authority["gate_gid"])
                        os.fchmod(member_fd, 0o440)
                        os.fsync(member_fd)
                        sealed = os.fstat(member_fd)
                        if not stat.S_ISREG(sealed.st_mode) or (sealed.st_uid, sealed.st_gid, stat.S_IMODE(sealed.st_mode), sealed.st_nlink) != (0, self.authority["gate_gid"], 0o440, 1):
                            raise ContractError("evidence file owner/mode/link seal invalid")
                    self._effect("evidence_seal", seal_member)
                finally:
                    final.close_beneath(member_fd)
            # Capture every directory while construction ownership is unchanged.
            # PinnedRoot ancestor leases intentionally reject a group transition;
            # close those leases before sealing our exact retained descriptors.
            for path in sorted(directories, key=lambda value: (-value.count("/"), value)):
                directory_fd = final.open_beneath(path, directory=True)
                try:
                    # Narrow raw duplicates only seal these captured inodes; they
                    # are never accepted as independent ancestry capabilities.
                    seal_fd = os.dup(directory_fd)
                    directory_fds.append((path, seal_fd, descriptor_identity(directory_fd)[:2], descriptor_mount(directory_fd)))
                finally:
                    final.close_beneath(directory_fd)
            final.check()
            root_fd = os.dup(final.fd)
            root_identity, root_mount = descriptor_identity(root_fd)[:2], descriptor_mount(root_fd)
            final.close()
            for path, directory_fd, expected_identity, expected_mount in directory_fds + [("", root_fd, root_identity, root_mount)]:
                def seal_directory():
                    if descriptor_identity(directory_fd)[:2] != expected_identity or descriptor_mount(directory_fd) != expected_mount:
                        raise ContractError("held evidence directory substitution")
                    os.fchown(directory_fd, 0, self.authority["gate_gid"])
                    os.fchmod(directory_fd, 0o750)
                    os.fsync(directory_fd)
                    info = os.fstat(directory_fd)
                    if not stat.S_ISDIR(info.st_mode) or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, self.authority["gate_gid"], 0o750):
                        raise ContractError("evidence directory traversal seal invalid")
                self._effect("evidence_seal", seal_directory)
            self._effect("evidence_fsync", lambda: os.fsync(root_fd))
            self._effect("evidence_publish", lambda: self._publish_evidence_directory(final_stage, root_identity, root_mount))
            self._effect("evidence_parent_fsync", lambda: os.fsync(self.evidence_parent.fd))
        finally:
            final.close()
            for unused_path, directory_fd, unused_identity, unused_mount in directory_fds:
                os.close(directory_fd)
            if root_fd is not None:
                os.close(root_fd)
        return digest(terminal)

    def _publish_evidence_directory(self, source, expected_identity, expected_mount):
        """Publish the deliberate root:gate seal, not a root:root walker node."""
        target = self.grant["attempt_id"]
        validate_path(source)
        validate_path(target)
        if "/" in source or "/" in target:
            raise ContractError("evidence publication is one direct held-parent edge")
        self.evidence_parent.check()
        parent_fd = self.evidence_parent.fd
        before = os.stat(source, dir_fd=parent_fd, follow_symlinks=False)
        fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            def projection(info):
                return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink, info.st_size, info.st_mtime_ns)
            held = os.fstat(fd)
            if (held.st_dev, held.st_ino) != expected_identity or projection(held) != projection(before) or descriptor_mount(fd) != expected_mount:
                raise ContractError("sealed evidence named/open identity drift")
            if not stat.S_ISDIR(held.st_mode) or (held.st_uid, held.st_gid, stat.S_IMODE(held.st_mode)) != (0, self.authority["gate_gid"], 0o750):
                raise ContractError("sealed evidence owner/mode invalid")
            if projection(os.stat(source, dir_fd=parent_fd, follow_symlinks=False)) != projection(held):
                raise ContractError("sealed evidence post-open drift")
            self.evidence_parent.check()
            self.syscalls.publish_noreplace(parent_fd, source, target)
            # Rename changes ctime; it must not change the held inode, content,
            # traversal mode, owner/group, mount, or named destination identity.
            if projection(os.fstat(fd)) != projection(held) or projection(os.stat(target, dir_fd=parent_fd, follow_symlinks=False)) != projection(held) or descriptor_mount(fd) != expected_mount:
                raise ContractError("sealed evidence publication substitution")
            self.evidence_parent.check()
        finally:
            os.close(fd)

    def abort(self):
        # EOF closes every pipe, including a fork/pidfd handoff failure prefix.
        for fd in list(self.owned_pipe_fds):
            self._close_pipe(fd)
        self.capture.clear()
        self.control = self.ready = None
        # pidfds pin descendants; fallback signals only our direct unreaped child.
        for name in ("init_pidfd", "worker_pidfd"):
            pidfd = getattr(self, name, None)
            if pidfd is not None:
                try:
                    self._cleanup_effect("abort_signal", lambda pidfd=pidfd: signal.pidfd_send_signal(pidfd, signal.SIGKILL))
                except ProcessLookupError:
                    pass
        if self.worker is not None:
            if self.worker_pidfd is None:
                try:
                    self._cleanup_effect("abort_signal", lambda: os.kill(self.worker, signal.SIGKILL))
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 2.0
            while self.worker is not None:
                try:
                    def reap():
                        expected = self.worker
                        result = os.waitpid(expected, os.WNOHANG)
                        if result[0]:
                            if result[0] != expected:
                                raise ContractError("foreign waitpid result")
                            self.worker = None  # before the post-fault callback
                        return result
                    pid, unused_status = self._cleanup_effect("abort_reap", reap)
                    if pid:
                        self.worker = None
                        break
                except ChildProcessError:
                    self.worker = None
                    break
                if time.monotonic() >= deadline:
                    raise ContractError("STOP_UNCONFIRMED: owned worker did not reap before deadline")
                select.select([], [], [], 0.01)

    def finalization_guard(self):
        def validate():
            if self.closed or self.lock_fd is None or self.worker is not None or self.capture:
                raise ContractError("unfinished custody cannot commit")
            return True
        return self._effect("finalization_guard", validate)

    def prepare_finalize(self):
        self.abort()
        self.finalization_guard()
        self.prepared_finalization = True

    def close(self):
        if self.closed:
            if self.cancelled:
                raise ContractError("late supervisor signal ambiguity")
            return
        errors = []
        def cleanup(action):
            try:
                action()
            except Exception as error:
                errors.append(error)
        cleanup(self.abort)
        if self.worker is not None:
            raise ContractError("STOP_UNCONFIRMED: custody still owns live worker")
        for fd in list(self.capture):
            cleanup(lambda fd=fd: os.close(fd))
        self.capture.clear()
        for name in ("control", "ready", "namespace_fd", "init_pidfd", "worker_pidfd", "gate_evidence_fd", "pending_snapshot_fd"):
            fd = getattr(self, name, None)
            if fd is not None:
                cleanup(lambda fd=fd, name=name: self.root.close_beneath(fd) if name == "pending_snapshot_fd" else os.close(fd))
                setattr(self, name, None)
        if self.lock_fd is not None:
            # Lock extends through provisional evidence/runtime publication; final
            # commit is impossible unless custody cleanup succeeds completely.
            cleanup(lambda: self._effect("lock_release", lambda: fcntl.flock(self.lock_fd, fcntl.LOCK_UN)))
            cleanup(lambda: self.root.close_beneath(self.lock_fd))
            self.lock_fd = None
        for obj in (self.held, self.gate_evidence_root, self.stage, self.evidence, getattr(self, "evidence_parent", None), self.root):
            if obj is not None:
                cleanup(obj.close)
        for fd in self.device_fds:
            cleanup(lambda fd=fd: os.close(fd))
        self.device_fds.clear()
        for signum, handler in self.old_signals.items():
            cleanup(lambda signum=signum, handler=handler: signal.signal(signum, handler))
        self.closed = True
        if self.cancelled:
            errors.append(ContractError("late supervisor signal ambiguity"))
        if errors:
            raise ContractError("custody cleanup ambiguity: " + type(errors[0]).__name__)


def seal_namespace(backend, consumed, authority, grant, held_snapshot):
    if not isinstance(backend, LinuxCustodyBackend):
        raise ContractError("real namespace backend required")
    if not backend.bound:
        backend.bind(consumed, authority, grant)
    return backend.seal_namespace(held_snapshot)
