"""Fixed forward-only one-attempt broker. Authority is supplied by bootstrap."""
import hashlib
import os
import stat
import ctypes
import platform
import weakref
from dataclasses import dataclass

from canonical import ContractError, canonical_bytes, digest, exact_keys, parse_canonical_object, validate_digest, validate_integer, validate_path
from ledger import consume_once, descriptor_identity, descriptor_mount, validate_parent

AUTHORITY_KEYS = frozenset("schema authority_id candidate_commit candidate_tree attempt_generation package_index_sha256 broker_bundle_sha256 bootstrap_python_path bootstrap_python_sha256 snapshot_root snapshot_manifest_sha256 provenance_sha256 candidate_controller_sha256 gate_argv_sha256 environment_sha256 caller_uid gate_uid gate_gid sudoers_path sudoers_sha256 ledger_directory runtime_journal_path live_lock_path scratch_parent evidence_parent install_grant_sha256 install_identity_sha256".split())
LIVE_KEYS = frozenset("schema authority_id authority_sha256 attempt_id candidate_commit attempt_generation snapshot_manifest_sha256 rootfs_sha256 golden_sha256 gate_argv environment capacity timeout_sec output_limit_bytes evidence_allowlist startup_receipt one_terminal_attempt".split())
RUNTIME_PHASES = ("consumed", "snapshot_verifying", "snapshot_held", "namespace_sealing", "namespace_sealed",
                  "live_admitting", "lock_held", "running", "collecting", "publishing", "terminal_pass", "terminal_fail")
RUNTIME_EFFECTS = ("snapshot_verify", "namespace_seal", "live_admit", "lock_acquire", "child_exec",
                   "child_exit", "evidence_validate", "evidence_publish", "terminal_publish", "custody_close", "terminal_commit",
                   "finalization_prepare", "acceptance_readonly", "finalization_guard")
RUNTIME_FAULTS = tuple(side + ":runtime:" + effect for effect in RUNTIME_EFFECTS for side in ("pre", "post")) + tuple(
    point + ":" + phase for phase in RUNTIME_PHASES for point in ("pre:journal_stage", "post:journal_stage", "post:phase"))
JOURNAL_KEYS = frozenset("schema attempt_id consumed_sha256 identity_sha256 generation phase previous_journal_sha256 journal_sha256 outcome evidence_sha256 records".split())
FIXED_ENVIRONMENT = {
    "PATH": "/opt/friday/quality-toolchain/bin:/usr/bin:/bin", "HOME": "/run/friday-gate/home",
    "TMPDIR": "/run/friday-gate/tmp", "XDG_CACHE_HOME": "/run/friday-gate/xdg/cache",
    "XDG_CONFIG_HOME": "/run/friday-gate/xdg/config", "XDG_DATA_HOME": "/run/friday-gate/xdg/data",
    "XDG_STATE_HOME": "/run/friday-gate/xdg/state", "XDG_RUNTIME_DIR": "/run/friday-gate/xdg/runtime",
    "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
    "PLAYWRIGHT_BROWSERS_PATH": "/opt/friday/quality-toolchain/browsers", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
    "GIT_OPTIONAL_LOCKS": "0", "GOLDEN_JOURNEY_RELEASE_ROOT": "/inputs/golden"}
FIXED_ARGV_PREFIX = ("/opt/friday/quality-toolchain/venv/bin/python", "-I", "-S", "-B", "/work/candidate/tools/quality_gate.py")
FORBIDDEN_ENVIRONMENT = ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "PYTHONSTARTUP", "PYTEST_ADDOPTS", "PYTEST_PLUGINS",
    "COVERAGE_PROCESS_START", "PIP_CONFIG_FILE", "PIP_INDEX_URL", "UV_INDEX_URL", "NODE_OPTIONS", "NODE_PATH",
    "NPM_CONFIG_USERCONFIG", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LOCPATH", "HTTP_PROXY", "HTTPS_PROXY",
    "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "VIRTUAL_ENV")


