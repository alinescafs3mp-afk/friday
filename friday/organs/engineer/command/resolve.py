"""Resolve and hold attested executable FDs. Do not re-open by pathname at spawn."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import stat
from pathlib import Path

from .contracts import (
    BASH_EXECUTABLE,
    BWRAP_EXECUTABLE,
    DESTRUCTIVE_BASENAMES,
    FORBIDDEN_EXACT_PATHS,
    FORBIDDEN_PATH_PREFIXES,
    MAX_EXECUTABLE_BYTES,
    SHELL_FLAG_PREFIX,
    CommandError,
    CommandLane,
    CommandRequest,
    HeldExecutable,
    PathRoot,
    ResolvedExecutable,
    TrustedPathContract,
    VerifiedCommandGrant,
)

_MAX_SHEBANG = 4096
_INTERPRETER_DEPTH = 1
_ARGV_DISPATCHERS = frozenset(
    {
        "awk",
        "bash",
        "busybox",
        "chrt",
        "cmake",
        "csh",
        "dash",
        "dotnet",
        "env",
        "find",
        "fish",
        "gawk",
        "git",
        "gmake",
        "guile",
        "ionice",
        "java",
        "ksh",
        "lua",
        "luajit",
        "make",
        "mawk",
        "meson",
        "mksh",
        "mono",
        "nawk",
        "nice",
        "ninja",
        "node",
        "nodejs",
        "nohup",
        "parallel",
        "perl",
        "php",
        "prlimit",
        "ruby",
        "sed",
        "setsid",
        "sh",
        "stdbuf",
        "taskset",
        "tcsh",
        "tclsh",
        "timeout",
        "toybox",
        "watch",
        "wish",
        "xargs",
        "zsh",
    }
)


def _argv_can_dispatch(request: CommandRequest, resolved: ResolvedExecutable) -> bool:
    """Recognize argv entry points that can execute a second, unattested command."""
    basename = Path(resolved.canonical_path).name.lower()
    if basename == "env":
        # Plain ``env`` only reports the fixed child environment. Any operand
        # can become a command after option/assignment parsing, so fail closed.
        return bool(request.argv[1:])
    if basename == "find":
        return any(item in {"-exec", "-execdir", "-ok", "-okdir"} for item in request.argv[1:])
    if basename in _ARGV_DISPATCHERS:
        return True
    return basename.startswith(("python", "pypy", "ruby", "perl"))


def _is_forbidden(path: str) -> bool:
    if path in FORBIDDEN_EXACT_PATHS:
        return True
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in FORBIDDEN_PATH_PREFIXES)


def _reject_traversal(path: str) -> None:
    if not path or "\x00" in path:
        raise CommandError("invalid_executable")
    if ".." in Path(path).parts:
        raise CommandError("path_escape")


def _lexical_normalize(path: str) -> str:
    if not path.startswith("/") or "\x00" in path:
        raise CommandError("invalid_executable")
    parts: list[str] = []
    for part in path.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise CommandError("path_escape")
            parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts)


def _open_named_nofollow(path: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise CommandError("symlink_refused") from exc
        if exc.errno == errno.ENOENT:
            raise CommandError("executable_not_found") from exc
        raise CommandError("executable_unreadable") from exc


def _one_hop_alias(named: str) -> str:
    try:
        st = os.lstat(named)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise CommandError("executable_not_found") from exc
        raise CommandError("executable_unreadable") from exc
    if not stat.S_ISLNK(st.st_mode):
        return named
    try:
        target = os.readlink(named)
    except OSError as exc:
        raise CommandError("symlink_refused") from exc
    if not target or "\x00" in target:
        raise CommandError("symlink_refused")
    dest = target if target.startswith("/") else str(Path(named).parent / target)
    dest = _lexical_normalize(dest)
    if _is_forbidden(dest):
        raise CommandError("forbidden_path")
    try:
        dest_st = os.lstat(dest)
    except OSError as exc:
        raise CommandError("executable_not_found") from exc
    if stat.S_ISLNK(dest_st.st_mode):
        raise CommandError("symlink_refused")
    return dest


def _hash_fd(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_EXECUTABLE_BYTES:
            raise CommandError("executable_too_large")
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _read_shebang(fd: int) -> str | None:
    os.lseek(fd, 0, os.SEEK_SET)
    head = os.read(fd, _MAX_SHEBANG)
    os.lseek(fd, 0, os.SEEK_SET)
    if not head.startswith(b"#!"):
        return None
    try:
        line = head.split(b"\n", 1)[0][2:].decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise CommandError("invalid_shebang") from exc
    if not line:
        raise CommandError("invalid_shebang")
    interpreter = line.split()[0]
    if interpreter in {"/usr/bin/env", "/bin/env"}:
        raise CommandError("env_shebang_refused")
    return interpreter


def sealed_payload_memfd(payload: bytes, *, label: str = "friday-payload") -> int:
    dest = os.memfd_create(label[:249], os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(dest, view)
            view = view[written:]
        os.lseek(dest, 0, os.SEEK_SET)
        fcntl.fcntl(
            dest,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL,
        )
        flags = fcntl.fcntl(dest, fcntl.F_GETFD)
        fcntl.fcntl(dest, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
        os.set_inheritable(dest, True)
        return dest
    except Exception:
        os.close(dest)
        raise


def snapshot_sealed_memfd(src_fd: int, *, label: str = "friday-exec") -> int:
    """Copy attested bytes into a sealed memfd. Later in-place rewrites cannot alter it."""
    dest = os.memfd_create(label[:249], os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.lseek(src_fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(src_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(dest, view)
                view = view[written:]
        os.lseek(dest, 0, os.SEEK_SET)
        fcntl.fcntl(
            dest,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL,
        )
        flags = fcntl.fcntl(dest, fcntl.F_GETFD)
        fcntl.fcntl(dest, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
        os.set_inheritable(dest, True)
        return dest
    except Exception:
        os.close(dest)
        raise


def _seals_are_final(fd: int) -> bool:
    try:
        seals = int(fcntl.fcntl(fd, fcntl.F_GET_SEALS))
    except OSError:
        return False
    required = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
    return seals & required == required


def attest_open_fd(
    fd: int,
    *,
    named: str,
    expected: ResolvedExecutable | None = None,
    named_stat: os.stat_result | None = None,
) -> ResolvedExecutable:
    try:
        st = os.fstat(fd)
        named_st = named_stat if named_stat is not None else os.lstat(named)
    except OSError as exc:
        raise CommandError("executable_unreadable") from exc
    if stat.S_ISLNK(named_st.st_mode):
        raise CommandError("symlink_refused")
    if not stat.S_ISREG(st.st_mode):
        raise CommandError("not_regular_file")
    if (st.st_dev, st.st_ino) != (named_st.st_dev, named_st.st_ino):
        raise CommandError("identity_changed")
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o022:
        raise CommandError("writable_executable")
    if st.st_mode & (stat.S_ISUID | stat.S_ISGID):
        raise CommandError("setid_refused")
    euid = os.geteuid()
    egid = os.getegid()
    executable = bool(
        (st.st_uid == euid and mode & 0o100) or (st.st_gid == egid and mode & 0o010) or (mode & 0o001)
    )
    if not executable:
        raise CommandError("not_executable")
    resolved = ResolvedExecutable(
        requested=named,
        canonical_path=named,
        owner_uid=int(st.st_uid),
        owner_gid=int(st.st_gid),
        mode=int(st.st_mode),
        device=int(st.st_dev),
        inode=int(st.st_ino),
        size_bytes=int(st.st_size),
        mtime_ns=int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
        sha256=_hash_fd(fd),
    )
    if expected is not None and resolved.identity_tuple() != expected.identity_tuple():
        raise CommandError("identity_changed")
    return resolved


def confirm_held_fd(fd: int, expected: ResolvedExecutable) -> None:
    if not _seals_are_final(fd):
        raise CommandError("identity_changed")
    try:
        st = os.fstat(fd)
    except OSError as exc:
        raise CommandError("identity_changed") from exc
    if int(st.st_size) != expected.size_bytes:
        raise CommandError("identity_changed")
    if _hash_fd(fd) != expected.sha256:
        raise CommandError("identity_changed")


def confirm_held(held: HeldExecutable) -> None:
    confirm_held_fd(held.executable_fd, held.resolved)
    if held.interpreter_fd is not None and held.interpreter is not None:
        confirm_held_fd(held.interpreter_fd, held.interpreter)
    if held.script_fd is not None and held.script is not None:
        confirm_held_fd(held.script_fd, held.script)


def _lookup_relative(name: str, trusted_path: TrustedPathContract) -> str:
    if "/" in name or name in {".", ".."} or name.startswith("-"):
        raise CommandError("relative_name_invalid")
    for directory in trusted_path.directories:
        candidate = f"{directory}/{name}"
        try:
            os.lstat(candidate)
        except OSError:
            continue
        try:
            return _one_hop_alias(candidate)
        except CommandError as exc:
            if exc.code in {"executable_not_found", "symlink_refused", "forbidden_path", "path_escape"}:
                continue
            raise
    raise CommandError("executable_not_found")


def _open_relative_held(name: str, roots: tuple[PathRoot, ...]) -> tuple[int, str, os.stat_result]:
    """Open a PATH entry relative to an attested directory descriptor."""
    if "/" in name or name in {".", ".."} or name.startswith("-"):
        raise CommandError("relative_name_invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for root in roots:
        candidate = name
        display = f"{root.path}/{name}"
        try:
            named_st = os.stat(candidate, dir_fd=root.dir_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISLNK(named_st.st_mode):
            try:
                target = os.readlink(candidate, dir_fd=root.dir_fd)
            except OSError:
                continue
            # A one-hop same-directory alias is sufficient for normal PATH
            # layouts and remains anchored to the held directory. Absolute,
            # nested, or parent-relative aliases would reintroduce pathname
            # traversal and are refused.
            if not target or target.startswith("/") or "/" in target or target in {".", ".."}:
                continue
            candidate = target
            display = f"{root.path}/{target}"
            try:
                named_st = os.stat(candidate, dir_fd=root.dir_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(named_st.st_mode):
                continue
        if _is_forbidden(display):
            continue
        try:
            fd = os.open(candidate, flags, dir_fd=root.dir_fd)
        except OSError:
            continue
        return fd, display, named_st
    raise CommandError("executable_not_found")


def resolve_named(
    path: str,
    *,
    trusted_path: TrustedPathContract | None = None,
    depth: int = 0,
    expected: ResolvedExecutable | None = None,
    hold: bool = False,
    path_roots: tuple[PathRoot, ...] | None = None,
) -> ResolvedExecutable | tuple[ResolvedExecutable, int, str | None]:
    contract = trusted_path or TrustedPathContract.default()
    _reject_traversal(path)
    named_stat = None
    if path.startswith("/") or path_roots is None:
        named = path if path.startswith("/") else _lookup_relative(path, contract)
        named = _one_hop_alias(named)
        if _is_forbidden(named):
            raise CommandError("forbidden_path")
        fd = _open_named_nofollow(named)
    else:
        fd, named, named_stat = _open_relative_held(path, path_roots)
    try:
        resolved = attest_open_fd(fd, named=named, expected=expected, named_stat=named_stat)
        shebang = _read_shebang(fd)
        if shebang:
            if depth >= _INTERPRETER_DEPTH:
                raise CommandError("nested_script_refused")
            if not shebang.startswith("/"):
                raise CommandError("relative_interpreter")
            interp_held = resolve_held(
                shebang,
                trusted_path=contract,
                depth=depth + 1,
                path_roots=path_roots,
            )
            interp_held.close()
        if hold:
            sealed = snapshot_sealed_memfd(fd, label=Path(named).name or "friday-exec")
            os.close(fd)
            return resolved, sealed, shebang
    except Exception:
        os.close(fd)
        raise
    os.close(fd)
    return resolved


def resolve_held(
    path: str,
    *,
    trusted_path: TrustedPathContract | None = None,
    depth: int = 0,
    path_roots: tuple[PathRoot, ...] | None = None,
) -> HeldExecutable:
    contract = trusted_path or TrustedPathContract.default()
    resolved, fd, shebang = resolve_named(  # type: ignore[misc]
        path,
        trusted_path=contract,
        depth=depth,
        hold=True,
        path_roots=path_roots,
    )
    assert isinstance(resolved, ResolvedExecutable)
    if not shebang:
        os.set_inheritable(fd, True)
        return HeldExecutable(resolved=resolved, executable_fd=fd)
    if depth >= _INTERPRETER_DEPTH:
        os.close(fd)
        raise CommandError("nested_script_refused")
    try:
        interpreter = resolve_held(
            shebang,
            trusted_path=contract,
            depth=depth + 1,
            path_roots=path_roots,
        )
    except Exception:
        os.close(fd)
        raise
    os.set_inheritable(fd, True)
    os.set_inheritable(interpreter.executable_fd, True)
    return HeldExecutable(
        resolved=interpreter.resolved,
        executable_fd=interpreter.executable_fd,
        interpreter=interpreter.resolved,
        interpreter_fd=interpreter.executable_fd,
        script=resolved,
        script_fd=fd,
    )


def resolve_root_helper(path: str) -> HeldExecutable:
    named = _one_hop_alias(path)
    fd = _open_named_nofollow(named)
    try:
        st = os.fstat(fd)
        if st.st_uid != 0:
            raise CommandError("helper_untrusted")
        resolved = attest_open_fd(fd, named=named)
        sealed = snapshot_sealed_memfd(fd, label=Path(named).name or "friday-helper")
    except Exception:
        os.close(fd)
        raise
    os.close(fd)
    return HeldExecutable(resolved=resolved, executable_fd=sealed)


def require_destructive_grant(
    request: CommandRequest,
    grant: VerifiedCommandGrant,
    resolved: ResolvedExecutable,
) -> None:
    # Shell has no attested subcommand policy in this slice: every shell-lane
    # execution needs a distinct confirmation. Substring filters are not an
    # authority boundary.
    if request.lane is CommandLane.SHELL:
        needs = True
    else:
        basename = Path(resolved.canonical_path).name
        needs = (
            basename in DESTRUCTIVE_BASENAMES
            or _argv_can_dispatch(request, resolved)
            or resolved.owner_uid != 0
        )
    if needs and not grant.destructive_confirmed:
        raise CommandError("destructive_confirmation_required")


def resolve_request(
    request: CommandRequest,
    grant: VerifiedCommandGrant,
    *,
    trusted_path: TrustedPathContract,
    path_roots: tuple[PathRoot, ...] | None = None,
) -> HeldExecutable:
    if request.lane is CommandLane.SHELL:
        held = resolve_held(BASH_EXECUTABLE, trusted_path=trusted_path, path_roots=path_roots)
        if Path(held.resolved.canonical_path).name != "bash":
            held.close()
            raise CommandError("bash_path_mismatch")
        try:
            require_destructive_grant(request, grant, held.resolved)
        except CommandError:
            held.close()
            raise
        held.inner_rest = (*SHELL_FLAG_PREFIX, request.shell_command or "")
        return held
    held = resolve_held(request.argv[0], trusted_path=trusted_path, path_roots=path_roots)
    try:
        require_destructive_grant(request, grant, held.script or held.resolved)
        if held.script is not None:
            # A script is arbitrary code even if its filename is innocuous;
            # also classify its held interpreter independently.
            require_destructive_grant(request, grant, held.resolved)
        for item in request.argv[1:]:
            if item.startswith("/proc/") or item.startswith("/sys/") or item.startswith("/dev/"):
                raise CommandError("forbidden_path")
            if "docker.sock" in item:
                raise CommandError("forbidden_path")
    except CommandError:
        held.close()
        raise
    held.inner_rest = request.argv[1:]
    return held


def resolve_bwrap() -> HeldExecutable:
    return resolve_root_helper(BWRAP_EXECUTABLE)


def _canonical_path_root(directory: str) -> str:
    try:
        st = os.lstat(directory)
    except OSError as exc:
        raise CommandError("untrusted_path_root") from exc
    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(directory)
        except OSError as exc:
            raise CommandError("untrusted_path_root") from exc
        dest = target if target.startswith("/") else str(Path(directory).parent / target)
        dest = _lexical_normalize(dest)
        try:
            dest_st = os.lstat(dest)
        except OSError as exc:
            raise CommandError("untrusted_path_root") from exc
        if stat.S_ISLNK(dest_st.st_mode) or not stat.S_ISDIR(dest_st.st_mode):
            raise CommandError("untrusted_path_root")
        return dest
    if not stat.S_ISDIR(st.st_mode):
        raise CommandError("untrusted_path_root")
    return directory


def attest_trusted_path(contract: TrustedPathContract) -> tuple[PathRoot, ...]:
    roots: list[PathRoot] = []
    seen: set[tuple[int, int]] = set()
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    for directory in contract.directories:
        named = _canonical_path_root(directory)
        try:
            fd = os.open(named, flags)
        except OSError as exc:
            raise CommandError("untrusted_path_root") from exc
        keep_fd = False
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                raise CommandError("untrusted_path_root")
            mode = stat.S_IMODE(st.st_mode)
            if mode & 0o022:
                raise CommandError("untrusted_path_root")
            ident = (int(st.st_dev), int(st.st_ino))
            if ident in seen:
                continue
            seen.add(ident)
            roots.append(
                PathRoot(
                    path=named,
                    owner_uid=int(st.st_uid),
                    owner_gid=int(st.st_gid),
                    mode=int(st.st_mode),
                    device=int(st.st_dev),
                    inode=int(st.st_ino),
                    mtime_ns=int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
                    dir_fd=fd,
                )
            )
            keep_fd = True
        finally:
            if not keep_fd:
                os.close(fd)
    return tuple(roots)


def confirm_path_roots(roots: tuple[PathRoot, ...]) -> None:
    for root in roots:
        try:
            st = os.fstat(root.dir_fd)
        except OSError as exc:
            raise CommandError("untrusted_path_root") from exc
        if (
            not stat.S_ISDIR(st.st_mode)
            or int(st.st_uid) != root.owner_uid
            or int(st.st_gid) != root.owner_gid
            or int(st.st_mode) != root.mode
            or int(st.st_dev) != root.device
            or int(st.st_ino) != root.inode
            or int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))) != root.mtime_ns
        ):
            raise CommandError("untrusted_path_root")
