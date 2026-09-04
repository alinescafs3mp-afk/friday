"""Pure Engineer final-carrier policy.

The policy consumes already-inventoried result paths.  It does not open a
path, send a message, archive bytes, or mutate a store.  Internal command
evidence is not a user result unless the caller explicitly opts it in.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class EngineerResultCarrierKind(StrEnum):
    """The only final carrier shapes permitted for an Engineer result."""

    TEXT = "text"
    FILE = "file"
    ARCHIVE = "archive"
    AUTO = "auto"


ResultCarrierKind = EngineerResultCarrierKind
EngineerResultCarrier = EngineerResultCarrierKind


class EngineerResultPolicyError(ValueError):
    """A result carrier would be empty, misleading, or internally scoped."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "engineer_result_policy_invalid")
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class EngineerResultFile:
    """A path-only result descriptor; bytes stay with the effect owner."""

    relative_path: str
    mime_type: str = "application/octet-stream"
    size_bytes: int | None = None
    internal: bool = False


@dataclass(frozen=True, slots=True)
class EngineerResultCarrierPlan:
    carrier: EngineerResultCarrierKind
    files: tuple[EngineerResultFile, ...]
    reason: str = ""

    @property
    def kind(self) -> EngineerResultCarrierKind:
        return self.carrier

    @property
    def result_carrier(self) -> EngineerResultCarrierKind:
        return self.carrier

    @property
    def is_archive(self) -> bool:
        return self.carrier is EngineerResultCarrierKind.ARCHIVE


_INTERNAL_COMPONENTS = frozenset(
    {
        ".cache",
        "cache",
        "caches",
        "log",
        "logs",
        "manifest",
        "manifests",
        "receipt",
        "receipts",
        "tmp",
        "temp",
        "__pycache__",
    }
)
_INTERNAL_BASENAME_RE = re.compile(
    r"(?:^|[-_.])(cache|caches|log|logs|manifest|manifests|receipt|receipts|tmp|stdout|stderr)(?:[-_.]|$)"
)
_ABSOLUTE_RESULT_PATH_RE = re.compile(r"^(?:/|[A-Za-z]:[\\/])")


def _validate_result_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise EngineerResultPolicyError("result_path_invalid")
    if _ABSOLUTE_RESULT_PATH_RE.match(value) or "\\" in value or "\x00" in value:
        raise EngineerResultPolicyError("result_path_invalid")
    parts = value.split("/")
    if any(
        not part
        or part in {".", ".."}
        or any(ord(character) < 32 or ord(character) == 127 for character in part)
        for part in parts
    ):
        raise EngineerResultPolicyError("result_path_invalid")
    return value


def _path_is_internal(path: str) -> bool:
    parts = tuple(part.casefold() for part in path.split("/"))
    if any(part in _INTERNAL_COMPONENTS for part in parts[:-1]):
        return True
    basename = parts[-1]
    if basename in {"receipt", "receipt.json", "manifest", "manifest.json", "stdout", "stderr"}:
        return True
    return _INTERNAL_BASENAME_RE.search(basename) is not None


def _normalise_result_file(value: object) -> EngineerResultFile:
    if isinstance(value, EngineerResultFile):
        path = _validate_result_path(value.relative_path)
        mime_type = value.mime_type
        size_bytes = value.size_bytes
        explicit_internal = value.internal
    elif isinstance(value, str):
        path = _validate_result_path(value)
        mime_type = "application/octet-stream"
        size_bytes = None
        explicit_internal = False
    elif isinstance(value, Mapping):
        path_value = value.get("relative_path", value.get("filename", value.get("path")))
        path = _validate_result_path(path_value)
        mime_type = value.get("mime_type", "application/octet-stream")
        size_bytes = value.get("size_bytes")
        explicit_internal = value.get("internal", False)
        if not isinstance(explicit_internal, bool):
            raise EngineerResultPolicyError("result_internal_flag_invalid")
    else:
        raise EngineerResultPolicyError("result_file_invalid")
    if (
        not isinstance(mime_type, str)
        or not mime_type.strip()
        or any(ord(character) < 32 for character in mime_type)
    ):
        raise EngineerResultPolicyError("result_mime_type_invalid")
    if size_bytes is not None and (
        isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0
    ):
        raise EngineerResultPolicyError("result_size_invalid")
    return EngineerResultFile(
        relative_path=path,
        mime_type=mime_type,
        size_bytes=size_bytes,
        internal=bool(explicit_internal) or _path_is_internal(path),
    )


