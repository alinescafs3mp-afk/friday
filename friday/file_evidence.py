"""Immutable file-evidence contracts shared by legacy and V12.

This module deliberately imports neither runtime, storage, ingestion nor
orchestration.  It describes already-authorized evidence; it cannot mint that
authority from a public dictionary or read a source by itself.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from friday.source_identity import raw_source_identity_sha256

MAX_FILE_EVIDENCE_ITEMS = 12
_PROCESS_AUTHORITY = object()
_CURRENT_TURN_REFERENCE_ATTR = "_current_turn_file_reference"


@dataclass(frozen=True, slots=True)
class CurrentTurnFileReferenceToken:
    """Unforgeable HTTP-boundary proof that this Raw row entered this turn."""

    raw_id: str
    source_identity_sha256: str
    _process_authority: object = field(repr=False, compare=False)
    reinspect_current_upload: bool = False


def stamp_current_turn_file_reference(
    carrier: Any,
    raw: Mapping[str, Any],
    *,
    reinspect_current_upload: bool = False,
) -> Any:
    """Attach a private source pin to one server-owned mapping carrier."""

    raw_id = str(raw.get("id") or "").strip()
    if re.fullmatch(r"raw_[0-9a-f]{16}", raw_id) is None:
        return carrier
    projection = {
        "id": raw_id,
        "source": raw.get("source"),
        "source_ref": raw.get("source_ref"),
        "content_type": raw.get("content_type"),
        "received_at": raw.get("received_at"),
        "content_hash": raw.get("content_hash"),
        "_raw_content": raw.get("raw_content"),
        "_raw_metadata": raw.get("metadata_json"),
    }
    token = CurrentTurnFileReferenceToken(
        raw_id=raw_id,
        source_identity_sha256=raw_source_identity_sha256(projection),
        _process_authority=_PROCESS_AUTHORITY,
        reinspect_current_upload=reinspect_current_upload is True,
    )
    try:
        object.__setattr__(carrier, _CURRENT_TURN_REFERENCE_ATTR, token)
    except (AttributeError, TypeError):
        return carrier
    return carrier


def current_turn_file_reference_of(carrier: Any) -> CurrentTurnFileReferenceToken | None:
    """Return the hidden pin only from the exact process-owned token."""

    token = getattr(carrier, _CURRENT_TURN_REFERENCE_ATTR, None)
    if (
        type(token) is not CurrentTurnFileReferenceToken
        or token._process_authority is not _PROCESS_AUTHORITY
        or str(carrier.get("raw_object_id") or "") != token.raw_id
    ):
        return None
    return token


class FileRegistrationKind(str, Enum):
    """Process-private registration class for one attachment."""

    NONE = "none"
    LEGACY = "legacy"
    INVALID = "invalid"
    VALID = "valid"


class FileBodyKind(str, Enum):
    """Process-private body class for one attachment."""

    NONE = "none"
    EMPTY = "empty"
    EXTRACTED = "extracted"
    ADVISORY = "advisory"
    PROJECTED = "projected"


@dataclass(frozen=True, slots=True)
class FileEvidenceView:
    """Immutable evidence after authorization and exact-byte verification."""

    raw_id: str | None
    source_identity_sha256: str | None
    registration: FileRegistrationKind
    disk_verified: bool
    workspace_relative_path: str | None
    workspace_sha256: str | None
    workspace_source_sha256: str | None
    body_kind: FileBodyKind
    source_complete: bool
    projection_applied: bool
    projection_empty_no_match: bool
    source_readable: bool
    verification_eligible: bool

    def identity_payload(self) -> dict[str, object]:
        return {
            "body_kind": self.body_kind.value,
            "disk_verified": self.disk_verified,
            "projection_applied": self.projection_applied,
            "projection_empty_no_match": self.projection_empty_no_match,
            "raw_id": self.raw_id,
            "registration": self.registration.value,
            "source_complete": self.source_complete,
            "source_identity_sha256": self.source_identity_sha256,
            "source_readable": self.source_readable,
            "verification_eligible": self.verification_eligible,
            "workspace_relative_path": self.workspace_relative_path,
            "workspace_sha256": self.workspace_sha256,
            "workspace_source_sha256": self.workspace_source_sha256,
        }


@dataclass(frozen=True, slots=True)
class FileEvidenceSet:
    """Closed ordered cardinality projection over private evidence views."""

    items: tuple[FileEvidenceView, ...]
    expected_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or any(
            type(item) is not FileEvidenceView for item in self.items
        ):
            raise ValueError("file evidence items must be an immutable tuple of exact views")
        if (
            not isinstance(self.expected_count, int)
            or isinstance(self.expected_count, bool)
            or not 0 <= self.expected_count <= MAX_FILE_EVIDENCE_ITEMS
        ):
            raise ValueError("file evidence expected_count is outside 0..12")

    @property
    def source_readable_count(self) -> int:
        return sum(1 for item in self.items if item.source_readable)

    @property
    def context_complete(self) -> bool:
        return bool(
            self.expected_count > 0
            and self.expected_count <= MAX_FILE_EVIDENCE_ITEMS
            and len(self.items) == self.expected_count
            and self.source_readable_count == self.expected_count
        )

    @property
    def coverage_complete(self) -> bool:
        return bool(self.context_complete and all(item.source_complete for item in self.items))

    @property
    def verification_complete(self) -> bool:
        return bool(
            self.coverage_complete
            and all(item.verification_eligible and item.source_readable for item in self.items)
        )

    def identity_sha256(self) -> str:
        """Bind exact order, cardinality and every authority-bearing field."""

        payload = {
            "expected_count": self.expected_count,
            "items": [item.identity_payload() for item in self.items],
            "schema": "friday.file-evidence-set.v1",
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "CurrentTurnFileReferenceToken",
    "FileBodyKind",
    "FileEvidenceSet",
    "FileEvidenceView",
    "FileRegistrationKind",
    "MAX_FILE_EVIDENCE_ITEMS",
    "current_turn_file_reference_of",
    "stamp_current_turn_file_reference",
]
