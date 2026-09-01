"""Exact, transaction-local storage lane for current-conversation transcripts.

This module is intentionally not mixed into :class:`FridayStorage`.  Its only
caller is the authenticated internal adapter.  The adapter first authorizes the
principal and current conversation, then issues the process-private storage
capability below inside the same caller-owned SQLite transaction.  Legacy
``message_search`` and archive readers therefore keep their existing behaviour.

Continuation tokens contain digests and row anchors, never message bodies or
plain scope identities.  They are authenticated with the deployment-local audit
privacy key and every resume replays the bounded source ledger before returning
any content.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn, SupportsIndex

from friday.audit_privacy import decode_audit_privacy_key
from friday.retrieval.contracts import MessageRole
from friday.retrieval.message_exact_contract import (
    MessageExactContinuation,
    MessageExactContractError,
    MessageExactPage,
    MessageExactRequest,
    MessageExactRow,
    _create_message_exact_page,
    _create_message_exact_row,
    _message_exact_row_revision_sha256,
)

_AUTHORITY_FACTORY = object()
_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{16}\Z")
_CONVERSATION_ID = re.compile(r"conv_[0-9a-f]{16}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TURN_ID = re.compile(r"turn_[0-9a-f]{64}\Z")

_AUTHORITY_SCHEMA = "friday.message-exact-storage-authority.v1"
_CURSOR_SCHEMA = "friday.message-exact-continuation.v1"
_LEDGER_SCHEMA = "friday.message-exact-row-ledger.v1"
_SNAPSHOT_SCHEMA = "friday.message-exact-storage-snapshot.v1"

# The transcript lane is deliberately finite even when the caller asks for full
# bodies.  We stream at most this many rows and never materialize more than one
# public page.  These are storage safety bounds, not model-output limits.
MESSAGE_EXACT_MAX_WINDOW_ROWS = 1_000
MESSAGE_EXACT_MAX_ROW_UTF8_BYTES = 256 * 1024
MESSAGE_EXACT_MAX_METADATA_UTF8_BYTES = 64 * 1024
MESSAGE_EXACT_MAX_SNAPSHOT_UTF8_BYTES = 8 * 1024 * 1024
MESSAGE_EXACT_MAX_PAGE_UTF8_BYTES = 4_000_000
MESSAGE_EXACT_MAX_CONTINUATION_BYTES = 4_096
_FETCH_BATCH = 16
_MAX_REPLY_DEPTH = 128


class MessageExactStorageError(ValueError):
    """Body-free failure at the exact transcript storage boundary."""


class MessageExactStorageDrift(MessageExactStorageError):
    """The selected snapshot changed before continuation or publication."""


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii", errors="strict")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise MessageExactStorageError("message exact canonical value is invalid") from None


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _hmac(key: bytes, *, domain: str, material: bytes) -> str:
    return hmac.new(
        key,
        domain.encode("ascii", errors="strict") + b"\x00" + material,
        hashlib.sha256,
    ).hexdigest()


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MessageExactStorageError("message exact JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> NoReturn:
    raise MessageExactStorageError(f"message exact JSON constant {value!r} is invalid")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise MessageExactStorageError("message exact JSON number is not finite")
    return parsed


def _strict_json(value: str, *, label: str) -> object:
    if not isinstance(value, str):
        raise MessageExactStorageError(f"{label} is invalid")
    try:
        return json.loads(
            value,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
            parse_float=_finite_float,
        )
    except MessageExactStorageError:
        raise
    except (UnicodeError, OverflowError, RecursionError, ValueError):
        raise MessageExactStorageError(f"{label} is invalid") from None


def _scope(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MessageExactStorageError(f"{label} is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise MessageExactStorageError(f"{label} is invalid") from None
    if len(encoded) > 200 or any(unicodedata.category(character).startswith("C") for character in value):
        raise MessageExactStorageError(f"{label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise MessageExactStorageError(f"{label} is invalid")
    return value


def _message_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _MESSAGE_ID.fullmatch(value) is None:
        raise MessageExactStorageError(f"{label} is invalid")
    return value


def _conversation_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _CONVERSATION_ID.fullmatch(value) is None:
        raise MessageExactStorageError(f"{label} is invalid")
    return value


def _utc(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 64:
        raise MessageExactStorageError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MessageExactStorageError(f"{label} is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MessageExactStorageError(f"{label} is invalid")
    normalized = parsed.astimezone(UTC).isoformat()
    if value != normalized:
        raise MessageExactStorageError(f"{label} is not canonical UTC")
    return normalized


def _private_text(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise MessageExactStorageError(f"{label} is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise MessageExactStorageError(f"{label} is invalid") from None
    if len(encoded) > maximum:
        raise MessageExactStorageError(f"{label} exceeds its storage byte limit")
    return value


def _load_key(conn: sqlite3.Connection) -> bytes:
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'").fetchone()
        return decode_audit_privacy_key(row[0] if row is not None else None)
    except Exception:  # noqa: BLE001 - missing authority must fail closed
        raise MessageExactStorageError("message exact storage key is unavailable") from None


def _require_transaction(conn: sqlite3.Connection) -> None:
    if type(conn) is not sqlite3.Connection or not conn.in_transaction:
        raise RuntimeError("message exact storage requires a caller-owned transaction")


def _request_identity(request: MessageExactRequest) -> str:
    if type(request) is not MessageExactRequest:
        raise MessageExactStorageError("message exact request is invalid")
    raw = request.to_identity_json()
    if not isinstance(raw, str) or len(raw.encode("utf-8", errors="strict")) > 16_384:
        raise MessageExactStorageError("message exact request identity is invalid")
    parsed = _strict_json(raw, label="message exact request identity")
    canonical = _canonical_bytes(parsed)
    if canonical != raw.encode("ascii", errors="strict"):
        raise MessageExactStorageError("message exact request identity is not canonical")
    return hashlib.sha256(canonical).hexdigest()


def _enum_value(value: object, *, label: str) -> str:
    raw = getattr(value, "value", None)
    if not isinstance(raw, str) or not raw:
        raise MessageExactStorageError(f"{label} is invalid")
    return raw


def _selector_payload(request: MessageExactRequest) -> dict[str, object]:
    conversation = _conversation_id(request.conversation_id, label="request conversation identity")
    boundary = _message_id(
        request.accepted_boundary_user_message_id,
        label="request accepted boundary identity",
    )
    roles = tuple(_enum_value(item, label="request role") for item in request.roles)
    if not roles or len(roles) != len(set(roles)) or roles != tuple(sorted(roles)):
        raise MessageExactStorageError("request roles are not closed and canonical")
    page_size = request.page_size
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise MessageExactStorageError("request page size is invalid")
    since = request.since
    until = request.until
    if (since is None) != (until is None):
        raise MessageExactStorageError("request time bounds are incomplete")
    if since is not None and until is not None:
        start = _utc(since, label="request since")
        end = _utc(until, label="request until")
        if start >= end:
            raise MessageExactStorageError("request time window is empty")
    else:
        start = end = None
    return {
        "schema": "friday.message-exact-selector.v1",
        "conversation_id": conversation,
        "accepted_boundary_user_message_id": boundary,
        "since": start,
        "until": end,
        "roles": list(roles),
        "page_size": page_size,
        "content_mode": _enum_value(request.content_mode, label="request content mode"),
    }


def _authorization_payload(
    value: tuple[tuple[str, str, str], ...],
    *,
    principal_id: str,
) -> list[dict[str, str]]:
    if type(value) is not tuple or len(value) != 2:
        raise MessageExactStorageError("message exact authorization bindings are invalid")
    expected = ("conversations.read", "search.use")
    result: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if type(item) is not tuple or len(item) != 3:
            raise MessageExactStorageError("message exact authorization binding is invalid")
        security_id, user_id, preset_key = item
        if security_id != expected[index] or user_id != principal_id:
            raise MessageExactStorageError("message exact authorization binding escaped its principal")
        result.append(
            {
                "security_id": security_id,
                "user_id": _scope(user_id, label="authorization user identity"),
                "preset_key": _scope(preset_key, label="authorization preset identity"),
            }
        )
    return result


class MessageExactStorageAuthority:
    """Process-private, database-keyed capability for one exact request."""

    __slots__ = (
        "_adapter_binding_sha256",
        "_authority_context_sha256",
        "_authority_handle",
        "_authorization_binding_sha256",
        "_capability_binding_sha256",
        "_context_authority_sha256",
        "_person_binding_sha256",
        "_principal_id",
        "_request",
        "_request_identity_sha256",
        "_seal",
        "_selector_sha256",
        "_tenant_binding_sha256",
        "_turn_authority_sha256",
        "_turn_id_sha256",
    )

    def __init__(
        self,
        *,
        request: MessageExactRequest,
        principal_id: str,
        material: dict[str, object],
        seal: str,
        factory: object = None,
    ) -> None:
        if factory is not _AUTHORITY_FACTORY:
            raise MessageExactStorageError("message exact storage authority is process-private")
        object.__setattr__(self, "_request", request)
        object.__setattr__(self, "_principal_id", principal_id)
        for name in (
            "authority_handle",
            "authority_context_sha256",
            "request_identity_sha256",
            "selector_sha256",
            "turn_id_sha256",
            "turn_authority_sha256",
            "context_authority_sha256",
            "tenant_binding_sha256",
            "person_binding_sha256",
            "adapter_binding_sha256",
            "capability_binding_sha256",
            "authorization_binding_sha256",
        ):
            object.__setattr__(self, f"_{name}", _digest(material[name], label=name))
        object.__setattr__(self, "_seal", _digest(seal, label="storage authority seal"))

    @property
    def authority_handle(self) -> str:
        return self._authority_handle

    @property
    def request(self) -> MessageExactRequest:
        return self._request

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("message exact storage authority is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("message exact storage authority is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("message exact storage authority is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("message exact storage authority is process-private")

    def __repr__(self) -> str:
        return "<MessageExactStorageAuthority sealed>"


def _authority_material(authority: MessageExactStorageAuthority) -> dict[str, object]:
    return {
        "schema": _AUTHORITY_SCHEMA,
        "authority_handle": authority._authority_handle,
        "authority_context_sha256": authority._authority_context_sha256,
        "request_identity_sha256": authority._request_identity_sha256,
        "selector_sha256": authority._selector_sha256,
        "turn_id_sha256": authority._turn_id_sha256,
        "turn_authority_sha256": authority._turn_authority_sha256,
        "context_authority_sha256": authority._context_authority_sha256,
        "tenant_binding_sha256": authority._tenant_binding_sha256,
        "person_binding_sha256": authority._person_binding_sha256,
        "adapter_binding_sha256": authority._adapter_binding_sha256,
        "capability_binding_sha256": authority._capability_binding_sha256,
        "authorization_binding_sha256": authority._authorization_binding_sha256,
        "principal_sha256": hashlib.sha256(authority._principal_id.encode("utf-8")).hexdigest(),
    }


def _verify_authority(
    conn: sqlite3.Connection,
    authority: MessageExactStorageAuthority,
    request: MessageExactRequest,
) -> bytes:
    if type(authority) is not MessageExactStorageAuthority or authority._request is not request:
        raise MessageExactStorageError("message exact storage authority is invalid")
    request_identity = _request_identity(request)
    selector_sha256 = _sha256(_selector_payload(request))
    if (
        request_identity != authority._request_identity_sha256
        or selector_sha256 != authority._selector_sha256
    ):
        raise MessageExactStorageError("message exact storage authority request binding changed")
    material = _authority_material(authority)
    expected_handle = _sha256({key: value for key, value in material.items() if key != "authority_handle"})
    if expected_handle != authority._authority_handle:
        raise MessageExactStorageError("message exact storage authority handle is invalid")
    key = _load_key(conn)
    expected_seal = _hmac(
        key,
        domain=_AUTHORITY_SCHEMA,
        material=_canonical_bytes(material),
    )
    if not hmac.compare_digest(expected_seal, authority._seal):
        raise MessageExactStorageError("message exact storage authority seal is invalid")
    return key


def _issue_message_exact_storage_authority_in_transaction(
    conn: sqlite3.Connection,
    *,
    request: MessageExactRequest,
    principal_id: str,
    turn_id: str,
    turn_authority_sha256: str,
    context_authority_sha256: str,
    tenant_binding_sha256: str,
    person_binding_sha256: str,
    adapter_binding_sha256: str,
    authorization_bindings: tuple[tuple[str, str, str], ...],
) -> MessageExactStorageAuthority:
    """Issue the adapter's storage-only capability after its authorization checks.

    Decision ids and human reasons are intentionally absent: the seal binds only
    the stable capability, principal and preset identities rechecked by the
    adapter.  The function is private because it must never be an API endpoint.
    """

    _require_transaction(conn)
    principal = _scope(principal_id, label="message exact principal identity")
    if not isinstance(turn_id, str) or _TURN_ID.fullmatch(turn_id) is None:
        raise MessageExactStorageError("message exact turn identity is invalid")
    request_identity_sha256 = _request_identity(request)
    selector_sha256 = _sha256(_selector_payload(request))
    authorizations = _authorization_payload(authorization_bindings, principal_id=principal)
    authorization_binding_sha256 = _sha256(
        {"schema": "friday.message-exact-authorization-bindings.v1", "items": authorizations}
    )
    capability_binding_sha256 = _sha256(
        {
            "schema": "friday.message-exact-capability-binding.v1",
            "security_ids": [item["security_id"] for item in authorizations],
            "authorization_binding_sha256": authorization_binding_sha256,
        }
    )
    stable = {
        "schema": _AUTHORITY_SCHEMA,
        "request_identity_sha256": request_identity_sha256,
        "selector_sha256": selector_sha256,
        "turn_id_sha256": hashlib.sha256(turn_id.encode("ascii")).hexdigest(),
        "turn_authority_sha256": _digest(turn_authority_sha256, label="turn authority binding"),
        "context_authority_sha256": _digest(context_authority_sha256, label="context authority binding"),
        "tenant_binding_sha256": _digest(tenant_binding_sha256, label="tenant binding"),
        "person_binding_sha256": _digest(person_binding_sha256, label="person binding"),
        "adapter_binding_sha256": _digest(adapter_binding_sha256, label="adapter binding"),
        "capability_binding_sha256": capability_binding_sha256,
        "authorization_binding_sha256": authorization_binding_sha256,
        "principal_sha256": hashlib.sha256(principal.encode("utf-8")).hexdigest(),
    }
    authority_context_sha256 = _sha256(
        {key: value for key, value in stable.items() if key not in {"request_identity_sha256"}}
    )
    material = {
        **stable,
        "authority_context_sha256": authority_context_sha256,
    }
    authority_handle = _sha256(material)
    sealed_material = {**material, "authority_handle": authority_handle}
    key = _load_key(conn)
    seal = _hmac(key, domain=_AUTHORITY_SCHEMA, material=_canonical_bytes(sealed_material))
    return MessageExactStorageAuthority(
        request=request,
        principal_id=principal,
        material=sealed_material,
        seal=seal,
        factory=_AUTHORITY_FACTORY,
    )


@dataclass(frozen=True, slots=True, repr=False)
class _StoredMaterial:
    message_id: str
    conversation_id: str
    principal_id: str
    role: str
    content: str
    metadata_json: str
    reply_to_message_id: str | None
    created_at: str
    storage_sequence: int
    storage_bytes: int


@dataclass(frozen=True, slots=True, repr=False)
class _StoredRevision(_StoredMaterial):
    reply_parent_revision_sha256: str | None
    revision_sha256: str


@dataclass(frozen=True, slots=True, repr=False)
class _ReplyRevision:
    message_id: str
    conversation_id: str
    principal_id: str
    role: str
    storage_sequence: int
    revision_sha256: str


@dataclass(slots=True)
class _ReplyRevisionBudget:
    unique_rows: int = 0
    material_bytes: int = 0

    def admit(self, material: _StoredMaterial) -> None:
        self.unique_rows += 1
        self.material_bytes += material.storage_bytes
        if (
            self.unique_rows > MESSAGE_EXACT_MAX_WINDOW_ROWS
            or self.material_bytes > MESSAGE_EXACT_MAX_SNAPSHOT_UTF8_BYTES
        ):
            raise MessageExactStorageError("message exact reply dependencies exceed their storage limits")


def _metadata(value: object) -> tuple[str, int]:
    raw = _private_text(
        value,
        label="stored message metadata",
        maximum=MESSAGE_EXACT_MAX_METADATA_UTF8_BYTES,
    )
    parsed = _strict_json(raw, label="stored message metadata")
    if not isinstance(parsed, dict):
        raise MessageExactStorageError("stored message metadata must be an object")
    _canonical_bytes(parsed)  # Reject numeric overflow to non-finite Python floats.
    raw_bytes = raw.encode("utf-8", errors="strict")
    return raw, len(raw_bytes)


def _stored_material(values: dict[str, Any], *, prefix: str) -> _StoredMaterial:
    message = _message_id(values[f"{prefix}_id"], label="stored message identity")
    conversation = _conversation_id(values[f"{prefix}_conversation_id"], label="stored conversation identity")
    principal = _scope(values[f"{prefix}_principal_id"], label="stored principal identity")
    role = values[f"{prefix}_role"]
    if role not in {"user", "assistant"}:
        raise MessageExactStorageError("stored message role is invalid")
    content = _private_text(
        values[f"{prefix}_content"],
        label="stored message content",
        maximum=MESSAGE_EXACT_MAX_ROW_UTF8_BYTES,
    )
    metadata_json, metadata_bytes = _metadata(values[f"{prefix}_metadata_json"])
    reply_to_raw = values[f"{prefix}_reply_to"]
    reply_to = None if reply_to_raw is None else _message_id(reply_to_raw, label="stored reply identity")
    created_at = _utc(values[f"{prefix}_created_at"], label="stored message timestamp")
    sequence = values[f"{prefix}_rowid"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise MessageExactStorageError("stored message sequence is invalid")
    content_bytes = len(content.encode("utf-8", errors="strict"))
    return _StoredMaterial(
        message_id=message,
        conversation_id=conversation,
        principal_id=principal,
        role=role,
        content=content,
        metadata_json=metadata_json,
        reply_to_message_id=reply_to,
        created_at=created_at,
        storage_sequence=sequence,
        storage_bytes=content_bytes + metadata_bytes,
    )


def _semantic_revision(
    material: _StoredMaterial,
    *,
    reply_parent_revision_sha256: str | None,
) -> str:
    try:
        role = MessageRole(material.role)
    except ValueError:
        raise MessageExactStorageError("stored message role is invalid") from None
    try:
        return _message_exact_row_revision_sha256(
            message_id=material.message_id,
            conversation_id=material.conversation_id,
            principal_id=material.principal_id,
            role=role,
            content=material.content,
            metadata_json=material.metadata_json,
            reply_to_message_id=material.reply_to_message_id,
            reply_revision_sha256=reply_parent_revision_sha256,
            created_at=material.created_at,
        )
    except MessageExactContractError:
        raise MessageExactStorageError("stored message revision is invalid") from None


def _resolve_reply_parent(
    conn: sqlite3.Connection,
    child: _StoredMaterial,
    *,
    boundary_sequence: int,
    cache: dict[str, _ReplyRevision],
    budget: _ReplyRevisionBudget,
    trail: tuple[str, ...],
) -> str | None:
    parent_id = child.reply_to_message_id
    if parent_id is None:
        return None
    if len(trail) >= _MAX_REPLY_DEPTH or parent_id in trail:
        raise MessageExactStorageError("stored reply chain is cyclic or too deep")
    cached = cache.get(parent_id)
    if cached is not None:
        if (
            cached.conversation_id != child.conversation_id
            or cached.principal_id != child.principal_id
            or cached.role == child.role
            or cached.storage_sequence >= child.storage_sequence
            or cached.storage_sequence >= boundary_sequence
        ):
            raise MessageExactStorageError("stored reply parent escaped its exact scope")
        return cached.revision_sha256

    # This body-free probe prevents a corrupt cross-scope edge from becoming a
    # content oracle.  Only an exact owned, older opposite-role parent earns the
    # size preflight and later query which read its revision material.
    probe_cursor = conn.execute(
        """SELECT rowid, role FROM messages
             WHERE id=? AND user_id=? AND conversation_id=? LIMIT 1""",
        (parent_id, child.principal_id, child.conversation_id),
    )
    probe = probe_cursor.fetchone()
    probe_cursor.close()
    if (
        probe is None
        or isinstance(probe[0], bool)
        or not isinstance(probe[0], int)
        or probe[0] <= 0
        or probe[0] >= child.storage_sequence
        or probe[0] >= boundary_sequence
        or probe[1] not in {"user", "assistant"}
        or probe[1] == child.role
    ):
        raise MessageExactStorageError("stored reply parent escaped its exact scope")
    size_cursor = conn.execute(
        """SELECT length(CAST(content AS BLOB)),
                  length(CAST(metadata_json AS BLOB))
             FROM messages
            WHERE id=? AND user_id=? AND conversation_id=? AND rowid=? LIMIT 1""",
        (parent_id, child.principal_id, child.conversation_id, probe[0]),
    )
    sizes = size_cursor.fetchone()
    size_cursor.close()
    if (
        sizes is None
        or isinstance(sizes[0], bool)
        or not isinstance(sizes[0], int)
        or not 0 <= sizes[0] <= MESSAGE_EXACT_MAX_ROW_UTF8_BYTES
        or isinstance(sizes[1], bool)
        or not isinstance(sizes[1], int)
        or not 0 < sizes[1] <= MESSAGE_EXACT_MAX_METADATA_UTF8_BYTES
    ):
        raise MessageExactStorageError("stored reply parent is outside storage limits")
    fields = _select_fields("parent", "parent")
    cursor = conn.execute(
        f"""SELECT {fields} FROM messages parent
              WHERE parent.id=? AND parent.user_id=?
                AND parent.conversation_id=? AND parent.rowid=? LIMIT 1""",  # nosec B608
        (parent_id, child.principal_id, child.conversation_id, probe[0]),
    )
    raw = cursor.fetchone()
    if raw is None:
        cursor.close()
        raise MessageExactStorageError("stored reply parent changed during selection")
    parent = _stored_material(_record(cursor, raw), prefix="parent")
    cursor.close()
    if (
        parent.message_id != parent_id
        or parent.conversation_id != child.conversation_id
        or parent.principal_id != child.principal_id
        or parent.role == child.role
        or parent.storage_sequence != probe[0]
    ):
        raise MessageExactStorageError("stored reply parent escaped its exact scope")
    budget.admit(parent)
    grandparent_revision = _resolve_reply_parent(
        conn,
        parent,
        boundary_sequence=boundary_sequence,
        cache=cache,
        budget=budget,
        trail=(*trail, parent_id),
    )
    revision = _semantic_revision(
        parent,
        reply_parent_revision_sha256=grandparent_revision,
    )
    cache[parent_id] = _ReplyRevision(
        message_id=parent.message_id,
        conversation_id=parent.conversation_id,
        principal_id=parent.principal_id,
        role=parent.role,
        storage_sequence=parent.storage_sequence,
        revision_sha256=revision,
    )
    return revision


def _revision_with_parent(
    conn: sqlite3.Connection,
    values: dict[str, Any],
    *,
    prefix: str,
    boundary_sequence: int,
    cache: dict[str, _ReplyRevision],
    reply_budget: _ReplyRevisionBudget,
) -> _StoredRevision:
    material = _stored_material(values, prefix=prefix)
    parent_revision = _resolve_reply_parent(
        conn,
        material,
        boundary_sequence=boundary_sequence,
        cache=cache,
        budget=reply_budget,
        trail=(material.message_id,),
    )
    revision = _semantic_revision(
        material,
        reply_parent_revision_sha256=parent_revision,
    )
    observed = cache.get(material.message_id)
    if observed is not None and (
        observed.conversation_id != material.conversation_id
        or observed.principal_id != material.principal_id
        or observed.role != material.role
        or observed.storage_sequence != material.storage_sequence
        or observed.revision_sha256 != revision
    ):
        raise MessageExactStorageError("stored message revision changed within its snapshot")
    cache[material.message_id] = _ReplyRevision(
        message_id=material.message_id,
        conversation_id=material.conversation_id,
        principal_id=material.principal_id,
        role=material.role,
        storage_sequence=material.storage_sequence,
        revision_sha256=revision,
    )
    return _StoredRevision(
        message_id=material.message_id,
        conversation_id=material.conversation_id,
        principal_id=material.principal_id,
        role=material.role,
        content=material.content,
        metadata_json=material.metadata_json,
        reply_to_message_id=material.reply_to_message_id,
        created_at=material.created_at,
        storage_sequence=material.storage_sequence,
        storage_bytes=material.storage_bytes,
        reply_parent_revision_sha256=parent_revision,
        revision_sha256=revision,
    )


def _select_fields(alias: str, prefix: str) -> str:
    return ", ".join(
        (
            f"{alias}.id AS {prefix}_id",
            f"{alias}.conversation_id AS {prefix}_conversation_id",
            f"{alias}.user_id AS {prefix}_principal_id",
            f"{alias}.role AS {prefix}_role",
            f"{alias}.content AS {prefix}_content",
            f"{alias}.metadata_json AS {prefix}_metadata_json",
            f"{alias}.reply_to AS {prefix}_reply_to",
            f"{alias}.created_at AS {prefix}_created_at",
            f"{alias}.rowid AS {prefix}_rowid",
        )
    )


def _record(cursor: sqlite3.Cursor, row: sqlite3.Row | tuple[object, ...]) -> dict[str, Any]:
    columns = tuple(str(item[0]) for item in (cursor.description or ()))
    return dict(zip(columns, tuple(row), strict=True))


def _probe_boundary(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    conversation_id: str,
    boundary_message_id: str,
) -> int | None:
    # Deliberately body-free and first: no COUNT, message content or metadata is
    # touched until both the adapter-issued token and this ownership probe pass.
    cursor = conn.execute(
        """SELECT boundary.rowid
               FROM users principal
               JOIN conversations owned
                 ON owned.user_id=principal.id AND owned.id=?
               JOIN messages boundary
                 ON boundary.id=?
                AND boundary.user_id=principal.id
                AND boundary.conversation_id=owned.id
              WHERE principal.id=? AND principal.status='active'
                AND boundary.role='user'
              LIMIT 1""",
        (conversation_id, boundary_message_id, principal_id),
    )
    row = cursor.fetchone()
    cursor.close()
    if row is None:
        return None
    sequence = row[0]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise MessageExactStorageError("stored accepted boundary sequence is invalid")
    return sequence


def _read_boundary(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    conversation_id: str,
    boundary_message_id: str,
    boundary_sequence: int,
    revision_cache: dict[str, _ReplyRevision],
    reply_budget: _ReplyRevisionBudget,
) -> _StoredRevision:
    size_cursor = conn.execute(
        """SELECT length(CAST(content AS BLOB)),
                  length(CAST(metadata_json AS BLOB))
             FROM messages
            WHERE id=? AND user_id=? AND conversation_id=? AND rowid=?
              AND role='user' LIMIT 1""",
        (boundary_message_id, principal_id, conversation_id, boundary_sequence),
    )
    sizes = size_cursor.fetchone()
    size_cursor.close()
    if (
        sizes is None
        or isinstance(sizes[0], bool)
        or not isinstance(sizes[0], int)
        or not 0 <= sizes[0] <= MESSAGE_EXACT_MAX_ROW_UTF8_BYTES
        or isinstance(sizes[1], bool)
        or not isinstance(sizes[1], int)
        or not 0 < sizes[1] <= MESSAGE_EXACT_MAX_METADATA_UTF8_BYTES
    ):
        raise MessageExactStorageError("stored accepted boundary is outside storage limits")
    child_fields = _select_fields("boundary", "boundary")
    cursor = conn.execute(
        f"""SELECT {child_fields}
                FROM messages boundary
               WHERE boundary.id=? AND boundary.user_id=?
                 AND boundary.conversation_id=? AND boundary.rowid=?
                 AND boundary.role='user'
               LIMIT 1""",  # nosec B608 - selected aliases are module constants
        (boundary_message_id, principal_id, conversation_id, boundary_sequence),
    )
    row = cursor.fetchone()
    if row is None:
        cursor.close()
        raise MessageExactStorageError("accepted message boundary changed during selection")
    values = _record(cursor, row)
    cursor.close()
    boundary = _revision_with_parent(
        conn,
        values,
        prefix="boundary",
        boundary_sequence=boundary_sequence,
        cache=revision_cache,
        reply_budget=reply_budget,
    )
    if (
        boundary.message_id != boundary_message_id
        or boundary.conversation_id != conversation_id
        or boundary.principal_id != principal_id
        or boundary.storage_sequence != boundary_sequence
        or boundary.role != "user"
    ):
        raise MessageExactStorageError("accepted message boundary escaped its exact scope")
    return boundary


def _ledger_seed() -> str:
    return _sha256({"schema": _LEDGER_SCHEMA, "rows": []})


def _ledger_extend(previous: str, row: _StoredRevision) -> str:
    return _sha256(
        {
            "schema": _LEDGER_SCHEMA,
            "previous_sha256": previous,
            "storage_sequence": row.storage_sequence,
            "revision_sha256": row.revision_sha256,
        }
    )


def _to_contract_row(row: _StoredRevision) -> MessageExactRow:
    try:
        role = MessageRole(row.role)
    except ValueError:
        raise MessageExactStorageError("stored message role is invalid") from None
    return _create_message_exact_row(
        message_id=row.message_id,
        conversation_id=row.conversation_id,
        principal_id=row.principal_id,
        role=role,
        content=row.content,
        metadata_json=row.metadata_json,
        reply_to_message_id=row.reply_to_message_id,
        reply_revision_sha256=row.reply_parent_revision_sha256,
        created_at=row.created_at,
        storage_sequence=row.storage_sequence,
    )


@dataclass(frozen=True, slots=True, repr=False)
class _CursorState:
    offset: int
    total_rows: int
    snapshot_bytes: int
    full_ledger_sha256: str
    prefix_ledger_sha256: str
    anchor_storage_sequence: int
    anchor_revision_sha256: str
    boundary_revision_sha256: str
    snapshot_handle: str


def _cursor_binding(authority: MessageExactStorageAuthority) -> dict[str, str]:
    return {
        "authority_context_sha256": authority._authority_context_sha256,
        "selector_sha256": authority._selector_sha256,
        "turn_id_sha256": authority._turn_id_sha256,
        "turn_authority_sha256": authority._turn_authority_sha256,
        "context_authority_sha256": authority._context_authority_sha256,
        "tenant_binding_sha256": authority._tenant_binding_sha256,
        "person_binding_sha256": authority._person_binding_sha256,
        "adapter_binding_sha256": authority._adapter_binding_sha256,
        "capability_binding_sha256": authority._capability_binding_sha256,
        "authorization_binding_sha256": authority._authorization_binding_sha256,
        "scope_sha256": _sha256(
            {
                "schema": "friday.message-exact-scope.v1",
                "person_binding_sha256": authority._person_binding_sha256,
                "conversation_id": authority._request.conversation_id,
                "boundary_message_id": authority._request.accepted_boundary_user_message_id,
            }
        ),
    }


def _encode_cursor(
    key: bytes,
    authority: MessageExactStorageAuthority,
    state: _CursorState,
) -> str:
    payload: dict[str, object] = {
        "schema": _CURSOR_SCHEMA,
        **_cursor_binding(authority),
        "offset": state.offset,
        "total_rows": state.total_rows,
        "snapshot_bytes": state.snapshot_bytes,
        "full_ledger_sha256": state.full_ledger_sha256,
        "prefix_ledger_sha256": state.prefix_ledger_sha256,
        "anchor_storage_sequence": state.anchor_storage_sequence,
        "anchor_revision_sha256": state.anchor_revision_sha256,
        "boundary_revision_sha256": state.boundary_revision_sha256,
        "snapshot_handle": state.snapshot_handle,
    }
    payload_bytes = _canonical_bytes(payload)
    envelope = {
        "payload": payload,
        "signature": _hmac(key, domain=_CURSOR_SCHEMA, material=payload_bytes),
    }
    encoded = base64.urlsafe_b64encode(_canonical_bytes(envelope)).rstrip(b"=").decode("ascii")
    if len(encoded.encode("ascii")) > MESSAGE_EXACT_MAX_CONTINUATION_BYTES:
        raise MessageExactStorageError("message exact continuation exceeds its byte limit")
    return encoded


def _decode_cursor(
    token: str,
    *,
    key: bytes,
    authority: MessageExactStorageAuthority,
) -> _CursorState:
    if (
        not isinstance(token, str)
        or not token
        or token != token.strip()
        or len(token.encode("utf-8", errors="strict")) > MESSAGE_EXACT_MAX_CONTINUATION_BYTES
        or re.fullmatch(r"[A-Za-z0-9_-]+", token) is None
    ):
        raise MessageExactStorageError("message exact continuation is invalid")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, UnicodeError):
        raise MessageExactStorageError("message exact continuation is invalid") from None
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != token:
        raise MessageExactStorageError("message exact continuation is not canonical")
    try:
        text = raw.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        raise MessageExactStorageError("message exact continuation is invalid") from None
    envelope = _strict_json(text, label="message exact continuation")
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
        raise MessageExactStorageError("message exact continuation envelope is invalid")
    if raw != _canonical_bytes(envelope):
        raise MessageExactStorageError("message exact continuation envelope is not canonical")
    payload = envelope["payload"]
    signature = envelope["signature"]
    if (
        not isinstance(payload, dict)
        or not isinstance(signature, str)
        or _SHA256.fullmatch(signature) is None
    ):
        raise MessageExactStorageError("message exact continuation envelope is invalid")
    expected_keys = {
        "schema",
        *_cursor_binding(authority),
        "offset",
        "total_rows",
        "snapshot_bytes",
        "full_ledger_sha256",
        "prefix_ledger_sha256",
        "anchor_storage_sequence",
        "anchor_revision_sha256",
        "boundary_revision_sha256",
        "snapshot_handle",
    }
    if set(payload) != expected_keys or payload.get("schema") != _CURSOR_SCHEMA:
        raise MessageExactStorageError("message exact continuation payload is invalid")
    payload_bytes = _canonical_bytes(payload)
    expected_signature = _hmac(key, domain=_CURSOR_SCHEMA, material=payload_bytes)
    if not hmac.compare_digest(signature, expected_signature):
        raise MessageExactStorageError("message exact continuation signature is invalid")
    for binding, expected in _cursor_binding(authority).items():
        if payload.get(binding) != expected:
            raise MessageExactStorageError("message exact continuation authority changed")

    def integer(name: str, *, low: int, high: int) -> int:
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise MessageExactStorageError("message exact continuation counter is invalid")
        return value

    offset = integer("offset", low=1, high=MESSAGE_EXACT_MAX_WINDOW_ROWS)
    total = integer("total_rows", low=offset + 1, high=MESSAGE_EXACT_MAX_WINDOW_ROWS)
    snapshot_bytes = integer("snapshot_bytes", low=0, high=MESSAGE_EXACT_MAX_SNAPSHOT_UTF8_BYTES)
    return _CursorState(
        offset=offset,
        total_rows=total,
        snapshot_bytes=snapshot_bytes,
        full_ledger_sha256=_digest(payload["full_ledger_sha256"], label="cursor ledger"),
        prefix_ledger_sha256=_digest(payload["prefix_ledger_sha256"], label="cursor prefix"),
        anchor_storage_sequence=integer("anchor_storage_sequence", low=1, high=2**63 - 1),
        anchor_revision_sha256=_digest(payload["anchor_revision_sha256"], label="cursor anchor revision"),
        boundary_revision_sha256=_digest(
            payload["boundary_revision_sha256"], label="cursor boundary revision"
        ),
        snapshot_handle=_digest(payload["snapshot_handle"], label="cursor snapshot"),
    )


@dataclass(frozen=True, slots=True, repr=False)
class _ScanResult:
    page_rows: tuple[_StoredRevision, ...]
    total_rows: int
    snapshot_bytes: int
    full_ledger_sha256: str
    resume_prefix_sha256: str
    resume_anchor: _StoredRevision | None
    next_prefix_sha256: str
    next_anchor: _StoredRevision | None


def _scan_rows(
    conn: sqlite3.Connection,
    *,
    request: MessageExactRequest,
    principal_id: str,
    boundary: _StoredRevision,
    offset: int,
    revision_cache: dict[str, _ReplyRevision],
    reply_budget: _ReplyRevisionBudget,
) -> _ScanResult:
    selector = _selector_payload(request)
    roles = tuple(selector["roles"])
    placeholders = ",".join("?" for _item in roles)
    time_clause = ""
    time_parameters: tuple[object, ...] = ()
    if selector["since"] is not None:
        # Both request and stored values are required to be canonical UTC
        # ``+00:00`` text, so direct comparison preserves every accepted
        # fractional digit.  SQLite's julianday() rounds sub-milliseconds.
        time_clause = "AND message.created_at>=? AND message.created_at<?"
        time_parameters = (selector["since"], selector["until"])
    child_fields = _select_fields("message", "message")
    parameters = (
        principal_id,
        request.conversation_id,
        boundary.storage_sequence,
        *roles,
        *time_parameters,
    )
    preflight = conn.execute(
        f"""WITH bounded AS MATERIALIZED (
                   SELECT message.rowid AS message_rowid
                     FROM messages message
                    WHERE message.user_id=? AND message.conversation_id=?
                      AND message.rowid<? AND message.role IN ({placeholders})
                      {time_clause}
                    ORDER BY message.created_at ASC, message.rowid ASC
                    LIMIT {MESSAGE_EXACT_MAX_WINDOW_ROWS + 1}
               )
               SELECT COUNT(*),
                      COALESCE(SUM(length(CAST(message.content AS BLOB))
                                   + length(CAST(message.metadata_json AS BLOB))), 0),
                      COALESCE(MAX(length(CAST(message.content AS BLOB))), 0),
                      COALESCE(MAX(length(CAST(message.metadata_json AS BLOB))), 0)
                 FROM bounded
                 JOIN messages message ON message.rowid=bounded.message_rowid""",  # nosec B608
        parameters,
    ).fetchone()
    if (
        preflight is None
        or any(isinstance(value, bool) or not isinstance(value, int) for value in preflight)
        or not 0 <= preflight[0] <= MESSAGE_EXACT_MAX_WINDOW_ROWS
        or not 0 <= preflight[1] <= MESSAGE_EXACT_MAX_SNAPSHOT_UTF8_BYTES
        or not 0 <= preflight[2] <= MESSAGE_EXACT_MAX_ROW_UTF8_BYTES
        or not 0 <= preflight[3] <= MESSAGE_EXACT_MAX_METADATA_UTF8_BYTES
    ):
        raise MessageExactStorageError("message exact transcript exceeds its storage limits")
    cursor = conn.execute(
        f"""SELECT {child_fields}
                FROM messages message
               WHERE message.user_id=? AND message.conversation_id=?
                 AND message.rowid<? AND message.role IN ({placeholders})
                 {time_clause}
               ORDER BY message.created_at ASC, message.rowid ASC""",  # nosec B608 - placeholders and fixed clause only
        parameters,
    )
    ledger = _ledger_seed()
    total = 0
    snapshot_bytes = 0
    page_bytes = boundary.storage_bytes
    if page_bytes > MESSAGE_EXACT_MAX_PAGE_UTF8_BYTES:
        raise MessageExactStorageError("message exact page exceeds its carrier byte limit")
    page_rows: list[_StoredRevision] = []
    resume_prefix = _ledger_seed() if offset == 0 else ""
    resume_anchor: _StoredRevision | None = None
    next_prefix = ""
    next_anchor: _StoredRevision | None = None
    page_end = offset + request.page_size
    try:
        while True:
            batch = cursor.fetchmany(_FETCH_BATCH)
            if not batch:
                break
            columns = tuple(str(item[0]) for item in (cursor.description or ()))
            for raw in batch:
                row = _revision_with_parent(
                    conn,
                    dict(zip(columns, tuple(raw), strict=True)),
                    prefix="message",
                    boundary_sequence=boundary.storage_sequence,
                    cache=revision_cache,
                    reply_budget=reply_budget,
                )
                if (
                    row.principal_id != principal_id
                    or row.conversation_id != request.conversation_id
                    or row.storage_sequence >= boundary.storage_sequence
                    or row.role not in roles
                ):
                    raise MessageExactStorageError("stored transcript row escaped its exact selector")
                total += 1
                if total > MESSAGE_EXACT_MAX_WINDOW_ROWS:
                    raise MessageExactStorageError("message exact transcript exceeds its row limit")
                snapshot_bytes += row.storage_bytes
                if snapshot_bytes > MESSAGE_EXACT_MAX_SNAPSHOT_UTF8_BYTES:
                    raise MessageExactStorageError("message exact transcript exceeds its byte limit")
                ledger = _ledger_extend(ledger, row)
                if total == offset:
                    resume_prefix = ledger
                    resume_anchor = row
                if offset < total <= page_end:
                    page_bytes += row.storage_bytes
                    if page_bytes > MESSAGE_EXACT_MAX_PAGE_UTF8_BYTES:
                        raise MessageExactStorageError("message exact page exceeds its carrier byte limit")
                    page_rows.append(row)
                if total == page_end:
                    next_prefix = ledger
                    next_anchor = row
    finally:
        cursor.close()
    if offset > total:
        raise MessageExactStorageError("message exact continuation offset exceeds its snapshot")
    if total != preflight[0] or snapshot_bytes != preflight[1]:
        raise MessageExactStorageError("message exact transcript changed during its snapshot")
    if offset > 0 and (not resume_prefix or resume_anchor is None):
        raise MessageExactStorageError("message exact continuation anchor is unavailable")
    return _ScanResult(
        page_rows=tuple(page_rows),
        total_rows=total,
        snapshot_bytes=snapshot_bytes,
        full_ledger_sha256=ledger,
        resume_prefix_sha256=resume_prefix,
        resume_anchor=resume_anchor,
        next_prefix_sha256=next_prefix,
        next_anchor=next_anchor,
    )


def _snapshot_handle(
    authority: MessageExactStorageAuthority,
    boundary: _StoredRevision,
    scan: _ScanResult,
) -> str:
    return _sha256(
        {
            "schema": _SNAPSHOT_SCHEMA,
            "authority_context_sha256": authority._authority_context_sha256,
            "selector_sha256": authority._selector_sha256,
            "boundary_revision_sha256": boundary.revision_sha256,
            "full_ledger_sha256": scan.full_ledger_sha256,
            "total_rows": scan.total_rows,
            "snapshot_bytes": scan.snapshot_bytes,
        }
    )


def select_message_exact_page_in_transaction(
    conn: sqlite3.Connection,
    authority: MessageExactStorageAuthority,
    request: MessageExactRequest | None = None,
) -> MessageExactPage:
    """Select one exact page after authority and current ownership are proven."""

    _require_transaction(conn)
    selected_request = authority.request if request is None else request
    key = _verify_authority(conn, authority, selected_request)
    continuation = selected_request.continuation
    cursor_state: _CursorState | None = None
    if continuation is not None:
        if type(continuation) is not MessageExactContinuation:
            raise MessageExactStorageError("message exact continuation carrier is invalid")
        cursor_state = _decode_cursor(continuation.token, key=key, authority=authority)
    selector = _selector_payload(selected_request)
    try:
        boundary_sequence = _probe_boundary(
            conn,
            principal_id=authority._principal_id,
            conversation_id=str(selector["conversation_id"]),
            boundary_message_id=str(selector["accepted_boundary_user_message_id"]),
        )
        if boundary_sequence is None:
            raise MessageExactStorageError("message exact current conversation is unavailable")
        revision_cache: dict[str, _ReplyRevision] = {}
        reply_budget = _ReplyRevisionBudget()
        boundary = _read_boundary(
            conn,
            principal_id=authority._principal_id,
            conversation_id=str(selector["conversation_id"]),
            boundary_message_id=str(selector["accepted_boundary_user_message_id"]),
            boundary_sequence=boundary_sequence,
            revision_cache=revision_cache,
            reply_budget=reply_budget,
        )
        offset = 0 if cursor_state is None else cursor_state.offset
        scan = _scan_rows(
            conn,
            request=selected_request,
            principal_id=authority._principal_id,
            boundary=boundary,
            offset=offset,
            revision_cache=revision_cache,
            reply_budget=reply_budget,
        )
    except MessageExactStorageError:
        if cursor_state is not None:
            raise MessageExactStorageDrift("message exact continuation source changed") from None
        raise
    snapshot_handle = _snapshot_handle(authority, boundary, scan)
    if cursor_state is not None:
        anchor = scan.resume_anchor
        if (
            cursor_state.total_rows != scan.total_rows
            or cursor_state.snapshot_bytes != scan.snapshot_bytes
            or cursor_state.full_ledger_sha256 != scan.full_ledger_sha256
            or cursor_state.prefix_ledger_sha256 != scan.resume_prefix_sha256
            or cursor_state.boundary_revision_sha256 != boundary.revision_sha256
            or cursor_state.snapshot_handle != snapshot_handle
            or anchor is None
            or cursor_state.anchor_storage_sequence != anchor.storage_sequence
            or cursor_state.anchor_revision_sha256 != anchor.revision_sha256
        ):
            raise MessageExactStorageDrift("message exact continuation snapshot changed")
    next_offset = offset + len(scan.page_rows)
    next_continuation: MessageExactContinuation | None = None
    if next_offset < scan.total_rows:
        anchor = scan.next_anchor
        if anchor is None or not scan.next_prefix_sha256 or next_offset <= offset:
            raise MessageExactStorageError("message exact continuation could not be anchored")
        token = _encode_cursor(
            key,
            authority,
            _CursorState(
                offset=next_offset,
                total_rows=scan.total_rows,
                snapshot_bytes=scan.snapshot_bytes,
                full_ledger_sha256=scan.full_ledger_sha256,
                prefix_ledger_sha256=scan.next_prefix_sha256,
                anchor_storage_sequence=anchor.storage_sequence,
                anchor_revision_sha256=anchor.revision_sha256,
                boundary_revision_sha256=boundary.revision_sha256,
                snapshot_handle=snapshot_handle,
            ),
        )
        next_continuation = MessageExactContinuation.create(token)
    return _create_message_exact_page(
        request=selected_request,
        principal_id=authority._principal_id,
        authority_handle=authority.authority_handle,
        snapshot_handle=snapshot_handle,
        boundary=_to_contract_row(boundary),
        rows=tuple(_to_contract_row(row) for row in scan.page_rows),
        offset=offset,
        total_rows=scan.total_rows,
        next_continuation=next_continuation,
    )


def reselect_message_exact_page_in_transaction(
    conn: sqlite3.Connection,
    new_storage_authority: MessageExactStorageAuthority,
    original_page: MessageExactPage,
) -> MessageExactPage:
    """Freshly reselect a page after the adapter's late publication reauth."""

    _require_transaction(conn)
    if type(original_page) is not MessageExactPage or not original_page._is_process_owned():
        raise MessageExactStorageError("message exact publication page is invalid")
    if new_storage_authority.request is not original_page.request:
        raise MessageExactStorageError("message exact publication request identity changed")
    _verify_authority(conn, new_storage_authority, original_page.request)
    try:
        fresh = select_message_exact_page_in_transaction(
            conn,
            new_storage_authority,
            original_page.request,
        )
    except MessageExactStorageDrift:
        raise
    except MessageExactStorageError:
        raise MessageExactStorageDrift("message exact publication source changed") from None
    if (
        fresh.authority_handle != original_page.authority_handle
        or fresh.selection_handle != original_page.selection_handle
    ):
        raise MessageExactStorageDrift("message exact publication snapshot changed")
    return fresh


__all__ = [
    "MESSAGE_EXACT_MAX_CONTINUATION_BYTES",
    "MESSAGE_EXACT_MAX_METADATA_UTF8_BYTES",
    "MESSAGE_EXACT_MAX_PAGE_UTF8_BYTES",
    "MESSAGE_EXACT_MAX_ROW_UTF8_BYTES",
    "MESSAGE_EXACT_MAX_SNAPSHOT_UTF8_BYTES",
    "MESSAGE_EXACT_MAX_WINDOW_ROWS",
    "MessageExactStorageAuthority",
    "MessageExactStorageDrift",
    "MessageExactStorageError",
    "_issue_message_exact_storage_authority_in_transaction",
    "reselect_message_exact_page_in_transaction",
    "select_message_exact_page_in_transaction",
]
