"""Opaque durable checkpoints for bounded DocumentCatalog convergence."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any

from friday.audit_privacy import decode_audit_privacy_key
from friday.user_ids import validate_user_id

DOCUMENT_CATALOG_WORKER_STATE_KEY = "workers:document_catalog_reconcile:cursor:v1"
_STATE_VERSION = 1
_TENANT_KEY_DOMAIN = b"friday-document-catalog-worker\0"
_PHASES = ("reconcile", "backfill")
_FAILURE_FIELDS = ("reconcile_failed", "backfill_failed")


@dataclass
class DocumentCatalogTenantState:
    """Two exact phase cursors plus sticky, body-free retry health."""

    reconcile: str | None = None
    backfill: str | None = None
    reconcile_failed: bool = False
    backfill_failed: bool = False


@dataclass
class DocumentCatalogWorkerState:
    """Decoded state; tenant keys are one-way digests and cursors remain exact."""

    cursor: int = 0
    round: int = 0
    retry: int = 0
    tenants: dict[str, DocumentCatalogTenantState] = field(default_factory=dict)


def load_document_catalog_worker_namespace_key(executor: Any) -> bytes:
    """Load the deployment-local privacy authority through storage/transaction."""

    row = executor.execute("SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'").fetchone()
    return decode_audit_privacy_key(row[0] if row is not None else None)


def document_catalog_worker_tenant_key(user_id: str, *, namespace_key: bytes) -> str:
    """Return the deployment-keyed state identity for one validated tenant."""

    owner = validate_user_id(user_id)
    if not isinstance(namespace_key, bytes) or len(namespace_key) < 32:
        raise ValueError("document catalog worker namespace key is invalid")
    return hmac.new(
        namespace_key,
        _TENANT_KEY_DOMAIN + owner.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _valid_tenant_key(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _encode_cursor(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("document catalog cursor must be exact TEXT or None")
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def _decode_cursor(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("document catalog cursor encoding is invalid")
    try:
        encoded = value.encode("ascii")
        return base64.b64decode(encoded, altchars=b"-_", validate=True).decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError) as exc:
        raise ValueError("document catalog cursor encoding is invalid") from exc


def decode_document_catalog_worker_state(value: Any) -> tuple[DocumentCatalogWorkerState, bool]:
    """Decode the closed state format; malformed/future values fail closed."""

    if value is None:
        return DocumentCatalogWorkerState(), True
    try:
        parsed = json.loads(value if isinstance(value, str) else str(value))
    except (TypeError, ValueError):
        return DocumentCatalogWorkerState(), False
    if not isinstance(parsed, dict):
        return DocumentCatalogWorkerState(), False

    version = parsed.get("version")
    expected_fields = {"version", "cursor", "round", "retry", "tenants"}
    if version != _STATE_VERSION or set(parsed) != expected_fields:
        return DocumentCatalogWorkerState(), False
    cursor = parsed.get("cursor")
    round_number = parsed.get("round")
    retry = parsed.get("retry", 0)
    encoded_tenants = parsed.get("tenants")
    if (
        type(cursor) is not int
        or cursor < 0
        or type(round_number) is not int
        or round_number < 0
        or type(retry) is not int
        or retry < 0
        or not isinstance(encoded_tenants, dict)
    ):
        return DocumentCatalogWorkerState(), False

    tenants: dict[str, DocumentCatalogTenantState] = {}
    try:
        for tenant_key, encoded_phases in encoded_tenants.items():
            expected_entry_fields = {*_PHASES, *_FAILURE_FIELDS}
            if (
                not _valid_tenant_key(tenant_key)
                or not isinstance(encoded_phases, dict)
                or set(encoded_phases) != expected_entry_fields
            ):
                raise ValueError("document catalog tenant checkpoint is invalid")
            failures = {field: encoded_phases[field] for field in _FAILURE_FIELDS}
            if any(type(item) is not bool for item in failures.values()):
                raise ValueError("document catalog tenant failure checkpoint is invalid")
            tenants[tenant_key] = DocumentCatalogTenantState(
                reconcile=_decode_cursor(encoded_phases["reconcile"]),
                backfill=_decode_cursor(encoded_phases["backfill"]),
                reconcile_failed=failures["reconcile_failed"],
                backfill_failed=failures["backfill_failed"],
            )
    except (TypeError, ValueError):
        return DocumentCatalogWorkerState(), False
    return DocumentCatalogWorkerState(
        cursor=cursor,
        round=round_number,
        retry=retry,
        tenants=tenants,
    ), True


def encode_document_catalog_worker_state(state: DocumentCatalogWorkerState) -> str:
    """Serialize one compact, deterministic state blob without plaintext ids."""

    if (
        type(state.cursor) is not int
        or state.cursor < 0
        or type(state.round) is not int
        or state.round < 0
        or type(state.retry) is not int
        or state.retry < 0
    ):
        raise ValueError("document catalog worker rotation is invalid")
    for tenant_key, tenant_state in state.tenants.items():
        if not _valid_tenant_key(tenant_key) or not isinstance(tenant_state, DocumentCatalogTenantState):
            raise ValueError("document catalog tenant checkpoint is invalid")
        if type(tenant_state.reconcile_failed) is not bool or type(tenant_state.backfill_failed) is not bool:
            raise ValueError("document catalog tenant failure checkpoint is invalid")

    payload = {
        "version": _STATE_VERSION,
        "cursor": state.cursor,
        "round": state.round,
        "retry": state.retry,
        "tenants": {
            tenant_key: {
                "reconcile": _encode_cursor(tenant_state.reconcile),
                "backfill": _encode_cursor(tenant_state.backfill),
                "reconcile_failed": tenant_state.reconcile_failed,
                "backfill_failed": tenant_state.backfill_failed,
            }
            for tenant_key, tenant_state in sorted(state.tenants.items())
        },
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def document_catalog_worker_entry_fingerprint(
    value: Any,
    user_id: str,
    *,
    namespace_key: bytes,
) -> tuple[str | None, bool]:
    """Return an opaque fingerprint for account-deletion race detection."""

    state, supported = decode_document_catalog_worker_state(value)
    if not supported:
        return None, False
    tenant_key = document_catalog_worker_tenant_key(user_id, namespace_key=namespace_key)
    entry = state.tenants.get(tenant_key)
    if entry is None:
        return None, True
    material = json.dumps(
        {
            "reconcile": _encode_cursor(entry.reconcile),
            "backfill": _encode_cursor(entry.backfill),
            "reconcile_failed": entry.reconcile_failed,
            "backfill_failed": entry.backfill_failed,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256((tenant_key + "\0" + material).encode("utf-8")).hexdigest(), True


def remove_document_catalog_worker_entry(
    value: Any,
    user_id: str,
    *,
    namespace_key: bytes,
) -> tuple[str, bool]:
    """Remove one account checkpoint while retaining every neighbour and rotation."""

    state, supported = decode_document_catalog_worker_state(value)
    if not supported:
        raise ValueError("document catalog worker state is unsupported")
    tenant_key = document_catalog_worker_tenant_key(user_id, namespace_key=namespace_key)
    removed = state.tenants.pop(tenant_key, None) is not None
    return encode_document_catalog_worker_state(state), removed


__all__ = [
    "DOCUMENT_CATALOG_WORKER_STATE_KEY",
    "DocumentCatalogTenantState",
    "DocumentCatalogWorkerState",
    "decode_document_catalog_worker_state",
    "document_catalog_worker_entry_fingerprint",
    "document_catalog_worker_tenant_key",
    "encode_document_catalog_worker_state",
    "load_document_catalog_worker_namespace_key",
    "remove_document_catalog_worker_entry",
]
