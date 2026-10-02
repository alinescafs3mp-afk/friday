"""No-follow descriptor custody and create-only private filesystem operations.

NativeBackend performs unprivileged filesystem syscalls only. A caller must hold
the write capability for its root; no pathname is accepted outside that root.
"""
import ctypes
import errno
import hashlib
import os
import stat
from canonical import ContractError, validate_digest, validate_integer, validate_path

READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
DIR_FLAGS = READ_FLAGS | os.O_DIRECTORY
FS_EFFECTS = ("mkdir", "create", "write", "file_fsync", "parent_fsync",
              "publish_noreplace", "unlink", "rmdir", "symlink")


def mount_id(fd):
    # fdinfo is used only for this process's held descriptor, never as traversal.
    try:
        with open("/proc/self/fdinfo/" + str(fd), "rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ContractError("oversize fdinfo")
        matches = [line.split(b":", 1)[1].strip() for line in raw.splitlines()
                   if line.startswith(b"mnt_id:")]
        if len(matches) != 1 or not matches[0].isdigit():
            raise ContractError("mount identity unavailable")
        return int(matches[0])
    except OSError as exc:
        raise ContractError("mount identity unavailable") from exc


def identity(value):
    return (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode), value.st_uid,
            value.st_gid, stat.S_IMODE(value.st_mode), 0 if stat.S_ISDIR(value.st_mode) else value.st_nlink)


def stable_identity(value):
    return identity(value) + (value.st_size, value.st_mtime_ns, value.st_ctime_ns)


class NativeBackend:
    """Small syscall capability, overridable for bounded hostile-node models."""
    def __init__(self, observer=None):
        self.observer = observer

    def event(self, point, path):
        if self.observer is not None:
            self.observer(point, path)

    def stat(self, name, fd):
        return os.stat(name, dir_fd=fd, follow_symlinks=False)

    def open(self, name, flags, fd, mode=0o600):
        return os.open(name, flags, mode, dir_fd=fd)

    def fstat(self, fd):
        return os.fstat(fd)

    def mount(self, fd):
        return mount_id(fd)

    def listdir(self, fd):
        return os.listdir(fd)

    def read(self, fd, length):
        return os.read(fd, length)

    def readlink(self, name, fd):
        return os.readlink(name, dir_fd=fd)

    def xattrs(self, fd):
        return os.listxattr(fd)

    def publish(self, source, source_fd, destination, destination_fd):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise ContractError("atomic no-replace publication unavailable")
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), 1):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))

    def symlink(self, target, name, fd):
        os.symlink(target, name, dir_fd=fd)

    def hardlink(self, source, source_fd, target, target_fd):
        os.link(source, target, src_dir_fd=source_fd, dst_dir_fd=target_fd, follow_symlinks=False)

    def write(self, fd, raw):
        return os.write(fd, raw)

    def fsync(self, fd):
        return os.fsync(fd)

    def mkdir(self, name, mode, fd):
        return os.mkdir(name, mode, dir_fd=fd)

    def unlink(self, name, fd):
        return os.unlink(name, dir_fd=fd)

    def rmdir(self, name, fd):
        return os.rmdir(name, dir_fd=fd)


