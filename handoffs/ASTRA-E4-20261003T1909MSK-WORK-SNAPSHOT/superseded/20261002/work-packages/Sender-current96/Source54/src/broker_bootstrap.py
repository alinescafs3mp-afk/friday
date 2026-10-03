"""Small self-contained fixed sudo transport and closed exact-spec loader."""
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import weakref
from dataclasses import dataclass
from types import MappingProxyType

MODULE_ORDER = ("canonical", "pinned_fs", "provenance", "archive", "manifest", "assemble", "ledger",
    "custody_linux", "broker_runtime", "broker_bootstrap", "install_bootstrap", "install")
BUNDLE_NAMES = tuple(sorted(name + ".py" for name in MODULE_ORDER))
TRUST_KEYS = frozenset("schema authority_id authority_sha256 package_index_sha256 runtime_index_sha256 bootstrap_python_sha256 environment sys_path allowed_fds".split())
PIN_KEYS = frozenset("schema authority_sha256 live_grant_sha256 effect_bill_sha256".split())
AUTHORITY_BOOTSTRAP_KEYS = frozenset("schema authority_id candidate_commit candidate_tree attempt_generation package_index_sha256 broker_bundle_sha256 bootstrap_python_path bootstrap_python_sha256 snapshot_root snapshot_manifest_sha256 provenance_sha256 candidate_controller_sha256 gate_argv_sha256 environment_sha256 caller_uid gate_uid gate_gid sudoers_path sudoers_sha256 ledger_directory runtime_journal_path live_lock_path scratch_parent evidence_parent install_grant_sha256 install_identity_sha256".split())
BOOTSTRAP_NEGATIVES = ("argv", "operation", "environment", "sys_path", "preload", "descriptor", "flags", "interpreter",
    "index_digest", "index_extra", "index_missing", "source_digest", "source_owner", "source_mode", "source_links",
    "source_type", "source_substitution", "source_mount", "self_digest", "trust_binding", "grant_pin")
BOOTSTRAP_PIN_FIELDS = ("authority_sha256", "package_index_sha256", "runtime_index_sha256", "bootstrap_python_sha256")
BOOTSTRAP_PIN_NEGATIVES = tuple("trust-" + field + "-" + kind for field in BOOTSTRAP_PIN_FIELDS for kind in ("absent", "null", "boolean", "integer", "wrong")) + tuple(
    "live-pin-" + field + "-" + kind for field in sorted(PIN_KEYS - {"schema"}) for kind in ("absent", "null", "boolean", "integer", "wrong"))


class BootstrapError(ValueError):
    pass


