"""Dedicated Coding-worker boundary: planned visibility, not a host scan.

The four isolation booleans are observations of what this boundary would make
visible.  This module does not spawn a process, import Docker, or reuse the
Engineer bubblewrap sandbox.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass

DOCKER_SOCKET_PATHS = ("/var/run/docker.sock", "/run/docker.sock")
CODING_WORKER_STATE_NAME = "friday-coding-worker"


@dataclass(frozen=True, slots=True)
class CodingWorkerBoundaryV1:
    """Planned dedicated root and the paths a future worker may see."""

    worker_root: str
    visible_paths: tuple[str, ...]
    friday_home: str
    owner_home: str
    database_path: str
    workspace_path: str
    export_path: str
    network_disabled: bool = True
    host_network: bool = False


def _posix(value: object) -> str | None:
    if type(value) is not str or not value or value != value.strip():
        return None
    if "\\" in value or any(unicodedata.category(character).startswith("C") for character in value):
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    parts = tuple(part for part in value.split("/") if part)
    if any(part in {".", ".."} for part in parts):
        return None
    if value == "/":
        return "/"
    return "/" + "/".join(parts)


def _covers(visible: str, hazard: str) -> bool:
    if visible == "/":
        return True
    if hazard == "/":
        return False
    return hazard == visible or hazard.startswith(visible + "/") or visible.startswith(hazard + "/")


def _unsafe() -> dict[str, bool]:
    return {
        "host_secrets_visible": True,
        "docker_socket_present": True,
        "production_database_reachable": True,
        "owner_ssh_keys_visible": True,
    }


def default_coding_worker_root() -> str:
    """Return a dedicated state root that is not FRIDAY_HOME."""

    state = os.environ.get("XDG_STATE_HOME")
    if type(state) is str:
        posix = _posix(state)
        if posix is not None and posix != "/":
            return posix + "/" + CODING_WORKER_STATE_NAME
    home = _posix(os.path.expanduser("~"))
    if home is None or home == "/":
        return "/var/tmp/" + CODING_WORKER_STATE_NAME
    return home + "/.local/state/" + CODING_WORKER_STATE_NAME


def default_coding_worker_boundary(
    *,
    friday_home: str,
    owner_home: str,
    database_path: str,
    worker_root: str | None = None,
    workspace_path: str = "work/op",
    export_path: str = "out/op",
) -> CodingWorkerBoundaryV1:
    """Build the closed default: only the dedicated worker root is visible."""

    root = worker_root if type(worker_root) is str and worker_root else default_coding_worker_root()
    return CodingWorkerBoundaryV1(
        worker_root=root,
        visible_paths=(root,),
        friday_home=friday_home,
        owner_home=owner_home,
        database_path=database_path,
        workspace_path=workspace_path,
        export_path=export_path,
        network_disabled=True,
        host_network=False,
    )


def observe_coding_worker_isolation(boundary: CodingWorkerBoundaryV1) -> dict[str, bool]:
    """Return the four isolation booleans for one planned boundary."""

    root = _posix(boundary.worker_root)
    friday_home = _posix(boundary.friday_home)
    owner_home = _posix(boundary.owner_home)
    database = _posix(boundary.database_path)
    if root is None or friday_home is None or owner_home is None or database is None:
        return _unsafe()
    ssh = _posix(owner_home + "/.ssh")
    if ssh is None:
        return _unsafe()
    visible: list[str] = []
    for path in (root, *boundary.visible_paths):
        item = _posix(path)
        if item is None:
            return _unsafe()
        visible.append(item)
    return {
        "host_secrets_visible": any(_covers(item, friday_home) for item in visible),
        "docker_socket_present": any(
            _covers(item, socket) for item in visible for socket in DOCKER_SOCKET_PATHS
        ),
        "production_database_reachable": any(_covers(item, database) for item in visible),
        "owner_ssh_keys_visible": any(_covers(item, ssh) for item in visible),
    }


def coding_worker_hazard_paths(boundary: CodingWorkerBoundaryV1) -> tuple[str, ...]:
    """Absolute hazard paths the spawned probe must not observe."""

    friday_home = _posix(boundary.friday_home)
    owner_home = _posix(boundary.owner_home)
    database = _posix(boundary.database_path)
    if friday_home is None or owner_home is None or database is None:
        return DOCKER_SOCKET_PATHS
    return (
        *DOCKER_SOCKET_PATHS,
        friday_home,
        database,
        owner_home + "/.ssh",
    )
