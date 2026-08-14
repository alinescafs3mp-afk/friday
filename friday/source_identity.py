"""Process-private identity pins for source-derived evidence.

The public source payload is deliberately ordinary JSON.  Authority for that
payload is not: it lives on private Python carriers which ``json.dumps`` cannot
see and a caller cannot reproduce by adding a similarly named dictionary key.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_PROCESS_AUTHORITY = object()

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


def authorized_file_snapshot_token_is_process_owned(value: Any) -> bool:
    """Reject caller-constructed lookalikes at the parser admission seam."""

    return (
        type(value) is AuthorizedFileSnapshotToken
        and value._process_authority is _PROCESS_AUTHORITY
        and _raw_source_snapshot_is_process_owned(value.source)
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
    "authorized_file_snapshot_token_is_process_owned",
    "private_source_search_page",
    "raw_source_identity_sha256",
    "raw_source_snapshot",
    "source_search_page_snapshots",
]