def _git(value):
    if not isinstance(value, str) or len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
        raise ContractError("invalid candidate Git identity")


def _id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in value):
        raise ContractError("invalid fixed authority ID")


def parse_installed_authority(raw, expected_sha256):
    validate_digest(expected_sha256)
    authority = parse_canonical_object(raw, AUTHORITY_KEYS, schema="installed-authority.v1", expected_sha256=expected_sha256)
    _id(authority["authority_id"])
    for name in ("candidate_commit", "candidate_tree"):
        _git(authority[name])
    for name, value in authority.items():
        if name.endswith("_sha256"):
            validate_digest(value)
    validate_integer(authority["attempt_generation"], minimum=1)
    for name in ("caller_uid", "gate_uid", "gate_gid"):
        validate_integer(authority[name], minimum=1, maximum=2**31 - 1)
    package = authority["package_index_sha256"]
    snapshot = authority["snapshot_manifest_sha256"]
    candidate = authority["candidate_commit"]
    generation = str(authority["attempt_generation"])
    fixed = {
        "bootstrap_python_path": "/usr/bin/python3.14",
        "snapshot_root": "/usr/libexec/friday/quality-gate-toolchain-v1/" + snapshot,
        "sudoers_path": "/etc/sudoers.d/friday-quality-gate-" + authority["authority_id"],
        "ledger_directory": "/var/lib/friday/quality-gate-v1/attempts/" + candidate + "/" + generation,
        "live_lock_path": "/var/lib/friday/quality-gate-v1/live.lock",
        "scratch_parent": "/var/lib/friday/quality-gate-v1/scratch",
        "evidence_parent": "/var/lib/friday/quality-gate-v1/evidence"}
    fixed["runtime_journal_path"] = fixed["ledger_directory"] + "/runtime.v1.json"
    for name, expected in fixed.items():
        validate_path(authority[name], absolute=True)
        if authority[name] != expected:
            raise ContractError("authority path is not fixed: " + name)
    return authority