def _keys(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise BootstrapError("exact document keys required")
    return value


def _digest(value):
    if type(value) is not str or re.fullmatch("[0-9a-f]{64}", value) is None:
        raise BootstrapError("external digest malformed")
    return value


def _bytes(value):
    try:
        return (json.dumps(value, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise BootstrapError("canonical value invalid") from error


def _parse(raw, keys, schema, expected_sha256=None):
    if type(raw) is not bytes or not 0 < len(raw) <= 2097152:
        raise BootstrapError("bootstrap document size invalid")
    if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != _digest(expected_sha256):
        raise BootstrapError("external digest mismatch")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise BootstrapError("duplicate key")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=pairs,
                           parse_constant=lambda token: (_ for _ in ()).throw(BootstrapError("nonfinite JSON")))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise BootstrapError("bootstrap document parse failed") from error
    count = 0
    def bound(node, depth):
        nonlocal count
        count += 1
        if count > 50000 or depth > 32:
            raise BootstrapError("bootstrap document limits exceeded")
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str) or not key.isascii():
                    raise BootstrapError("non-ASCII key")
                bound(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                bound(child, depth + 1)
    bound(value, 0)
    if _bytes(value) != raw:
        raise BootstrapError("noncanonical bootstrap JSON")
    _keys(value, keys)
    if value["schema"] != schema:
        raise BootstrapError("bootstrap schema mismatch")
    return value


def _integer(value, maximum=2**31 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise BootstrapError("exact bounded integer required")
    return value


def _path(value):
    if type(value) is not str or not value.startswith("/") or len(value) > 4096 or not value.isascii() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value) or any(part in ("", ".", "..") for part in value[1:].split("/")):
        raise BootstrapError("canonical absolute startup path required")
    return value


def parse_bootstrap_trust(raw, expected_sha256):
    """Strict externally bound trust; callable before loading any package code."""
    _digest(expected_sha256)
    trust = _parse(raw, TRUST_KEYS, "friday.bootstrap-trust.v1", expected_sha256)
    for field in BOOTSTRAP_PIN_FIELDS:
        _digest(trust[field])
    if type(trust["authority_id"]) is not str or re.fullmatch(r"[a-z0-9_-]{1,128}", trust["authority_id"]) is None:
        raise BootstrapError("authority identity malformed")
    environment = trust["environment"]
    if type(environment) is not dict or len(environment) > 64:
        raise BootstrapError("closed environment object required")
    for key, value in environment.items():
        if type(key) is not str or re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key) is None or type(value) is not str or len(value) > 4096 or "\x00" in value:
            raise BootstrapError("environment key/value type invalid")
    paths = trust["sys_path"]
    if type(paths) is not list or not 1 <= len(paths) <= 16 or any(type(value) is not str for value in paths) or len(set(paths)) != len(paths):
        raise BootstrapError("closed unique sys.path list required")
    for value in paths:
        _path(value)
    fds = trust["allowed_fds"]
    if type(fds) is not list or any(type(fd) is not int for fd in fds) or fds != [0, 1, 2]:
        raise BootstrapError("exact integer descriptor inventory required")
    return trust


def parse_live_pin(raw, expected_sha256):
    _digest(expected_sha256)
    pin = _parse(raw, PIN_KEYS, "live-grant-pin.v1", expected_sha256)
    for key in PIN_KEYS - {"schema"}:
        _digest(pin[key])
    return pin


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _mount(fd):
    with open("/proc/self/fdinfo/" + str(fd), "r", encoding="ascii") as stream:
        for line in stream:
            if line.startswith("mnt_id:"):
                return int(line.split()[1])
    raise BootstrapError("bootstrap mount identity unavailable")


def stable_read(directory_fd, name, *, owner_uid=0, owner_gid=0, mode=0o444, expected_sha256=None,
                expected_size=None, maximum=2097152, fault=None):
    if type(name) is not str or not name or "/" in name or name in (".", "..") or "\x00" in name:
        raise BootstrapError("closed bootstrap filename required")
    parent_before = _identity(os.fstat(directory_fd))
    parent_mount = _mount(directory_fd)
    before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or (before.st_uid, before.st_gid, before.st_nlink, stat.S_IMODE(before.st_mode)) != (owner_uid, owner_gid, 1, mode) or before.st_size > maximum:
        raise BootstrapError("bootstrap member identity/mode/type invalid")
    if fault is not None:
        fault("before_open", name)
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
    try:
        opened = os.fstat(fd)
        if _identity(opened) != _identity(before) or _mount(fd) != parent_mount:
            raise BootstrapError("bootstrap member opened identity/mount drift")
        chunks, length = [], 0
        while True:
            chunk = os.read(fd, min(65536, maximum + 1 - length))
            if not chunk:
                break
            length += len(chunk)
            if length > maximum:
                raise BootstrapError("bootstrap member bound exceeded")
            chunks.append(chunk)
        raw = b"".join(chunks)
        if fault is not None:
            fault("after_read", name)
        after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(os.fstat(fd)) != _identity(before) or _identity(after) != _identity(before) or len(raw) != before.st_size or _identity(os.fstat(directory_fd)) != parent_before or _mount(directory_fd) != parent_mount:
            raise BootstrapError("bootstrap member/parent changed during read")
        if expected_size is not None and len(raw) != expected_size:
            raise BootstrapError("bootstrap member size mismatch")
        if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != _digest(expected_sha256):
            raise BootstrapError("bootstrap member external digest mismatch")
        return raw
    finally:
        os.close(fd)


@dataclass(frozen=True)
class TransportState:
    argv: tuple
    environment: dict
    sys_path: tuple
    modules: tuple
    descriptors: tuple
    interpreter: str
    isolated: bool
    no_site: bool
    no_bytecode: bool


def actual_transport():
    descriptors = []
    # fstat probing adds no enumeration fd to the descriptor set.
    for fd in sorted(int(name) for name in os.listdir("/proc/self/fd") if name.isdecimal()):
        try:
            os.fstat(fd)
        except OSError:
            continue
        descriptors.append(fd)
    return TransportState(tuple(sys.argv), dict(os.environ), tuple(sys.path), tuple(sys.modules),
                          tuple(descriptors), sys.executable, bool(sys.flags.isolated), bool(sys.flags.no_site), bool(sys.dont_write_bytecode))


def verify_transport(state, fixed_broker_path, fixed_environment, expected_sys_path, allowed_fds):
    if state.argv != (fixed_broker_path, "run-v1"):
        raise BootstrapError("only exact fixed run-v1 invocation admitted")
    if state.environment != fixed_environment:
        raise BootstrapError("bootstrap environment not exact")
    if state.sys_path != tuple(expected_sys_path):
        raise BootstrapError("bootstrap sys.path drift")
    if type(allowed_fds) not in (tuple, list) or any(type(fd) is not int for fd in allowed_fds) or any(type(fd) is not int for fd in state.descriptors) or state.descriptors != tuple(allowed_fds) or tuple(allowed_fds) != (0, 1, 2):
        raise BootstrapError("unexpected inherited descriptor")
    if state.interpreter != "/usr/bin/python3.14" or any(type(value) is not bool or value is not True for value in (state.isolated, state.no_site, state.no_bytecode)):
        raise BootstrapError("fixed isolated bootstrap interpreter required")
    forbidden = set(MODULE_ORDER) | {"broker_bootstrap"}
    if any(name.split(".")[0] in forbidden or name.startswith("friday_quality") for name in state.modules):
        raise BootstrapError("foreign package module preloaded")
    return True


@dataclass(frozen=True)
class AuthenticatedBundle:
    index_sha256: str
    source: dict
    root_identity: tuple
    root_mount: int
    path: str


_ISSUED_BUNDLES = {}


def _bundle_projection(bundle):
    return (bundle.index_sha256, tuple(sorted((name, hashlib.sha256(raw).hexdigest()) for name, raw in bundle.source.items())),
        bundle.root_identity, bundle.root_mount, bundle.path)


@dataclass(frozen=True)
class FixtureBootstrapCapability:
    """Non-root retained-file tests only; cannot confer host startup authority."""
    root_fd: int
    identity: tuple
    uid: int
    gid: int

    @classmethod
    def create(cls, root_fd):
        uid, gid = os.geteuid(), os.getegid()
        info = os.fstat(root_fd)
        if uid == 0 or gid == 0 or not stat.S_ISDIR(info.st_mode) or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (uid, gid, 0o700):
            raise BootstrapError("non-root private recording root required")
        return cls(root_fd, _identity(info), uid, gid)

    def validate(self):
        if os.geteuid() != self.uid or self.uid == 0 or os.getegid() != self.gid or _identity(os.fstat(self.root_fd))[:6] != self.identity[:6]:
            raise BootstrapError("fixture bootstrap capability drift")


def verify_startup_identity(observation, bundle, interpreter_sha256, *, fixture=False):
    """Bind opened/named/running identities; fixture data never proves lifetime."""
    _digest(interpreter_sha256)
    _keys(observation, ("interpreter_opened", "interpreter_named", "interpreter_running", "interpreter_sha256",
        "bootstrap_opened", "bootstrap_named", "bootstrap_running", "bootstrap_sha256", "protected_lifetime", "authority_mode"))
    for prefix in ("interpreter", "bootstrap"):
        identities = [observation[prefix + "_" + suffix] for suffix in ("opened", "named", "running")]
        if any(type(value) not in (tuple, list) or len(value) != 9 or any(type(item) is not int for item in value) for value in identities) or any(tuple(value) != tuple(identities[0]) for value in identities[1:]):
            raise BootstrapError("executing startup identity mismatch: " + prefix)
    if observation["interpreter_sha256"] != interpreter_sha256 or observation["bootstrap_sha256"] != hashlib.sha256(bundle.source["broker_bootstrap.py"]).hexdigest():
        raise BootstrapError("executing startup source digest mismatch")
    if observation["protected_lifetime"] is not True:
        raise BootstrapError("protected startup lifetime NOT_PROVEN")
    if not fixture or observation["authority_mode"] != "fixture":
        # No independently accepted host lifetime mechanism has been supplied.
        # Matching current filenames/hashes or a self-written receipt cannot do it.
        raise BootstrapError("production protected startup lifetime NOT_PROVEN")
    return True


def authenticate_bundle(root_fd, package_index, expected_index_sha256, *, owner_uid=0, owner_gid=0,
                        fixed_bundle_path="", source_mode=0o444, fault=None):
    _digest(expected_index_sha256)
    _integer(source_mode, 0o777)
    index = _parse(package_index, ("schema", "members"), "friday.package-index.v1", expected_index_sha256)
    if stable_read(root_fd, "package-index.v1.json", owner_uid=owner_uid, owner_gid=owner_gid,
                   mode=source_mode, expected_sha256=expected_index_sha256) != package_index:
        raise BootstrapError("runtime index substituted")
    members = index["members"]
    if type(members) is not list or len(members) != len(BUNDLE_NAMES):
        raise BootstrapError("closed runtime module count mismatch")
    records = {}
    for record in members:
        _keys(record, ("path", "role", "mode", "size", "sha256"))
        if type(record["path"]) is not str or record["path"] not in BUNDLE_NAMES or record["path"] in records or record["role"] != "runtime" or type(record["mode"]) is not int or record["mode"] != source_mode or type(record["size"]) is not int or not 0 <= record["size"] <= 2097152:
            raise BootstrapError("closed runtime member declaration invalid")
        _digest(record["sha256"])
        records[record["path"]] = record
    if [record["path"] for record in members] != list(BUNDLE_NAMES):
        raise BootstrapError("runtime index not exactly sorted/complete")
    if sorted(os.listdir(root_fd)) != sorted(list(BUNDLE_NAMES) + ["package-index.v1.json"]):
        raise BootstrapError("runtime bundle extra/missing member")
    info = os.fstat(root_fd)
    if not stat.S_ISDIR(info.st_mode) or (info.st_uid, info.st_gid) != (owner_uid, owner_gid) or info.st_mode & 0o022:
        raise BootstrapError("runtime bundle parent unprotected")
    before, mount = _identity(info), _mount(root_fd)
    sources = {}
    for name in BUNDLE_NAMES:
        record = records[name]
        sources[name] = stable_read(root_fd, name, owner_uid=owner_uid, owner_gid=owner_gid,
                                    mode=source_mode, expected_sha256=record["sha256"], expected_size=record["size"], fault=fault)
    if _identity(os.fstat(root_fd)) != before or _mount(root_fd) != mount or sorted(os.listdir(root_fd)) != sorted(list(BUNDLE_NAMES) + ["package-index.v1.json"]):
        raise BootstrapError("runtime bundle changed during authentication")
    result = AuthenticatedBundle(expected_index_sha256, MappingProxyType(sources), before, mount, fixed_bundle_path)
    key = id(result)
    _ISSUED_BUNDLES[key] = (weakref.ref(result, lambda unused, key=key: _ISSUED_BUNDLES.pop(key, None)), _bundle_projection(result))
    return result


@contextlib.contextmanager
def exact_spec_load(bundle):
    if type(bundle) is not AuthenticatedBundle:
        raise BootstrapError("externally authenticated bundle required")
    issued = _ISSUED_BUNDLES.get(id(bundle))
    if issued is None or issued[0]() is not bundle or issued[1] != _bundle_projection(bundle):
        raise BootstrapError("closed bundle was not authenticated or changed")
    if any(name in sys.modules for name in MODULE_ORDER):
        raise BootstrapError("runtime package preloaded")
    initial_path = tuple(sys.path)
    created = []
    try:
        for name in MODULE_ORDER:
            filename = bundle.path + "/" + name + ".py"
            spec = importlib.util.spec_from_loader(name, loader=None, origin=filename)
            module = importlib.util.module_from_spec(spec)
            module.__file__ = filename
            sys.modules[name] = module
            created.append(name)
            exec(compile(bundle.source[name + ".py"], filename, "exec", dont_inherit=True), module.__dict__)
            if tuple(sys.path) != initial_path:
                raise BootstrapError("runtime import changed sys.path")
        if any(name.startswith("friday_quality") for name in sys.modules):
            raise BootstrapError("unexpected loaded package module")
        yield sys.modules["broker_runtime"]
    finally:
        for name in reversed(created):
            sys.modules.pop(name, None)


class ProtectedChain:
    """Retain every ancestor through the complete descriptor operation lifetime."""
    def __init__(self, descriptors, links):
        self.descriptors, self.links = descriptors, links
        self.fd = descriptors[-1]

    def check(self):
        for parent, name, child, identity, parent_identity, mount in self.links:
            if _identity(os.fstat(parent)) != parent_identity or _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity or _identity(os.fstat(child)) != identity or _mount(parent) != mount or _mount(child) != mount:
                raise BootstrapError("held bootstrap ancestor identity/mount drift")
        return True

    def close(self):
        for fd in reversed(self.descriptors):
            os.close(fd)
        self.descriptors.clear()


def _open_chain(root_fd, absolute, *, directory=False, owner_uid=0, owner_gid=0):
    if type(absolute) is not str or not absolute.startswith("/") or any(part in ("", ".", "..") for part in absolute[1:].split("/")):
        raise BootstrapError("fixed absolute canonical path required")
    current = os.dup(root_fd)
    descriptors, links = [current], []
    try:
        parts = absolute[1:].split("/")
        for position, part in enumerate(parts):
            is_directory = position < len(parts) - 1 or directory
            before = os.stat(part, dir_fd=current, follow_symlinks=False)
            if (before.st_uid, before.st_gid) != (owner_uid, owner_gid) or before.st_mode & 0o022 or (is_directory and not stat.S_ISDIR(before.st_mode)) or (not is_directory and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1)):
                raise BootstrapError("bootstrap protected path chain invalid")
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | (os.O_DIRECTORY if is_directory else 0)
            fd = os.open(part, flags, dir_fd=current)
            descriptors.append(fd)
            parent_identity, mount = _identity(os.fstat(current)), _mount(current)
            if _identity(os.fstat(fd)) != _identity(before) or _identity(os.stat(part, dir_fd=current, follow_symlinks=False)) != _identity(before) or _mount(fd) != mount:
                raise BootstrapError("bootstrap path chain substitution")
            links.append((current, part, fd, _identity(before), parent_identity, mount))
            current = fd
        result = ProtectedChain(descriptors, links)
        result.check()
        descriptors = []
        return result
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _read_absolute(root_fd, path, *, expected_sha256=None, mode=0o400, maximum=2097152):
    parent, name = path.rsplit("/", 1)
    directory = _open_chain(root_fd, parent, directory=True)
    try:
        directory.check()
        raw = stable_read(directory.fd, name, mode=mode, expected_sha256=expected_sha256, maximum=maximum)
        directory.check()
        return raw
    finally:
        directory.close()