class ParentLease:
    """Own every operation ancestor until the last identity/effect recheck."""
    def __init__(self, root, path):
        validate_path(path)
        self.root, self.name, self.closed = root, path.split("/")[-1], False
        self.fds, self.edges = [], []
        root.check()
        try:
            parent = os.dup(root.fd)
            self.fds.append(parent)
            for name in path.split("/")[:-1]:
                child = root._open_child(parent, name, directory=True)
                self.fds.append(child)
                self.edges.append((parent, name, child, identity(root.backend.fstat(child)), root.backend.mount(child)))
                parent = child
            self.fd = parent
            self.check(check_root=False)
            root._leases.append(self)
        except BaseException:
            self.close()
            raise

    def check(self, *, check_root=True):
        if self.closed:
            raise ContractError("ancestor lease closed")
        if check_root:
            self.root.check()
        backend = self.root.backend
        if identity(backend.fstat(self.fds[0])) != self.root.identity or backend.mount(self.fds[0]) != self.root.mount:
            raise ContractError("held operation root drift")
        for parent, name, child, expected, mount in self.edges:
            before = backend.stat(name, parent)
            held = backend.fstat(child)
            if identity(before) != expected or identity(held) != expected or backend.mount(child) != mount or backend.mount(parent) != self.root.mount:
                raise ContractError("held subordinate ancestor detachment/metadata/mount drift")
            opened = backend.open(name, DIR_FLAGS, parent)
            try:
                after = backend.stat(name, parent)
                if stable_identity(before) != stable_identity(backend.fstat(opened)) or stable_identity(before) != stable_identity(after) or backend.mount(opened) != mount:
                    raise ContractError("named/open/post subordinate ancestor drift")
                self.root._check_metadata(child, held, directory=True)
            finally:
                os.close(opened)
        return self

    def close(self):
        if not self.closed:
            for fd in reversed(self.fds):
                os.close(fd)
            self.closed = True
            if self in self.root._leases:
                self.root._leases.remove(self)

    def __enter__(self):
        try:
            return self.check()
        except OSError as exc:
            raise ContractError("retained ancestor unavailable") from exc

    def __exit__(self, *unused):
        self.close()


