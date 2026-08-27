"""Native bwrap construction for isolated_workspace and closed profile validation."""

from __future__ import annotations

import os
from pathlib import Path

from .contracts import (
    BWRAP_BLOCK_FD,
    BWRAP_EXEC_FD,
    BWRAP_EXECUTABLE,
    BWRAP_EXPORT_FD,
    BWRAP_EXPORT_IMPL_FD,
    BWRAP_PATH_ROOT_FD_BASE,
    BWRAP_SCRIPT_FD,
    BWRAP_STDIN_PAYLOAD_FD,
    SANDBOX_EXPORT,
    SANDBOX_EXPORT_IMPL,
    SANDBOX_JOB,
    SANDBOX_SCRIPT,
    SANDBOX_STDIN,
    CommandError,
    HeldExecutable,
    IsolationProfile,
    PathRoot,
    ResourceLimits,
)
from .workspace import JobWorkspace

OUTPUT_EXPORT_SCRIPT = b"""#!/usr/bin/bash
set -eu
exec 29<&0
exec 0</run/friday/stdin
exec /usr/bin/python3 /run/friday/export-impl.py "$@"
"""
OUTPUT_EXPORT_IMPL = Path(__file__).with_name("export_helper.py").read_bytes()


def extra_ro_binds(roots: tuple[PathRoot, ...]) -> tuple[tuple[int, str], ...]:
    # Bind every attested PATH root from its still-open descriptor. A pathname
    # rename between admission and bwrap mount setup cannot redirect the bind.
    return tuple((BWRAP_PATH_ROOT_FD_BASE + index, root.path) for index, root in enumerate(roots))


def bwrap_argv(
    *,
    workspace: JobWorkspace,
    held: HeldExecutable,
    env: dict[str, str],
    extra_binds: tuple[tuple[int, str], ...],
    limits: ResourceLimits,
    sync_fd: int | None = None,
) -> list[str]:
    command = [
        BWRAP_EXECUTABLE,
    ]
    if sync_fd is not None:
        command.extend(["--block-fd", str(BWRAP_BLOCK_FD)])
    command.extend(
        [
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
    )
    exec_name = Path(held.resolved.canonical_path).name
    if not exec_name or "/" in exec_name or exec_name in {".", ".."}:
        raise CommandError("invalid_executable")
    exec_dest = f"/run/friday/bin/{exec_name}"
    command.extend(["--perms", "0755", "--ro-bind-data", str(BWRAP_EXEC_FD), exec_dest])
    if held.script_fd is not None:
        command.extend(["--perms", "0755", "--ro-bind-data", str(BWRAP_SCRIPT_FD), SANDBOX_SCRIPT])
    command.extend(
        [
            "--perms",
            "0755",
            "--ro-bind-data",
            str(BWRAP_EXPORT_FD),
            SANDBOX_EXPORT,
            "--perms",
            "0400",
            "--ro-bind-data",
            str(BWRAP_EXPORT_IMPL_FD),
            SANDBOX_EXPORT_IMPL,
            "--perms",
            "0400",
            "--ro-bind-data",
            str(BWRAP_STDIN_PAYLOAD_FD),
            SANDBOX_STDIN,
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
            "--size",
            str(int(limits.tmpfs_tmp)),
            "--tmpfs",
            "/tmp",
            "--dir",
            SANDBOX_JOB,
            "--size",
            str(int(limits.tmpfs_workspace)),
            "--tmpfs",
            f"{SANDBOX_JOB}/workspace",
            "--size",
            str(int(limits.output_bytes)),
            "--tmpfs",
            f"{SANDBOX_JOB}/output",
            "--size",
            str(int(limits.tmpfs_job_tmp)),
            "--tmpfs",
            f"{SANDBOX_JOB}/tmp",
            "--chdir",
            SANDBOX_JOB,
        ]
    )
    for source_fd, dest in extra_binds:
        command.extend(["--ro-bind-fd", str(source_fd), dest])
    command.extend(["--clearenv"])
    for key in ("HOME", "LANG", "LC_ALL", "PATH", "PWD", "TMPDIR", "TZ"):
        command.extend(["--setenv", key, env[key]])
    command.extend(["--", SANDBOX_EXPORT, held.resolved.canonical_path])
    if held.script_fd is not None:
        command.extend([exec_dest, SANDBOX_SCRIPT, *held.inner_rest])
    else:
        command.extend([exec_dest, *held.inner_rest])
    return command


def require_profile(profile: IsolationProfile) -> None:
    if profile not in {IsolationProfile.ISOLATED_WORKSPACE, IsolationProfile.HOST_USER}:
        raise CommandError("invalid_isolation_profile")
