"""Root-owned network authority for the native host-agent boundary."""

from __future__ import annotations

import ipaddress
import os
import stat
import tomllib
from pathlib import Path
from typing import Any

from friday.host_control.policy import NetworkPolicy

_MAX_POLICY_BYTES = 16 * 1024
_ROOT_UID = 0


def load_agent_network_policy(path: str | Path) -> NetworkPolicy:
    """Load one exact root-owned policy without trusting backend configuration."""

    selected = Path(path)
    if not selected.is_absolute() or "\x00" in str(selected):
        raise ValueError("host-agent network policy path must be absolute")
    try:
        if selected.is_symlink() or selected.resolve(strict=True) != selected:
            raise ValueError("host-agent network policy path must be canonical")
        parent = selected.parent
        parent_details = parent.stat()
        if (
            parent.resolve(strict=True) != parent
            or not stat.S_ISDIR(parent_details.st_mode)
            or parent_details.st_uid != _ROOT_UID
            or parent_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ValueError("host-agent network policy directory is not root-controlled")
    except OSError as exc:
        raise ValueError("host-agent network policy path is unavailable") from exc

    descriptor = -1
    try:
        descriptor = os.open(
            selected,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != _ROOT_UID
            or before.st_nlink != 1
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or not 1 <= before.st_size <= _MAX_POLICY_BYTES
        ):
            raise ValueError("host-agent network policy metadata is unsafe")
        payload = os.read(descriptor, _MAX_POLICY_BYTES + 1)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise ValueError("host-agent network policy could not be opened safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) != before.st_size or _file_identity(before) != _file_identity(after):
        raise ValueError("host-agent network policy changed while being read")
    try:
        decoded = tomllib.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("host-agent network policy is invalid TOML") from exc
    return _parse_policy(decoded)


def _file_identity(observed: os.stat_result) -> tuple[int, ...]:
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_uid,
        observed.st_gid,
        observed.st_nlink,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _parse_policy(value: Any) -> NetworkPolicy:
    if not isinstance(value, dict) or set(value) != {"network"}:
        raise ValueError("host-agent policy must contain only the network table")
    network = value["network"]
    if not isinstance(network, dict) or set(network) != {
        "allow_public",
        "allowed_cidrs",
        "schema_version",
    }:
        raise ValueError("host-agent network policy fields are invalid")
    if network["schema_version"] != 1 or isinstance(network["schema_version"], bool):
        raise ValueError("host-agent network policy schema is unsupported")
    allow_public = network["allow_public"]
    raw_cidrs = network["allowed_cidrs"]
    if not isinstance(allow_public, bool) or not isinstance(raw_cidrs, list):
        raise ValueError("host-agent network policy types are invalid")
    if len(raw_cidrs) > 32 or any(not isinstance(item, str) for item in raw_cidrs):
        raise ValueError("host-agent allowed CIDR set is invalid")

    parsed: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    canonical: list[str] = []
    for raw in raw_cidrs:
        try:
            candidate = ipaddress.ip_network(raw, strict=True)
        except ValueError as exc:
            raise ValueError("host-agent allowed CIDR is invalid") from exc
        if str(candidate) != raw or candidate.prefixlen == 0:
            raise ValueError("host-agent allowed CIDR is not exact and canonical")
        if candidate.is_global and not allow_public:
            raise ValueError("host-agent public CIDR requires allow_public")
        if any(candidate.version == existing.version and candidate.overlaps(existing) for existing in parsed):
            raise ValueError("host-agent allowed CIDRs overlap")
        parsed.append(candidate)
        canonical.append(raw)
    return NetworkPolicy(
        connected_cidrs=(),
        allowed_cidrs=tuple(canonical),
        allow_public=allow_public,
    )


__all__ = ["load_agent_network_policy"]
