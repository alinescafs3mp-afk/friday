"""Deterministic delivery archive for verified Engineer command outputs.

This module is deliberately downstream of the command workspace reader.  It
does not open paths and it never trusts the live ``output`` tree; callers pass
the exact bytes re-read from ``sealed`` together with the terminal receipt
which inventoried them.  The builder then closes the final carrier shape: one
bounded, uncompressed ZIP whose metadata and member order are byte-stable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import math
import re
import stat
import unicodedata
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    MAX_OUTPUT_DEPTH,
    MAX_OUTPUT_FILE_BYTES,
    MAX_OUTPUT_FILES,
    MAX_OUTPUT_TREE_BYTES,
    MAX_STDERR_BYTES,
    MAX_STDOUT_BYTES,
    CommandLane,
    CommandOrigin,
    CommandReceipt,
    CommandStatus,
    GeneratedFile,
    IsolationProfile,
)

COMMAND_OUTPUT_MANIFEST_SCHEMA = "friday.engineer.command-output-manifest.v1"
COMMAND_OUTPUT_RECEIPT_SCHEMA = "friday.engineer.command-output-receipt.v1"
COMMAND_OUTPUT_MIME_TYPE = "application/zip"
MAX_COMMAND_OUTPUT_ARCHIVE_BYTES = 36 * 1024 * 1024
MAX_COMMAND_OUTPUT_METADATA_BYTES = 128 * 1024

_ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_ARCHIVE_FILE_MODE = 0o644
_JOB_ID = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_UNSAFE_WINDOWS_CHARS = frozenset('<>:"\\|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)
_PUBLISHABLE_STATUSES = frozenset(
    {
        CommandStatus.COMPLETED,
        CommandStatus.FAILED,
        CommandStatus.CANCELLED,
        CommandStatus.TIMEOUT,
    }
)


class CommandOutputPublicationError(ValueError):
    """Closed failure at the sealed-output publication boundary."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "command_output_publication_failed")
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class CommandOutputArchive:
    filename: str
    mime_type: str
    payload: bytes = field(repr=False)
    sha256: str
    manifest: Mapping[str, Any]
    receipt: Mapping[str, Any]

    def attachment(self) -> dict[str, str]:
        """Return the exact existing generated-file carrier shape."""

        return {
            "kind": "document",
            "filename": self.filename,
            "mime_type": self.mime_type,
            "content_base64": base64.b64encode(self.payload).decode("ascii"),
        }


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> bytes:
    try:
        payload = (
            json.dumps(
                value,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise CommandOutputPublicationError("command_output_metadata_invalid") from exc
    if len(payload) > MAX_COMMAND_OUTPUT_METADATA_BYTES:
        raise CommandOutputPublicationError("command_output_metadata_limit")
    return payload


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1_024:
        raise CommandOutputPublicationError("command_output_path_invalid")
    if unicodedata.normalize("NFKC", value) != value:
        raise CommandOutputPublicationError("command_output_path_invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise CommandOutputPublicationError("command_output_path_invalid") from exc
    if len(encoded) > 4_096 or value.startswith("/") or "\\" in value:
        raise CommandOutputPublicationError("command_output_path_invalid")
    parts = value.split("/")
    if not 1 <= len(parts) <= MAX_OUTPUT_DEPTH + 1:
        raise CommandOutputPublicationError("command_output_path_invalid")
    for part in parts:
        stem = part.split(".", 1)[0].casefold()
        if (
            not part
            or part in {".", ".."}
            or len(part) > 180
            or part != part.strip(" .")
            or stem in _WINDOWS_RESERVED_NAMES
            or any(character in _UNSAFE_WINDOWS_CHARS for character in part)
            or any(
                ord(character) < 32
                or ord(character) == 127
                or unicodedata.category(character).startswith("C")
                for character in part
            )
        ):
            raise CommandOutputPublicationError("command_output_path_invalid")
    return value


def _validated_receipt(receipt: object) -> CommandReceipt:
    if type(receipt) is not CommandReceipt:
        raise CommandOutputPublicationError("command_output_receipt_invalid")
    assert isinstance(receipt, CommandReceipt)
    if (
        not isinstance(receipt.job_id, str)
        or _JOB_ID.fullmatch(receipt.job_id) is None
        or type(receipt.status) is not CommandStatus
        or receipt.status not in _PUBLISHABLE_STATUSES
        or type(receipt.lane) is not CommandLane
        or type(receipt.origin) is not CommandOrigin
        or type(receipt.isolation_profile) is not IsolationProfile
        or isinstance(receipt.started_at, bool)
        or not isinstance(receipt.started_at, int | float)
        or not math.isfinite(receipt.started_at)
        or receipt.started_at < 0
        or isinstance(receipt.finished_at, bool)
        or not isinstance(receipt.finished_at, int | float)
        or not math.isfinite(receipt.finished_at)
        or receipt.finished_at < receipt.started_at
        or any(
            not isinstance(value, str) or _SHA256.fullmatch(value) is None
            for value in (
                receipt.command_digest,
                receipt.argv_sha256,
                receipt.source_hash,
                receipt.stdout_sha256,
                receipt.stderr_sha256,
                receipt.receipt_mac,
            )
        )
        or not isinstance(receipt.generated_files, tuple)
        or not isinstance(receipt.stdout, bytes)
        or not isinstance(receipt.stderr, bytes)
        or len(receipt.stdout) > MAX_STDOUT_BYTES
        or len(receipt.stderr) > MAX_STDERR_BYTES
        or not hmac.compare_digest(_digest(receipt.stdout), receipt.stdout_sha256)
        or not hmac.compare_digest(_digest(receipt.stderr), receipt.stderr_sha256)
    ):
        raise CommandOutputPublicationError("command_output_receipt_invalid")
    try:
        public = receipt.to_public_payload()
    except (AttributeError, TypeError, ValueError) as exc:
        raise CommandOutputPublicationError("command_output_receipt_invalid") from exc
    if (
        public.get("job_id") != receipt.job_id
        or public.get("command_digest") != receipt.command_digest
        or public.get("status") != receipt.status.value
        or public.get("receipt_mac") != receipt.receipt_mac
        or public.get("generated_file_count") != len(receipt.generated_files)
    ):
        raise CommandOutputPublicationError("command_output_receipt_invalid")
    return receipt


def _validated_inventory(
    receipt: CommandReceipt,
    outputs: Sequence[tuple[GeneratedFile, bytes]],
) -> tuple[tuple[GeneratedFile, bytes], ...]:
    try:
        supplied = tuple(outputs)
    except TypeError as exc:
        raise CommandOutputPublicationError("command_output_inventory_invalid") from exc
    expected = tuple(receipt.generated_files)
    if len(expected) > MAX_OUTPUT_FILES or len(supplied) != len(expected):
        raise CommandOutputPublicationError("command_output_count_invalid")

    expected_by_path: dict[str, GeneratedFile] = {}
    total_expected = 0
    for descriptor in expected:
        if type(descriptor) is not GeneratedFile:
            raise CommandOutputPublicationError("command_output_inventory_invalid")
        path = _safe_relative_path(descriptor.relative_path)
        if (
            path in expected_by_path
            or isinstance(descriptor.size_bytes, bool)
            or not isinstance(descriptor.size_bytes, int)
            or not 0 <= descriptor.size_bytes <= MAX_OUTPUT_FILE_BYTES
            or isinstance(descriptor.mode, bool)
            or not isinstance(descriptor.mode, int)
            or not 0 <= descriptor.mode <= 0o7777
            or _SHA256.fullmatch(descriptor.sha256) is None
        ):
            raise CommandOutputPublicationError("command_output_inventory_invalid")
        expected_by_path[path] = descriptor
        total_expected += descriptor.size_bytes
        if total_expected > MAX_OUTPUT_TREE_BYTES:
            raise CommandOutputPublicationError("command_output_size_limit")

    admitted: list[tuple[GeneratedFile, bytes]] = []
    seen_paths: set[str] = set()
    portable_paths: set[str] = set()
    for item in supplied:
        if not isinstance(item, tuple) or len(item) != 2:
            raise CommandOutputPublicationError("command_output_inventory_invalid")
        descriptor, payload = item
        if type(descriptor) is not GeneratedFile or not isinstance(payload, bytes):
            raise CommandOutputPublicationError("command_output_inventory_invalid")
        path = _safe_relative_path(descriptor.relative_path)
        portable = path.casefold()
        expected_descriptor = expected_by_path.get(path)
        if (
            expected_descriptor is None
            or descriptor != expected_descriptor
            or path in seen_paths
            or portable in portable_paths
        ):
            raise CommandOutputPublicationError("command_output_inventory_mismatch")
        if len(payload) != descriptor.size_bytes:
            raise CommandOutputPublicationError("command_output_size_mismatch")
        if not hmac.compare_digest(_digest(payload), descriptor.sha256):
            raise CommandOutputPublicationError("command_output_digest_mismatch")
        seen_paths.add(path)
        portable_paths.add(portable)
        admitted.append((descriptor, payload))
    if seen_paths != set(expected_by_path):
        raise CommandOutputPublicationError("command_output_inventory_mismatch")
    return tuple(sorted(admitted, key=lambda item: item[0].relative_path.encode("utf-8")))


def _write_entry(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_ARCHIVE_TIMESTAMP)
    info.create_system = 3
    info.create_version = 20
    info.extract_version = 20
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (stat.S_IFREG | _ARCHIVE_FILE_MODE) << 16
    info.internal_attr = 0
    info.extra = b""
    info.comment = b""
    archive.writestr(info, payload)


def build_command_output_archive(
    receipt: CommandReceipt,
    outputs: Sequence[tuple[GeneratedFile, bytes]],
    *,
    max_archive_bytes: int = MAX_COMMAND_OUTPUT_ARCHIVE_BYTES,
) -> CommandOutputArchive:
    """Build one byte-stable ZIP from an exact terminal receipt and sealed bytes.

    ``outputs`` may arrive in any order, but it must be a one-for-one copy of
    ``receipt.generated_files``.  No path is opened here; a workspace/kernel
    reader must already have revalidated the sealed file identity and supplied
    immutable ``bytes``.
    """

    checked_receipt = _validated_receipt(receipt)
    ordered = _validated_inventory(checked_receipt, outputs)
    if isinstance(max_archive_bytes, bool) or not isinstance(max_archive_bytes, int):
        raise CommandOutputPublicationError("command_output_archive_limit_invalid")
    archive_limit = min(max_archive_bytes, MAX_COMMAND_OUTPUT_ARCHIVE_BYTES)
    if archive_limit <= 0:
        raise CommandOutputPublicationError("command_output_archive_limit_invalid")

    output_rows = [
        {
            "archive_mode": "0644",
            "archive_path": f"outputs/{descriptor.relative_path}",
            "original_mode": f"{descriptor.mode:04o}",
            "relative_path": descriptor.relative_path,
            "sha256": descriptor.sha256,
            "size_bytes": descriptor.size_bytes,
        }
        for descriptor, _payload in ordered
    ]
    inventory_bytes = _canonical_json(output_rows)
    public_receipt = checked_receipt.to_public_payload()
    public_receipt_bytes = _canonical_json(public_receipt)
    evidence_rows = [
        {
            "archive_mode": "0644",
            "archive_path": archive_path,
            "sha256": digest,
            "size_bytes": len(payload),
            "truncated": truncated,
        }
        for archive_path, payload, digest, truncated in (
            (
                "stdout.bin",
                checked_receipt.stdout,
                checked_receipt.stdout_sha256,
                checked_receipt.truncated_stdout,
            ),
            (
                "stderr.bin",
                checked_receipt.stderr,
                checked_receipt.stderr_sha256,
                checked_receipt.truncated_stderr,
            ),
        )
        if payload
    ]
    evidence_bytes = _canonical_json(evidence_rows)
    manifest: dict[str, Any] = {
        "schema": COMMAND_OUTPUT_MANIFEST_SCHEMA,
        "job_id": checked_receipt.job_id,
        "command_digest": checked_receipt.command_digest,
        "command_receipt_sha256": _digest(public_receipt_bytes),
        "evidence": evidence_rows,
        "evidence_count": len(evidence_rows),
        "evidence_inventory_sha256": _digest(evidence_bytes),
        "output_count": len(output_rows),
        "output_bytes": sum(row["size_bytes"] for row in output_rows),
        "output_inventory_sha256": _digest(inventory_bytes),
        "outputs": output_rows,
        "receipt_path": "RECEIPT.json",
    }
    manifest_bytes = _canonical_json(manifest)
    delivery_receipt: dict[str, Any] = {
        "schema": COMMAND_OUTPUT_RECEIPT_SCHEMA,
        "job_id": checked_receipt.job_id,
        "command_digest": checked_receipt.command_digest,
        "command_receipt": public_receipt,
        "command_receipt_sha256": _digest(public_receipt_bytes),
        "evidence_inventory_sha256": _digest(evidence_bytes),
        "manifest_sha256": _digest(manifest_bytes),
        "output_inventory_sha256": _digest(inventory_bytes),
    }
    delivery_receipt_bytes = _canonical_json(delivery_receipt)

    buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(
            buffer,
            mode="w",
            compression=zipfile.ZIP_STORED,
            allowZip64=False,
            strict_timestamps=True,
        ) as archive:
            archive.comment = b""
            _write_entry(archive, "MANIFEST.json", manifest_bytes)
            _write_entry(archive, "RECEIPT.json", delivery_receipt_bytes)
            if checked_receipt.stdout:
                _write_entry(archive, "stdout.bin", checked_receipt.stdout)
            if checked_receipt.stderr:
                _write_entry(archive, "stderr.bin", checked_receipt.stderr)
            for descriptor, payload in ordered:
                _write_entry(archive, f"outputs/{descriptor.relative_path}", payload)
    except (OSError, OverflowError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise CommandOutputPublicationError("command_output_archive_write_failed") from exc
    payload = buffer.getvalue()
    if not payload or len(payload) > archive_limit:
        raise CommandOutputPublicationError("command_output_archive_size_limit")

    filename = f"engineer-command-{checked_receipt.job_id}.zip"
    return CommandOutputArchive(
        filename=filename,
        mime_type=COMMAND_OUTPUT_MIME_TYPE,
        payload=payload,
        sha256=_digest(payload),
        manifest=manifest,
        receipt=delivery_receipt,
    )


__all__ = [
    "COMMAND_OUTPUT_MANIFEST_SCHEMA",
    "COMMAND_OUTPUT_MIME_TYPE",
    "COMMAND_OUTPUT_RECEIPT_SCHEMA",
    "CommandOutputArchive",
    "CommandOutputPublicationError",
    "MAX_COMMAND_OUTPUT_ARCHIVE_BYTES",
    "MAX_COMMAND_OUTPUT_METADATA_BYTES",
    "build_command_output_archive",
]
