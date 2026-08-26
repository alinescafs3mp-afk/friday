"""Resolve and re-attest executables without following the named path."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from .contracts import (
    BASH_EXECUTABLE,
    DESTRUCTIVE_BASENAMES,
    FORBIDDEN_EXACT_PATHS,
    FORBIDDEN_PATH_PREFIXES,
    SHELL_ARGV_PREFIX,
    TRUSTED_PATH,
    CommandError,
    CommandLane,
    CommandRequest,
    ResolvedExecutable,
    VerifiedCommandGrant,
    sha256_bytes,
)

_MAX_SHEBANG = 4096
_INTERPRETER_DEPTH = 1
_DANGEROUS_SHELL_MARKERS = (
    "sudo",
    "doas",
    "pkexec",
    "machinectl",
    "nsenter",
    "unshare",
    "chroot",
    "docker",
    "podman",
    "nerdctl",
    "chmod 777",
    "rm -rf /",
    "rm -fr /",
    "/var/run/docker.sock",
    "/run/docker.sock",
)


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
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > 64 * 1024 * 1024:
            raise CommandError("executable_too_large")
        chunks.append(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return sha256_bytes(b"".join(chunks))


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


def _attest_fd(fd: int, *, named: str, expected: ResolvedExecutable | None = None) -> ResolvedExecutable:
    try:
        st = os.fstat(fd)
        named_st = os.lstat(named)
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
        (st.st_uid == euid and mode & 0o100)
        or (st.st_gid == egid and mode & 0o010)
        or (mode & 0o001)
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


def _lookup_relative(name: str) -> str:
    if "/" in name or name in {".", ".."} or name.startswith("-"):
        raise CommandError("relative_name_invalid")
    for directory in TRUSTED_PATH:
        candidate = f"{directory.rstrip('/')}/{name}"
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


def resolve_named(path: str, *, depth: int = 0, expected: ResolvedExecutable | None = None) -> ResolvedExecutable:
    _reject_traversal(path)
    named = path if path.startswith("/") else _lookup_relative(path)
    named = _one_hop_alias(named)
    if _is_forbidden(named):
        raise CommandError("forbidden_path")
    fd = _open_named_nofollow(named)
    try:
        resolved = _attest_fd(fd, named=named, expected=expected)
        shebang = _read_shebang(fd)
    finally:
        os.close(fd)
    if shebang:
        if depth >= _INTERPRETER_DEPTH:
            raise CommandError("nested_script_refused")
        if not shebang.startswith("/"):
            raise CommandError("relative_interpreter")
        resolve_named(shebang, depth=depth + 1)
    return resolved


def _shell_requires_confirmation(command: str) -> bool:
    lowered = command.lower()
    return any(marker in lowered for marker in _DANGEROUS_SHELL_MARKERS)


def require_destructive_grant(request: CommandRequest, grant: VerifiedCommandGrant, resolved: ResolvedExecutable) -> None:
    basename = Path(resolved.canonical_path).name
    if basename in DESTRUCTIVE_BASENAMES and not grant.destructive_confirmed:
        raise CommandError("destructive_confirmation_required")
    if (
        request.lane is CommandLane.SHELL
        and request.shell_command
        and _shell_requires_confirmation(request.shell_command)
        and not grant.destructive_confirmed
    ):
        raise CommandError("destructive_confirmation_required")


def resolve_request(request: CommandRequest, grant: VerifiedCommandGrant) -> tuple[tuple[str, ...], ResolvedExecutable]:
    if request.lane is CommandLane.SHELL:
        resolved = resolve_named(BASH_EXECUTABLE)
        if Path(resolved.canonical_path).name != "bash":
            raise CommandError("bash_path_mismatch")
        argv = (*SHELL_ARGV_PREFIX, request.shell_command or "")
        require_destructive_grant(request, grant, resolved)
        return argv, resolved
    resolved = resolve_named(request.argv[0])
    require_destructive_grant(request, grant, resolved)
    return (resolved.canonical_path, *request.argv[1:]), resolved


def reopen_and_confirm(resolved: ResolvedExecutable) -> None:
    fd = _open_named_nofollow(resolved.canonical_path)
    try:
        _attest_fd(fd, named=resolved.canonical_path, expected=resolved)
    finally:
        os.close(fd)
