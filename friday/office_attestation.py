"""Installation-local authenticity for persisted Office structure indexes.

The structure index deliberately contains no document literals, but some of its
coverage claims describe binary OOXML facts that cannot be re-derived from the
stored flat text.  A keyed attestation lets later readers distinguish the index
created by Friday's parser from caller metadata or a coordinated database edit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import Any

from friday.audit_privacy import decode_audit_privacy_key

OFFICE_STRUCTURE_ATTESTATION_METADATA_KEY = "office_structure_attestation_v1"

_ATTESTATION_DOMAIN = b"friday.office-structure-attestation.v1\0"
_ATTESTATION_RE = re.compile(r"[0-9a-f]{64}")
_SOURCE_HASH_RE = re.compile(r"[0-9a-f]{64}")


def _canonical_index_bytes(index: Mapping[str, Any]) -> bytes:
    return json.dumps(
        index,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _installation_key(storage: Any) -> bytes:
    row = storage.execute("SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'").fetchone()
    return decode_audit_privacy_key(row[0] if row is not None else None)


def sign_office_structure_index(
    storage: Any,
    index: Mapping[str, Any],
    source_hash: object,
) -> str | None:
    """Return a deterministic local HMAC, or ``None`` when proof is unavailable."""

    if not isinstance(source_hash, str) or _SOURCE_HASH_RE.fullmatch(source_hash) is None:
        return None
    try:
        key = _installation_key(storage)
        payload = _ATTESTATION_DOMAIN + source_hash.encode("ascii") + b"\0" + _canonical_index_bytes(index)
        return hmac.new(key, payload, hashlib.sha256).hexdigest()
    except Exception:  # noqa: BLE001 - inability to prove origin must fail closed
        return None


def verify_office_structure_attestation(
    storage: Any,
    index: Mapping[str, Any],
    source_hash: object,
    token: object,
) -> bool:
    """Verify the exact canonical index with the installation-local key."""

    if not isinstance(token, str) or _ATTESTATION_RE.fullmatch(token) is None:
        return False
    expected = sign_office_structure_index(storage, index, source_hash)
    return expected is not None and hmac.compare_digest(expected, token)


__all__ = [
    "OFFICE_STRUCTURE_ATTESTATION_METADATA_KEY",
    "sign_office_structure_index",
    "verify_office_structure_attestation",
]
