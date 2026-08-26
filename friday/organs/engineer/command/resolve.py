"""Resolve and hold attested executable FDs. Do not re-open by pathname at spawn."""

from __future__ import annotations

import errno
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
    ResolvedExecutable,
    TrustedPathContract,
    VerifiedCommandGrant,
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


def attest_open_fd(fd: int, *, named: str, expected: ResolvedExecutable | None = None) -> ResolvedExecutable:
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
    try:
        st = os.fstat(fd)
    except OSError as exc:
        raise CommandError("identity_changed") from exc
    if (
        int(st.st_dev) != expected.device
        or int(st.st_ino) != expected.inode
        or int(st.st_uid) != expected.owner_uid
        or int(st.st_gid) != expected.owner_gid
        or int(st.st_mode) != expected.mode
        or int(st.st_size) != expected.size_bytes
    ):
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


def resolve_named(
    path: str,
    *,
    trusted_path: TrustedPathContract | None = None,
    depth: int = 0,
    expected: ResolvedExecutable | None = None,
    hold: bool = False,
) -> ResolvedExecutable | tuple[ResolvedExecutable, int, str | None]:
    contract = trusted_path or TrustedPathContract.default()
    _reject_traversal(path)
    named = path if path.startswith("/") else _lookup_relative(path, contract)
    named = _one_hop_alias(named)
    if _is_forbidden(named):
        raise CommandError("forbidden_path")
    fd = _open_named_nofollow(named)
    try:
        resolved = attest_open_fd(fd, named=named, expected=expected)
        shebang = _read_shebang(fd)
        if shebang:
            if depth >= _INTERPRETER_DEPTH:
                raise CommandError("nested_script_refused")
            if not shebang.startswith("/"):
                raise CommandError("relative_interpreter")
            interp_held = resolve_held(shebang, trusted_path=contract, depth=depth + 1)
            interp_held.close()
        if hold:
            return resolved, fd, shebang
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
) -> HeldExecutable:
    contract = trusted_path or TrustedPathContract.default()
    resolved, fd, shebang = resolve_named(path, trusted_path=contract, depth=depth, hold=True)  # type: ignore[misc]
    assert isinstance(resolved, ResolvedExecutable)
    if not shebang:
        os.set_inheritable(fd, True)
        return HeldExecutable(resolved=resolved, executable_fd=fd)
    if depth >= _INTERPRETER_DEPTH:
        os.close(fd)
        raise CommandError("nested_script_refused")
    try:
        interpreter = resolve_held(shebang, trusted_path=contract, depth=depth + 1)
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
    except Exception:
        os.close(fd)
        raise
    os.set_inheritable(fd, True)
    return HeldExecutable(resolved=resolved, executable_fd=fd)


def _shell_requires_confirmation(command: str) -> bool:
    lowered = command.lower()
    return any(marker in lowered for marker in _DANGEROUS_SHELL_MARKERS)


def require_destructive_grant(
    request: CommandRequest,
    grant: VerifiedCommandGrant,
    resolved: ResolvedExecutable,
) -> None:
    basename = Path(resolved.canonical_path).name
    needs = basename in DESTRUCTIVE_BASENAMES
    if (
        request.lane is CommandLane.SHELL
        and request.shell_command
        and _shell_requires_confirmation(request.shell_command)
    ):
        needs = True
    if needs and not grant.destructive_confirmed:
        raise CommandError("destructive_confirmation_required")


def resolve_request(
    request: CommandRequest,
    grant: VerifiedCommandGrant,
    *,
    trusted_path: TrustedPathContract,
) -> HeldExecutable:
    if request.lane is CommandLane.SHELL:
        held = resolve_held(BASH_EXECUTABLE, trusted_path=trusted_path)
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
    held = resolve_held(request.argv[0], trusted_path=trusted_path)
    try:
        require_destructive_grant(request, grant, held.script or held.resolved)
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
