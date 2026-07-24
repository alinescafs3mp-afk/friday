"""Crash-safe host-local process leases backed by operating-system locks."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, cast

_OWNED_LEASES: set[tuple[str, str]] = set()
_OWNED_LEASES_LOCK = threading.Lock()


class RuntimeLeaseError(RuntimeError):
    """Another process already owns the requested runtime role."""


def _linux_anchor_address(path: Path) -> str:
    identity = os.path.normcase(os.path.abspath(path))
    digest = hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"\0jericho.lease.{os.getuid()}.{digest}"


def _lease_identity(path: Path, protocol: str) -> tuple[str, str]:
    return (os.path.normcase(os.path.abspath(path)), protocol)


def process_owns_lease(path: Path, *, protocol: str) -> bool:
    """Return whether this Python process currently owns the exact lease."""

    with _OWNED_LEASES_LOCK:
        return _lease_identity(path, protocol) in _OWNED_LEASES


def _pid_is_alive(pid: int) -> bool | None:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def inspect_process_lease(path: Path, *, protocol: str) -> dict[str, Any]:
    """Inspect a lease without creating, truncating, or taking ownership of it.

    File existence alone is deliberately not treated as an active process.  On
    Linux we probe the same abstract socket anchor used by :class:`ProcessLease`;
    on other platforms the metadata and PID liveness remain advisory.
    """

    is_symlink = path.is_symlink()
    exists = path.exists()
    result: dict[str, Any] = {
        "path": str(path),
        "exists": exists or is_symlink,
        "protocol": protocol,
        "active": False,
        "state": "absent",
    }
    # ``Path.exists`` is false for a broken symlink. Diagnose it before the
    # absence fast path so operators do not mistake an unsafe lease target for
    # a harmless missing file.
    if is_symlink:
        result.update({"state": "unsafe_symlink", "healthy": False})
        return result
    if not exists:
        return result

    metadata: dict[str, Any] = {}
    try:
        raw = path.read_bytes()[:4096]
        parsed = json.loads(raw.decode("utf-8", errors="strict").strip() or "{}")
        if isinstance(parsed, dict):
            metadata = parsed
        else:
            result["metadata_error"] = "lease metadata root is not an object"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        result["metadata_error"] = f"{type(exc).__name__}: {exc}"

    recorded_protocol = str(metadata.get("protocol") or "")
    pid_value = metadata.get("pid")
    pid: int | None = None
    if isinstance(pid_value, int) and not isinstance(pid_value, bool):
        pid = pid_value
    result.update(
        {
            "recorded_protocol": recorded_protocol or None,
            "pid": pid,
            "pid_alive": _pid_is_alive(pid) if pid is not None else None,
            "acquired_at": metadata.get("acquired_at"),
        }
    )

    active: bool | None = None
    if sys.platform.startswith("linux"):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            probe.bind(_linux_anchor_address(path))
            active = False
        except OSError:
            active = True
        finally:
            probe.close()
    result["active"] = active

    protocol_matches = recorded_protocol in {"", protocol}
    result["protocol_matches"] = protocol_matches
    if active is True:
        result["state"] = "active" if protocol_matches else "active_foreign_protocol"
    elif active is False:
        result["state"] = "stale_metadata" if metadata else "inactive"
    else:
        alive = result.get("pid_alive")
        if alive is True:
            result["state"] = "active_hint" if protocol_matches else "foreign_protocol_hint"
        elif alive is False:
            result["state"] = "stale_metadata"
        else:
            result["state"] = "unknown"
    result["healthy"] = result["state"] not in {
        "unsafe_symlink",
        "active_foreign_protocol",
        "foreign_protocol_hint",
    }
    return result


class ProcessLease:
    """Hold one exclusive process role until the descriptor is closed.

    The lock survives stale metadata files because ownership lives in the OS,
    not in file existence.  Linux also gets an abstract Unix-socket anchor so a
    replaced lock file cannot create two primaries on the same host.
    """

    def __init__(self, path: Path, *, protocol: str) -> None:
        self.path = path
        self.protocol = protocol
        self._descriptor: int | None = None
        self._anchor_socket: socket.socket | None = None
        self._mutex = threading.Lock()

    @property
    def acquired(self) -> bool:
        with self._mutex:
            return self._descriptor is not None

    def acquire(self) -> None:
        with self._mutex:
            if self._descriptor is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            anchor = self._acquire_linux_anchor()
            if self.path.is_symlink():
                if anchor is not None:
                    anchor.close()
                raise RuntimeLeaseError(f"runtime lease cannot be a symlink: {self.path}")

            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.path, flags, 0o600)
            except BaseException:
                if anchor is not None:
                    anchor.close()
                raise
            try:
                self._lock_descriptor(descriptor)
                metadata = (
                    json.dumps(
                        {
                            "protocol": self.protocol,
                            "pid": os.getpid(),
                            "acquired_at": datetime.now(UTC).isoformat(),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    + b"\n"
                )
                os.ftruncate(descriptor, 0)
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, metadata)
                os.fsync(descriptor)
            except BaseException:
                os.close(descriptor)
                if anchor is not None:
                    anchor.close()
                raise
            self._descriptor = descriptor
            self._anchor_socket = anchor
            with _OWNED_LEASES_LOCK:
                _OWNED_LEASES.add(_lease_identity(self.path, self.protocol))

    def release(self) -> None:
        with self._mutex:
            descriptor = self._descriptor
            if descriptor is None:
                return
            anchor = self._anchor_socket
            self._descriptor = None
            self._anchor_socket = None
            with _OWNED_LEASES_LOCK:
                _OWNED_LEASES.discard(_lease_identity(self.path, self.protocol))
            try:
                self._unlock_descriptor(descriptor)
            finally:
                os.close(descriptor)
                if anchor is not None:
                    anchor.close()

    def _acquire_linux_anchor(self) -> socket.socket | None:
        if not sys.platform.startswith("linux"):
            return None
        anchor = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            anchor.bind(_linux_anchor_address(self.path))
        except OSError as exc:
            anchor.close()
            raise RuntimeLeaseError(f"another process owns runtime lease {self.path}") from exc
        return anchor

    def _lock_descriptor(self, descriptor: int) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                # ``msvcrt`` is only fully described by typeshed on Windows.
                # Runtime attribute lookup keeps this module type-checkable on
                # Unix while preserving the native Windows implementation.
                windows_api = cast(Any, msvcrt)
                windows_api.locking(descriptor, windows_api.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeLeaseError(f"another process owns runtime lease {self.path}") from exc

    @staticmethod
    def _unlock_descriptor(descriptor: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            windows_api = cast(Any, msvcrt)
            windows_api.locking(descriptor, windows_api.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)

    def __enter__(self) -> ProcessLease:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.release()


__all__ = [
    "ProcessLease",
    "RuntimeLeaseError",
    "inspect_process_lease",
    "process_owns_lease",
]
