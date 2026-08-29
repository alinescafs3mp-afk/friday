"""Immutable file-evidence contracts shared by legacy and V12.

This module deliberately imports neither runtime, storage, ingestion nor
orchestration.  It describes already-authorized evidence; it cannot mint that
authority from a public dictionary or read a source by itself.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast

from friday.source_identity import canonical_tenant_id, raw_source_identity_sha256

MAX_FILE_EVIDENCE_ITEMS = 12
_PROCESS_AUTHORITY = object()
_TOKEN_BINDING_KEY = secrets.token_bytes(32)
_CURRENT_TURN_REFERENCE_ATTR = "_current_turn_file_reference"


@dataclass(frozen=True, slots=True)
class CurrentTurnFileReferenceToken:
    """Unforgeable HTTP-boundary proof that this Raw row entered this turn."""

    raw_id: str
    source_identity_sha256: str
    content_sha256: str
    _process_authority: object = field(repr=False, compare=False)
    reinspect_current_upload: bool = False
    tenant_id: str | None = field(default=None, repr=False)
    _binding_sha256: str | None = field(default=None, repr=False, compare=False)


def _current_reference_binding_sha256(
    *,
    raw_id: str,
    source_identity_sha256: str,
    content_sha256: str,
    tenant_id: str,
    reinspect_current_upload: bool,
) -> str | None:
    if (
        type(raw_id) is not str
        or type(source_identity_sha256) is not str
        or type(content_sha256) is not str
        or type(tenant_id) is not str
        or type(reinspect_current_upload) is not bool
    ):
        return None
    digest = hmac.new(_TOKEN_BINDING_KEY, b"friday/current-turn-file-reference/v1\0", hashlib.sha256)
    for value in (raw_id, source_identity_sha256, content_sha256, tenant_id):
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return None
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    digest.update(b"\x01" if reinspect_current_upload else b"\x00")
    return digest.hexdigest()


def _stamp_current_turn_file_reference(
    carrier: Any,
    raw: Mapping[str, Any],
    *,
    reinspect_current_upload: bool,
    tenant_id: str | None,
) -> Any:
    raw_id_value = raw.get("id")
    digest_value = raw.get("content_hash")
    if tenant_id is not None and (type(raw_id_value) is not str or type(digest_value) is not str):
        return carrier
    if tenant_id is None:
        raw_id = str(raw_id_value or "").strip()
        content_sha256 = str(digest_value or "").strip().casefold()
    else:
        raw_id = cast(str, raw_id_value)
        content_sha256 = cast(str, digest_value)
    if re.fullmatch(r"raw_[0-9a-f]{16}", raw_id) is None:
        return carrier
    if re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None:
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
    source_identity_sha256 = raw_source_identity_sha256(projection)
    binding_sha256 = (
        _current_reference_binding_sha256(
            raw_id=raw_id,
            source_identity_sha256=source_identity_sha256,
            content_sha256=content_sha256,
            tenant_id=tenant_id,
            reinspect_current_upload=reinspect_current_upload is True,
        )
        if tenant_id is not None
        else None
    )
    token = CurrentTurnFileReferenceToken(
        raw_id=raw_id,
        source_identity_sha256=source_identity_sha256,
        content_sha256=content_sha256,
        _process_authority=_PROCESS_AUTHORITY,
        reinspect_current_upload=reinspect_current_upload is True,
        tenant_id=tenant_id,
        _binding_sha256=binding_sha256,
    )
    try:
        object.__setattr__(carrier, _CURRENT_TURN_REFERENCE_ATTR, token)
    except (AttributeError, TypeError):
        return carrier
    return carrier


def stamp_current_turn_file_reference(
    carrier: Any,
    raw: Mapping[str, Any],
    *,
    reinspect_current_upload: bool = False,
) -> Any:
    """Attach a private source pin to one server-owned mapping carrier."""

    return _stamp_current_turn_file_reference(
        carrier,
        raw,
        reinspect_current_upload=reinspect_current_upload,
        tenant_id=None,
    )


def stamp_current_turn_file_reference_for_tenant(
    carrier: Any,
    raw: Mapping[str, Any],
    *,
    tenant_id: str,
    reinspect_current_upload: bool = False,
) -> Any:
    """Attach a token only when the Raw row belongs to the exact tenant."""

    tenant = canonical_tenant_id(tenant_id)
    if tenant is None or canonical_tenant_id(raw.get("user_id")) != tenant:
        return carrier
    return _stamp_current_turn_file_reference(
        carrier,
        raw,
        reinspect_current_upload=reinspect_current_upload,
        tenant_id=tenant,
    )


def current_turn_file_reference_of(carrier: Any) -> CurrentTurnFileReferenceToken | None:
    """Return the hidden pin only from the exact process-owned token."""

    token = getattr(carrier, _CURRENT_TURN_REFERENCE_ATTR, None)
    if (
        type(token) is not CurrentTurnFileReferenceToken
        or token._process_authority is not _PROCESS_AUTHORITY
        or type(token.raw_id) is not str
        or re.fullmatch(r"raw_[0-9a-f]{16}", token.raw_id) is None
        or type(token.source_identity_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", token.source_identity_sha256) is None
        or type(token.content_sha256) is not str
        or str(carrier.get("raw_object_id") or "") != token.raw_id
        or re.fullmatch(r"[0-9a-f]{64}", token.content_sha256) is None
        or type(token.reinspect_current_upload) is not bool
        or (token.tenant_id is None and token._binding_sha256 is not None)
        or (
            token.tenant_id is not None
            and not current_turn_file_reference_token_authorizes_tenant(
                token,
                tenant_id=token.tenant_id,
            )
        )
    ):
        return None
    return token


def current_turn_file_reference_for_tenant(
    carrier: Any,
    *,
    tenant_id: str,
) -> CurrentTurnFileReferenceToken | None:
    """Return only a current-turn token explicitly bound to this tenant."""

    raw_id = carrier.get("raw_object_id") if isinstance(carrier, Mapping) else None
    token = getattr(carrier, _CURRENT_TURN_REFERENCE_ATTR, None)
    return (
        token
        if type(raw_id) is str
        and type(token) is CurrentTurnFileReferenceToken
        and raw_id == token.raw_id
        and current_turn_file_reference_token_authorizes_tenant(token, tenant_id=tenant_id)
        else None
    )


def current_turn_file_reference_token_authorizes_tenant(
    value: Any,
    *,
    tenant_id: str,
) -> bool:
    """Require a process-owned current-turn token bound to this exact tenant."""

    tenant = canonical_tenant_id(tenant_id)
    expected_binding = (
        _current_reference_binding_sha256(
            raw_id=value.raw_id,
            source_identity_sha256=value.source_identity_sha256,
            content_sha256=value.content_sha256,
            tenant_id=value.tenant_id,
            reinspect_current_upload=value.reinspect_current_upload,
        )
        if type(value) is CurrentTurnFileReferenceToken and type(value.tenant_id) is str
        else None
    )
    return bool(
        type(value) is CurrentTurnFileReferenceToken
        and value._process_authority is _PROCESS_AUTHORITY
        and tenant is not None
        and type(value.tenant_id) is str
        and value.tenant_id == tenant
        and type(value.raw_id) is str
        and re.fullmatch(r"raw_[0-9a-f]{16}", value.raw_id) is not None
        and type(value.source_identity_sha256) is str
        and re.fullmatch(r"[0-9a-f]{64}", value.source_identity_sha256) is not None
        and type(value.content_sha256) is str
        and re.fullmatch(r"[0-9a-f]{64}", value.content_sha256) is not None
        and type(value.reinspect_current_upload) is bool
        and type(value._binding_sha256) is str
        and re.fullmatch(r"[0-9a-f]{64}", value._binding_sha256) is not None
        and expected_binding is not None
        and hmac.compare_digest(value._binding_sha256, expected_binding)
    )


def retain_current_turn_file_reference(source: Any, carrier: Any) -> Any:
    """Carry the process-owned upload token across an in-memory rewrap."""

    token = current_turn_file_reference_of(source)
    if token is None or str(carrier.get("raw_object_id") or "") != token.raw_id:
        return carrier
    try:
        object.__setattr__(carrier, _CURRENT_TURN_REFERENCE_ATTR, token)
    except (AttributeError, TypeError):
        return carrier
    return carrier


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
    "current_turn_file_reference_for_tenant",
    "current_turn_file_reference_of",
    "current_turn_file_reference_token_authorizes_tenant",
    "retain_current_turn_file_reference",
    "stamp_current_turn_file_reference",
    "stamp_current_turn_file_reference_for_tenant",
]
