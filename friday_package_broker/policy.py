"""Root-owned package-broker policy with default-deny package admission."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import AptTransaction, BrokerContractError, PackageAction, PackageRef

_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,127}$")
_BROKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True, slots=True)
class BrokerPolicy:
    broker_id: str
    allowed_peer_uids: frozenset[int]
    allowed_packages: frozenset[str]
    max_package_changes: int = 64
    max_download_bytes: int = 2 * 1024 * 1024 * 1024
    max_installed_delta_bytes: int = 4 * 1024 * 1024 * 1024
    plan_ttl_sec: int = 900

    def __post_init__(self) -> None:
        if _BROKER_ID.fullmatch(self.broker_id) is None:
            raise BrokerContractError("broker policy identity is invalid")
        if not self.allowed_peer_uids or any(
            isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 2**31 - 1
            for item in self.allowed_peer_uids
        ):
            raise BrokerContractError("broker peer uid allowlist is invalid")
        if (
            not self.allowed_packages
            or len(self.allowed_packages) > 128
            or any(_PACKAGE_NAME.fullmatch(item) is None for item in self.allowed_packages)
        ):
            raise BrokerContractError("broker package allowlist is invalid")
        if isinstance(self.max_package_changes, bool) or not 1 <= self.max_package_changes <= 256:
            raise BrokerContractError("broker package-change limit is invalid")
        if (
            isinstance(self.max_download_bytes, bool)
            or not 0 <= self.max_download_bytes <= 8 * 1024 * 1024 * 1024
        ):
            raise BrokerContractError("broker download limit is invalid")
        if (
            isinstance(self.max_installed_delta_bytes, bool)
            or not 0 <= self.max_installed_delta_bytes <= 16 * 1024 * 1024 * 1024
        ):
            raise BrokerContractError("broker disk limit is invalid")
        if isinstance(self.plan_ttl_sec, bool) or not 60 <= self.plan_ttl_sec <= 3600:
            raise BrokerContractError("broker plan TTL is invalid")

    def authorize(self, transaction: AptTransaction) -> None:
        self.authorize_requested(transaction.requested)
        if len(transaction.changes) > self.max_package_changes:
            raise BrokerContractError("APT transaction exceeds package-change limit")
        if transaction.download_bytes > self.max_download_bytes:
            raise BrokerContractError("APT transaction exceeds download limit")
        if transaction.installed_delta_bytes > self.max_installed_delta_bytes:
            raise BrokerContractError("APT transaction exceeds disk limit")
        if any(
            item.action in {PackageAction.REMOVE, PackageAction.DOWNGRADE} for item in transaction.changes
        ):
            raise BrokerContractError("initial broker forbids removals and downgrades")
        for change in transaction.changes:
            if change.action is PackageAction.REMOVE:
                continue
            if not change.origins or any(origin.trusted is not True for origin in change.origins):
                raise BrokerContractError("APT candidate origin is not fully authenticated")

    def authorize_requested(self, requested: tuple[PackageRef, ...]) -> None:
        if (
            not requested
            or len(requested) > 16
            or any(not isinstance(item, PackageRef) for item in requested)
        ):
            raise BrokerContractError("requested package set is invalid")
        names = {item.name for item in requested}
        if len(names) != len(requested) or not names.issubset(self.allowed_packages):
            raise BrokerContractError("requested package is outside broker policy")


def _closed_policy(value: Any) -> BrokerPolicy:
    expected = {
        "broker_id",
        "allowed_peer_uids",
        "allowed_packages",
        "max_package_changes",
        "max_download_bytes",
        "max_installed_delta_bytes",
        "plan_ttl_sec",
    }
    if not isinstance(value, dict) or set(value) - expected:
        raise BrokerContractError("broker policy fields are invalid")
    peers = value.get("allowed_peer_uids")
    packages = value.get("allowed_packages")
    if not isinstance(peers, list) or not isinstance(packages, list):
        raise BrokerContractError("broker policy allowlists are invalid")
    try:
        return BrokerPolicy(
            broker_id=value.get("broker_id", "local-package-broker"),
            allowed_peer_uids=frozenset(peers),
            allowed_packages=frozenset(packages),
            max_package_changes=value.get("max_package_changes", 64),
            max_download_bytes=value.get("max_download_bytes", 2 * 1024 * 1024 * 1024),
            max_installed_delta_bytes=value.get("max_installed_delta_bytes", 4 * 1024 * 1024 * 1024),
            plan_ttl_sec=value.get("plan_ttl_sec", 900),
        )
    except TypeError as exc:
        raise BrokerContractError("broker policy field types are invalid") from exc


def load_broker_policy(path: str | Path, *, require_root_owner: bool = True) -> BrokerPolicy:
    """Read one exact non-symlink TOML policy with a small hard byte limit."""

    selected = Path(path)
    descriptor = -1
    try:
        descriptor = os.open(selected, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise BrokerContractError("broker policy is not a private regular file")
        if require_root_owner and observed.st_uid != 0:
            raise BrokerContractError("broker policy must be root-owned")
        if observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or observed.st_size > 64 * 1024:
            raise BrokerContractError("broker policy permissions/size are unsafe")
        payload = os.read(descriptor, 64 * 1024 + 1)
    except OSError as exc:
        raise BrokerContractError("broker policy could not be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > 64 * 1024:
        raise BrokerContractError("broker policy is oversized")
    try:
        decoded = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise BrokerContractError("broker policy TOML is invalid") from exc
    if set(decoded) != {"broker"}:
        raise BrokerContractError("broker policy requires one [broker] table")
    return _closed_policy(decoded["broker"])


__all__ = ["BrokerPolicy", "load_broker_policy"]
