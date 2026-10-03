"""Grant-bound installer capabilities and descriptor-relative filesystem effects.

No privileged effect happens on import. Production effects require a capability
created from an externally authenticated root-install/revoke-remove grant. The
recording backend never calls chown, chmod, visudo or creates host special nodes.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import os
import stat
import subprocess
import weakref
from dataclasses import dataclass
from contextlib import contextmanager

from canonical import (ContractError, canonical_bytes, digest, exact_keys,
                       parse_canonical_object, validate_digest, validate_integer,
                       validate_path)

TEST_TOKEN = "SOL017-SYNTHETIC-RETAINED-ROOT-V1"
NAMESPACE = "var/lib/friday/quality-gate-v1"
INSTALL_DIRECTORY = NAMESPACE + "/install"
JOURNAL_PATH = INSTALL_DIRECTORY + "/journal.v1.json"
JOURNAL_STAGE = INSTALL_DIRECTORY + "/.journal.v1.json.new"
INSTALL_LOCK = INSTALL_DIRECTORY + "/install.lock"
LIVE_LOCK = NAMESPACE + "/live.lock"
PERMANENT_PREFIXES = (NAMESPACE + "/attempts", NAMESPACE + "/evidence")
GRANT_KEYS = frozenset({"schema", "transaction_id", "candidate_commit", "candidate_tree",
    "attempt_generation", "authority_id", "caller_uid", "package_index_sha256",
    "snapshot_manifest_sha256", "provenance_sha256", "assembly_recipe_sha256",
    "creation_tool_sha256", "owner_approval_sha256", "approved_authorities",
    "bootstrap_python_path", "bootstrap_python_sha256", "source_root_identity",
    "members", "authority", "bootstrap_trust", "effect_bill", "effect_bill_sha256", "policy_sha256",
    "previous_removed_journal_sha256", "retained_directories", "snapshot_directories", "snapshot_envelope"})
MEMBER_KEYS = frozenset({"source", "destination", "type", "size", "sha256", "target",
                         "uid", "gid", "mode", "source_identity"})
IDENTITY_KEYS = frozenset({"dev", "ino", "mount", "uid", "gid", "mode", "nlink", "type"})
EFFECT_BILL_KEYS = frozenset({"schema", "bill_id", "version", "scope", "allowed_effects",
                              "forbidden_effects", "required_authority", "implies"})
BILL_SEMANTICS = {
    "root-install": {
        "allowed_effects": ["authority-publish", "broker-publish", "descriptor-copy", "exact-policy-publish", "fsync", "install-journal", "install-lock", "metadata-seal", "runtime-prerequisites", "snapshot-envelope", "snapshot-publish", "visudo-fixed-validation"],
        "forbidden_effects": ["archive-extraction", "browser", "gate", "live", "live-grant", "r6", "resource-sampling", "retry"],
        "scope": ["digest-specific-broker", "digest-specific-snapshot", "new-quality-gate-namespace"],
        "required_authority": "separate-external-root-install-grant"},
    "revoke-remove": {
        "allowed_effects": ["exact-grant-remove", "exact-installed-identity-remove", "execution-fence", "policy-revoke-first", "removal-receipt"],
        "forbidden_effects": ["foreign-residue-deletion", "ledger-deletion", "policy-republish", "retention-change", "retry", "terminal-evidence-deletion"],
        "scope": ["journaled-install-namespace"],
        "required_authority": "separate-exact-revoke-remove-grant"}}


def validate_install_bill(raw, expected_sha256, operation):
    from canonical import parse_effect_bill
    bill = parse_effect_bill(raw, expected_sha256, operation, expected_version=2)
    if operation not in BILL_SEMANTICS or any(bill[k] != v for k, v in BILL_SEMANTICS[operation].items()):
        raise ContractError("effect bill semantic capability mismatch")
    return bill


def validate_mode(root, fake_root, test_token=None, *, euid=None):
    """Validate transport before opening, creating or locking anything."""
    euid = os.geteuid() if euid is None else euid
    if root != "/":
        validate_path(root, absolute=True)
    if fake_root:
        if euid == 0 or root == "/" or test_token != TEST_TOKEN:
            raise ContractError("fake root requires non-root, explicit token and non-/ root")
    elif euid != 0 or root != "/" or test_token is not None:
        raise ContractError("real installation requires root / and no test token")
    return True


def _identifier(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 80 or
            not value.isascii() or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in value)):
        raise ContractError("noncanonical identifier")
    return value


def validate_identity(value):
    exact_keys(value, IDENTITY_KEYS)
    for key in ("dev", "ino", "mount", "uid", "gid", "mode", "nlink"):
        validate_integer(value[key], maximum=0o7777 if key == "mode" else 2**32-1 if key in ("uid", "gid") else 2**63 - 1)
    if value["type"] not in ("file", "directory", "symlink"):
        raise ContractError("unsupported identity type")
    if value["nlink"] != (0 if value["type"] == "directory" else 1) or value["mode"] & 0o7000:
        raise ContractError("identity link/special-mode contract")
    return value


def parse_install_grant(raw, expected_sha256):
    validate_digest(expected_sha256)
    grant = parse_canonical_object(raw, GRANT_KEYS, schema="friday.install-grant.v3",
        expected_sha256=expected_sha256, max_bytes=64<<20, max_items=10_000_000)
    for key in ("transaction_id", "authority_id"):
        _identifier(grant[key])
    for key in ("candidate_commit", "candidate_tree"):
        value = grant[key]
        if not isinstance(value, str) or len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
            raise ContractError("invalid git identity")
    validate_integer(grant["attempt_generation"], minimum=1)
    validate_integer(grant["caller_uid"], minimum=1, maximum=2**32-2)
    for key in GRANT_KEYS:
        if key.endswith("sha256"):
            validate_digest(grant[key])
    retained = grant["retained_directories"]
    if type(retained) is not list or len(retained) > 50000:
        raise ContractError("bounded retained directory inventory required")
    names = []
    for item in retained:
        exact_keys(item, {"path", "identity"})
        names.append(validate_path(item["path"]))
        identity = validate_identity(item["identity"])
        if identity["type"] != "directory" or identity["uid"] or identity["gid"]:
            raise ContractError("retained authority is exact root-owned directories only")
    if names != sorted(set(names)):
        raise ContractError("retained directories must be sorted unique")
    if retained and grant["previous_removed_journal_sha256"] == "0" * 64:
        raise ContractError("retained directories require external removed predecessor pin")
    if grant["bootstrap_python_path"] != "/usr/bin/python3.14":
        raise ContractError("bootstrap interpreter is fixed")
    validate_identity(grant["source_root_identity"])
    if not isinstance(grant["members"], list) or not 1 <= len(grant["members"]) <= 50000:
        raise ContractError("bounded nonempty member set required")
    seen, previous = set(), ""
    for member in grant["members"]:
        exact_keys(member, MEMBER_KEYS)
        validate_path(member["source"])
        validate_path(member["destination"], absolute=True)
        if member["destination"] in seen:
            raise ContractError("duplicate destination")
        if member["source"] <= previous:
            raise ContractError("member source inventory must be sorted unique")
        previous = member["source"]
        seen.add(member["destination"])
        validate_identity(member["source_identity"])
        for key in ("uid", "gid", "mode", "size"):
            validate_integer(member[key], maximum=2**63-1)
        if member["uid"] != 0 or member["gid"] != 0:
            raise ContractError("installed ownership must be root:root")
        validate_digest(member["sha256"])
        if member["type"] == "file":
            if member["target"] is not None or member["mode"] not in (0o444, 0o555, 0o400):
                raise ContractError("invalid installed regular member")
        elif member["type"] == "symlink":
            target = member["target"]
            if not isinstance(target, str) or not target or "\0" in target or member["mode"] != 0o777:
                raise ContractError("invalid symlink target")
            if hashlib.sha256(target.encode("utf-8")).hexdigest() != member["sha256"]:
                raise ContractError("symlink target digest mismatch")
        else:
            raise ContractError("installer copies files and manifest-approved symlinks only")
    if type(grant["approved_authorities"]) is not list or not 1 <= len(grant["approved_authorities"]) <= 128:
        raise ContractError("external authorities required")
    for authority in grant["approved_authorities"]:
        from provenance import _authority
        _authority(authority)
    ids = [a["authority_id"] for a in grant["approved_authorities"]]
    if ids != sorted(set(ids)):
        raise ContractError("authority inventory order/duplicate")
    exact_keys(grant["effect_bill"], EFFECT_BILL_KEYS)
    bill = grant["effect_bill"]
    validate_install_bill(canonical_bytes(bill), grant["effect_bill_sha256"], "root-install")
    if (bill["schema"] != "friday.effect-bill.v1" or bill["bill_id"] != "root-install" or
            bill["version"] != 2 or bill["implies"] != [] or
            bill["required_authority"] != "separate-external-root-install-grant" or
            digest(bill) != grant["effect_bill_sha256"]):
        raise ContractError("effect bill does not authorize this installation")
    if not isinstance(bill["allowed_effects"], list) or any(not isinstance(v, str) for v in bill["allowed_effects"]):
        raise ContractError("invalid effect bill")
    if not isinstance(grant["authority"], dict) or not isinstance(grant["bootstrap_trust"], dict):
        raise ContractError("exact authority and trust objects required")
    from broker_runtime import parse_installed_authority
    from broker_bootstrap import parse_bootstrap_trust
    parse_installed_authority(canonical_bytes(grant["authority"]), digest(grant["authority"]))
    try:
        parse_bootstrap_trust(canonical_bytes(grant["bootstrap_trust"]), digest(grant["bootstrap_trust"]))
    except ValueError as error:
        raise ContractError("strict bootstrap trust rejected") from error
    directories = grant["snapshot_directories"]
    if type(directories) is not list or not 1 <= len(directories) <= 50000:
        raise ContractError("bounded snapshot directory inventory required")
    names = []
    for item in directories:
        exact_keys(item, {"path", "uid", "gid", "mode"})
        names.append(validate_path(item["path"]))
        for key in ("uid", "gid", "mode"):
            validate_integer(item[key], maximum=0o7777 if key == "mode" else 2**32-1)
        if item["uid"] or item["gid"] or item["mode"] != 0o555:
            raise ContractError("snapshot directory authority metadata")
    if names != sorted(set(names)):
        raise ContractError("snapshot directory order/duplicate")
    envelope = grant["snapshot_envelope"]
    if type(envelope) is not list or len(envelope) != 2:
        raise ContractError("complete snapshot envelope required")
    for item, name, pin in zip(envelope, ("manifest.v1.json", "provenance.v1.json"), ("snapshot_manifest_sha256", "provenance_sha256")):
        exact_keys(item, {"name", "size", "sha256", "uid", "gid", "mode"})
        validate_integer(item["size"], minimum=1, maximum=64<<20)
        for key in ("uid", "gid", "mode"):
            validate_integer(item[key])
        if item["name"] != name or item["sha256"] != grant[pin] or item["uid"] or item["gid"] or item["mode"] != 0o444:
            raise ContractError("snapshot envelope external binding mismatch")
    return grant


def mount_identity(fd):
    with open("/proc/self/fdinfo/" + str(fd), "rb") as handle:
        raw = handle.read(8192)
    for line in raw.splitlines():
        if line.startswith(b"mnt_id:"):
            return int(line.split(b":", 1)[1])
    raise ContractError("mount identity unavailable")


def _stat_identity(info, mount):
    kind = ("directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode)
            else "symlink" if stat.S_ISLNK(info.st_mode) else "special")
    return {"dev": info.st_dev, "ino": info.st_ino, "mount": mount,
            "uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode),
            "nlink": 0 if kind == "directory" else info.st_nlink, "type": kind}


@dataclass(frozen=True)
class InstallCapability:
    root: str
    fake_root: bool
    identity_sha256: str
    effects: frozenset
    operation: str
    test_token: str | None = None
    paths: frozenset = frozenset()
    approval_sha256: str = ""
    live_grants: tuple = ()
    approved_materials: object = None

    def __post_init__(self):
        validate_mode(self.root, self.fake_root, self.test_token)
        validate_digest(self.identity_sha256)
        validate_digest(self.approval_sha256)
        if (self.operation not in ("root-install", "revoke-remove") or
                not isinstance(self.effects, frozenset) or not isinstance(self.paths, frozenset) or not self.paths):
            raise ContractError("invalid install capability")
        if self.operation == "root-install":
            from provenance import require_approved_materials
            require_approved_materials(self.approved_materials, fixture_authority=self.fake_root)


_ISSUED_CAPABILITIES = {}


def _capability_projection(value):
    return (value.root,value.fake_root,value.identity_sha256,value.operation,value.test_token,
        tuple(sorted(value.effects)),tuple(sorted(value.paths)),value.approval_sha256,
        digest(list(value.live_grants)),id(value.approved_materials))


def _issue_install_capability(value):
    """Only the strict InstallPlan consumer issues the immutable capability."""
    key=id(value)
    _ISSUED_CAPABILITIES[key]=(weakref.ref(value,lambda unused,key=key:_ISSUED_CAPABILITIES.pop(key,None)),_capability_projection(value))
    return value


def require_install_capability(value):
    if type(value) is not InstallCapability: raise ContractError("exact issued install capability required")
    issued=_ISSUED_CAPABILITIES.get(id(value))
    if issued is None or issued[0]() is not value or issued[1]!=_capability_projection(value):
        raise ContractError("unissued/replaced/changed install capability")
    if value.operation=="root-install":
        from provenance import require_approved_materials
        require_approved_materials(value.approved_materials,fixture_authority=value.fake_root)
    return value


class NativeInstallSyscalls:
    """Explicit native capability; never constructed for inert controls."""
    inert = False
    def event(self, point, path): pass
    def unlock(self,fd): pass
    def seal(self, backend, path, fd, parent, name, uid, gid, mode, kind):
        if kind == "symlink":
            os.chown(name, uid, gid, dir_fd=parent, follow_symlinks=False)
        else:
            os.fchown(fd, uid, gid)
            os.fchmod(fd, mode)
    def publish(self, left, lname, right, rname):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None: raise ContractError("RENAME_NOREPLACE unavailable")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(left, os.fsencode(lname), right, os.fsencode(rname), 1):
            raise OSError(ctypes.get_errno(), "no-replace publication refused")
    def flock(self, fd):
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return False
        return True
    def policy(self, fd=None):
        argv = ["/usr/sbin/visudo", "-c"] if fd is None else ["/usr/sbin/visudo", "-cf", "/proc/self/fd/"+str(fd)]
        return subprocess.run(argv, env={"PATH":"/usr/bin:/bin","LANG":"C","LC_ALL":"C"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=() if fd is None else (fd,), timeout=30, check=False).returncode == 0


class InertInstallSyscalls:
    """Retained ordinary files only; privileged metadata/lock/policy are data."""
    inert = True
    _lock_owners = {}
    def __init__(self, observer=None):
        self.observer, self.calls, self.blocked_locks = observer, [], set()
    def event(self, point, path):
        self.calls.append((point,path))
        if self.observer is not None: self.observer(point,path)
    def seal(self, backend, path, fd, parent, name, uid, gid, mode, kind):
        backend._metadata.setdefault(path,{}).update(uid=uid,gid=gid,mode=mode)
    def publish(self, left, lname, right, rname):
        try: os.stat(rname,dir_fd=right,follow_symlinks=False)
        except FileNotFoundError: pass
        else: raise ContractError("create-only publication collision")
        os.rename(lname,rname,src_dir_fd=left,dst_dir_fd=right)
    def flock(self, fd):
        path=os.readlink("/proc/self/fd/"+str(fd))
        if path in self.blocked_locks: return False
        info=os.fstat(fd); key=(path,info.st_dev,info.st_ino)
        owner=self._lock_owners.get(key)
        if owner is not None and owner != (self,fd): return False
        self._lock_owners[key]=(self,fd)
        return True
    def unlock(self,fd):
        for key,owner in tuple(self._lock_owners.items()):
            if owner==(self,fd): self._lock_owners.pop(key)
    def policy(self, fd=None):
        if fd is None: return True
        raw=os.pread(fd,16385,0)
        return b"NOSETENV:" in raw and b"*" not in raw


class FilesystemBackend:
    """Shared actual methods with held full-identity ancestor leases."""
    def __init__(self, capability, *, syscalls=None):
        require_install_capability(capability)
        self.capability=capability
        self.syscalls=syscalls if syscalls is not None else NativeInstallSyscalls()
        if capability.fake_root != (type(self.syscalls) is InertInstallSyscalls):
            raise ContractError("fake root requires exact inert syscall adapter")
        self.fd=os.open(capability.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        self.mount=mount_identity(self.fd)
        self.root_identity=_stat_identity(os.fstat(self.fd),self.mount)
        self.events,self._locks,self._metadata,self._known,self._parent_chains=[],{},{},{},{}
        self.expected={}
        self.live_grant_identities={i["path"]:i["identity"] for i in capability.live_grants}
        self.live_grant_digests={i["path"]:i["sha256"] for i in capability.live_grants}
    def close(self):
        for parent in tuple(self._parent_chains): self.release_parent(parent)
        for fd in self._locks.values(): self.syscalls.unlock(fd); os.close(fd)
        self._locks.clear()
        if self.fd is not None: os.close(self.fd); self.fd=None
    def check(self):
        if self.fd is None or _stat_identity(os.fstat(self.fd),mount_identity(self.fd))!=self.root_identity:
            raise ContractError("held root identity drift")
        fd=os.open(self.capability.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:
            if _stat_identity(os.fstat(fd),mount_identity(fd))!=self.root_identity:
                raise ContractError("root pathname substitution")
        finally: os.close(fd)
    def _name(self,path,*,removal=False):
        validate_path(path)
        if removal and any(path==p or path.startswith(p+"/") for p in PERMANENT_PREFIXES):
            raise ContractError("permanent audit deletion forbidden")
        allowed=(INSTALL_DIRECTORY,NAMESPACE+"/authorities",NAMESPACE+"/grants",NAMESPACE+"/attempts",
            NAMESPACE+"/evidence",NAMESPACE+"/scratch",NAMESPACE+"/bootstrap-trust",
            "usr/libexec/friday/quality-gate-broker-v1","usr/libexec/friday/quality-gate-toolchain-v1","etc/sudoers.d")
        ancestors={"var","var/lib","var/lib/friday",NAMESPACE,"usr","usr/libexec","usr/libexec/friday","etc"}
        if path not in (LIVE_LOCK,"usr/bin/python3.14") and path not in ancestors and not any(path==p or path.startswith(p+"/") for p in allowed):
            raise ContractError("path outside namespace")
        self.check()
        return path
    def _write_scope(self,path):
        require_install_capability(self.capability)
        if path not in self.capability.paths: raise ContractError("path not granted")
        self._name(path)
    def _parent(self,path,*,removal=False):
        self._name(path,removal=removal)
        current,chain=os.dup(self.fd),[]
        try:
            parts=path.split("/")
            for index,name in enumerate(parts[:-1]):
                self.syscalls.event("pre:parent_open",path)
                before=os.stat(name,dir_fd=current,follow_symlinks=False)
                prefix="/".join(parts[:index+1])
                expected=self.observed(before,prefix,self.mount)
                child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=current)
                try:
                    opened=os.fstat(child)
                    self.syscalls.event("post:parent_open",path)
                    named=os.stat(name,dir_fd=current,follow_symlinks=False)
                    if expected!=self.observed(opened,prefix,mount_identity(child)) or expected!=self.observed(named,prefix,self.mount):
                        raise ContractError("parent pre/open/post identity/mount mismatch")
                    model=self._metadata.get("/".join(parts[:index+1]),{})
                    if model.get("type","directory")!="directory" or model.get("mount",self.mount)!=self.mount:
                        raise ContractError("modeled parent substitution")
                    if not self.capability.fake_root and (opened.st_uid or opened.st_gid or opened.st_mode&0o022):
                        raise ContractError("unprotected parent")
                except BaseException: os.close(child); raise
                chain.append((current,name,expected,child,prefix))
                current=child
            self._parent_chains[current]=chain
            self.check_parent(current)
            return current,parts[-1]
        except BaseException:
            self._parent_chains.pop(current,None)
            os.close(current)
            for parent,*unused in chain: os.close(parent)
            raise
    def check_parent(self,fd):
        self.check()
        for parent,name,expected,child,prefix in self._parent_chains.get(fd,()):
            if self.observed(os.stat(name,dir_fd=parent,follow_symlinks=False),prefix,self.mount)!=expected or self.observed(os.fstat(child),prefix,mount_identity(child))!=expected:
                raise ContractError("held operation ancestor detached/substituted")
    def release_parent(self,fd):
        chain=self._parent_chains.pop(fd,())
        os.close(fd)
        for parent,*unused in chain: os.close(parent)
    @contextmanager
    def parent_lease(self,path,*,removal=False):
        parent,name=self._parent(path,removal=removal)
        try:
            yield parent,name
            self.check_parent(parent)
        finally: self.release_parent(parent)
    def observed(self,info,path,mount):
        value=_stat_identity(info,mount)
        model=self._metadata.get(path,{})
        value.update({k:v for k,v in model.items() if k in IDENTITY_KEYS})
        if model.get("xattrs") or value["mount"]!=self.mount or value["type"] not in ("file","directory","symlink") or (value["type"]!="directory" and value["nlink"]!=1):
            raise ContractError("unsafe type/link/mount/attribute")
        return value
    def expect(self,objects): self.expected=dict(objects)
    def require_expected(self,path,observed):
        expected=self.expected.get(path,self._known.get(path))
        if expected is not None and observed!=expected: raise ContractError("exact predecessor substitution: "+path)
        return observed
    def exists(self,path):
        try: return self.identity(path)
        except FileNotFoundError: return None
    def identity(self,path):
        with self.parent_lease(path) as (parent,name):
            before=os.stat(name,dir_fd=parent,follow_symlinks=False)
            fd=None
            try:
                if not (stat.S_ISREG(before.st_mode) or stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode)):
                    raise ContractError("unsafe identity type")
                mount=self.mount
                initial=self.observed(before,path,mount)
                if not stat.S_ISLNK(before.st_mode):
                    fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=parent)
                    mount=mount_identity(fd)
                    if self.observed(os.fstat(fd),path,mount)!=initial or os.listxattr(fd):
                        raise ContractError("identity pre/open mismatch")
                self.syscalls.event("post:identity_open",path)
                named=self.observed(os.stat(name,dir_fd=parent,follow_symlinks=False),path,mount)
                if named!=initial: raise ContractError("identity post metadata drift")
                return named
            finally:
                if fd is not None: os.close(fd)
    def _regular(self,path,flags=os.O_RDONLY):
        parent,name=self._parent(path)
        fd=None
        try:
            before=os.stat(name,dir_fd=parent,follow_symlinks=False)
            expected=self.observed(before,path,self.mount)
            if expected["type"]!="file": raise ContractError("regular file required")
            fd=os.open(name,flags|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK,dir_fd=parent)
            self.syscalls.event("post:file_open",path)
            if self.observed(os.fstat(fd),path,mount_identity(fd))!=expected or self.observed(os.stat(name,dir_fd=parent,follow_symlinks=False),path,self.mount)!=expected or os.listxattr(fd):
                raise ContractError("file pre/open/post identity mismatch")
            self.check_parent(parent)
            return fd,parent,name,before
        except BaseException:
            if fd is not None: os.close(fd)
            self.release_parent(parent)
            raise
    def read(self,path,*,maximum=2097152):
        validate_integer(maximum,maximum=64<<20)
        fd,parent,name,before=self._regular(path)
        expected=self.observed(before,path,self.mount)
        stable=lambda x: (_stat_identity(x,self.mount),x.st_size,x.st_mtime_ns,x.st_ctime_ns)
        try:
            if before.st_size>maximum: raise ContractError("read size bound")
            chunks,total=[],0
            while True:
                raw=os.read(fd,min(65536,maximum+1-total))
                if not raw: break
                total+=len(raw)
                if total>maximum: raise ContractError("growing source")
                chunks.append(raw)
            self.syscalls.event("post:read",path)
            after,named=os.fstat(fd),os.stat(name,dir_fd=parent,follow_symlinks=False)
            if stable(before)!=stable(after) or stable(after)!=stable(named) or total!=before.st_size or mount_identity(fd)!=self.mount or self.observed(after,path,self.mount)!=expected:
                raise ContractError("read full metadata/mount drift")
            self.check_parent(parent)
            return b"".join(chunks)
        finally: os.close(fd); self.release_parent(parent)
    def mkdir(self,path):
        self._write_scope(path)
        with self.parent_lease(path) as (parent,name):
            self.syscalls.event("pre:mkdir",path); self.check_parent(parent)
            os.mkdir(name,0o700,dir_fd=parent)
            if self.capability.fake_root:
                # Model only the ownership of this new inode, before publishing
                # the post-syscall observation to the inert control adapter.
                self._metadata[path] = {"uid": 0, "gid": 0, "mode": 0o700}
            self.syscalls.event("post:mkdir",path); os.fsync(parent)
        self._known[path]=self.identity(path)
        self.events.append(("mkdir",path))
    def create(self,path,data,*,link_target=None):
        self._write_scope(path)
        if type(data) is not bytes: raise ContractError("exact creation bytes required")
        with self.parent_lease(path) as (parent,name):
            self.syscalls.event("pre:create",path); self.check_parent(parent)
            if link_target is not None and not self.capability.fake_root:
                os.symlink(link_target,name,dir_fd=parent)
            else:
                raw=data if link_target is None else link_target.encode("ascii")
                fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=parent)
                try:
                    view=memoryview(raw)
                    while view:
                        count=os.write(fd,view)
                        if count<=0: raise ContractError("short write")
                        view=view[count:]
                    self.syscalls.event("pre:create_file_fsync",path)
                    os.fsync(fd)
                    self.syscalls.event("post:create_file_fsync",path)
                finally: os.close(fd)
                if link_target is not None: self._metadata[path]={"type":"symlink","mode":0o777,"target":link_target}
            self.syscalls.event("post:create",path); os.fsync(parent)
        self._known[path]=self.identity(path)
        self.events.append(("create",path))
    def seal(self,path,uid,gid,mode):
        self._write_scope(path)
        expected=self.require_expected(path,self.identity(path))
        with self.parent_lease(path) as (parent,name):
            fd=None
            try:
                self.syscalls.event("pre:seal",path)
                if self.observed(os.stat(name,dir_fd=parent,follow_symlinks=False),path,self.mount)!=expected:
                    raise ContractError("seal named target substitution")
                if expected["type"]!="symlink":
                    fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
                    if self.observed(os.fstat(fd),path,mount_identity(fd))!=expected: raise ContractError("seal opened target substitution")
                self.check_parent(parent)
                self.syscalls.seal(self,path,fd,parent,name,uid,gid,mode,expected["type"])
                self.syscalls.event("post:seal",path)
                sealed=dict(expected,uid=uid,gid=gid,mode=mode)
                if self.observed(os.stat(name,dir_fd=parent,follow_symlinks=False),path,self.mount)!=sealed or (fd is not None and self.observed(os.fstat(fd),path,mount_identity(fd))!=sealed):
                    raise ContractError("unapproved seal identity delta")
            finally:
                if fd is not None: os.close(fd)
        self._known[path]=self.identity(path)
        self.events.append(("seal",path))
    def fsync(self,path,*,parent_only=False):
        self._write_scope(path)
        with self.parent_lease(path) as (parent,name):
            if not parent_only and self.identity(path)["type"]!="symlink":
                fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
                try:
                    self.require_expected(path,self.observed(os.fstat(fd),path,mount_identity(fd)))
                    os.fsync(fd)
                finally: os.close(fd)
            os.fsync(parent)
        self.events.append(("fsync_parent" if parent_only else "fsync",path))
    def publish(self,source,target,*,validate_source=None):
        self._write_scope(source); self._write_scope(target)
        expected=self.require_expected(source,self.identity(source))
        with self.parent_lease(source) as (left,lname), self.parent_lease(target) as (right,rname):
            held=os.open(lname,(os.O_PATH if expected["type"]=="symlink" and not self.capability.fake_root else os.O_RDONLY)|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=left)
            try:
                self.syscalls.event("pre:publish",source)
                if validate_source is not None: validate_source()
                if self.observed(os.stat(lname,dir_fd=left,follow_symlinks=False),source,self.mount)!=expected or self.observed(os.fstat(held),source,mount_identity(held))!=expected:
                    raise ContractError("publish source substitution")
                self.check_parent(left); self.check_parent(right)
                self.syscalls.publish(left,lname,right,rname)
                for path in tuple(self._metadata):
                    if path==source or path.startswith(source+"/"):
                        self._metadata[target+path[len(source):]]=self._metadata.pop(path)
                self.syscalls.event("post:publish",target)
                if self.observed(os.stat(rname,dir_fd=right,follow_symlinks=False),target,self.mount)!=expected or self.observed(os.fstat(held),target,mount_identity(held))!=expected:
                    raise ContractError("published held/named identity substitution")
                os.fsync(left); os.fsync(right)
            finally: os.close(held)
        self._known.pop(source,None); self._known[target]=expected
        self.events.append(("publish",target))
    def journal_replace(self,stage,target,expected):
        self._write_scope(stage); self._write_scope(target)
        if (stage,target)!=(JOURNAL_STAGE,JOURNAL_PATH) or INSTALL_LOCK not in self._locks:
            raise ContractError("journal promotion needs exact held exclusive install lock")
        staged=self.identity(stage)
        with self.parent_lease(stage) as (left,lname), self.parent_lease(target) as (right,rname):
            held=os.open(lname,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=left)
            predecessor=None
            try:
                if expected is not None: predecessor=os.open(rname,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=right)
                self.syscalls.event("pre:journal_replace",target)
                if self.exists(target)!=expected or self.identity(stage)!=staged or self.observed(os.fstat(held),stage,mount_identity(held))!=staged: raise ContractError("journal stage/predecessor substituted")
                if predecessor is not None and self.observed(os.fstat(predecessor),target,mount_identity(predecessor))!=expected: raise ContractError("journal held predecessor substituted")
                self.check_parent(left); self.check_parent(right)
                os.rename(lname,rname,src_dir_fd=left,dst_dir_fd=right)
                if stage in self._metadata: self._metadata[target]=self._metadata.pop(stage)
                self.syscalls.event("post:journal_replace",target)
                if self.identity(target)!=staged or self.observed(os.fstat(held),target,mount_identity(held))!=staged: raise ContractError("journal published identity mismatch")
                if predecessor is not None and os.fstat(predecessor).st_nlink!=0: raise ContractError("journal predecessor not unlinked")
                os.fsync(right)
            finally:
                os.close(held)
                if predecessor is not None: os.close(predecessor)
        self._known.pop(stage,None); self._known[target]=staged
        self.events.append(("journal_replace",target))
    def unlink(self,path,expected):
        self._write_scope(path)
        with self.parent_lease(path,removal=True) as (parent,name):
            held=None
            try:
                self.syscalls.event("pre:unlink",path)
                if self.observed(os.stat(name,dir_fd=parent,follow_symlinks=False),path,self.mount)!=expected:
                    raise ContractError("deletion target substitution")
                if expected["type"]!="symlink":
                    held=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=parent)
                    if self.observed(os.fstat(held),path,mount_identity(held))!=expected: raise ContractError("deletion opened mismatch")
                self.syscalls.event("post:unlink_open",path)
                if self.observed(os.stat(name,dir_fd=parent,follow_symlinks=False),path,self.mount)!=expected: raise ContractError("deletion named predecessor changed")
                self.check_parent(parent)
                if expected["type"]=="directory": os.rmdir(name,dir_fd=parent)
                elif expected["type"] in ("file","symlink") and expected["nlink"]==1: os.unlink(name,dir_fd=parent)
                else: raise ContractError("unsafe deletion type")
                self.syscalls.event("post:unlink",path)
                try: os.stat(name,dir_fd=parent,follow_symlinks=False)
                except FileNotFoundError: pass
                else: raise ContractError("removed name substituted")
                if held is not None and expected["type"]=="file" and os.fstat(held).st_nlink!=0: raise ContractError("held file not removed")
                self._metadata.pop(path,None); self._known.pop(path,None); os.fsync(parent)
            finally:
                if held is not None: os.close(held)
        self.events.append(("unlink",path))
    def inventory(self,path):
        with self.parent_lease(path) as (parent,name):
            expected=self.identity(path)
            fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
            try:
                if self.observed(os.fstat(fd),path,mount_identity(fd))!=expected: raise ContractError("inventory opened mismatch")
                names=sorted(os.listdir(fd)); result={}
                for item in names:
                    child=path+"/"+item; result[child]=self.identity(child)
                    if result[child]["type"]=="directory": result.update(self.inventory(child))
                self.syscalls.event("post:inventory",path)
                if names!=sorted(os.listdir(fd)) or self.identity(path)!=expected: raise ContractError("inventory drift")
                if any(self.identity(child)!=identity for child,identity in result.items()): raise ContractError("inventory descendant identity drift")
                return result
            finally: os.close(fd)
    def lock(self,path):
        self._write_scope(path)
        if path in self._locks:
            if _stat_identity(os.fstat(self._locks[path]),self.mount)!=_stat_identity(os.stat(path,dir_fd=self.fd,follow_symlinks=False),self.mount):
                raise ContractError("held lock substituted")
            return True
        with self.parent_lease(path) as (parent,name):
            fd=os.open(name,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=parent)
            info=os.fstat(fd)
            uid,gid=(os.geteuid(),os.getegid()) if self.capability.fake_root else (0,0)
            if not stat.S_ISREG(info.st_mode) or (info.st_uid,info.st_gid,info.st_nlink,stat.S_IMODE(info.st_mode))!=(uid,gid,1,0o600) or os.listxattr(fd):
                os.close(fd); raise ContractError("invalid canonical lock")
            if not self.syscalls.flock(fd): os.close(fd); return False
            self._locks[path]=fd; os.fsync(fd); os.fsync(parent)
        return True
    def validate_policy(self,path,expected_bytes):
        if self.read(path)!=expected_bytes: raise ContractError("policy byte mismatch")
        fd,parent,name,unused=self._regular(path)
        try:
            if not self.syscalls.policy(fd): raise ContractError("fixed policy validation failed")
            self.check_parent(parent)
        finally: os.close(fd); self.release_parent(parent)
        self.events.append(("validate_policy",path))
    def validate_revocation(self,path):
        if self.exists(path) is not None or not self.syscalls.policy(): raise ContractError("revoked policy invalid")
        self.events.append(("validate_revocation",path))


class RecordingBackend(FilesystemBackend):
    """Uses the production source methods with an explicit inert syscall adapter."""
    def __init__(self,capability,*,syscalls=None):
        if not capability.fake_root: raise ContractError("recording capability required")
        super().__init__(capability,syscalls=syscalls or InertInstallSyscalls())
        self.active_run=False
    def lock(self,path):
        if path==LIVE_LOCK and self.active_run: return False
        return super().lock(path)
    def model_substitution(self,path,**fields):
        self._name(path); self._metadata.setdefault(path,{}).update(fields)
    def export_state(self): return {k:dict(v) for k,v in self._metadata.items()}
    def import_state(self,state): self._metadata={k:dict(v) for k,v in state.items()}