def _production_main():
    # Capture transport before opening trust descriptors; caller values select no
    # command, authority, live grant, filesystem output, environment or reset.
    state = actual_transport()
    match = re.fullmatch(r"/usr/libexec/friday/quality-gate-broker-v1/([0-9a-f]{64})/broker_bootstrap\.py", state.argv[0] if state.argv else "")
    if match is None or os.geteuid() != 0 or os.getegid() != 0:
        raise BootstrapError("installed root-only digest-specific broker required")
    # No reviewed independent startup/trust anchor exists in this source package.
    # This refusal occurs before package loading, root traversal or consumption.
    # Never manufacture that anchor from a matching current path or local receipt.
    raise BootstrapError("production external startup/trust anchor NOT_PROVEN")


def validate_result_transport(raw, expected_transport, *, invocation_complete, exit_code):
    """Consume an independently authenticated invocation, never directory blobs.

    The caller owns channel authentication and final process/EOF observation.
    Matching a projection does not itself authenticate an operating-system peer.
    """
    if invocation_complete is not True or type(exit_code) is not int or exit_code != 0:
        raise BootstrapError("interrupted or unsuccessful invocation cannot PASS")
    result = _parse(raw, ("schema", "supervisor", "transport"), "friday.supervisor-transport.v1")
    _keys(expected_transport, ("operation", "authority_sha256", "live_grant_sha256", "runtime_index_sha256", "package_index_sha256", "bootstrap_python_sha256"))
    for name in set(expected_transport) - {"operation"}:
        _digest(expected_transport[name])
    if expected_transport["operation"] != "run-v1" or result["transport"] != expected_transport:
        raise BootstrapError("supervisor transport external binding mismatch")
    supervisor = result["supervisor"]
    _keys(supervisor, ("schema", "attempt_id", "consumed_sha256", "runtime_sha256", "evidence_sha256", "commit_sha256", "finalization_complete", "outcome"))
    if supervisor["schema"] != "attempt-supervisor-result.v1" or type(supervisor["attempt_id"]) is not str or supervisor["finalization_complete"] is not True or supervisor["outcome"] != "PASS":
        raise BootstrapError("supervisor finalization receipt invalid")
    for key in ("consumed_sha256", "runtime_sha256", "evidence_sha256", "commit_sha256"):
        _digest(supervisor[key])
    return result


