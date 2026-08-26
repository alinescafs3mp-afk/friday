#!/usr/bin/python3
"""Create Host Control user assets without ever running path writes as root."""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import secrets
import stat
from pathlib import Path

_KEY = re.compile(rb"[0-9a-f]{48}")
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_DIRECTORY | os.O_NOFOLLOW


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prepare_user_assets.py")
    parser.add_argument("--home", required=True)
    parser.add_argument("--data-dir", required=True)
    return parser


def _open_owned_root(value: str, *, label: str) -> tuple[Path, int]:
    path = Path(value)
    if not path.is_absolute() or "\x00" in str(path):
        raise ValueError(f"{label} must be absolute")
    if path.is_symlink() or path.resolve(strict=True) != path:
        raise ValueError(f"{label} must be canonical")
    descriptor = os.open(path, _DIRECTORY_FLAGS)
    observed = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        os.close(descriptor)
        raise ValueError(f"{label} has unsafe ownership or permissions")
    return path, descriptor


def _ensure_chain(root_descriptor: int, components: tuple[tuple[str, int | None], ...]) -> int:
    """Return an fd for a no-symlink user-owned directory chain."""

    current = os.dup(root_descriptor)
    try:
        for name, exact_mode in components:
            if not name or "/" in name or name in {".", ".."}:
                raise ValueError("unsafe user asset component")
            create_mode = exact_mode if exact_mode is not None else 0o700
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, create_mode, dir_fd=current)
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
            observed = os.fstat(current)
            if (
                not stat.S_ISDIR(observed.st_mode)
                or observed.st_uid != os.geteuid()
                or observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise ValueError("user asset directory has unsafe metadata")
            if exact_mode is not None:
                os.fchmod(current, exact_mode)
                after = os.fstat(current)
                if (
                    after.st_uid != os.geteuid()
                    or after.st_gid != os.getegid()
                    or stat.S_IMODE(after.st_mode) != exact_mode
                ):
                    raise ValueError("user asset directory mode could not be sealed")
        result = current
        current = -1
        return result
    finally:
        if current >= 0:
            os.close(current)


def _ensure_agent_key(config_descriptor: int) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    try:
        descriptor = os.open("agent.key", flags, dir_fd=config_descriptor)
    except FileNotFoundError:
        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
        created = os.open("agent.key", create_flags, 0o600, dir_fd=config_descriptor)
        try:
            payload = secrets.token_hex(24).encode("ascii")
            if os.write(created, payload) != len(payload):
                raise OSError("short write while creating agent key")
            os.fsync(created)
        finally:
            os.close(created)
        descriptor = os.open("agent.key", flags, dir_fd=config_descriptor)
    try:
        observed = os.fstat(descriptor)
        payload = os.read(descriptor, 65)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or observed.st_uid != os.geteuid()
        or observed.st_gid != os.getegid()
        or stat.S_IMODE(observed.st_mode) != 0o600
        or observed.st_size != 48
        or after.st_dev != observed.st_dev
        or after.st_ino != observed.st_ino
        or after.st_size != observed.st_size
        or after.st_mtime_ns != observed.st_mtime_ns
        or _KEY.fullmatch(payload) is None
    ):
        raise ValueError("agent key has unsafe metadata or content")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.geteuid() == 0:
        raise SystemExit("prepare_user_assets.py must run as the selected non-root user")
    previous_umask = os.umask(0o077)
    home_descriptor = -1
    data_descriptor = -1
    config_descriptor = -1
    state_descriptor = -1
    jobs_descriptor = -1
    try:
        _home, home_descriptor = _open_owned_root(args.home, label="selected user home")
        _data, data_descriptor = _open_owned_root(args.data_dir, label="Friday data directory")
        config_descriptor = _ensure_chain(
            home_descriptor,
            ((".config", None), ("friday-host-agent", 0o700)),
        )
        state_descriptor = _ensure_chain(
            home_descriptor,
            ((".local", None), ("state", None), ("friday-host-agent", 0o700)),
        )
        jobs_descriptor = _ensure_chain(
            data_descriptor,
            (("host-control", 0o700), ("jobs", 0o700)),
        )
        _ensure_agent_key(config_descriptor)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"user asset preparation rejected: {exc}") from None
    finally:
        for descriptor in (
            jobs_descriptor,
            state_descriptor,
            config_descriptor,
            data_descriptor,
            home_descriptor,
        ):
            if descriptor >= 0:
                os.close(descriptor)
        os.umask(previous_umask)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