def parse_live_grant(raw, expected_sha256, authority, authority_sha256):
    validate_digest(expected_sha256)
    validate_digest(authority_sha256)
    if digest(authority) != authority_sha256:
        raise ContractError("external authority binding required")
    grant = parse_canonical_object(raw, LIVE_KEYS, schema="live-grant.v1", expected_sha256=expected_sha256)
    expected = {"authority_id": authority["authority_id"], "authority_sha256": authority_sha256,
                "candidate_commit": authority["candidate_commit"], "attempt_generation": authority["attempt_generation"],
                "snapshot_manifest_sha256": authority["snapshot_manifest_sha256"],
                "attempt_id": authority["candidate_commit"] + "-" + str(authority["attempt_generation"])}
    for key, value in expected.items():
        if grant[key] != value:
            raise ContractError("live grant binding drift: " + key)
    validate_integer(grant["attempt_generation"], minimum=1)
    for name in ("authority_id", "attempt_id", "candidate_commit"):
        if type(grant[name]) is not str:
            raise ContractError("live grant binding type invalid")
    if grant["one_terminal_attempt"] is not True:
        raise ContractError("one-terminal-attempt grant required")
    for name in ("rootfs_sha256", "golden_sha256"):
        validate_digest(grant[name])
    argv = grant["gate_argv"]
    if not isinstance(argv, list) or tuple(argv[:5]) != FIXED_ARGV_PREFIX or len(argv) > 64:
        raise ContractError("fixed gate transport required")
    if any(not isinstance(value, str) or len(value) > 4096 or "\x00" in value for value in argv):
        raise ContractError("invalid gate argument")
    if digest(argv) != authority["gate_argv_sha256"]:
        raise ContractError("external gate argv digest mismatch")
    environment = grant["environment"]
    if not isinstance(environment, dict) or set(environment) not in (set(FIXED_ENVIRONMENT), set(FIXED_ENVIRONMENT) | {"SOURCE_DATE_EPOCH"}):
        raise ContractError("environment key set is not closed")
    for key, value in FIXED_ENVIRONMENT.items():
        if environment[key] != value:
            raise ContractError("fixed environment drift: " + key)
    if "SOURCE_DATE_EPOCH" in environment:
        epoch = environment["SOURCE_DATE_EPOCH"]
        if not isinstance(epoch, str) or not epoch.isascii() or not epoch.isdecimal() or str(int(epoch)) != epoch:
            raise ContractError("invalid SOURCE_DATE_EPOCH")
    if digest(environment) != authority["environment_sha256"]:
        raise ContractError("external environment digest mismatch")
    exact_keys(grant["capacity"], ("cpus", "memory_min_bytes", "worker_count", "cpu_max", "memory_max"))
    cpus = grant["capacity"]["cpus"]
    if not isinstance(cpus, list) or not cpus or cpus != sorted(set(cpus)):
        raise ContractError("invalid admitted CPU topology")
    for cpu in cpus:
        validate_integer(cpu, maximum=65535)
    validate_integer(grant["capacity"]["memory_min_bytes"], minimum=1)
    validate_integer(grant["capacity"]["worker_count"], minimum=1, maximum=len(cpus))
    for name in ("cpu_max", "memory_max"):
        value = grant["capacity"][name]
        if not isinstance(value, str) or not value or len(value) > 128 or "\n" in value:
            raise ContractError("invalid admitted cgroup quota")
    validate_integer(grant["timeout_sec"], minimum=1, maximum=86400)
    validate_integer(grant["output_limit_bytes"], minimum=1, maximum=64 * 1024 * 1024)
    names = grant["evidence_allowlist"]
    if not isinstance(names, list) or names != sorted(set(names)) or "terminal.v1.json" not in names or len(names) > 1024:
        raise ContractError("evidence allowlist not exact")
    for name in names:
        validate_path(name)
    exact_keys(grant["startup_receipt"], ("interpreter", "prefix", "node", "unrar", "browser", "headless_browser", "driver", "descendants", "forbidden_startup_absent"))
    receipt = grant["startup_receipt"]
    if receipt["interpreter"] != FIXED_ARGV_PREFIX[0] or receipt["prefix"] != "/opt/friday/quality-toolchain/venv" or receipt["forbidden_startup_absent"] is not True:
        raise ContractError("startup receipt is not protected")
    exact_keys(receipt["descendants"], ("build_backend", "wheel_verifier", "pip", "pytest", "xdist", "execnet"))
    if any(value != FIXED_ARGV_PREFIX[0] for value in receipt["descendants"].values()):
        raise ContractError("descendant interpreter escape")
    for name in ("node", "unrar", "browser", "headless_browser", "driver"):
        validate_path(receipt[name], absolute=True)
        if not receipt[name].startswith("/opt/friday/quality-toolchain/"):
            raise ContractError("startup executable outside protected tree")
    return grant


_SUPERVISOR_ISSUER = object()
_ISSUED_SUPERVISOR_RESULTS = {}


def _result_identity(result):
    return (result.attempt_id, result.consumed_sha256, result.runtime_sha256,
        result.evidence_sha256, result.commit_sha256, result.finalization_complete)


@dataclass(frozen=True)
class SupervisorResult:
    """Synchronous authenticated-code result, never a self-authenticating JSON.

    Serialized bytes require an independently authenticated invocation transport,
    its successful final process status and complete EOF. A retained directory or
    arbitrary callback cannot manufacture this in-process capability.
    """
    attempt_id: str
    consumed_sha256: str
    runtime_sha256: str
    evidence_sha256: str
    commit_sha256: str
    finalization_complete: bool
    issuer: object

    def projection(self):
        issued = _ISSUED_SUPERVISOR_RESULTS.get(id(self))
        if self.issuer is not _SUPERVISOR_ISSUER or self.finalization_complete is not True or issued is None or issued[0]() is not self or issued[1] != _result_identity(self):
            raise ContractError("unfinished or foreign supervisor result")
        return {"schema": "attempt-supervisor-result.v1", "attempt_id": self.attempt_id,
            "consumed_sha256": self.consumed_sha256, "runtime_sha256": self.runtime_sha256,
            "evidence_sha256": self.evidence_sha256, "commit_sha256": self.commit_sha256,
            "finalization_complete": True, "outcome": "PASS"}


