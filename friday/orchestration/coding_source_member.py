"""Immutable, body-free facts for one observed coding-source member."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

MAX_SOURCE_MEMBER_PATH_CHARS = 4_096
MAX_SOURCE_MEMBER_SIZE = (1 << 63) - 1
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class CodingSourceMemberError(ValueError):
    """A source-member fact is outside the static inspection contract."""


class CodingSourceFileKind(StrEnum):
    """Closed kinds accepted by the source inspection boundary."""

    REGULAR_FILE = "regular_file"
    DIRECTORY = "directory"

    FILE = REGULAR_FILE
    REGULAR = REGULAR_FILE
    DIR = DIRECTORY


class CodingSourceLinkKind(StrEnum):
    """Observed link metadata; links are rejected by the source tree gate."""

    NONE = "none"
    SYMLINK = "symlink"
    HARDLINK = "hardlink"

    HARD_LINK = HARDLINK


def _path(value: object) -> str:
    if type(value) is not str or not value or len(value) > MAX_SOURCE_MEMBER_PATH_CHARS:
        raise CodingSourceMemberError("relative_path is invalid")
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise CodingSourceMemberError("relative_path is invalid")
    if value.startswith(("/", "\\")) or _DRIVE_RE.match(value) is not None:
        raise CodingSourceMemberError("relative_path is absolute")
    if "\\" in value:
        raise CodingSourceMemberError("relative_path uses an unsafe separator")
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise CodingSourceMemberError("relative_path traverses or is empty")
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value:
        raise CodingSourceMemberError("relative_path is not normalized")
    return value


def _size(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_SOURCE_MEMBER_SIZE:
        raise CodingSourceMemberError("size is outside its closed bound")
    return value


def _file_kind(value: object) -> CodingSourceFileKind:
    if isinstance(value, CodingSourceFileKind):
        return value
    if type(value) is not str:
        raise CodingSourceMemberError("file_kind is not closed")
    aliases = {
        "file": CodingSourceFileKind.REGULAR_FILE,
        "regular": CodingSourceFileKind.REGULAR_FILE,
        "regular_file": CodingSourceFileKind.REGULAR_FILE,
        "directory": CodingSourceFileKind.DIRECTORY,
        "dir": CodingSourceFileKind.DIRECTORY,
    }
    try:
        return aliases[value.strip().casefold()]
    except KeyError as exc:
        raise CodingSourceMemberError("file_kind is not closed") from exc


def _link_kind(value: object) -> CodingSourceLinkKind:
    if isinstance(value, CodingSourceLinkKind):
        return value
    if type(value) is not str:
        raise CodingSourceMemberError("link_kind is not closed")
    aliases = {
        "none": CodingSourceLinkKind.NONE,
        "no_link": CodingSourceLinkKind.NONE,
        "regular": CodingSourceLinkKind.NONE,
        "symlink": CodingSourceLinkKind.SYMLINK,
        "symbolic_link": CodingSourceLinkKind.SYMLINK,
        "hardlink": CodingSourceLinkKind.HARDLINK,
        "hard_link": CodingSourceLinkKind.HARDLINK,
    }
    try:
        return aliases[value.strip().casefold()]
    except KeyError as exc:
        raise CodingSourceMemberError("link_kind is not closed") from exc


@dataclass(frozen=True, slots=True)
class CodingSourceMemberV1:
    """Immutable metadata for one source member, never its body."""

    relative_path: str
    size: int
    file_kind: CodingSourceFileKind
    executable: bool
    link_kind: CodingSourceLinkKind

    def __post_init__(self) -> None:
        relative_path = _path(self.relative_path)
        size = _size(self.size)
        file_kind = _file_kind(self.file_kind)
        link_kind = _link_kind(self.link_kind)
        if type(self.executable) is not bool:
            raise CodingSourceMemberError("executable must be boolean")
        object.__setattr__(self, "relative_path", relative_path)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "file_kind", file_kind)
        object.__setattr__(self, "link_kind", link_kind)

    @property
    def path(self) -> str:
        return self.relative_path

    @property
    def executable_bit(self) -> bool:
        return self.executable

    @property
    def is_executable(self) -> bool:
        return self.executable

    @property
    def kind(self) -> CodingSourceFileKind:
        return self.file_kind

    @property
    def link(self) -> CodingSourceLinkKind:
        return self.link_kind

    def to_mapping(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "size": self.size,
            "file_kind": self.file_kind.value,
            "executable": self.executable,
            "link_kind": self.link_kind.value,
        }


SourceMemberV1 = CodingSourceMemberV1
SourceMemberFileKind = CodingSourceFileKind
SourceMemberLinkKind = CodingSourceLinkKind


def _member_from_mapping(value: Mapping[str, object]) -> CodingSourceMemberV1:
    allowed = {
        "relative_path",
        "path",
        "name",
        "size",
        "bytes",
        "file_kind",
        "kind",
        "type",
        "executable",
        "executable_bit",
        "is_executable",
        "link_kind",
        "link_type",
        "link",
    }
    if set(value) - allowed:
        raise CodingSourceMemberError("member contains unknown fields")
    relative_path = value.get("relative_path", value.get("path", value.get("name")))
    size = value.get("size", value.get("bytes"))
    file_kind = value.get("file_kind", value.get("kind", value.get("type")))
    executable = value.get("executable", value.get("executable_bit", value.get("is_executable", False)))
    link_kind = value.get("link_kind", value.get("link_type", value.get("link", "none")))
    return CodingSourceMemberV1(
        relative_path=relative_path,  # type: ignore[arg-type]
        size=size,  # type: ignore[arg-type]
        file_kind=file_kind,  # type: ignore[arg-type]
        executable=executable,  # type: ignore[arg-type]
        link_kind=link_kind,  # type: ignore[arg-type]
    )


def build_coding_source_member(
    value: CodingSourceMemberV1 | Mapping[str, object],
) -> CodingSourceMemberV1:
    """Build one member from immutable facts without touching its path."""

    if isinstance(value, CodingSourceMemberV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        return _member_from_mapping(value)
    raise CodingSourceMemberError("member must be a mapping or CodingSourceMemberV1")


validate_coding_source_member = build_coding_source_member
SourceMember = CodingSourceMemberV1


__all__ = (
    "CodingSourceFileKind",
    "CodingSourceLinkKind",
    "CodingSourceMemberError",
    "CodingSourceMemberV1",
    "MAX_SOURCE_MEMBER_PATH_CHARS",
    "MAX_SOURCE_MEMBER_SIZE",
    "SourceMember",
    "SourceMemberFileKind",
    "SourceMemberLinkKind",
    "SourceMemberV1",
    "build_coding_source_member",
    "validate_coding_source_member",
)
