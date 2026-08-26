"""Native bwrap/cgroup boundary for isolated_workspace. host_user is not isolated."""

from __future__ import annotations

import os
from pathlib import Path

from .contracts import (
    BWRAP_EXECUTABLE,
    SANDBOX_JOB,
    SANDBOX_SCRIPT,
    CommandError,
    HeldExecutable,
    IsolationProfile,
    TrustedPathContract,
)
from .workspace import JobWorkspace


def current_cgroup_dir() -> Path | None:
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return None
    relative = ""
    for line in lines:
        if line.startswith("0::"):
            relative = line[3:]
            break
    if not relative.startswith("/") or ".." in relative.split("/"):
        return None
    path = Path("/sys/fs/cgroup") / relative.lstrip("/")
    if not path.is_dir():
        return None
    return path


def create_job_cgroup(job_id: str) -> Path | None:
    parent = current_cgroup_dir()
    if parent is None:
        return None
    path = parent / f"ecmd-{job_id[:16]}"
    try:
        path.mkdir(mode=0o700, exist_ok=False)
    except OSError:
        return None
    return path


def move_pid(cgroup: Path, pid: int) -> None:
    try:
        (cgroup / "cgroup.procs").write_text(str(int(pid)), encoding="ascii")
    except OSError as exc:
        raise CommandError("cgroup_move_failed") from exc


def cgroup_populated(cgroup: Path) -> bool | None:
    try:
        raw = (cgroup / "cgroup.events").read_text(encoding="ascii")
    except OSError:
        return None
    for line in raw.splitlines():
        if line.startswith("populated "):
            value = line.split(" ", 1)[1]
            if value == "0":
                return False
            if value == "1":
                return True
            return None
    return None


def cgroup_pids(cgroup: Path) -> list[int]:
    try:
        raw = (cgroup / "cgroup.procs").read_text(encoding="ascii")
    except OSError:
        return []
    pids: list[int] = []
    for line in raw.splitlines():
        if line.isdigit():
            pids.append(int(line))
    return pids


def remove_cgroup(cgroup: Path | None) -> None:
    if cgroup is None:
        return
    try:
        cgroup.rmdir()
    except OSError:
        return


def extra_ro_binds(trusted_path: TrustedPathContract) -> tuple[tuple[str, str], ...]:
    binds: list[tuple[str, str]] = []
    covered = ("/usr", "/bin", "/lib", "/lib64")
    for directory in trusted_path.directories:
        if directory in covered or directory.startswith("/usr/") or directory in {"/usr", "/bin"}:
            continue
        if not os.path.isdir(directory):
            continue
        binds.append((directory, directory))
    return tuple(binds)


def bwrap_argv(
    *,
    workspace: JobWorkspace,
    held: HeldExecutable,
    env: dict[str, str],
    extra_binds: tuple[tuple[str, str], ...],
) -> list[str]:
    command = [
        BWRAP_EXECUTABLE,
        "--unshare-all",
        "--unshare-user",
        "--uid",
        str(os.geteuid()),
        "--gid",
        str(os.getegid()),
        "--cap-drop",
        "ALL",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--dir",
        "/run",
        "--dir",
        "/run/friday",
        "--dir",
        "/run/friday/bin",
    ]
    exec_name = Path(held.resolved.canonical_path).name
    if not exec_name or "/" in exec_name or exec_name in {".", ".."}:
        raise CommandError("invalid_executable")
    exec_dest = f"/run/friday/bin/{exec_name}"
    # Snapshot held FDs into the sandbox. --ro-bind-fd looks up /proc/self/fd after
    # unshare and fails once the original directory entry is replaced.
    command.extend(["--perms", "0755", "--ro-bind-data", str(held.executable_fd), exec_dest])
    if held.script_fd is not None:
        command.extend(["--perms", "0755", "--ro-bind-data", str(held.script_fd), SANDBOX_SCRIPT])
    command.extend(
        [
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind-try",
        "/lib",
        "/lib",
        "--ro-bind-try",
        "/lib64",
        "/lib64",
        "--dir",
        "/etc",
        "--ro-bind-try",
        "/etc/ld.so.cache",
        "/etc/ld.so.cache",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        SANDBOX_JOB,
        "--bind",
        str(workspace.home),
        f"{SANDBOX_JOB}/workspace",
        "--bind",
        str(workspace.output),
        f"{SANDBOX_JOB}/output",
        "--bind",
        str(workspace.tmp),
        f"{SANDBOX_JOB}/tmp",
        "--chdir",
        SANDBOX_JOB,
        ]
    )
    for host, dest in extra_binds:
        command.extend(["--ro-bind", host, dest])
    command.extend(["--clearenv"])
    for key in ("HOME", "LANG", "LC_ALL", "PATH", "PWD", "TMPDIR", "TZ"):
        command.extend(["--setenv", key, env[key]])
    command.extend(["--argv0", held.resolved.canonical_path, "--"])
    if held.script_fd is not None:
        command.extend([exec_dest, SANDBOX_SCRIPT, *held.inner_rest])
    else:
        command.extend([exec_dest, *held.inner_rest])
    return command


def host_user_argv(held: HeldExecutable) -> list[str]:
    argv0 = held.resolved.canonical_path
    if held.script_fd is not None:
        return [argv0, f"/proc/self/fd/{held.script_fd}", *held.inner_rest]
    return [argv0, *held.inner_rest]


def pass_fds_for(held: HeldExecutable, *, bwrap_fd: int | None, extra: tuple[int, ...] = ()) -> tuple[int, ...]:
    fds = [held.executable_fd, *extra]
    if held.script_fd is not None:
        fds.append(held.script_fd)
    if bwrap_fd is not None:
        fds.append(bwrap_fd)
    return tuple(sorted(set(fd for fd in fds if fd is not None and fd >= 0)))


def require_profile(profile: IsolationProfile, host_user_authorized: bool) -> None:
    if profile is IsolationProfile.HOST_USER and not host_user_authorized:
        raise CommandError("host_user_authorization_required")
    if profile is IsolationProfile.ISOLATED_WORKSPACE and host_user_authorized:
        raise CommandError("invalid_isolation_profile")
