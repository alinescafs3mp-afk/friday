"""Private, bounded, content-addressed evidence for APT transactions.

Raw stdout/stderr is retained only in private evidence blobs.  Only bounded
metadata, hashes, and references cross into receipts, persistence, or prompts.
"""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from friday.host_control.contracts import ContractError, canonical_json_bytes

from .contracts import (
    MAX_PACKAGE_EVIDENCE_BYTES,
    MAX_PACKAGE_OUTPUT_EVIDENCE_BYTES,
    PackageEvidenceReference,
    ServiceUnitObservation,
)

MAX_CAPTURE_BYTES_PER_STREAM = MAX_PACKAGE_OUTPUT_EVIDENCE_BYTES
MAX_PROGRESS_EVENTS = 4096
_PROGRESS_EVENT_KINDS = ("conffile", "dpkg_status", "error", "processing", "status")


class PackageEvidenceError(RuntimeError):
    """Evidence could not be captured or durably persisted."""


@dataclass(frozen=True, slots=True)
class OutputCapture:
    status: str
    stdout_bytes: bytes = field(repr=False)
    stderr_bytes: bytes = field(repr=False)
    stdout_total_size_bytes: int | None
    stderr_total_size_bytes: int | None
    stdout_total_size_complete: bool
    stderr_total_size_complete: bool
    progress_event_counts: tuple[tuple[str, int], ...] = ()
    progress_events_truncated: bool = False

    def __post_init__(self) -> None:
        if self.status not in {"captured", "not_applicable", "unavailable"}:
            raise PackageEvidenceError("output capture status is invalid")
        if not isinstance(self.stdout_bytes, bytes) or not isinstance(self.stderr_bytes, bytes):
            raise PackageEvidenceError("output capture bytes are invalid")
        if not isinstance(self.progress_events_truncated, bool):
            raise PackageEvidenceError("progress truncation marker is invalid")
        if self.status != "captured":
            if (
                self.stdout_bytes
                or self.stderr_bytes
                or self.stdout_total_size_bytes is not None
                or self.stderr_total_size_bytes is not None
                or self.stdout_total_size_complete
                or self.stderr_total_size_complete
                or self.progress_event_counts
                or self.progress_events_truncated
            ):
                raise PackageEvidenceError("uncaptured output claims evidence")
            return
        for payload in (self.stdout_bytes, self.stderr_bytes):
            if len(payload) > MAX_CAPTURE_BYTES_PER_STREAM:
                raise PackageEvidenceError("output capture size is invalid")
        for retained, total, complete in (
            (len(self.stdout_bytes), self.stdout_total_size_bytes, self.stdout_total_size_complete),
            (len(self.stderr_bytes), self.stderr_total_size_bytes, self.stderr_total_size_complete),
        ):
            if (
                isinstance(total, bool)
                or not isinstance(total, int)
                or total < retained
                or total > 2**63 - 1
                or not isinstance(complete, bool)
            ):
                raise PackageEvidenceError("output capture total size is invalid")
        if (
            not isinstance(self.progress_event_counts, tuple)
            or len(self.progress_event_counts) > len(_PROGRESS_EVENT_KINDS)
            or tuple(sorted(self.progress_event_counts)) != self.progress_event_counts
            or len({kind for kind, _count in self.progress_event_counts}) != len(self.progress_event_counts)
        ):
            raise PackageEvidenceError("progress event counters are invalid")
        for kind, count in self.progress_event_counts:
            if (
                kind not in _PROGRESS_EVENT_KINDS
                or isinstance(count, bool)
                or not 0 <= count <= MAX_PROGRESS_EVENTS
            ):
                raise PackageEvidenceError("progress event counter is invalid")

    @classmethod
    def empty(cls) -> OutputCapture:
        return cls("captured", b"", b"", 0, 0, True, True)

    @classmethod
    def unavailable(cls) -> OutputCapture:
        return cls("unavailable", b"", b"", None, None, False, False)

    @classmethod
    def not_applicable(cls) -> OutputCapture:
        return cls("not_applicable", b"", b"", None, None, False, False)

    @property
    def stdout_sha256(self) -> str | None:
        return None if self.status != "captured" else hashlib.sha256(self.stdout_bytes).hexdigest()

    @property
    def stdout_size_bytes(self) -> int | None:
        return None if self.status != "captured" else len(self.stdout_bytes)

    @property
    def stderr_sha256(self) -> str | None:
        return None if self.status != "captured" else hashlib.sha256(self.stderr_bytes).hexdigest()

    @property
    def stderr_size_bytes(self) -> int | None:
        return None if self.status != "captured" else len(self.stderr_bytes)

    @property
    def stdout_truncated(self) -> bool:
        return self.status == "captured" and (
            not self.stdout_total_size_complete or self.stdout_total_size_bytes != self.stdout_size_bytes
        )

    @property
    def stderr_truncated(self) -> bool:
        return self.status == "captured" and (
            not self.stderr_total_size_complete or self.stderr_total_size_bytes != self.stderr_size_bytes
        )

    @property
    def truncated(self) -> bool:
        return self.stdout_truncated or self.stderr_truncated or self.progress_events_truncated


