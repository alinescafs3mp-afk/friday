"""Small, transport-independent contracts for the Obsidian note core."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import TypeAlias

_REVISION = re.compile(r"[0-9a-f]{64}")


class ObsidianNoteError(Exception):
    """Base class for failures with a stable note-service meaning."""


class VaultPathError(ObsidianNoteError, ValueError):
    """A requested path is not a safe path below the configured vault."""


class VaultLimitError(ObsidianNoteError):
    """A synchronized vault exceeded a hard resource boundary."""


class NoteNotFoundError(ObsidianNoteError, FileNotFoundError):
    """The requested note does not exist in the vault."""


class NoteAlreadyExistsError(ObsidianNoteError, FileExistsError):
    """A create operation would replace an existing note."""


class FrontmatterError(ObsidianNoteError, ValueError):
    """Frontmatter cannot be read or safely changed."""


class InvalidPropertyError(FrontmatterError):
    """A property is outside the supported typed property contract."""


class InvalidOperationIdError(ObsidianNoteError, ValueError):
    """An operation ID is missing or too large to be an idempotency key."""


class IdempotencyConflictError(ObsidianNoteError):
    """An operation ID was already used with different arguments."""


class RevisionConflictError(ObsidianNoteError):
    """The note no longer has the revision observed by the caller."""

    def __init__(self, expected_revision: str, actual_revision: str | None) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        actual = actual_revision or "missing"
        super().__init__(f"note revision conflict: expected {expected_revision}, actual {actual}")


class PropertyType(StrEnum):
    TEXT = "text"
    LIST = "list"
    NUMBER = "number"
    CHECKBOX = "checkbox"
    DATE = "date"
    DATETIME = "datetime"


PropertyPrimitive: TypeAlias = str | tuple[str, ...] | int | float | bool | date | datetime


def validate_revision(value: str) -> str:
    """Validate the closed SHA-256 revision representation."""

    if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
        raise ValueError("expected_revision must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class PropertyValue:
    """One explicitly typed Obsidian property value."""

    type: PropertyType
    value: PropertyPrimitive

    def __post_init__(self) -> None:
        try:
            kind = PropertyType(self.type)
        except (TypeError, ValueError) as exc:
            raise InvalidPropertyError("unsupported property type") from exc
        object.__setattr__(self, "type", kind)

        value = self.value
        if kind is PropertyType.TEXT:
            if not isinstance(value, str):
                raise InvalidPropertyError("text property must be a string")
            _valid_text(value, label="text property")
        elif kind is PropertyType.LIST:
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise InvalidPropertyError("list property must contain strings")
            normalized = tuple(value)
            if any(not isinstance(item, str) for item in normalized):
                raise InvalidPropertyError("list property must contain strings")
            for item in normalized:
                _valid_text(item, label="list property item")
            object.__setattr__(self, "value", normalized)
        elif kind is PropertyType.NUMBER:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InvalidPropertyError("number property must be an int or finite float")
            if isinstance(value, float) and not math.isfinite(value):
                raise InvalidPropertyError("number property must be finite")
        elif kind is PropertyType.CHECKBOX:
            if not isinstance(value, bool):
                raise InvalidPropertyError("checkbox property must be a bool")
        elif kind is PropertyType.DATE:
            if isinstance(value, datetime) or not isinstance(value, date):
                raise InvalidPropertyError("date property must be a date")
        elif kind is PropertyType.DATETIME and not isinstance(value, datetime):
            raise InvalidPropertyError("datetime property must be a datetime")

    @classmethod
    def coerce(cls, value: PropertyInput) -> PropertyValue:
        """Accept native values or an explicit ``{type, value}`` boundary object."""

        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            if set(value) != {"type", "value"}:
                raise InvalidPropertyError("typed property object must contain only type and value")
            raw_type = value["type"]
            try:
                kind = PropertyType(str(raw_type))
            except ValueError as exc:
                raise InvalidPropertyError("unsupported property type") from exc
            return cls(kind, value["value"])  # type: ignore[arg-type]
        if isinstance(value, datetime):
            return cls(PropertyType.DATETIME, value)
        if isinstance(value, date):
            return cls(PropertyType.DATE, value)
        if isinstance(value, bool):
            return cls(PropertyType.CHECKBOX, value)
        if isinstance(value, (int, float)):
            return cls(PropertyType.NUMBER, value)
        if isinstance(value, str):
            return cls(PropertyType.TEXT, value)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return cls(PropertyType.LIST, tuple(value))  # type: ignore[arg-type]
        raise InvalidPropertyError("unsupported property value")

    def as_python(self) -> PropertyPrimitive:
        return self.value


PropertyInput: TypeAlias = (
    PropertyValue | str | Sequence[str] | int | float | bool | date | datetime | Mapping[str, object]
)


def _valid_text(value: str, *, label: str) -> None:
    if "\x00" in value:
        raise InvalidPropertyError(f"{label} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise InvalidPropertyError(f"{label} must be valid UTF-8") from exc


@dataclass(frozen=True, slots=True)
class ObsidianVaultConvention:
    daily_folder: str = "Daily"
    daily_format: str = "YYYY-MM-DD"
    template_folder: str = "Templates"
    attachment_folder: str = "Attachments"


@dataclass(frozen=True, slots=True)
class VaultDeliveryState:
    """Independent delivery facts; unknown remote facts never become success."""

    local_write_complete: bool
    server_scan_complete: bool
    android_connected: bool
    android_completion: float | None
    android_received: bool
    obsidian_opened: bool

    @classmethod
    def local_only(cls) -> VaultDeliveryState:
        return cls(
            local_write_complete=True,
            server_scan_complete=False,
            android_connected=False,
            android_completion=None,
            android_received=False,
            obsidian_opened=False,
        )


@dataclass(frozen=True, slots=True)
class NoteDocument:
    path: str
    title: str
    content: str
    body: str
    properties: Mapping[str, PropertyValue]
    revision: str
    size_bytes: int
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class NoteSummary:
    path: str
    title: str
    revision: str
    size_bytes: int
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class NoteSearchResult:
    path: str
    title: str
    excerpt: str
    revision: str
    score: float
    match_channels: tuple[str, ...]
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class NoteWriteResult:
    path: str
    revision: str
    previous_revision: str | None
    created: bool
    applied: bool
    operation_id: str | None
    delivery: VaultDeliveryState

    @property
    def local_write_complete(self) -> bool:
        return self.delivery.local_write_complete

    @property
    def server_scan_complete(self) -> bool:
        return self.delivery.server_scan_complete

    @property
    def android_connected(self) -> bool:
        return self.delivery.android_connected

    @property
    def android_completion(self) -> float | None:
        return self.delivery.android_completion

    @property
    def android_received(self) -> bool:
        return self.delivery.android_received

    @property
    def obsidian_opened(self) -> bool:
        return self.delivery.obsidian_opened


__all__ = [
    "FrontmatterError",
    "IdempotencyConflictError",
    "InvalidOperationIdError",
    "InvalidPropertyError",
    "NoteAlreadyExistsError",
    "NoteDocument",
    "NoteNotFoundError",
    "NoteSearchResult",
    "NoteSummary",
    "NoteWriteResult",
    "ObsidianNoteError",
    "ObsidianVaultConvention",
    "PropertyInput",
    "PropertyType",
    "PropertyValue",
    "RevisionConflictError",
    "VaultDeliveryState",
    "VaultLimitError",
    "VaultPathError",
    "validate_revision",
]