def _normalise_result_files(
    files: Iterable[EngineerResultFile | str | Mapping[str, Any]]
    | EngineerResultFile
    | str
    | Mapping[str, Any]
    | None,
) -> tuple[EngineerResultFile, ...]:
    if files is None:
        raw_files: tuple[object, ...] = ()
    elif isinstance(files, (EngineerResultFile, str, Mapping)):
        raw_files = (files,)
    else:
        try:
            raw_files = tuple(files)
        except TypeError as exc:
            raise EngineerResultPolicyError("result_files_invalid") from exc
    normalised = tuple(_normalise_result_file(item) for item in raw_files)
    seen: set[str] = set()
    for item in normalised:
        portable = item.relative_path.casefold()
        if portable in seen:
            raise EngineerResultPolicyError("result_file_duplicate")
        seen.add(portable)
    return tuple(sorted(normalised, key=lambda item: item.relative_path.encode("utf-8")))


def is_internal_result_file(value: EngineerResultFile | str | Mapping[str, Any]) -> bool:
    """Return whether a descriptor names command-internal evidence."""

    item = _normalise_result_file(value)
    return item.internal or _path_is_internal(item.relative_path)


def select_user_result_files(
    files: Iterable[EngineerResultFile | str | Mapping[str, Any]]
    | EngineerResultFile
    | str
    | Mapping[str, Any]
    | None,
    *,
    include_internal: bool = False,
) -> tuple[EngineerResultFile, ...]:
    """Keep user outputs and hide receipts/logs/tmp/evidence by default."""

    if not isinstance(include_internal, bool):
        raise EngineerResultPolicyError("include_internal_invalid")
    normalised = _normalise_result_files(files)
    if include_internal:
        return normalised
    return tuple(
        item for item in normalised if not item.internal and not _path_is_internal(item.relative_path)
    )


filter_user_result_files = select_user_result_files


def _carrier(value: EngineerResultCarrierKind | str) -> EngineerResultCarrierKind:
    if isinstance(value, EngineerResultCarrierKind):
        return value
    try:
        return EngineerResultCarrierKind(str(value))
    except ValueError as exc:
        raise EngineerResultPolicyError("result_carrier_invalid") from exc


def validate_engineer_result_carrier(
    carrier: EngineerResultCarrierKind | str,
    files: Iterable[EngineerResultFile | str | Mapping[str, Any]]
    | EngineerResultFile
    | str
    | Mapping[str, Any]
    | None = None,
    *,
    include_internal: bool = False,
) -> EngineerResultCarrierPlan:
    """Validate one explicit carrier, refusing empty and one-file ZIPs."""

    selected = select_user_result_files(files, include_internal=include_internal)
    requested = _carrier(carrier)
    if requested is EngineerResultCarrierKind.AUTO:
        return select_engineer_result_carrier(selected, include_internal=True)
    if requested is EngineerResultCarrierKind.TEXT:
        if selected:
            raise EngineerResultPolicyError("text_carrier_drops_files")
        return EngineerResultCarrierPlan(requested, (), "no_user_output")
    if requested is EngineerResultCarrierKind.FILE:
        if len(selected) != 1:
            raise EngineerResultPolicyError("file_carrier_requires_one_file")
        return EngineerResultCarrierPlan(requested, selected, "one_user_output")
    if not selected:
        raise EngineerResultPolicyError("empty_archive")
    if len(selected) == 1 and not selected[0].internal:
        raise EngineerResultPolicyError("single_ordinary_file_archive_forbidden")
    return EngineerResultCarrierPlan(requested, selected, "multiple_user_outputs")