class BoundedDigestSink:
    """Drain arbitrary bytes while retaining an exact bounded prefix."""

    def __init__(self, maximum: int = MAX_CAPTURE_BYTES_PER_STREAM) -> None:
        if not 1 <= maximum <= MAX_CAPTURE_BYTES_PER_STREAM:
            raise ValueError("capture maximum is invalid")
        self._maximum = maximum
        self._retained = bytearray()
        self._total_size = 0
        self._total_size_complete = True

    def feed(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes):
            raise TypeError("capture chunk must be bytes")
        if self._total_size > 2**63 - 1 - len(chunk):
            raise PackageEvidenceError("output capture total size overflow")
        self._total_size += len(chunk)
        remaining = self._maximum - len(self._retained)
        retained = chunk[:remaining]
        if retained:
            self._retained.extend(retained)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self._retained).hexdigest()

    @property
    def size(self) -> int:
        return len(self._retained)

    @property
    def retained_bytes(self) -> bytes:
        return bytes(self._retained)

    @property
    def total_size(self) -> int:
        return self._total_size

    @property
    def total_size_complete(self) -> bool:
        return self._total_size_complete

    @property
    def truncated(self) -> bool:
        return not self._total_size_complete or self._total_size != len(self._retained)

    def mark_truncated(self) -> None:
        self._total_size_complete = False