class PinnedRoot:
    def __init__(self, path_or_fd, *, expected_uid=None, expected_gid=None,
                 backend=None, require_readonly=False):
        for expected in (expected_uid, expected_gid):
            if expected is not None:
                validate_integer(expected, maximum=2**32 - 1)
        if type(require_readonly) is not bool:
            raise ContractError("exact readonly boolean required")
        self.backend = backend or NativeBackend()
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self.require_readonly = require_readonly
        self._chain = []
        self._leases = []
        self._descriptors = {}
        self.closed = False
        fd = None
        try:
            if isinstance(path_or_fd, PinnedRoot):
                path_or_fd.check()
                fd = os.dup(path_or_fd.fd)
                self._chain = path_or_fd._copy_chain(path_or_fd._chain)
            elif type(path_or_fd) is int:
                fd = os.dup(path_or_fd)
            else:
                path = validate_path(os.fspath(path_or_fd), absolute=True)
                parent = os.open("/", DIR_FLAGS)
                fd = parent
                initial = self.backend.fstat(parent)
                if expected_uid == 0 and (initial.st_uid != 0 or initial.st_gid != 0 or stat.S_IMODE(initial.st_mode) & 0o022):
                    raise ContractError("untrusted filesystem root")
                for name in path[1:].split("/"):
                    before = self.backend.stat(name, parent)
                    child = self.backend.open(name, DIR_FLAGS, parent)
                    if stable_identity(before) != stable_identity(self.backend.fstat(child)):
                        os.close(child)
                        raise ContractError("root chain substitution")
                    after = self.backend.stat(name, parent)
                    if stable_identity(before) != stable_identity(after):
                        os.close(child)
                        raise ContractError("root chain drift")
                    if expected_uid == 0 and (after.st_uid != 0 or after.st_gid != 0 or stat.S_IMODE(after.st_mode) & 0o022):
                        os.close(child)
                        raise ContractError("untrusted writable root ancestor")
                    self._chain.append((parent, name, identity(after), self.backend.mount(child), identity(self.backend.fstat(parent)), self.backend.mount(parent)))
                    parent = child
                    fd = child
                fd = parent
            value = self.backend.fstat(fd)
            if not stat.S_ISDIR(value.st_mode):
                raise ContractError("root must be held directory")
            self.fd = fd
            self.identity = identity(value)
            self.mount = self.backend.mount(fd)
            self._check_metadata(fd, value, directory=True)
        except BaseException:
            if fd is not None:
                os.close(fd)
            for parent, *_ in self._chain:
                os.close(parent)
            raise

    def __enter__(self):
        self.check()
        return self

    def __exit__(self, *unused):
        self.close()

    def close(self):
        if not self.closed:
            for fd in list(self._descriptors):
                self.close_beneath(fd)
            for lease in list(self._leases):
                lease.close()
            os.close(self.fd)
            for parent, *_ in self._chain:
                os.close(parent)
            self.closed = True

    def _check_metadata(self, fd, value, directory=False):
        if self.expected_uid is not None and value.st_uid != self.expected_uid:
            raise ContractError("owner mismatch")
        if self.expected_gid is not None and value.st_gid != self.expected_gid:
            raise ContractError("group mismatch")
        mode = stat.S_IMODE(value.st_mode)
        if mode & 0o7000 or (self.expected_uid == 0 and mode & 0o022) or (self.require_readonly and mode & 0o222):
            raise ContractError("unsafe mode")
        if not directory and value.st_nlink != 1:
            raise ContractError("multiple regular links")
        try:
            attributes = self.backend.xattrs(fd)
        except OSError as exc:
            raise ContractError("xattr observation unavailable") from exc
        if attributes:
            raise ContractError("unreviewed ACL/capability/xattr")

    def check(self):
        if self.closed:
            raise ContractError("root descriptor closed")
        if identity(self.backend.fstat(self.fd)) != self.identity or self.backend.mount(self.fd) != self.mount:
            raise ContractError("root identity/mount drift")
        for parent, name, expected, child_mount, parent_identity, parent_mount in self._chain:
            try:
                if identity(self.backend.fstat(parent)) != parent_identity or self.backend.mount(parent) != parent_mount:
                    raise ContractError("root held ancestor metadata/mount drift")
                observed = self.backend.stat(name, parent)
                if identity(observed) != expected:
                    raise ContractError("root ancestor rename/substitution")
                child = self.backend.open(name, DIR_FLAGS, parent)
                try:
                    if stable_identity(self.backend.fstat(child)) != stable_identity(observed) or identity(observed) != expected or stable_identity(self.backend.stat(name, parent)) != stable_identity(observed) or self.backend.mount(child) != child_mount:
                        raise ContractError("root ancestor mount/substitution: " + name)
                finally:
                    os.close(child)
            except OSError as exc:
                raise ContractError("root ancestor unavailable") from exc
        self._check_metadata(self.fd, self.backend.fstat(self.fd), directory=True)
        for lease in self._leases:
            try:
                lease.check(check_root=False)
            except OSError as exc:
                raise ContractError("retained subordinate ancestor unavailable") from exc
        for fd in self._descriptors:
            self._check_descriptor(fd)

    @staticmethod
    def _copy_chain(chain):
        """Clone named origin custody, independently of the source's lifetime."""
        copied = []
        try:
            for parent, *edge in chain:
                copied.append((os.dup(parent), *edge))
            return copied
        except BaseException:
            for parent, *_ in copied:
                os.close(parent)
            raise

    def _check_descriptor(self, fd, *, expected_identity=None):
        entry = self._descriptors.get(fd)
        if entry is None:
            raise ContractError("unowned beneath descriptor")
        lease, expected, mount, directory = entry
        if expected_identity is not None:
            if type(expected_identity) is not tuple or len(expected_identity) != 7 or any(type(v) is not int for v in expected_identity):
                raise ContractError("exact authorized descriptor identity required")
            if expected_identity[:3] != expected[:3]:
                raise ContractError("authorized metadata transition changed node")
            expected = expected_identity
        try:
            lease.check(check_root=False)
            before = self.backend.stat(lease.name, lease.fd)
            held = self.backend.fstat(fd)
            if identity(before) != expected or identity(held) != expected or self.backend.mount(fd) != mount:
                raise ContractError("named handoff target detachment/metadata/mount drift")
            opened = self.backend.open(lease.name, DIR_FLAGS if directory else READ_FLAGS, lease.fd)
            try:
                if stable_identity(before) != stable_identity(self.backend.fstat(opened)) or stable_identity(before) != stable_identity(self.backend.stat(lease.name, lease.fd)) or self.backend.mount(opened) != mount:
                    raise ContractError("named/open/post handoff target drift")
            finally:
                os.close(opened)
        except OSError as exc:
            raise ContractError("named handoff target unavailable") from exc
        return lease, expected, mount, directory

    def close_beneath(self, fd):
        """Paired close of the exact returned fd and its owned ancestry lease.

        os.close alone is not this capability's close API. Root closure closes all
        remaining capabilities; repeated paired cleanup is harmless afterward.
        """
        if type(fd) is not int:
            raise ContractError("exact descriptor required")
        entry = self._descriptors.pop(fd, None)
        if entry is None:
            if self.closed:
                return
            raise ContractError("unowned beneath descriptor close")
        try:
            os.close(fd)
        finally:
            entry[0].close()

    def handoff(self, fd, *, expected_uid=None, expected_gid=None,
                require_readonly=None, expected_identity=None):
        """Own a full origin + named target chain across a directory fd handoff.

        An explicit metadata identity is only a caller-authorized transition; it
        preserves dev/inode/type/mount and does not itself confer authority.
        All ancestry checks remain sampled observations, not atomic kernel proof.
        """
        original = self._descriptors.get(fd)
        lease, expected, mount, directory = self._check_descriptor(fd, expected_identity=expected_identity)
        if not directory:
            raise ContractError("directory handoff required")
        self._descriptors[fd] = (lease, expected, mount, directory)
        child = None
        try:
            self.check()
            child = PinnedRoot(fd,
                expected_uid=self.expected_uid if expected_uid is None else expected_uid,
                expected_gid=self.expected_gid if expected_gid is None else expected_gid,
                backend=self.backend,
                require_readonly=self.require_readonly if require_readonly is None else require_readonly)
            chain = list(self._chain)
            chain += [(parent, name, target, target_mount, identity(self.backend.fstat(parent)), self.backend.mount(parent))
                      for parent, name, unused, target, target_mount in lease.edges]
            chain.append((lease.fd, lease.name, expected, mount, identity(self.backend.fstat(lease.fd)), self.backend.mount(lease.fd)))
            child._chain = self._copy_chain(chain)
            child.check()
            self.check()
            return child
        except BaseException:
            self._descriptors[fd] = original
            if child is not None:
                child.close()
            raise

    def _parent(self, path):
        raise ContractError("bare parent descriptors are unavailable; retain parent_lease")

    def parent_lease(self, path):
        return ParentLease(self, path)

    def _open_child(self, parent, name, *, directory=False):
        if type(directory) is not bool:
            raise ContractError("exact node-type boolean required")
        fd = None
        try:
            self.backend.event("pre:open", name)
            before = self.backend.stat(name, parent)
            if not (stat.S_ISDIR(before.st_mode) if directory else stat.S_ISREG(before.st_mode)):
                raise ContractError("unexpected node type")
            self.backend.event("post:stat", name)
            fd = self.backend.open(name, DIR_FLAGS if directory else READ_FLAGS, parent)
            opened = self.backend.fstat(fd)
            self.backend.event("post:open", name)
            after = self.backend.stat(name, parent)
            if stable_identity(before) != stable_identity(opened) or stable_identity(opened) != stable_identity(after):
                raise ContractError("stat/open identity race")
            if opened.st_dev != self.identity[0] or self.backend.mount(fd) != self.mount:
                raise ContractError("cross-device or bind mount")
            self._check_metadata(fd, opened, directory=directory)
            return fd
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            raise ContractError("no-follow open refused") from exc
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise

    def open_beneath(self, path, *, directory=False):
        if type(directory) is not bool:
            raise ContractError("exact node-type boolean required")
        lease = self.parent_lease(path)
        parent, name = lease.fd, lease.name
        result = None
        try:
            result = self._open_child(parent, name, directory=directory)
            self._descriptors[result] = (lease, identity(self.backend.fstat(result)), self.backend.mount(result), directory)
            self.check()
            return result
        except BaseException:
            if result is not None:
                self._descriptors.pop(result, None)
                os.close(result)
            lease.close()
            raise

    def read_exact(self, path, *, expected_sha256=None, expected_size=None, maximum=16_777_216):
        validate_integer(maximum, minimum=0, maximum=2**31)
        if expected_sha256 is not None:
            validate_digest(expected_sha256)
        if expected_size is not None:
            validate_integer(expected_size, maximum=maximum)
        lease = self.parent_lease(path)
        parent, name = lease.fd, lease.name
        fd = None
        try:
            fd = self._open_child(parent, name)
            before = self.backend.fstat(fd)
            if before.st_size > maximum or (expected_size is not None and before.st_size != expected_size):
                raise ContractError("file size bound/mismatch")
            chunks = []
            length = 0
            while True:
                chunk = self.backend.read(fd, min(65536, maximum - length + 1))
                if not chunk:
                    break
                length += len(chunk)
                if length > maximum:
                    raise ContractError("file grew beyond bound")
                chunks.append(chunk)
            self.backend.event("post:read", path)
            after = self.backend.fstat(fd)
            named = self.backend.stat(name, parent)
            if stable_identity(before) != stable_identity(after) or stable_identity(after) != stable_identity(named):
                raise ContractError("file read/recheck race")
            if self.backend.mount(fd) != self.mount or length != before.st_size:
                raise ContractError("file identity/size drift")
            raw = b"".join(chunks)
            if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
                raise ContractError("file digest mismatch")
            self.check()
            return raw
        except OSError as exc:
            raise ContractError("stable read unavailable") from exc
        finally:
            if fd is not None:
                os.close(fd)
            lease.close()

    def read_authenticated(self, path, *, expected_sha256, expected_size=None, maximum=16_777_216):
        validate_digest(expected_sha256)
        return self.read_exact(path, expected_sha256=expected_sha256, expected_size=expected_size, maximum=maximum)

    def identity_of(self, path):
        lease = self.parent_lease(path)
        parent, name = lease.fd, lease.name
        held = None
        try:
            before = self.backend.stat(name, parent)
            held = self._open_child(parent, name, directory=stat.S_ISDIR(before.st_mode))
            self.check()
            return identity(self.backend.fstat(held))
        finally:
            if held is not None:
                os.close(held)
            lease.close()

    def walk_exact(self, *, maximum_members=50000, maximum_bytes=2**31):
        validate_integer(maximum_members, minimum=1, maximum=500000)
        validate_integer(maximum_bytes, maximum=1 << 44)
        self.check()
        records = []
        aggregate = 0
        def descend(fd, prefix, depth):
            nonlocal aggregate
            if depth > 32:
                raise ContractError("tree depth bound")
            initial = stable_identity(self.backend.fstat(fd))
            names = self.backend.listdir(fd)
            self.backend.event("post:enumerate", prefix)
            if len(names) != len(set(names)):
                raise ContractError("duplicate directory entry")
            for name in sorted(names):
                path = prefix + "/" + name if prefix else name
                validate_path(path)
                if len(records) >= maximum_members:
                    raise ContractError("tree member bound")
                value = self.backend.stat(name, fd)
                base = dict(path=path, uid=value.st_uid, gid=value.st_gid,
                            mode=stat.S_IMODE(value.st_mode), nlink=value.st_nlink,
                            mount_domain=self.mount)
                if stat.S_ISDIR(value.st_mode):
                    child = self._open_child(fd, name, directory=True)
                    try:
                        records.append(dict(base, type="directory"))
                        descend(child, path, depth + 1)
                        if identity(self.backend.stat(name, fd)) != identity(value):
                            raise ContractError("directory enumeration swap")
                    finally:
                        os.close(child)
                elif stat.S_ISREG(value.st_mode):
                    raw = self.read_exact(path, maximum=min(maximum_bytes - aggregate, 2**31))
                    aggregate += len(raw)
                    records.append(dict(base, type="file", size=len(raw),
                                        sha256=hashlib.sha256(raw).hexdigest(),
                                        executable_class="executable" if base["mode"] & 0o111 else "data"))
                    if stable_identity(self.backend.stat(name, fd)) != stable_identity(value):
                        raise ContractError("member metadata race")
                elif stat.S_ISLNK(value.st_mode):
                    if value.st_nlink != 1 or value.st_mode & 0o7000:
                        raise ContractError("unsafe symlink metadata")
                    target = self.backend.readlink(name, fd)
                    if stable_identity(value) != stable_identity(self.backend.stat(name, fd)):
                        raise ContractError("symlink identity race")
                    records.append(dict(base, type="symlink", target=target,
                                        target_sha256=hashlib.sha256(target.encode("ascii")).hexdigest()))
                else:
                    raise ContractError("special/unknown snapshot member")
            if names != self.backend.listdir(fd) or stable_identity(self.backend.fstat(fd)) != initial:
                raise ContractError("directory inventory race")
        try:
            descend(self.fd, "", 0)
            self.check()
        except (OSError, UnicodeError) as exc:
            raise ContractError("tree observation unavailable") from exc
        return sorted(records, key=lambda item: item["path"].encode("ascii"))

    def _effect(self, kind, path, operation):
        self.backend.event("pre:" + kind, path)
        self.check()
        result = operation()
        self.backend.event("post:" + kind, path)
        self.check()
        return result

    def write_new(self, path, data, *, mode=0o600):
        if type(data) is not bytes or mode not in (0o600, 0o400, 0o444, 0o440, 0o555):
            raise ContractError("exact bytes and approved file mode required")
        lease = self.parent_lease(path)
        parent, name = lease.fd, lease.name
        fd = None
        try:
            fd = self._effect("create", path, lambda: self.backend.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, parent, mode))
            def write():
                remaining = memoryview(data)
                while remaining:
                    amount = self.backend.write(fd, remaining)
                    if amount <= 0:
                        raise ContractError("short write")
                    remaining = remaining[amount:]
            self._effect("write", path, write)
            self._effect("file_fsync", path, lambda: self.backend.fsync(fd))
            written = self.backend.fstat(fd)
            if not stat.S_ISREG(written.st_mode) or written.st_nlink != 1 or written.st_size != len(data) or stat.S_IMODE(written.st_mode) != mode:
                raise ContractError("created file identity mismatch")
            if stable_identity(written) != stable_identity(self.backend.stat(name, parent)):
                raise ContractError("created file substitution")
            self._effect("parent_fsync", path, lambda: self.backend.fsync(parent))
            self.check()
            return identity(written)
        except OSError as exc:
            raise ContractError("create-only write refused") from exc
        finally:
            if fd is not None:
                os.close(fd)
            lease.close()

    def mkdir_new(self, path, *, mode=0o700):
        if mode not in (0o700, 0o500, 0o555):
            raise ContractError("approved directory mode required")
        lease = self.parent_lease(path)
        parent, name = lease.fd, lease.name
        try:
            self._effect("mkdir", path, lambda: self.backend.mkdir(name, mode, parent))
            opened = self._open_child(parent, name, directory=True)
            try:
                if stat.S_IMODE(self.backend.fstat(opened).st_mode) != mode:
                    raise ContractError("created directory mode mismatch")
                self._effect("file_fsync", path, lambda: self.backend.fsync(opened))
            finally:
                os.close(opened)
            self._effect("parent_fsync", path, lambda: self.backend.fsync(parent))
            return self.identity_of(path)
        except OSError as exc:
            raise ContractError("create-only mkdir refused") from exc
        finally:
            lease.close()

    def publish_noreplace(self, source, destination):
        source_lease = self.parent_lease(source)
        target_lease = None
        source_fd, source_name = source_lease.fd, source_lease.name
        held = None
        try:
            target_lease = self.parent_lease(destination)
            target_fd, target_name = target_lease.fd, target_lease.name
            expected = identity(self.backend.stat(source_name, source_fd))
            if expected[2] not in (stat.S_IFREG, stat.S_IFDIR) or (expected[2] == stat.S_IFREG and expected[6] != 1):
                raise ContractError("unsafe publication source")
            held = self._open_child(source_fd, source_name, directory=expected[2] == stat.S_IFDIR)
            if identity(self.backend.fstat(held)) != expected:
                raise ContractError("held publication source mismatch")
            def publish():
                if identity(self.backend.stat(source_name, source_fd)) != expected or identity(self.backend.fstat(held)) != expected or self.backend.mount(held) != self.mount:
                    raise ContractError("publication source identity race")
                self.backend.publish(source_name, source_fd, target_name, target_fd)
            self._effect("publish_noreplace", destination, publish)
            if identity(self.backend.stat(target_name, target_fd)) != expected or identity(self.backend.fstat(held)) != expected or self.backend.mount(held) != self.mount:
                raise ContractError("published identity substitution")
            self._effect("parent_fsync", destination, lambda: self.backend.fsync(target_fd))
            if source_fd != target_fd:
                self._effect("parent_fsync", source, lambda: self.backend.fsync(source_fd))
            self.check()
            return expected
        except OSError as exc:
            raise ContractError("no-replace publication refused") from exc
        finally:
            if held is not None:
                os.close(held)
            source_lease.close()
            if target_lease is not None:
                target_lease.close()

    def symlink_new(self, path, target):
        # The approved archive/manifest layer proves lexical/resolution safety.
        # The primitive also refuses absolute targets and lexical root escape.
        from manifest import normalize_link
        if type(target) is not str or target.startswith("/"):
            raise ContractError("relative approved symlink target required")
        normalize_link(path, target)
        lease = self.parent_lease(path)
        parent, name = lease.fd, lease.name
        try:
            self._effect("symlink", path, lambda: self.backend.symlink(target, name, parent))
            value = self.backend.stat(name, parent)
            if not stat.S_ISLNK(value.st_mode) or value.st_nlink != 1 or self.backend.readlink(name, parent) != target:
                raise ContractError("created symlink substitution")
            self._effect("parent_fsync", path, lambda: self.backend.fsync(parent))
            self.check()
            return identity(value)
        except OSError as exc:
            raise ContractError("create-only symlink refused") from exc
        finally:
            lease.close()

    def unlink_exact(self, path, expected_identity):
        lease = self.parent_lease(path)
        parent, name = lease.fd, lease.name
        try:
            value = self.backend.stat(name, parent)
            if identity(value) != tuple(expected_identity) or not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                raise ContractError("substituted unlink target")
            held = self._open_child(parent, name)
            try:
                if identity(self.backend.stat(name, parent)) != tuple(expected_identity):
                    raise ContractError("unlink target race")
                def unlink():
                    if identity(self.backend.stat(name, parent)) != tuple(expected_identity):
                        raise ContractError("unlink target identity race")
                    self.backend.unlink(name, parent)
                self._effect("unlink", path, unlink)
                if self.backend.fstat(held).st_nlink != 0:
                    raise ContractError("unlink race/extra link")
                self._effect("parent_fsync", path, lambda: self.backend.fsync(parent))
            finally:
                os.close(held)
            self.check()
        except OSError as exc:
            raise ContractError("exact unlink refused") from exc
        finally:
            lease.close()

    def remove_private_tree(self, path, expected_inventory):
        held = self.open_beneath(path, directory=True)
        try:
            with self.handoff(held) as child:
                actual = child.walk_exact()
                if actual != expected_inventory or any(item["type"] == "symlink" for item in actual):
                    raise ContractError("private tree inventory/substitution")
                identities = {item["path"]: child.identity_of(item["path"]) for item in actual}
                for item in sorted(actual, key=lambda row: (row["path"].count("/"), row["path"]), reverse=True):
                    if item["type"] == "file":
                        child.unlink_exact(item["path"], identities[item["path"]])
                    else:
                        child._rmdir_exact(item["path"], identities[item["path"]])
                expected_root = child.identity
            # Release the target capability before the intentional final removal.
            self.close_beneath(held)
            held = None
            self._rmdir_exact(path, expected_root)
        finally:
            if held is not None:
                self.close_beneath(held)

    def _rmdir_exact(self, path, expected_identity):
        lease = self.parent_lease(path)
        parent, name = lease.fd, lease.name
        held = None
        try:
            held = self._open_child(parent, name, directory=True)
            if identity(self.backend.fstat(held)) != tuple(expected_identity) or self.backend.listdir(held):
                raise ContractError("directory substitution/residue")
            def remove():
                if identity(self.backend.stat(name, parent)) != tuple(expected_identity):
                    raise ContractError("directory removal identity race")
                self.backend.rmdir(name, parent)
            self._effect("rmdir", path, remove)
            self._effect("parent_fsync", path, lambda: self.backend.fsync(parent))
        except OSError as exc:
            raise ContractError("exact directory removal refused") from exc
        finally:
            if held is not None:
                os.close(held)
            lease.close()


def open_beneath(root, path, **options):
    return root.open_beneath(path, **options)


def read_exact(root, path, **options):
    return root.read_exact(path, **options)


def walk_exact(root, **options):
    return root.walk_exact(**options)


def write_new(root, path, data, **options):
    return root.write_new(path, data, **options)


def publish_noreplace(root, source, destination):
    return root.publish_noreplace(source, destination)


def unlink_exact(root, path, expected_identity):
    return root.unlink_exact(path, expected_identity)


def remove_private_tree(root, path, expected_inventory):
    return root.remove_private_tree(path, expected_inventory)
