"""Package-backed inventory over adapter-owned absolute executable paths."""

from __future__ import annotations

import stat
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol

from friday.host_control.adapters.base import AdapterSpec
from friday.host_control.contracts import ExecutableAttestation

from .executable_attestation import (
    ExecutableAttestationError,
    attest_executable,
    open_verified_executable,
    verify_executable,
)


@dataclass(frozen=True, slots=True)
class PackageIdentity:
    name: str
    version: str
    architecture: str


class PackageResolver(Protocol):
    def resolve(self, path: str) -> PackageIdentity | None: ...


class DpkgPackageResolver:
    """Read package ownership through a fixed dpkg-query executable, never PATH."""

    def __init__(self, executable: str = "/usr/bin/dpkg-query") -> None:
        path = Path(executable)
        try:
            observed = path.lstat()
        except OSError as exc:
            raise ValueError("dpkg-query is unavailable") from exc
        if (
            not path.is_absolute()
            or not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != 0
            or observed.st_mode & 0o022
            or not observed.st_mode & 0o111
            or str(path.resolve(strict=True)) != str(path)
        ):
            raise ValueError("dpkg-query must be a fixed trusted root executable")
        self._executable = executable

    def resolve(self, path: str) -> PackageIdentity | None:
        try:
            owner = subprocess.run(  # noqa: S603 - fixed executable and literal argv
                [self._executable, "-S", path],
                executable=self._executable,
                env={"LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if owner.returncode != 0 or len(owner.stdout) > 4096:
            return None
        package = owner.stdout.decode("utf-8", errors="replace").split(": ", 1)[0].strip()
        if not package:
            return None
        try:
            details = subprocess.run(  # noqa: S603 - fixed executable and literal argv
                [
                    self._executable,
                    "-W",
                    "-f=${binary:Package}\t${Version}\t${Architecture}\n",
                    package,
                ],
                executable=self._executable,
                env={"LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if details.returncode != 0 or len(details.stdout) > 4096:
            return None
        fields = details.stdout.decode("utf-8", errors="replace").strip().split("\t")
        if len(fields) != 3 or not all(fields):
            return None
        return PackageIdentity(fields[0].split(":", 1)[0], fields[1], fields[2])


VersionProbe = Callable[[str, int], str]


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    adapter_id: str
    state: str
    configured_paths: tuple[str, ...]
    attestation: ExecutableAttestation | None
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ExecutableInventory:
    def __init__(
        self,
        adapters: Iterable[AdapterSpec],
        *,
        package_resolver: PackageResolver,
        version_probes: dict[str, VersionProbe],
        allowed_owner_uids: Iterable[int] = (0,),
    ) -> None:
        adapter_items = tuple(adapters)
        self._adapters = {adapter.adapter_id: adapter for adapter in adapter_items}
        if len(self._adapters) != len(adapter_items):
            raise ValueError("inventory adapter ids must be unique")
        self._package_resolver = package_resolver
        self._version_probes = dict(version_probes)
        self._allowed_owner_uids = tuple(int(value) for value in allowed_owner_uids)

    def inspect(self, adapter_id: str) -> InventoryEntry:
        adapter = self._adapters.get(adapter_id)
        if adapter is None:
            raise KeyError(adapter_id)
        probe = self._version_probes.get(adapter_id)
        if probe is None:
            return InventoryEntry(
                adapter_id, "unattested", adapter.executable.allowed_paths, None, "version probe absent"
            )
        failures: list[str] = []
        found = False
        for path in adapter.executable.allowed_paths:
            candidate = Path(path)
            if not candidate.exists() and not candidate.is_symlink():
                continue
            found = True
            package = self._package_resolver.resolve(path)
            if package is None or package.name != adapter.executable.package_name:
                failures.append("package ownership could not be proven")
                continue
            try:
                identity = attest_executable(
                    path,
                    allowed_paths=adapter.executable.allowed_paths,
                    allowed_owner_uids=self._allowed_owner_uids,
                    package_name=package.name,
                    package_version=package.version,
                    architecture=package.architecture,
                    adapter_id=adapter.adapter_id,
                    adapter_schema_version=adapter.adapter_schema_version,
                    implementation_version=adapter.implementation_version,
                    observed_version="version-probe-pending",
                )
                # Never execute a pathname before proving its owner/mode/hash.
                # The probe receives the still-open verified inode; a concurrent
                # package rename cannot redirect the process to replacement bytes.
                with open_verified_executable(
                    identity,
                    allowed_owner_uids=self._allowed_owner_uids,
                ) as executable_fd:
                    observed_version = probe(path, executable_fd).strip()
                verify_executable(identity, allowed_owner_uids=self._allowed_owner_uids)
                attestation = replace(identity, observed_version=observed_version)
            except (ExecutableAttestationError, OSError, ValueError) as exc:
                failures.append(str(exc))
                continue
            return InventoryEntry(adapter_id, "available", adapter.executable.allowed_paths, attestation)
        state = "unattested" if found else "missing_package"
        return InventoryEntry(
            adapter_id,
            state,
            adapter.executable.allowed_paths,
            None,
            "; ".join(failures) or None,
        )

    def snapshot(self) -> tuple[InventoryEntry, ...]:
        return tuple(self.inspect(adapter_id) for adapter_id in sorted(self._adapters))


__all__ = [
    "DpkgPackageResolver",
    "ExecutableInventory",
    "InventoryEntry",
    "PackageIdentity",
    "PackageResolver",
]
