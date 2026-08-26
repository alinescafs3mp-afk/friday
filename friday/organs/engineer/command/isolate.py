"""Native bwrap/cgroup boundary for isolated_workspace. host_user is not in-process."""

from __future__ import annotations

import os
from pathlib import Path

from .contracts import (
    BWRAP_BLOCK_FD,
    BWRAP_EXEC_FD,
    BWRAP_EXECUTABLE,
    BWRAP_EXPORT_FD,
    BWRAP_SCRIPT_FD,
    SANDBOX_EXPORT,
    SANDBOX_HOST_OUTPUT,
    SANDBOX_JOB,
    SANDBOX_SCRIPT,
    CommandError,
    HeldExecutable,
    IsolationProfile,
    PathRoot,
    ResourceLimits,
)
from .workspace import JobWorkspace

OUTPUT_EXPORT_SCRIPT = b"""#!/usr/bin/bash
set -u
argv0=$1
shift
set +e
/usr/bin/bash -c 'exec -a "$1" "${@:2}"' _ "$argv0" "$@"
status=$?
/usr/bin/cp -a /job/output/. /job/host-output/
copy=$?
if [ "$copy" -ne 0 ]; then
  exit 125
fi
exit "$status"
"""


def extra_ro_binds(roots: tuple[PathRoot, ...]) -> tuple[tuple[str, str], ...]:
    binds: list[tuple[str, str]] = []
    covered = ("/usr", "/bin", "/lib", "/lib64")
    for root in roots:
        directory = root.path
        if directory in covered or directory.startswith("/usr/") or directory in {"/usr", "/bin"}:
            continue
        binds.append((directory, directory))
    return tuple(binds)


def bwrap_argv(
    *,
    workspace: JobWorkspace,
    held: HeldExecutable,
    env: dict[str, str],
    extra_binds: tuple[tuple[str, str], ...],
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
            "--bind",
            str(workspace.output),
            SANDBOX_HOST_OUTPUT,
            "--size",
            str(int(limits.tmpfs_job_tmp)),
            "--tmpfs",
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
    command.extend(["--", SANDBOX_EXPORT, held.resolved.canonical_path])
    if held.script_fd is not None:
        command.extend([exec_dest, SANDBOX_SCRIPT, *held.inner_rest])
    else:
        command.extend([exec_dest, *held.inner_rest])
    return command


def require_profile(profile: IsolationProfile) -> None:
    if profile is IsolationProfile.HOST_USER:
        raise CommandError("host_user_requires_broker")
    if profile is not IsolationProfile.ISOLATED_WORKSPACE:
        raise CommandError("invalid_isolation_profile")
