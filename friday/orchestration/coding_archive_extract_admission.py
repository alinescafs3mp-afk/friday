"""Pure admission gate for safe extraction of observed coding archives.

This contract consumes archive-member metadata only.  It does not open an
archive, read a path, call an archive library, or create an extraction tree.
The caller remains responsible for preserving the original archive digest and
for performing any later extraction only after this gate admits every member.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

CODING_ARCHIVE_EXTRACT_ADMISSION_SCHEMA = "friday.coding-archive-extract-admission.v1"
MAX_ADMISSION_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_ARCHIVE_MEMBER_PATH_CHARS = 4_096
MAX_ARCHIVE_MEMBER_COUNT = 4_096
MAX_ARCHIVE_COMPRESSED_SIZE = 64 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_SIZE = 256 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 100.0
MAX_ARCHIVE_NESTING_DEPTH = 16
MAX_OBSERVED_MEMBER_SIZE = (1 << 63) - 1

# Convenient names for callers that prefer the shorter archive terminology.
MAX_MEMBER_COUNT = MAX_ARCHIVE_MEMBER_COUNT
MAX_COMPRESSED_SIZE = MAX_ARCHIVE_COMPRESSED_SIZE
MAX_UNCOMPRESSED_SIZE = MAX_ARCHIVE_UNCOMPRESSED_SIZE
MAX_COMPRESSION_RATIO = MAX_ARCHIVE_COMPRESSION_RATIO
MAX_NESTING_DEPTH = MAX_ARCHIVE_NESTING_DEPTH
MAX_ARCHIVE_MEMBERS = MAX_ARCHIVE_MEMBER_COUNT
MAX_ARCHIVE_TOTAL_COMPRESSED_SIZE = MAX_ARCHIVE_COMPRESSED_SIZE
MAX_ARCHIVE_TOTAL_UNCOMPRESSED_SIZE = MAX_ARCHIVE_UNCOMPRESSED_SIZE
MAX_ARCHIVE_BOMB_RATIO = MAX_ARCHIVE_COMPRESSION_RATIO
MAX_ARCHIVE_DEPTH = MAX_ARCHIVE_NESTING_DEPTH

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class CodingArchiveExtractAdmissionError(ValueError):
    """An admission identity, result, or member fact is malformed."""


class CodingArchiveExtractAdmissionState(StrEnum):
    """Closed outcomes for one archive extraction admission."""

    EMPTY = "empty"
    ADMITTED = "admitted"
    BLOCKED = "blocked"


class CodingArchiveExtractAdmissionReason(StrEnum):
    """Closed reason for one archive extraction admission."""

    NO_MEMBERS = "no_members"
    ALL_MEMBERS_SAFE = "all_members_safe"
    PATH_TRAVERSAL = "path_traversal"
    ABSOLUTE_PATH = "absolute_path"
    SYMLINK = "symlink"
    HARDLINK = "hardlink"
    DEVICE_FILE = "device_file"
    UNSUPPORTED_FILE_KIND = "unsupported_file_kind"
    COMPRESSED_SIZE_LIMIT = "compressed_size_limit"
    UNCOMPRESSED_SIZE_LIMIT = "uncompressed_size_limit"
    COMPRESSION_BOMB = "compression_bomb"
    MEMBER_COUNT_LIMIT = "member_count_limit"
    NESTING_DEPTH_LIMIT = "nesting_depth_limit"
    CASEFOLD_COLLISION = "casefold_collision"
    INVALID_FACTS = "invalid_facts"

    # Compatibility spellings retain one closed value per hazard.
    ARCHIVE_BOMB = COMPRESSION_BOMB
    BOMB_RATIO = COMPRESSION_BOMB
    CASE_FOLD_COLLISION = CASEFOLD_COLLISION
    COMPRESSED_SIZE = COMPRESSED_SIZE_LIMIT
    UNCOMPRESSED_SIZE = UNCOMPRESSED_SIZE_LIMIT
    MEMBER_COUNT = MEMBER_COUNT_LIMIT
    NESTING_DEPTH = NESTING_DEPTH_LIMIT


class CodingArchiveLinkKind(StrEnum):
    """Observed link kind for one archive member."""

    NONE = "none"
    SYMLINK = "symlink"
    HARDLINK = "hardlink"

    HARD_LINK = HARDLINK


class CodingArchiveFileKind(StrEnum):
    """Observed filesystem kind for one archive member."""

    REGULAR_FILE = "regular_file"
    DIRECTORY = "directory"
    DEVICE = "device"

    FILE = REGULAR_FILE
    REGULAR = REGULAR_FILE
    DIR = DIRECTORY
    DEVICE_FILE = DEVICE


_ALLOWED_SAFE_FILE_KINDS = frozenset({CodingArchiveFileKind.REGULAR_FILE, CodingArchiveFileKind.DIRECTORY})


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingArchiveExtractAdmissionError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _text(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum or value != value.strip():
        _fail(field, "text")
    if any(unicodedata.category(character).startswith("C") for character in value):
        _fail(field, "control")
    return cast(str, value)


def _nonnegative_size(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_OBSERVED_MEMBER_SIZE:
        _fail(field, "range")
    return cast(int, value)


def _link_kind(value: object) -> CodingArchiveLinkKind:
    try:
        text = cast(str, value)
        if text in {"none", "no_link", "regular"}:
            return CodingArchiveLinkKind.NONE
        if text in {"symlink", "symbolic_link"}:
            return CodingArchiveLinkKind.SYMLINK
        if text in {"hardlink", "hard_link"}:
            return CodingArchiveLinkKind.HARDLINK
        return CodingArchiveLinkKind(text)
    except (TypeError, ValueError) as exc:
        raise CodingArchiveExtractAdmissionError("link_kind_closed") from exc


def _file_kind(value: object) -> CodingArchiveFileKind:
    try:
        text = cast(str, value)
        if text in {"regular_file", "regular", "file"}:
            return CodingArchiveFileKind.REGULAR_FILE
        if text in {"directory", "dir"}:
            return CodingArchiveFileKind.DIRECTORY
        if text in {"device", "device_file", "char_device", "block_device", "fifo", "socket"}:
            return CodingArchiveFileKind.DEVICE
        return CodingArchiveFileKind(text)
    except (TypeError, ValueError) as exc:
        raise CodingArchiveExtractAdmissionError("file_kind_closed") from exc


def _admission(value: object) -> CodingArchiveExtractAdmissionState:
    try:
        return CodingArchiveExtractAdmissionState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingArchiveExtractAdmissionError("admission_closed") from exc


def _reason(value: object) -> CodingArchiveExtractAdmissionReason:
    try:
        return CodingArchiveExtractAdmissionReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingArchiveExtractAdmissionError("reason_closed") from exc


def _count(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_ARCHIVE_MEMBER_COUNT:
        _fail(field, "range")
    return cast(int, value)


@dataclass(frozen=True, slots=True)
class CodingArchiveMemberV1:
    """Immutable, already-observed metadata for one archive member."""

    path: str
    compressed_size: int
    uncompressed_size: int
    link_kind: CodingArchiveLinkKind
    file_kind: CodingArchiveFileKind

    def __post_init__(self) -> None:
        _text(self.path, field="path", maximum=MAX_ARCHIVE_MEMBER_PATH_CHARS)
        _nonnegative_size(self.compressed_size, field="compressed_size")
        _nonnegative_size(self.uncompressed_size, field="uncompressed_size")
        link_kind = _link_kind(self.link_kind)
        file_kind = _file_kind(self.file_kind)
        object.__setattr__(self, "link_kind", link_kind)
        object.__setattr__(self, "file_kind", file_kind)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "compressed_size": self.compressed_size,
            "uncompressed_size": self.uncompressed_size,
            "link_kind": self.link_kind.value,
            "file_kind": self.file_kind.value,
        }


CodingArchiveMemberFactsV1 = CodingArchiveMemberV1
ArchiveMemberV1 = CodingArchiveMemberV1
ArchiveMemberLinkKind = CodingArchiveLinkKind
ArchiveMemberFileKind = CodingArchiveFileKind


@dataclass(frozen=True, slots=True)
class CodingArchiveExtractAdmissionV1:
    """Immutable, body-free extraction admission summary."""

    admission_id: str
    authenticated_turn_id: str
    admission: CodingArchiveExtractAdmissionState
    admitted_member_count: int
    member_count: int
    reason: CodingArchiveExtractAdmissionReason

    def __post_init__(self) -> None:
        _identifier(self.admission_id, field="admission_id", maximum=MAX_ADMISSION_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        admission = _admission(self.admission)
        reason = _reason(self.reason)
        object.__setattr__(self, "admission", admission)
        object.__setattr__(self, "reason", reason)
        admitted = _count(self.admitted_member_count, field="admitted_member_count")
        members = _count(self.member_count, field="member_count")
        if admitted > members:
            _fail("member_counts", "inconsistent")
        if admission is CodingArchiveExtractAdmissionState.BLOCKED and (admitted or members):
            _fail("blocked_counts", "nonzero")
        if admission is CodingArchiveExtractAdmissionState.EMPTY and members:
            _fail("empty_members", "nonzero")
        if admission is CodingArchiveExtractAdmissionState.ADMITTED and (members == 0 or admitted != members):
            _fail("admitted_members", "inconsistent")

    @property
    def state(self) -> CodingArchiveExtractAdmissionState:
        return self.admission

    @property
    def closed_admission(self) -> CodingArchiveExtractAdmissionState:
        return self.admission

    @property
    def decision(self) -> CodingArchiveExtractAdmissionState:
        return self.admission

    @property
    def closed_reason(self) -> CodingArchiveExtractAdmissionReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_ARCHIVE_EXTRACT_ADMISSION_SCHEMA,
            "admission_id": self.admission_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "admission": self.admission.value,
            "admitted_member_count": self.admitted_member_count,
            "member_count": self.member_count,
            "reason": self.reason.value,
        }


CodingArchiveExtractAdmission = CodingArchiveExtractAdmissionV1
ArchiveExtractAdmission = CodingArchiveExtractAdmissionV1
CodingArchiveExtractAdmissionDecision = CodingArchiveExtractAdmissionState
ExtractAdmissionState = CodingArchiveExtractAdmissionState
ExtractAdmissionReason = CodingArchiveExtractAdmissionReason


def _canonical_member_path(path: str) -> tuple[str, ...] | None:
    # Archive names are checked with both separators so a Unix extractor cannot
    # accidentally create a Windows traversal and vice versa.
    if path.startswith(("/", "\\")) or _DRIVE_RE.match(path) is not None:
        return None
    components = tuple(component for component in re.split(r"[/\\]", path) if component)
    if not components or any(component in {".", ".."} for component in components):
        return ()
    return components


def _member_from_mapping(value: Mapping[str, Any]) -> CodingArchiveMemberV1:
    allowed = {
        "path",
        "name",
        "compressed_size",
        "compressed_bytes",
        "compressed",
        "uncompressed_size",
        "uncompressed_bytes",
        "uncompressed",
        "link_kind",
        "link_type",
        "link",
        "file_kind",
        "kind",
        "type",
    }
    if set(value) - allowed:
        _fail("member", "unknown_fields")
    try:
        path = value.get("path", value.get("name"))
        compressed = value.get("compressed_size", value.get("compressed_bytes", value.get("compressed")))
        uncompressed = value.get(
            "uncompressed_size",
            value.get("uncompressed_bytes", value.get("uncompressed")),
        )
        link_kind = value.get("link_kind", value.get("link_type", value.get("link")))
        file_kind = value.get("file_kind", value.get("kind", value.get("type")))
        return CodingArchiveMemberV1(
            path=cast(str, path),
            compressed_size=cast(int, compressed),
            uncompressed_size=cast(int, uncompressed),
            link_kind=cast(CodingArchiveLinkKind, link_kind),
            file_kind=cast(CodingArchiveFileKind, file_kind),
        )
    except (TypeError, ValueError) as exc:
        raise CodingArchiveExtractAdmissionError("member_invalid") from exc


def _members(value: object) -> tuple[CodingArchiveMemberV1, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail("members", "sequence")
    if len(value) > MAX_ARCHIVE_MEMBER_COUNT:
        _fail("members", "count")
    result: list[CodingArchiveMemberV1] = []
    for item in value:
        if isinstance(item, CodingArchiveMemberV1):
            result.append(item)
        elif isinstance(item, Mapping):
            result.append(_member_from_mapping(item))
        else:
            _fail("member", "type")
    return tuple(result)


def _result(
    admission_id: str,
    authenticated_turn_id: str,
    admission: CodingArchiveExtractAdmissionState,
    reason: CodingArchiveExtractAdmissionReason,
    *,
    admitted: int = 0,
    members: int = 0,
) -> CodingArchiveExtractAdmissionV1:
    return CodingArchiveExtractAdmissionV1(
        admission_id=admission_id,
        authenticated_turn_id=authenticated_turn_id,
        admission=admission,
        admitted_member_count=admitted,
        member_count=members,
        reason=reason,
    )


def _hazard(member: CodingArchiveMemberV1) -> CodingArchiveExtractAdmissionReason | None:
    canonical = _canonical_member_path(member.path)
    if canonical is None:
        return CodingArchiveExtractAdmissionReason.ABSOLUTE_PATH
    if not canonical:
        return CodingArchiveExtractAdmissionReason.PATH_TRAVERSAL
    if member.link_kind is CodingArchiveLinkKind.SYMLINK:
        return CodingArchiveExtractAdmissionReason.SYMLINK
    if member.link_kind is CodingArchiveLinkKind.HARDLINK:
        return CodingArchiveExtractAdmissionReason.HARDLINK
    if member.file_kind is CodingArchiveFileKind.DEVICE:
        return CodingArchiveExtractAdmissionReason.DEVICE_FILE
    if member.file_kind not in _ALLOWED_SAFE_FILE_KINDS:
        return CodingArchiveExtractAdmissionReason.UNSUPPORTED_FILE_KIND
    if member.compressed_size > MAX_ARCHIVE_COMPRESSED_SIZE:
        return CodingArchiveExtractAdmissionReason.COMPRESSED_SIZE_LIMIT
    if member.uncompressed_size > MAX_ARCHIVE_UNCOMPRESSED_SIZE:
        return CodingArchiveExtractAdmissionReason.UNCOMPRESSED_SIZE_LIMIT
    if member.compressed_size == 0 and member.uncompressed_size > 0:
        return CodingArchiveExtractAdmissionReason.COMPRESSION_BOMB
    if member.compressed_size > 0 and (
        member.uncompressed_size / member.compressed_size > MAX_ARCHIVE_COMPRESSION_RATIO
    ):
        return CodingArchiveExtractAdmissionReason.COMPRESSION_BOMB
    if len(canonical) > MAX_ARCHIVE_NESTING_DEPTH:
        return CodingArchiveExtractAdmissionReason.NESTING_DEPTH_LIMIT
    return None


def _known_mapping_keys(raw: Mapping[str, Any]) -> None:
    known = {
        "schema",
        "admission_id",
        "authenticated_turn_id",
        "members",
        "member_facts",
        "archive_members",
        "admission",
        "state",
        "admitted_member_count",
        "member_count",
        "reason",
    }
    if set(raw) - known:
        _fail("admission", "unknown_fields")


def build_coding_archive_extract_admission(
    admission_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    members: object = (),
) -> CodingArchiveExtractAdmissionV1:
    """Admit only a bounded, collision-free set of safe archive members."""

    if isinstance(admission_id, Mapping):
        raw = admission_id
        _known_mapping_keys(raw)
        if (
            raw.get("schema", CODING_ARCHIVE_EXTRACT_ADMISSION_SCHEMA)
            != CODING_ARCHIVE_EXTRACT_ADMISSION_SCHEMA
        ):
            _fail("schema")
        output_keys = {
            "admission",
            "state",
            "admitted_member_count",
            "member_count",
            "reason",
        }
        fact_keys = {"members", "member_facts", "archive_members"}
        if output_keys.intersection(raw) and fact_keys.intersection(raw):
            _fail("admission", "duplicate_representations")
        if output_keys.intersection(raw):
            return CodingArchiveExtractAdmissionV1(
                admission_id=cast(str, raw.get("admission_id")),
                authenticated_turn_id=cast(str, raw.get("authenticated_turn_id")),
                admission=cast(CodingArchiveExtractAdmissionState, raw.get("admission", raw.get("state"))),
                admitted_member_count=cast(int, raw.get("admitted_member_count")),
                member_count=cast(int, raw.get("member_count")),
                reason=cast(CodingArchiveExtractAdmissionReason, raw.get("reason")),
            )
        admission_id = cast(str, raw.get("admission_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        members = raw.get("members", raw.get("member_facts", raw.get("archive_members", ())))

    admission_key = _identifier(admission_id, field="admission_id", maximum=MAX_ADMISSION_ID_CHARS)
    turn_key = _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    try:
        member_values = _members(members)
    except CodingArchiveExtractAdmissionError as exc:
        reason = (
            CodingArchiveExtractAdmissionReason.MEMBER_COUNT_LIMIT
            if "members_count" in str(exc)
            else CodingArchiveExtractAdmissionReason.INVALID_FACTS
        )
        return _result(
            admission_key,
            turn_key,
            CodingArchiveExtractAdmissionState.BLOCKED,
            reason,
        )
    if not member_values:
        return _result(
            admission_key,
            turn_key,
            CodingArchiveExtractAdmissionState.EMPTY,
            CodingArchiveExtractAdmissionReason.NO_MEMBERS,
        )

    canonical_paths: set[str] = set()
    for member in member_values:
        hazard = _hazard(member)
        if hazard is not None:
            return _result(
                admission_key,
                turn_key,
                CodingArchiveExtractAdmissionState.BLOCKED,
                hazard,
            )
        canonical = _canonical_member_path(member.path)
        assert canonical is not None
        folded = unicodedata.normalize("NFC", "/".join(canonical)).casefold()
        if folded in canonical_paths:
            return _result(
                admission_key,
                turn_key,
                CodingArchiveExtractAdmissionState.BLOCKED,
                CodingArchiveExtractAdmissionReason.CASEFOLD_COLLISION,
            )
        canonical_paths.add(folded)

    total_compressed = sum(member.compressed_size for member in member_values)
    total_uncompressed = sum(member.uncompressed_size for member in member_values)
    if total_compressed > MAX_ARCHIVE_COMPRESSED_SIZE:
        return _result(
            admission_key,
            turn_key,
            CodingArchiveExtractAdmissionState.BLOCKED,
            CodingArchiveExtractAdmissionReason.COMPRESSED_SIZE_LIMIT,
        )
    if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_SIZE:
        return _result(
            admission_key,
            turn_key,
            CodingArchiveExtractAdmissionState.BLOCKED,
            CodingArchiveExtractAdmissionReason.UNCOMPRESSED_SIZE_LIMIT,
        )
    if total_compressed == 0 and total_uncompressed > 0:
        return _result(
            admission_key,
            turn_key,
            CodingArchiveExtractAdmissionState.BLOCKED,
            CodingArchiveExtractAdmissionReason.COMPRESSION_BOMB,
        )
    if total_compressed > 0 and total_uncompressed / total_compressed > MAX_ARCHIVE_COMPRESSION_RATIO:
        return _result(
            admission_key,
            turn_key,
            CodingArchiveExtractAdmissionState.BLOCKED,
            CodingArchiveExtractAdmissionReason.COMPRESSION_BOMB,
        )
    count = len(member_values)
    return _result(
        admission_key,
        turn_key,
        CodingArchiveExtractAdmissionState.ADMITTED,
        CodingArchiveExtractAdmissionReason.ALL_MEMBERS_SAFE,
        admitted=count,
        members=count,
    )


def validate_coding_archive_extract_admission(value: object) -> bool:
    """Return whether a frozen result or serialized result is valid."""

    try:
        if isinstance(value, CodingArchiveExtractAdmissionV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping):
            return False
        _known_mapping_keys(value)
        if value.get("schema") != CODING_ARCHIVE_EXTRACT_ADMISSION_SCHEMA:
            return False
        required = {
            "schema",
            "admission_id",
            "authenticated_turn_id",
            "admission",
            "admitted_member_count",
            "member_count",
            "reason",
        }
        if set(value) != required:
            return False
        return (
            CodingArchiveExtractAdmissionV1(
                admission_id=cast(str, value.get("admission_id")),
                authenticated_turn_id=cast(str, value.get("authenticated_turn_id")),
                admission=cast(CodingArchiveExtractAdmissionState, value.get("admission")),
                admitted_member_count=cast(int, value.get("admitted_member_count")),
                member_count=cast(int, value.get("member_count")),
                reason=cast(CodingArchiveExtractAdmissionReason, value.get("reason")),
            )
            is not None
        )
    except (TypeError, ValueError):
        return False


decide_coding_archive_extract_admission = build_coding_archive_extract_admission
build_archive_extract_admission = build_coding_archive_extract_admission
validate_archive_extract_admission = validate_coding_archive_extract_admission


__all__ = [
    "MAX_ADMISSION_ID_CHARS",
    "MAX_ARCHIVE_BOMB_RATIO",
    "MAX_ARCHIVE_COMPRESSED_SIZE",
    "MAX_ARCHIVE_COMPRESSION_RATIO",
    "MAX_ARCHIVE_DEPTH",
    "MAX_ARCHIVE_MEMBERS",
    "MAX_ARCHIVE_MEMBER_COUNT",
    "MAX_ARCHIVE_MEMBER_PATH_CHARS",
    "MAX_ARCHIVE_NESTING_DEPTH",
    "MAX_ARCHIVE_TOTAL_COMPRESSED_SIZE",
    "MAX_ARCHIVE_TOTAL_UNCOMPRESSED_SIZE",
    "MAX_ARCHIVE_UNCOMPRESSED_SIZE",
    "MAX_COMPRESSED_SIZE",
    "MAX_COMPRESSION_RATIO",
    "MAX_MEMBER_COUNT",
    "MAX_NESTING_DEPTH",
    "MAX_UNCOMPRESSED_SIZE",
    "ArchiveExtractAdmission",
    "ArchiveMemberFileKind",
    "ArchiveMemberLinkKind",
    "ArchiveMemberV1",
    "CodingArchiveExtractAdmission",
    "CodingArchiveExtractAdmissionDecision",
    "CodingArchiveExtractAdmissionError",
    "CodingArchiveExtractAdmissionReason",
    "CodingArchiveExtractAdmissionState",
    "CodingArchiveExtractAdmissionV1",
    "CodingArchiveFileKind",
    "CodingArchiveLinkKind",
    "CodingArchiveMemberFactsV1",
    "CodingArchiveMemberV1",
    "CODING_ARCHIVE_EXTRACT_ADMISSION_SCHEMA",
    "ExtractAdmissionReason",
    "ExtractAdmissionState",
    "build_archive_extract_admission",
    "build_coding_archive_extract_admission",
    "decide_coding_archive_extract_admission",
    "validate_archive_extract_admission",
    "validate_coding_archive_extract_admission",
]
