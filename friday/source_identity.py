"""Process-private identity pins for source-derived evidence.

The public source payload is deliberately ordinary JSON.  Authority for that
payload is not: it lives on private Python carriers which ``json.dumps`` cannot
see and a caller cannot reproduce by adding a similarly named dictionary key.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_PROCESS_AUTHORITY = object()
_TOKEN_BINDING_KEY = secrets.token_bytes(32)
_MAX_TENANT_ID_BYTES = 512

_RAW_SOURCE_IDENTITY_FIELDS = (
    "id",
    "source",
    "source_ref",
    "content_type",
    "received_at",
    "content_hash",
    "_raw_content",
    "_raw_metadata",
)


@dataclass(frozen=True, slots=True)
class RawSourceSnapshot:
    """Identity of the exact canonical Raw row which supplied evidence."""

    raw_id: str
    identity_sha256: str
    _process_authority: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class AuthorizedFileSnapshotToken:
    """Same-transaction Raw identity and verified registered-byte digest."""

    source: RawSourceSnapshot
    content_sha256: str
    _process_authority: object = field(repr=False, compare=False)
    tenant_id: str | None = field(default=None, repr=False)
    storage_owner_id: str | None = field(default=None, repr=False)
    _binding_sha256: str | None = field(default=None, repr=False, compare=False)


class _PrivateSourceSearchPage(dict[str, Any]):
    """JSON-compatible page carrying identities outside its mapping fields."""

    __slots__ = ("_process_authority", "_source_snapshots")

    def __init__(
        self,
        payload: Mapping[str, Any],
        source_snapshots: Sequence[RawSourceSnapshot],
        *,
        process_authority: object,
    ) -> None:
        if process_authority is not _PROCESS_AUTHORITY:
            raise ValueError("source search authority is process-owned")
        super().__init__(payload)
        self._process_authority = process_authority
        self._source_snapshots = tuple(source_snapshots)


def _raw_source_snapshot_is_process_owned(value: Any) -> bool:
    return type(value) is RawSourceSnapshot and value._process_authority is _PROCESS_AUTHORITY


def canonical_tenant_id(value: Any) -> str | None:
    """Return one exact private scope identity or reject lookalike/coercible values."""

    if type(value) is not str or not value or value != value.strip():
        return None
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
    if len(encoded) > _MAX_TENANT_ID_BYTES or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


def _snapshot_token_binding_sha256(
    source: RawSourceSnapshot,
    content_sha256: str,
    tenant_id: str,
    storage_owner_id: str,
) -> str | None:
    if (
        type(source) is not RawSourceSnapshot
        or source._process_authority is not _PROCESS_AUTHORITY
        or type(source.raw_id) is not str
        or type(source.identity_sha256) is not str
        or type(content_sha256) is not str
        or type(tenant_id) is not str
        or type(storage_owner_id) is not str
    ):
        return None
    digest = hmac.new(_TOKEN_BINDING_KEY, b"friday/authorized-file-snapshot-token/v1\0", hashlib.sha256)
    for value in (
        str(id(source)),
        source.raw_id,
        source.identity_sha256,
        content_sha256,
        tenant_id,
        storage_owner_id,
    ):
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return None
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def raw_source_identity_sha256(raw: Mapping[str, Any]) -> str:
    """Hash the exact durable Raw projection used by every private pin."""

    digest = hashlib.sha256()
    for field_name in _RAW_SOURCE_IDENTITY_FIELDS:
        encoded = str(raw.get(field_name) or "").encode("utf-8", errors="surrogatepass")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def raw_source_snapshot(raw: Mapping[str, Any]) -> RawSourceSnapshot | None:
    """Build a typed snapshot only from one complete private Raw projection."""

    raw_id = str(raw.get("id") or "").strip()
    if not raw_id or any(field_name not in raw for field_name in _RAW_SOURCE_IDENTITY_FIELDS):
        return None
    return RawSourceSnapshot(
        raw_id=raw_id,
        identity_sha256=raw_source_identity_sha256(raw),
        _process_authority=_PROCESS_AUTHORITY,
    )


def authorized_file_snapshot_token(
    raw: Mapping[str, Any],
    *,
    content_sha256: str,
) -> AuthorizedFileSnapshotToken | None:
    """Bind verified registered bytes to their transaction-scoped Raw row."""

    source = raw_source_snapshot(raw)
    digest = str(content_sha256 or "").strip().casefold()
    if source is None or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None
    return AuthorizedFileSnapshotToken(
        source=source,
        content_sha256=digest,
        _process_authority=_PROCESS_AUTHORITY,
    )


def tenant_authorized_file_snapshot_token(
    raw: Mapping[str, Any],
    *,
    content_sha256: str,
    tenant_id: str,
    storage_owner_id: str,
) -> AuthorizedFileSnapshotToken | None:
    """Mint a token bound to exact tenant authority and the Raw storage owner."""

    tenant = canonical_tenant_id(tenant_id)
    owner = canonical_tenant_id(storage_owner_id)
    raw_owner = canonical_tenant_id(raw.get("user_id"))
    raw_id = raw.get("id")
    raw_content_sha256 = raw.get("content_hash")
    source = raw_source_snapshot(raw)
    digest = content_sha256 if type(content_sha256) is str else None
    if (
        tenant is None
        or owner is None
        or raw_owner != owner
        or type(raw_id) is not str
        or len(raw_id) != 20
        or not raw_id.startswith("raw_")
        or any(char not in "0123456789abcdef" for char in raw_id[4:])
        or type(raw_content_sha256) is not str
        or raw_content_sha256 != digest
        or source is None
        or digest is None
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        return None
    return AuthorizedFileSnapshotToken(
        source=source,
        content_sha256=digest,
        _process_authority=_PROCESS_AUTHORITY,
        tenant_id=tenant,
        storage_owner_id=owner,
        _binding_sha256=_snapshot_token_binding_sha256(source, digest, tenant, owner),
    )


def authorized_file_snapshot_token_is_process_owned(value: Any) -> bool:
    """Reject caller-constructed lookalikes at the parser admission seam."""

    if not (
        type(value) is AuthorizedFileSnapshotToken
        and value._process_authority is _PROCESS_AUTHORITY
        and _raw_source_snapshot_is_process_owned(value.source)
    ):
        return False
    if value.tenant_id is None or value.storage_owner_id is None:
        return value.tenant_id is None and value.storage_owner_id is None and value._binding_sha256 is None
    expected_binding = _snapshot_token_binding_sha256(
        value.source,
        value.content_sha256,
        value.tenant_id,
        value.storage_owner_id,
    )
    return bool(
        type(value.tenant_id) is str
        and type(value.storage_owner_id) is str
        and canonical_tenant_id(value.tenant_id) == value.tenant_id
        and canonical_tenant_id(value.storage_owner_id) == value.storage_owner_id
        and type(value._binding_sha256) is str
        and len(value._binding_sha256) == 64
        and all(char in "0123456789abcdef" for char in value._binding_sha256)
        and expected_binding is not None
        and hmac.compare_digest(value._binding_sha256, expected_binding)
    )


def authorized_file_snapshot_token_authorizes_tenant(
    value: Any,
    *,
    tenant_id: str,
) -> bool:
    """Require a process-owned token explicitly bound to this exact tenant."""

    tenant = canonical_tenant_id(tenant_id)
    return bool(
        tenant is not None
        and authorized_file_snapshot_token_is_process_owned(value)
        and value.tenant_id == tenant
        and value.storage_owner_id is not None
        and type(value.content_sha256) is str
        and len(value.content_sha256) == 64
        and all(char in "0123456789abcdef" for char in value.content_sha256)
        and type(value.source.raw_id) is str
        and len(value.source.raw_id) == 20
        and value.source.raw_id.startswith("raw_")
        and all(char in "0123456789abcdef" for char in value.source.raw_id[4:])
        and type(value.source.identity_sha256) is str
        and len(value.source.identity_sha256) == 64
        and all(char in "0123456789abcdef" for char in value.source.identity_sha256)
    )


def authorized_file_snapshot_token_authorizes_scope(
    value: Any,
    *,
    tenant_id: str,
    storage_owner_id: str,
) -> bool:
    """Require both exact tenant authority and the durable Raw storage owner."""

    owner = canonical_tenant_id(storage_owner_id)
    return bool(
        owner is not None
        and authorized_file_snapshot_token_authorizes_tenant(value, tenant_id=tenant_id)
        and value.storage_owner_id == owner
    )


def private_source_search_page(
    payload: Mapping[str, Any],
    source_snapshots: Sequence[RawSourceSnapshot],
) -> dict[str, Any]:
    """Return a JSON-compatible page whose authority is process-private."""

    snapshots = tuple(source_snapshots)
    if any(not _raw_source_snapshot_is_process_owned(snapshot) for snapshot in snapshots):
        raise ValueError("source search snapshots are not process-owned")
    return _PrivateSourceSearchPage(
        payload,
        snapshots,
        process_authority=_PROCESS_AUTHORITY,
    )


def source_search_page_snapshots(value: Any) -> tuple[RawSourceSnapshot, ...] | None:
    """Read the hidden stamp; a plain or lookalike mapping has no authority."""

    if (
        type(value) is not _PrivateSourceSearchPage
        or value._process_authority is not _PROCESS_AUTHORITY
        or any(not _raw_source_snapshot_is_process_owned(snapshot) for snapshot in value._source_snapshots)
    ):
        return None
    return value._source_snapshots


__all__ = [
    "AuthorizedFileSnapshotToken",
    "RawSourceSnapshot",
    "authorized_file_snapshot_token",
    "authorized_file_snapshot_token_authorizes_scope",
    "authorized_file_snapshot_token_authorizes_tenant",
    "authorized_file_snapshot_token_is_process_owned",
    "canonical_tenant_id",
    "private_source_search_page",
    "raw_source_identity_sha256",
    "raw_source_snapshot",
    "source_search_page_snapshots",
    "tenant_authorized_file_snapshot_token",
]