def main(recording=None):
    """Fixed transport; optional explicit inert non-root source-control adapter."""
    if recording is None:
        return _production_main()
    if type(recording.capability) is not FixtureBootstrapCapability:
        raise BootstrapError("explicit fixture bootstrap capability required")
    recording.capability.validate()
    state = recording.transport_state()
    match = re.fullmatch(r"/usr/libexec/friday/quality-gate-broker-v1/([0-9a-f]{64})/broker_bootstrap\.py", state.argv[0] if state.argv else "")
    if match is None:
        raise BootstrapError("fixed digest-specific transport required")
    package_sha = match.group(1)
    # Both expected pins arrive from the external fixture input, never the files.
    trust = parse_bootstrap_trust(recording.read_trust(), recording.trust_sha256)
    pin = parse_live_pin(recording.read_pin(), recording.live_pin_sha256)
    if trust["package_index_sha256"] != package_sha or pin["authority_sha256"] != trust["authority_sha256"]:
        raise BootstrapError("trust_binding/grant_pin mismatch")
    verify_transport(state, state.argv[0], trust["environment"], trust["sys_path"], trust["allowed_fds"])
    bundle = authenticate_bundle(recording.bundle_fd, recording.read_index(), trust["runtime_index_sha256"],
        owner_uid=recording.capability.uid, owner_gid=recording.capability.gid, source_mode=0o600,
        fixed_bundle_path=recording.bundle_path)
    verify_startup_identity(recording.startup_identity(), bundle, trust["bootstrap_python_sha256"], fixture=True)
    authority_raw, grant_raw, bill_raw = recording.read_subjects()
    for raw, expected in ((authority_raw, trust["authority_sha256"]), (grant_raw, pin["live_grant_sha256"]), (bill_raw, pin["effect_bill_sha256"])):
        if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != _digest(expected):
            raise BootstrapError("externally pinned authority/grant/effect subject mismatch")
    # Validate the complete authority projection and bindings before module exec.
    authority_projection = _parse(authority_raw, AUTHORITY_BOOTSTRAP_KEYS, "installed-authority.v1", trust["authority_sha256"])
    for key, value in authority_projection.items():
        if key.endswith("_sha256"):
            _digest(value)
    for key in ("attempt_generation", "caller_uid", "gate_uid", "gate_gid"):
        if _integer(authority_projection[key]) < 1:
            raise BootstrapError("authority integer identity invalid")
    for key in ("candidate_commit", "candidate_tree"):
        if type(authority_projection[key]) is not str or re.fullmatch("[0-9a-f]{40}", authority_projection[key]) is None:
            raise BootstrapError("authority Git identity invalid")
    for key, expected in (("broker_bundle_sha256", trust["runtime_index_sha256"]), ("package_index_sha256", package_sha), ("bootstrap_python_sha256", trust["bootstrap_python_sha256"])):
        if authority_projection[key] != expected:
            raise BootstrapError("authority runtime trust_binding mismatch")
    with exact_spec_load(bundle) as runtime:
        authority = runtime.parse_installed_authority(authority_raw, trust["authority_sha256"])
        grant = runtime.parse_live_grant(grant_raw, pin["live_grant_sha256"], authority, trust["authority_sha256"])
        sys.modules["custody_linux"].validate_live_effect_bill(bill_raw, pin["effect_bill_sha256"])
        backend = recording.backend(runtime, authority, grant)
        result = runtime.run_once(authority, grant, authority_sha256=trust["authority_sha256"],
            live_grant_sha256=pin["live_grant_sha256"], ledger_dir_fd=recording.ledger_fd, backend=backend,
            owner_uid=recording.capability.uid, owner_gid=recording.capability.gid,
            journal_publisher=recording.publisher)
        if result["phase"] != "terminal_pass" or "supervisor_result" not in result:
            return 1
        token = sys.modules["ledger"].load_consumed_for_recovery(recording.ledger_fd, authority,
            authority_sha256=trust["authority_sha256"], live_grant_sha256=pin["live_grant_sha256"],
            owner_uid=recording.capability.uid, owner_gid=recording.capability.gid)
        journal = runtime.RuntimeJournal(recording.ledger_fd, token, owner_uid=recording.capability.uid,
            owner_gid=recording.capability.gid, publisher=recording.publisher)
        try:
            evidence_raw = recording.evidence_raw(backend)
            runtime.validate_supervisor_result(journal, evidence_raw, result["supervisor_result"])
            transport = {"operation": "run-v1", "authority_sha256": trust["authority_sha256"],
                "live_grant_sha256": pin["live_grant_sha256"], "runtime_index_sha256": trust["runtime_index_sha256"],
                "package_index_sha256": package_sha, "bootstrap_python_sha256": trust["bootstrap_python_sha256"]}
            raw = _bytes({"schema": "friday.supervisor-transport.v1", "supervisor": result["supervisor_result"].projection(), "transport": transport})
            validate_result_transport(raw, transport, invocation_complete=True, exit_code=0)
            recording.emit_result(raw)
        finally:
            journal.close()
            token.close()
    recording.capability.validate()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(1)