class LinuxJournalPublisher:
    """Actual no-replace renameat2; recording tests inject their own publisher."""
    def publish(self, directory_fd, source, target):
        if os.geteuid() != 0 or platform.system() != "Linux" or platform.machine() != "x86_64":
            raise ContractError("native runtime journal publisher requires reviewed root Linux")
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        result = libc.syscall(ctypes.c_long(316), ctypes.c_int(directory_fd), ctypes.c_char_p(source.encode("ascii")),
                             ctypes.c_int(directory_fd), ctypes.c_char_p(target.encode("ascii")), ctypes.c_uint(1))
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))


class RuntimeJournal:
    """Hash-chained successor journal; pending/torn stages are never executed."""
    def __init__(self, directory_fd, consumed, *, owner_uid=0, owner_gid=0, fault=None, publisher=None):
        self.fd = os.dup(directory_fd)
        self.consumed = consumed
        self.uid, self.gid = owner_uid, owner_gid
        self.parent, self.mount = validate_parent(self.fd, owner_uid, owner_gid)
        self.fault = fault
        self.publisher = LinuxJournalPublisher() if publisher is None else publisher
        self.current = None
        self.current_identity = None
        self.identity_sha256 = digest(consumed.record)

    def close(self):
        os.close(self.fd)

    def _check(self):
        self.consumed.validate()
        if validate_parent(self.fd, self.uid, self.gid) != (self.parent, self.mount):
            raise ContractError("runtime journal parent substitution")

    def _point(self, name):
        if self.fault is not None:
            self.fault(name)

    def _read(self, name):
        before = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ContractError("runtime journal node type/link invalid")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=self.fd)
        try:
            identity = descriptor_identity(fd)
            if (before.st_dev, before.st_ino) != identity[:2] or not stat.S_ISREG(identity[2]) or identity[3:] != (self.uid, self.gid, 1) or stat.S_IMODE(identity[2]) != 0o400 or descriptor_mount(fd) != self.mount:
                raise ContractError("runtime journal node invalid")
            raw = os.read(fd, 65537)
            if descriptor_identity(fd) != identity:
                raise ContractError("runtime journal read drift")
            after = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if (after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_gid, after.st_nlink) != identity or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or after.st_size != len(raw):
                raise ContractError("runtime journal substitution")
        finally:
            os.close(fd)
        record = parse_canonical_object(raw, JOURNAL_KEYS, schema="attempt-runtime.v1")
        projection = {key: value for key, value in record.items() if key != "journal_sha256"}
        if record["journal_sha256"] != digest(projection) or record["identity_sha256"] != self.identity_sha256 or record["consumed_sha256"] != self.consumed.record_sha256 or record["attempt_id"] != self.consumed.record["attempt_id"]:
            raise ContractError("runtime journal binding/hash drift")
        validate_integer(record["generation"], minimum=1)
        if record["phase"] not in RUNTIME_PHASES:
            raise ContractError("runtime journal phase invalid")
        if record["outcome"] not in ("PENDING", "PASS", "FAIL"):
            raise ContractError("runtime journal outcome invalid")
        validate_digest(record["previous_journal_sha256"])
        if record["evidence_sha256"] is not None:
            validate_digest(record["evidence_sha256"])
        history = record["records"]
        if type(history) is not list or len(history) >= len(RUNTIME_PHASES) or record["generation"] != len(history) + 1:
            raise ContractError("runtime journal history/generation invalid")
        previous = None
        for number, item in enumerate(history + [{key: value for key, value in record.items() if key != "records"}]):
            exact_keys(item, JOURNAL_KEYS - {"records"})
            restored = {**item, "records": history[:number]}
            if item["journal_sha256"] != digest({key: value for key, value in restored.items() if key != "journal_sha256"}):
                raise ContractError("runtime journal history hash invalid")
            if item["generation"] != number + 1 or item["previous_journal_sha256"] != ("0" * 64 if previous is None else previous["journal_sha256"]):
                raise ContractError("runtime journal chain gap/rollback")
            if any(item[key] != record[key] for key in ("schema", "attempt_id", "consumed_sha256", "identity_sha256")):
                raise ContractError("runtime journal history identity mismatch")
            phase = item["phase"]
            if phase not in RUNTIME_PHASES or ((phase == "terminal_pass") != (item["outcome"] == "PASS")) or ((phase == "terminal_fail") != (item["outcome"] == "FAIL")):
                raise ContractError("runtime journal history terminal mismatch")
            if previous is None:
                if phase not in ("consumed", "terminal_fail"):
                    raise ContractError("runtime journal history initial phase invalid")
            elif previous["phase"].startswith("terminal_") or (phase != "terminal_fail" and RUNTIME_PHASES.index(phase) != RUNTIME_PHASES.index(previous["phase"]) + 1):
                raise ContractError("runtime journal history skipped/backward phase")
            previous = item
        return record, identity

    def load(self):
        self._check()
        try:
            self.current, self.current_identity = self._read("runtime.v1.json")
        except FileNotFoundError:
            self.current = None
            self.current_identity = None
        return self.current

    def advance(self, phase, *, outcome="PENDING", evidence_sha256=None):
        self._check()
        if phase not in RUNTIME_PHASES:
            raise ContractError("unknown runtime phase")
        if self.current is None:
            if phase not in ("consumed", "terminal_fail"):
                raise ContractError("runtime must start consumed or recovery FAIL")
        else:
            current, identity = self._read("runtime.v1.json")
            if current != self.current or identity != self.current_identity:
                raise ContractError("runtime journal current substitution")
            previous_phase = current["phase"]
            if previous_phase.startswith("terminal_"):
                raise ContractError("terminal runtime immutable")
            if phase != "terminal_fail" and RUNTIME_PHASES.index(phase) != RUNTIME_PHASES.index(previous_phase) + 1:
                raise ContractError("runtime phase is not exact successor")
        if (phase == "terminal_pass") != (outcome == "PASS") or (phase == "terminal_fail") != (outcome == "FAIL"):
            raise ContractError("runtime terminal outcome mismatch")
        record = {"schema": "attempt-runtime.v1", "attempt_id": self.consumed.record["attempt_id"],
            "consumed_sha256": self.consumed.record_sha256, "identity_sha256": self.identity_sha256,
            "generation": 1 if self.current is None else self.current["generation"] + 1,
            "phase": phase, "previous_journal_sha256": "0" * 64 if self.current is None else self.current["journal_sha256"],
            "outcome": outcome, "evidence_sha256": evidence_sha256,
            "records": [] if self.current is None else self.current["records"] + [{key: value for key, value in self.current.items() if key != "records"}]}
        record["journal_sha256"] = digest(record)
        self._point("pre:journal_stage:" + phase)
        stage = os.open(".runtime.v1.json.new", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400, dir_fd=self.fd)
        try:
            raw = canonical_bytes(record)
            offset = 0
            while offset < len(raw):
                amount = os.write(stage, raw[offset:])
                if amount <= 0:
                    raise ContractError("short runtime journal write")
                offset += amount
            os.fsync(stage)
        finally:
            os.close(stage)
        os.fsync(self.fd)
        self._point("post:journal_stage:" + phase)
        staged, staged_identity = self._read(".runtime.v1.json.new")
        if staged != record:
            raise ContractError("runtime journal stage drift")
        self._check()
        if self.current is None:
            self.publisher.publish(self.fd, ".runtime.v1.json.new", "runtime.v1.json")
        else:
            current, identity = self._read("runtime.v1.json")
            if current != self.current or identity != self.current_identity:
                raise ContractError("runtime journal changed before publish")
            os.replace(".runtime.v1.json.new", "runtime.v1.json", src_dir_fd=self.fd, dst_dir_fd=self.fd)
        os.fsync(self.fd)
        self.current, self.current_identity = self._read("runtime.v1.json")
        self._point("post:phase:" + phase)
        return self.current

    def recover_fail(self):
        """Validate retained stage; publish FAIL, never resume its operation."""
        self.load()
        if self.current is not None and self.current["phase"] == "terminal_fail":
            return self.current
        if self.current is not None and self.current["phase"] == "terminal_pass":
            return self.failure_marker("ambiguity after provisional terminal publication")
        try:
            staged, identity = self._read(".runtime.v1.json.new")
        except FileNotFoundError:
            staged = None
        if staged is not None:
            expected_generation = 1 if self.current is None else self.current["generation"] + 1
            expected_previous = "0" * 64 if self.current is None else self.current["journal_sha256"]
            if staged["generation"] != expected_generation or staged["previous_journal_sha256"] != expected_previous:
                raise ContractError("foreign runtime recovery stage")
            # Preserve exact stage permanently as audit; no destructive repair.
            retired = ".runtime.abandoned." + str(staged["generation"]) + ".v1.json"
            self.publisher.publish(self.fd, ".runtime.v1.json.new", retired)
            os.fsync(self.fd)
        return self.advance("terminal_fail", outcome="FAIL")

    def failure_marker(self, reason):
        """Permanent override; preserve ambiguous journal/evidence unchanged."""
        self._check()
        record = {"schema": "attempt-terminal-failure.v1", "attempt_id": self.consumed.record["attempt_id"],
                  "consumed_sha256": self.consumed.record_sha256, "outcome": "FAIL", "reason": reason}
        raw = canonical_bytes(record)
        try:
            fd = os.open("terminal-failure.v1.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400, dir_fd=self.fd)
        except FileExistsError:
            # Any preexisting marker, including corrupt/torn/substituted, forbids PASS.
            return {"phase": "terminal_fail", "outcome": "FAIL", "durable_terminal": False, "reason": "permanent failure marker present"}
        try:
            offset = 0
            while offset < len(raw):
                written = os.write(fd, raw[offset:])
                if written <= 0:
                    raise ContractError("short terminal failure write")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self.fd)
        return {"phase": "terminal_fail", "outcome": "FAIL", "durable_terminal": True,
                "reason": reason, "failure_sha256": hashlib.sha256(raw).hexdigest()}

    def commit_pass(self):
        self._check()
        current, identity = self._read("runtime.v1.json")
        if current != self.current or identity != self.current_identity or current["phase"] != "terminal_pass":
            raise ContractError("terminal commit binding mismatch")
        try:
            os.stat("terminal-failure.v1.json", dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ContractError("permanent failure veto present")
        record = {"schema": "attempt-terminal-commit.v1", "attempt_id": self.consumed.record["attempt_id"],
                  "consumed_sha256": self.consumed.record_sha256, "runtime_sha256": current["journal_sha256"],
                  "evidence_sha256": current["evidence_sha256"], "outcome": "PASS"}
        fd = os.open("terminal-commit.v1.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400, dir_fd=self.fd)
        try:
            raw = canonical_bytes(record)
            offset = 0
            while offset < len(raw):
                written = os.write(fd, raw[offset:])
                if written <= 0:
                    raise ContractError("short terminal commit write")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self.fd)
        return record


def recover_runtime(journal, backend=None):
    # No backend call, remount, admission, lock, spawn or gate is reachable here.
    try:
        return journal.recover_fail()
    except (OSError, ContractError):
        try:
            return journal.failure_marker("retained journal ambiguous; permanent consumed record remains")
        except (OSError, ContractError):
            return {"phase": "terminal_fail", "outcome": "FAIL", "durable_terminal": False,
                    "reason": "retained journal ambiguous; permanent consumed record remains"}


def validate_terminal_commit(journal, evidence_raw, expected_runtime_sha256):
    """Independent read-only acceptance; journal/evidence alone is never PASS.

    The expected runtime hash is supplied by the accepted supervisor result, not
    discovered from an untrusted evidence directory. Recovery after an interrupted
    invocation must run first and permanently veto its partial or ambiguous result.
    """
    validate_digest(expected_runtime_sha256)
    journal.load()
    current = journal.current
    if current is None or current["phase"] != "terminal_pass" or current["journal_sha256"] != expected_runtime_sha256:
        raise ContractError("complete externally bound terminal PASS required")
    def no_veto():
        try:
            os.stat("terminal-failure.v1.json", dir_fd=journal.fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise ContractError("permanent failure veto forbids PASS")
    no_veto()
    before = os.stat("terminal-commit.v1.json", dir_fd=journal.fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or (before.st_uid, before.st_gid, before.st_nlink, stat.S_IMODE(before.st_mode)) != (journal.uid, journal.gid, 1, 0o400):
        raise ContractError("terminal commit node invalid")
    fd = os.open("terminal-commit.v1.json", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=journal.fd)
    try:
        identity = descriptor_identity(fd)
        expected_identity = (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_gid, before.st_nlink)
        if identity != expected_identity or descriptor_mount(fd) != journal.mount:
            raise ContractError("terminal commit open identity invalid")
        raw = os.read(fd, 16385)
        after = os.stat("terminal-commit.v1.json", dir_fd=journal.fd, follow_symlinks=False)
        if descriptor_identity(fd) != identity or (after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_gid, after.st_nlink) != identity or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(raw) != before.st_size:
            raise ContractError("terminal commit read substitution")
    finally:
        os.close(fd)
    keys = ("schema", "attempt_id", "consumed_sha256", "runtime_sha256", "evidence_sha256", "outcome")
    commit = parse_canonical_object(raw, keys, schema="attempt-terminal-commit.v1")
    expected = {"schema": "attempt-terminal-commit.v1", "attempt_id": journal.consumed.record["attempt_id"],
        "consumed_sha256": journal.consumed.record_sha256, "runtime_sha256": expected_runtime_sha256,
        "evidence_sha256": current["evidence_sha256"], "outcome": "PASS"}
    if commit != expected or hashlib.sha256(evidence_raw).hexdigest() != current["evidence_sha256"]:
        raise ContractError("terminal commit/evidence binding mismatch")
    journal._check()
    reread, identity = journal._read("runtime.v1.json")
    if reread != current or identity != journal.current_identity:
        raise ContractError("terminal runtime changed during acceptance")
    no_veto()
    return True


def validate_supervisor_result(journal, evidence_raw, result):
    """Read-only consume the exact synchronous result before external transport."""
    if type(result) is not SupervisorResult or result.issuer is not _SUPERVISOR_ISSUER:
        raise ContractError("authenticated same-invocation supervisor result required")
    result.projection()
    if result.attempt_id != journal.consumed.record["attempt_id"] or result.consumed_sha256 != journal.consumed.record_sha256 or result.evidence_sha256 != hashlib.sha256(evidence_raw).hexdigest():
        raise ContractError("supervisor result identity/evidence mismatch")
    validate_terminal_commit(journal, evidence_raw, result.runtime_sha256)
    expected_commit = {"schema": "attempt-terminal-commit.v1", "attempt_id": result.attempt_id,
        "consumed_sha256": result.consumed_sha256, "runtime_sha256": result.runtime_sha256,
        "evidence_sha256": result.evidence_sha256, "outcome": "PASS"}
    if digest(expected_commit) != result.commit_sha256:
        raise ContractError("supervisor result commit mismatch")
    return True


def run_once(authority, live_grant, *, authority_sha256, live_grant_sha256,
             ledger_dir_fd, backend, owner_uid=0, owner_gid=0, fault=None, journal_publisher=None):
    # Reparse externally pinned documents; callers cannot bypass schema validation.
    authority = parse_installed_authority(canonical_bytes(authority), authority_sha256)
    live_grant = parse_live_grant(canonical_bytes(live_grant), live_grant_sha256, authority, authority_sha256)
    consumed = consume_once(ledger_dir_fd, authority, live_grant_sha256=live_grant_sha256,
                            authority_sha256=authority_sha256, owner_uid=owner_uid, owner_gid=owner_gid, fault=fault)
    journal = RuntimeJournal(ledger_dir_fd, consumed, owner_uid=owner_uid, owner_gid=owner_gid, fault=fault, publisher=journal_publisher)
    evidence = None
    backend_closed = False
    def effect(name, function):
        consumed.validate()
        if fault is not None:
            fault("pre:runtime:" + name)
        result = function()
        if fault is not None:
            fault("post:runtime:" + name)
        consumed.validate()
        return result
    try:
        backend.bind(consumed, authority, live_grant)
        journal.advance("consumed")
        journal.advance("snapshot_verifying")
        held = effect("snapshot_verify", backend.verify_snapshot)
        journal.advance("snapshot_held")
        journal.advance("namespace_sealing")
        effect("namespace_seal", lambda: backend.seal_namespace(held))
        journal.advance("namespace_sealed")
        journal.advance("live_admitting")
        effect("live_admit", backend.admit)
        effect("lock_acquire", backend.acquire_lock)
        journal.advance("lock_held")
        journal.advance("running")
        child = effect("child_exec", backend.start_child)
        status = effect("child_exit", lambda: backend.wait_child(child))
        journal.advance("collecting")
        evidence = effect("evidence_validate", lambda: backend.validate_evidence(status))
        if type(status) is not int or status != 0 or evidence["verdict"] != "PASS":
            raise ContractError("child did not produce canonical PASS")
        journal.advance("publishing")
        evidence_sha256 = effect("evidence_publish", lambda: backend.publish_evidence(evidence))
        validate_digest(evidence_sha256)
        # Published PASS evidence/journal is provisional until the separate commit.
        # Every detected later ambiguity creates an irrevocable failure veto; the
        # exact A009 phase graph and all historical evidence remain unchanged.
        if fault is not None:
            fault("pre:runtime:terminal_publish")
        terminal = journal.advance("terminal_pass", outcome="PASS", evidence_sha256=evidence_sha256)
        if fault is not None:
            fault("post:runtime:terminal_publish")
        # Keep the execution fence and signal guard through commit and read-only
        # acceptance. prepare_finalize stops/reaps descendants without releasing
        # the fence; only the final close may release it.
        effect("finalization_prepare", backend.prepare_finalize)
        effect("finalization_guard", backend.finalization_guard)
        commit = effect("terminal_commit", journal.commit_pass)
        effect("acceptance_readonly", lambda: validate_terminal_commit(journal, canonical_bytes(evidence), terminal["journal_sha256"]))
        effect("finalization_guard", backend.finalization_guard)
        effect("custody_close", backend.close)
        backend_closed = True
        result = SupervisorResult(live_grant["attempt_id"], consumed.record_sha256,
            terminal["journal_sha256"], evidence_sha256, digest(commit), True, _SUPERVISOR_ISSUER)
        key = id(result)
        _ISSUED_SUPERVISOR_RESULTS[key] = (weakref.ref(result,
            lambda unused, key=key: _ISSUED_SUPERVISOR_RESULTS.pop(key, None)), _result_identity(result))
        # A late close/signal exception has already taken the permanent veto path.
        # The expected runtime digest comes from this invocation, never a file.
        validate_supervisor_result(journal, canonical_bytes(evidence), result)
        return {**terminal, "supervisor_result": result}
    except Exception as error:
        # Stop the owned worker before validating/publishing any recovery evidence.
        try:
            backend.abort()
        except Exception:
            pass
        journal.fault = None
        terminal = recover_runtime(journal)
        return {**terminal, "reason": type(error).__name__ + ": " + str(error)}
    finally:
        try:
            if not backend_closed:
                backend.close()
        finally:
            journal.close()
            consumed.close()
