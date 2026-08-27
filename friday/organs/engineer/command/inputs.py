"""Canonical, body-safe identities for read-only Engineer command inputs.

This module deliberately carries metadata only.  Authorization tokens, host
paths, file descriptors and file bytes belong to the private admission and
execution seams, never to a grant or confirmation body.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import CommandError, canonical_json_bytes, sha256_bytes

INPUT_MANIFEST_SCHEMA = "friday.engineer.command-input-manifest.v1"
SANDBOX_INPUT_ROOT = "/job/input"
MAX_INPUT_FILES = 12
MAX_INPUT_FILE_BYTES = 16 * 1024 * 1024
MAX_INPUT_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ORIGINAL_FILENAME_BYTES = 180
MAX_SANDBOX_FILENAME_BYTES = 120

_RAW_ID = re.compile(r"raw_[0-9a-f]{16}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MIME_TYPE = re.compile(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+")
_SANDBOX_PATH = re.compile(r"/job/input/(?:0[1-9]|1[0-2])-[^/]+")
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "clock$",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)
_DESCRIPTOR_KEYS = frozenset(
    {
        "content_sha256",
        "mime_type",
        "original_filename",
        "raw_id",
        "sandbox_path",
        "size_bytes",
        "source_identity_sha256",
    }
)
_MANIFEST_KEYS = frozenset({"files", "schema", "total_size_bytes"})


def _truncate_utf8(value: str, limit: int) -> str:
    """Truncate one normalized component without cutting a Unicode scalar."""

    if len(value.encode("utf-8")) <= limit:
        return value
    result: list[str] = []
    consumed = 0
    for char in value:
        encoded = char.encode("utf-8")
        if consumed + len(encoded) > limit:
            break
        result.append(char)
        consumed += len(encoded)
    return "".join(result)


def _replace_non_body_characters(value: str) -> str:
    return "".join("_" if unicodedata.category(char).startswith("C") else char for char in value)


def canonical_input_filename(value: str) -> str:
    """Return the bounded filename retained in canonical request bodies.

    Directory components and platform-reserved syntax are discarded rather
    than copied into a durable body.  The result is deterministic and safe to
    show to an owner, but does not claim to be a host filesystem path.
    """

    if type(value) is not str:
        raise CommandError("input_filename_invalid")
    normalized = unicodedata.normalize("NFKC", value).replace("\\", "/").rsplit("/", 1)[-1]
    normalized = _replace_non_body_characters(normalized)
    normalized = re.sub(r'[<>:"/\\|?*]', "_", normalized)
    normalized = " ".join(normalized.split()).strip(" .")
    if not normalized or normalized in {".", ".."}:
        normalized = "input.bin"
    if normalized.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
        normalized = f"_{normalized}"
    normalized = _truncate_utf8(normalized, MAX_ORIGINAL_FILENAME_BYTES).rstrip(" .")
    return normalized or "input.bin"


def sanitize_input_filename(value: str) -> str:
    """Return one traversal-free sandbox path component for ``value``."""

    body_name = canonical_input_filename(value)
    result: list[str] = []
    separator_pending = False
    for char in body_name:
        if char.isalnum() or char in {".", "_", "-"}:
            if separator_pending and result and result[-1] != "-":
                result.append("-")
            separator_pending = False
            result.append(char)
        else:
            separator_pending = True
    component = "".join(result).strip(" .-") or "input.bin"
    if component in {".", ".."}:
        component = "input.bin"
    if component.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
        component = f"_{component}"
    component = _truncate_utf8(component, MAX_SANDBOX_FILENAME_BYTES).rstrip(" .-")
    return component or "input.bin"


def canonical_input_mime_type(value: str) -> str:
    """Canonicalize a source MIME label without retaining parameters."""

    if type(value) is not str:
        raise CommandError("input_mime_type_invalid")
    candidate = value.split(";", 1)[0].strip().casefold()
    if len(candidate) > 127 or _MIME_TYPE.fullmatch(candidate) is None:
        return "application/octet-stream"
    return candidate


def sandbox_input_path(position: int, filename: str) -> str:
    """Derive the closed one-based path for an ordered input descriptor."""

    if type(position) is not int or not 1 <= position <= MAX_INPUT_FILES:
        raise CommandError("input_position_invalid")
    return f"{SANDBOX_INPUT_ROOT}/{position:02d}-{sanitize_input_filename(filename)}"


def _original_filename_is_canonical(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return value == canonical_input_filename(value)
    except CommandError:
        return False


@dataclass(frozen=True, slots=True)
class CommandInputDescriptor:
    """Immutable, serializable identity of one already-authorized Raw file."""

    raw_id: str
    source_identity_sha256: str
    content_sha256: str
    size_bytes: int
    original_filename: str
    mime_type: str
    sandbox_path: str

    def __post_init__(self) -> None:
        if type(self.raw_id) is not str or _RAW_ID.fullmatch(self.raw_id) is None:
            raise CommandError("input_raw_id_invalid")
        if (
            type(self.source_identity_sha256) is not str
            or _SHA256.fullmatch(self.source_identity_sha256) is None
        ):
            raise CommandError("input_source_identity_invalid")
        if type(self.content_sha256) is not str or _SHA256.fullmatch(self.content_sha256) is None:
            raise CommandError("input_content_identity_invalid")
        if (
            type(self.size_bytes) is not int
            or not 0 <= self.size_bytes <= MAX_INPUT_FILE_BYTES
        ):
            raise CommandError("input_file_size_invalid")
        if not _original_filename_is_canonical(self.original_filename):
            raise CommandError("input_filename_invalid")
        if (
            type(self.mime_type) is not str
            or len(self.mime_type) > 127
            or _MIME_TYPE.fullmatch(self.mime_type) is None
            or self.mime_type != self.mime_type.casefold()
        ):
            raise CommandError("input_mime_type_invalid")
        path_match = (
            _SANDBOX_PATH.fullmatch(self.sandbox_path)
            if type(self.sandbox_path) is str
            else None
        )
        if path_match is None or "//" in self.sandbox_path:
            raise CommandError("input_sandbox_path_invalid")
        position = int(self.sandbox_path[len(SANDBOX_INPUT_ROOT) + 1 :].split("-", 1)[0])
        if self.sandbox_path != sandbox_input_path(position, self.original_filename):
            raise CommandError("input_sandbox_path_invalid")

    def to_payload(self) -> dict[str, str | int]:
        return {
            "content_sha256": self.content_sha256,
            "mime_type": self.mime_type,
            "original_filename": self.original_filename,
            "raw_id": self.raw_id,
            "sandbox_path": self.sandbox_path,
            "size_bytes": self.size_bytes,
            "source_identity_sha256": self.source_identity_sha256,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CommandInputDescriptor:
        if type(payload) is not dict or set(payload) != _DESCRIPTOR_KEYS:
            raise CommandError("input_descriptor_shape_invalid")
        descriptor = cls(
            raw_id=payload["raw_id"],
            source_identity_sha256=payload["source_identity_sha256"],
            content_sha256=payload["content_sha256"],
            size_bytes=payload["size_bytes"],
            original_filename=payload["original_filename"],
            mime_type=payload["mime_type"],
            sandbox_path=payload["sandbox_path"],
        )
        if descriptor.to_payload() != payload:
            raise CommandError("input_descriptor_noncanonical")
        return descriptor


def command_input_descriptor(
    *,
    position: int,
    raw_id: str,
    source_identity_sha256: str,
    content_sha256: str,
    size_bytes: int,
    original_filename: str,
    mime_type: str,
) -> CommandInputDescriptor:
    """Build one canonical descriptor without accepting bytes or host paths."""

    filename = canonical_input_filename(original_filename)
    return CommandInputDescriptor(
        raw_id=raw_id,
        source_identity_sha256=source_identity_sha256,
        content_sha256=content_sha256,
        size_bytes=size_bytes,
        original_filename=filename,
        mime_type=canonical_input_mime_type(mime_type),
        sandbox_path=sandbox_input_path(position, filename),
    )


@dataclass(frozen=True, slots=True)
class CommandInputManifest:
    """Exact ordered input inventory bound into grants and receipts."""

    files: tuple[CommandInputDescriptor, ...] = ()

    def __post_init__(self) -> None:
        if type(self.files) is not tuple or len(self.files) > MAX_INPUT_FILES:
            raise CommandError("input_manifest_count_invalid")
        if any(type(item) is not CommandInputDescriptor for item in self.files):
            raise CommandError("input_descriptor_invalid")
        raw_ids = tuple(item.raw_id for item in self.files)
        if len(raw_ids) != len(set(raw_ids)):
            raise CommandError("input_raw_id_duplicate")
        paths = tuple(item.sandbox_path for item in self.files)
        if len(paths) != len(set(paths)):
            raise CommandError("input_sandbox_path_duplicate")
        if sum(item.size_bytes for item in self.files) > MAX_INPUT_TOTAL_BYTES:
            raise CommandError("input_manifest_size_invalid")
        for position, item in enumerate(self.files, start=1):
            if item.sandbox_path != sandbox_input_path(position, item.original_filename):
                raise CommandError("input_manifest_order_invalid")

    @property
    def total_size_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)

    def to_payload(self) -> dict[str, Any]:
        return {
            "files": [item.to_payload() for item in self.files],
            "schema": INPUT_MANIFEST_SCHEMA,
            "total_size_bytes": self.total_size_bytes,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    def canonical_sha256(self) -> str:
        return sha256_bytes(self.canonical_bytes())

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CommandInputManifest:
        if type(payload) is not dict or set(payload) != _MANIFEST_KEYS:
            raise CommandError("input_manifest_shape_invalid")
        if payload.get("schema") != INPUT_MANIFEST_SCHEMA or type(payload.get("files")) is not list:
            raise CommandError("input_manifest_shape_invalid")
        raw_files = payload["files"]
        files = tuple(CommandInputDescriptor.from_payload(item) for item in raw_files)
        manifest = cls(files=files)
        if type(payload.get("total_size_bytes")) is not int or manifest.to_payload() != payload:
            raise CommandError("input_manifest_noncanonical")
        return manifest


def command_input_manifest(files: Sequence[CommandInputDescriptor] = ()) -> CommandInputManifest:
    """Freeze a caller sequence into an exact ordered manifest."""

    if isinstance(files, (str, bytes, bytearray)):
        raise CommandError("input_manifest_count_invalid")
    return CommandInputManifest(files=tuple(files))


EMPTY_INPUT_MANIFEST = CommandInputManifest()
EMPTY_INPUT_MANIFEST_SHA256 = EMPTY_INPUT_MANIFEST.canonical_sha256()


__all__ = [
    "EMPTY_INPUT_MANIFEST",
    "EMPTY_INPUT_MANIFEST_SHA256",
    "INPUT_MANIFEST_SCHEMA",
    "MAX_INPUT_FILES",
    "MAX_INPUT_FILE_BYTES",
    "MAX_INPUT_TOTAL_BYTES",
    "SANDBOX_INPUT_ROOT",
    "CommandInputDescriptor",
    "CommandInputManifest",
    "canonical_input_filename",
    "canonical_input_mime_type",
    "command_input_descriptor",
    "command_input_manifest",
    "sandbox_input_path",
    "sanitize_input_filename",
]