def select_engineer_result_carrier(
    files: Iterable[EngineerResultFile | str | Mapping[str, Any]]
    | EngineerResultFile
    | str
    | Mapping[str, Any]
    | None,
    *,
    requested: EngineerResultCarrierKind | str | None = None,
    archive_requested: bool = False,
    include_internal: bool = False,
) -> EngineerResultCarrierPlan:
    """Select a non-empty final carrier without wrapping one ordinary file."""

    if not isinstance(archive_requested, bool):
        raise EngineerResultPolicyError("archive_requested_invalid")
    selected = select_user_result_files(files, include_internal=include_internal)
    requested_kind = EngineerResultCarrierKind.AUTO if requested is None else _carrier(requested)
    if archive_requested:
        if requested_kind not in {EngineerResultCarrierKind.AUTO, EngineerResultCarrierKind.ARCHIVE}:
            raise EngineerResultPolicyError("carrier_request_conflict")
        requested_kind = EngineerResultCarrierKind.ARCHIVE
    if requested_kind is EngineerResultCarrierKind.TEXT:
        return validate_engineer_result_carrier(requested_kind, selected, include_internal=True)
    if requested_kind is EngineerResultCarrierKind.FILE:
        return validate_engineer_result_carrier(requested_kind, selected, include_internal=True)
    if requested_kind is EngineerResultCarrierKind.ARCHIVE:
        if not selected:
            return EngineerResultCarrierPlan(
                EngineerResultCarrierKind.TEXT,
                (),
                "empty_archive_replaced_by_text",
            )
        if len(selected) == 1:
            return EngineerResultCarrierPlan(
                EngineerResultCarrierKind.FILE,
                selected,
                "single_ordinary_file_sent_directly",
            )
        return validate_engineer_result_carrier(
            EngineerResultCarrierKind.ARCHIVE,
            selected,
            include_internal=True,
        )
    if not selected:
        return EngineerResultCarrierPlan(EngineerResultCarrierKind.TEXT, (), "no_user_output")
    if len(selected) == 1:
        return EngineerResultCarrierPlan(EngineerResultCarrierKind.FILE, selected, "one_user_output")
    return validate_engineer_result_carrier(
        EngineerResultCarrierKind.ARCHIVE,
        selected,
        include_internal=True,
    )


plan_engineer_result_carrier = select_engineer_result_carrier
choose_engineer_result_carrier = select_engineer_result_carrier
visible_result_files = select_user_result_files
validate_result_carrier = validate_engineer_result_carrier


def can_build_engineer_archive(
    files: Iterable[EngineerResultFile | str | Mapping[str, Any]]
    | EngineerResultFile
    | str
    | Mapping[str, Any]
    | None,
    *,
    include_internal: bool = False,
) -> bool:
    """Return whether an archive is a truthful final carrier for ``files``."""

    try:
        return validate_engineer_result_carrier(
            EngineerResultCarrierKind.ARCHIVE,
            files,
            include_internal=include_internal,
        ).is_archive
    except EngineerResultPolicyError:
        return False


__all__ = [
    "EngineerResultCarrier",
    "EngineerResultCarrierKind",
    "EngineerResultCarrierPlan",
    "EngineerResultFile",
    "EngineerResultPolicyError",
    "ResultCarrierKind",
    "can_build_engineer_archive",
    "choose_engineer_result_carrier",
    "filter_user_result_files",
    "is_internal_result_file",
    "plan_engineer_result_carrier",
    "select_engineer_result_carrier",
    "select_user_result_files",
    "validate_engineer_result_carrier",
    "validate_result_carrier",
    "visible_result_files",
]