class PackageEvidenceStore:
    """Write immutable 0600 evidence blobs and manifests below a private directory."""

    def __init__(self, directory: str | Path) -> None:
        selected = Path(directory)
        if not selected.is_absolute() or "\x00" in str(selected):
            raise PackageEvidenceError("package evidence directory must be absolute")
        if selected.resolve(strict=False) != selected:
            raise PackageEvidenceError("package evidence directory cannot traverse symlinks")
        self.directory = selected

    def persist_transaction(
        self,
        *,
        transaction_digest: str,
        outcome: str,
        error_code: str | None,
        output: OutputCapture,
        service_unit_observation_status: str,
        service_unit_observations: tuple[ServiceUnitObservation, ...],
    ) -> tuple[PackageEvidenceReference, ...]:
        if len(transaction_digest) != 64 or any(
            character not in "0123456789abcdef" for character in transaction_digest
        ):
            raise PackageEvidenceError("transaction evidence digest is invalid")
        if outcome not in {
            "already_satisfied",
            "completed",
            "failed_before_effect",
            "unknown",
        }:
            raise PackageEvidenceError("transaction evidence outcome is invalid")
        if error_code is not None and (
            not error_code or len(error_code) > 80 or not error_code.replace("_", "a").isalnum()
        ):
            raise PackageEvidenceError("transaction evidence error code is invalid")
        if service_unit_observation_status not in {
            "captured",
            "not_applicable",
            "partial",
            "unavailable",
        }:
            raise PackageEvidenceError("service observation status is invalid")
        if output.status != "captured":
            raise PackageEvidenceError("raw package output was not captured")
        self._ensure_private_directory()
        stdout_ref = self._persist_blob(
            kind="apt_stdout",
            extension="stdout",
            media_type="application/octet-stream",
            payload=output.stdout_bytes,
        )
        stderr_ref = self._persist_blob(
            kind="apt_stderr",
            extension="stderr",
            media_type="application/octet-stream",
            payload=output.stderr_bytes,
        )
        manifest = {
            "error_code": error_code,
            "outcome": outcome,
            "output": {
                "progress_event_counts": {kind: count for kind, count in output.progress_event_counts},
                "progress_events_truncated": output.progress_events_truncated,
                "status": output.status,
                "stderr": (
                    {
                        "ref": stderr_ref.to_payload(),
                        "retained_size_bytes": output.stderr_size_bytes,
                        "total_size_bytes": output.stderr_total_size_bytes,
                        "total_size_complete": output.stderr_total_size_complete,
                        "truncated": output.stderr_truncated,
                    }
                ),
                "stdout": (
                    {
                        "ref": stdout_ref.to_payload(),
                        "retained_size_bytes": output.stdout_size_bytes,
                        "total_size_bytes": output.stdout_total_size_bytes,
                        "total_size_complete": output.stdout_total_size_complete,
                        "truncated": output.stdout_truncated,
                    }
                ),
                "truncated": output.truncated,
            },
            "privacy": {
                "progress_callback_messages_retained": False,
                "raw_output_embedded_in_manifest": False,
                "raw_output_projected": False,
                "raw_output_retained_as_private_evidence": True,
                "schema": "bounded_raw_refs_v2",
            },
            "schema_version": 2,
            "service_unit_observation_status": service_unit_observation_status,
            "service_unit_observations": [item.to_payload() for item in service_unit_observations],
            "transaction_digest": transaction_digest,
        }
        try:
            payload = canonical_json_bytes(manifest, maximum=MAX_PACKAGE_EVIDENCE_BYTES)
        except (ContractError, TypeError, ValueError) as exc:
            raise PackageEvidenceError("package evidence manifest is invalid or oversized") from exc
        manifest_ref = self._persist_blob(
            kind="apt_dpkg_transaction",
            extension="json",
            media_type="application/json",
            payload=payload,
        )
        return (stdout_ref, stderr_ref, manifest_ref)

    def _persist_blob(
        self,
        *,
        kind: str,
        extension: str,
        media_type: str,
        payload: bytes,
    ) -> PackageEvidenceReference:
        digest = hashlib.sha256(payload).hexdigest()
        destination = self.directory / f"{digest}.{extension}"
        self._write_content_addressed(destination, payload)
        return PackageEvidenceReference(
            kind=kind,
            ref=f"evidence/{digest}.{extension}",
            sha256=digest,
            size_bytes=len(payload),
            media_type=media_type,
        )

    def _ensure_private_directory(self) -> None:
        try:
            self.directory.mkdir(mode=0o700, parents=False, exist_ok=True)
            observed = self.directory.stat(follow_symlinks=False)
        except OSError as exc:
            raise PackageEvidenceError("package evidence directory is unavailable") from exc
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or observed.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or self.directory.resolve(strict=True) != self.directory
        ):
            raise PackageEvidenceError("package evidence directory metadata is unsafe")

    def _write_content_addressed(self, destination: Path, payload: bytes) -> None:
        temporary = self.directory / f".tmp-{os.getpid()}-{secrets.token_hex(8)}"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise PackageEvidenceError("package evidence write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
            self._verify_existing(destination, payload)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except (OSError, PackageEvidenceError) as exc:
            if isinstance(exc, PackageEvidenceError):
                raise
            raise PackageEvidenceError("package evidence could not be persisted") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _verify_existing(destination: Path, payload: bytes) -> None:
        descriptor = -1
        try:
            descriptor = os.open(
                destination,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_uid != os.geteuid()
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_size != len(payload)
            ):
                raise PackageEvidenceError("existing package evidence metadata is unsafe")
            recovered = bytearray()
            while len(recovered) <= len(payload):
                chunk = os.read(descriptor, min(64 * 1024, len(payload) + 1 - len(recovered)))
                if not chunk:
                    break
                recovered.extend(chunk)
            if bytes(recovered) != payload:
                raise PackageEvidenceError("content-addressed package evidence mismatches")
        except OSError as exc:
            raise PackageEvidenceError("existing package evidence could not be verified") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)


__all__ = [
    "BoundedDigestSink",
    "MAX_CAPTURE_BYTES_PER_STREAM",
    "MAX_PROGRESS_EVENTS",
    "OutputCapture",
    "PackageEvidenceError",
    "PackageEvidenceStore",
]
