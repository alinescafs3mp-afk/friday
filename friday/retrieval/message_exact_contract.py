"""Closed contracts for an exact, queryless current-conversation window.

The request intentionally has no query field.  Storage selects one authorized
conversation snapshot before exposing counts or bodies, then returns the
process-private carriers defined here.  Only the bounded projection is suitable
for model input.  A later authorization check returns a separate body-free
publication decision; it never republishes the selected rows.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, NoReturn, SupportsIndex, cast

from friday.retrieval.contracts import MessageRole

MESSAGE_EXACT_REQUEST_SCHEMA = "friday.message-exact-request.private.v1"
MESSAGE_EXACT_REQUEST_IDENTITY_SCHEMA = "friday.message-exact-request-identity.private.v1"
MESSAGE_EXACT_MODEL_PROJECTION_SCHEMA = "friday.message-exact-projection.model.v1"
MESSAGE_EXACT_PUBLICATION_DECISION_SCHEMA = "friday.message-exact-publication-decision.v1"

MESSAGE_EXACT_DEFAULT_PAGE_SIZE = 50
MESSAGE_EXACT_MAX_PAGE_SIZE = 100
MESSAGE_EXACT_MAX_EXCERPT_CHARS = 600
MESSAGE_EXACT_MAX_FULL_ROW_CHARS = 8_000
MESSAGE_EXACT_MAX_FULL_PAGE_CHARS = 80_000

_MAX_REQUEST_JSON_BYTES = 8_192
_MAX_CONTINUATION_BYTES = 4_096
_MIN_CONTINUATION_BYTES = 32
_MAX_SCOPE_BYTES = 200
_MAX_STORED_BODY_BYTES = 1_000_000
_MAX_STORED_METADATA_BYTES = 262_144
_MAX_STORED_PAGE_BYTES = 4_000_000
# JSON may expand one visible control character to a six-byte ``\u00xx``
# escape.  Keep the serialized envelope bounded without silently shrinking the
# advertised 80k-character full-content budget.
_MAX_MODEL_JSON_BYTES = 600_000
_MAX_COUNT = 1_000_000_000
_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{16}\Z")
_CONVERSATION_ID = re.compile(r"conv_[0-9a-f]{16}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OPAQUE_TOKEN = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z")
_CARRIER_FACTORY = object()
_DECISION_FACTORY = object()
_PROCESS_KEY = secrets.token_bytes(32)


class MessageExactContractError(ValueError):
    """A value is outside the closed exact-message contract."""


class MessageExactContentMode(StrEnum):
    """How much already-authorized content may enter the bounded projection."""

    EXCERPT = "excerpt"
    FULL_CONTENT = "full_content"


class MessageExactRowCoverage(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class MessageExactContentCoverage(StrEnum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"


class MessageExactPublicationStatus(StrEnum):
    AUTHORIZED = "authorized"
    DENIED = "denied"
    DRIFTED = "drifted"
    UNAVAILABLE = "unavailable"


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise MessageExactContractError("exact-message value is not canonical JSON") from None


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MessageExactContractError("exact-message JSON contains a duplicate key")
        result[key] = value
    return result


def _parse_canonical_object(value: object, *, label: str) -> dict[str, Any]:
    if type(value) is not str or not value or value != value.strip():
        raise MessageExactContractError(f"{label} must be canonical JSON text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise MessageExactContractError(f"{label} must be valid UTF-8") from None
    if len(encoded) > _MAX_REQUEST_JSON_BYTES:
        raise MessageExactContractError(f"{label} exceeds the closed byte limit")
    try:
        parsed = json.loads(
            value,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                MessageExactContractError(f"{label} contains a non-finite number")
            ),
            object_pairs_hook=_closed_object,
        )
    except MessageExactContractError:
        raise
    except (UnicodeError, OverflowError, RecursionError, ValueError):
        raise MessageExactContractError(f"{label} must contain one JSON object") from None
    if type(parsed) is not dict or value != _canonical_json(parsed):
        raise MessageExactContractError(f"{label} must be closed canonical JSON")
    return cast(dict[str, Any], parsed)


def _exact_object(value: object, keys: frozenset[str], *, label: str) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != keys:
        raise MessageExactContractError(f"{label} keys do not match the closed contract")
    return cast(dict[str, Any], value)


def _valid_utf8(value: object, *, label: str, maximum_bytes: int, allow_empty: bool) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise MessageExactContractError(f"{label} must be text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise MessageExactContractError(f"{label} must be valid UTF-8") from None
    if len(encoded) > maximum_bytes:
        raise MessageExactContractError(f"{label} exceeds the closed byte limit")
    return value


def _scope(value: object, *, label: str) -> str:
    text = _valid_utf8(value, label=label, maximum_bytes=_MAX_SCOPE_BYTES, allow_empty=False)
    if text != text.strip() or any(unicodedata.category(char).startswith("C") for char in text):
        raise MessageExactContractError(f"{label} is not canonical")
    return text


def _conversation_id(value: object) -> str:
    text = _scope(value, label="conversation identity")
    if _CONVERSATION_ID.fullmatch(text) is None:
        raise MessageExactContractError("conversation identity is invalid")
    return text


def _message_id(value: object, *, label: str, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    text = _scope(value, label=label)
    if _MESSAGE_ID.fullmatch(text) is None:
        raise MessageExactContractError(f"{label} is invalid")
    return text


def _sha256(value: object, *, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise MessageExactContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _count(value: object, *, label: str, low: int = 0, high: int = _MAX_COUNT) -> int:
    if isinstance(value, bool) or type(value) is not int or not low <= value <= high:
        raise MessageExactContractError(f"{label} is outside the closed range")
    return value


def _instant(value: object, *, label: str) -> str:
    text = _valid_utf8(value, label=label, maximum_bytes=64, allow_empty=False)
    if text != text.strip():
        raise MessageExactContractError(f"{label} is not canonical")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise MessageExactContractError(f"{label} must be an ISO-8601 instant") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MessageExactContractError(f"{label} must include an offset")
    canonical = parsed.astimezone(UTC).isoformat()
    if text != canonical:
        raise MessageExactContractError(f"{label} must already be normalized to UTC")
    return canonical


def _instant_input(value: datetime | str | None, *, label: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise MessageExactContractError(f"{label} must include an offset")
        return value.astimezone(UTC).isoformat()
    return _instant(value, label=label)


def _roles(values: Iterable[MessageRole]) -> tuple[MessageRole, ...]:
    try:
        result = tuple(values)
    except TypeError:
        raise MessageExactContractError("message roles are outside the closed contract") from None
    if (
        not result
        or any(type(item) is not MessageRole for item in result)
        or not set(result) <= {MessageRole.USER, MessageRole.ASSISTANT}
    ):
        raise MessageExactContractError("message roles are outside the closed contract")
    canonical = tuple(sorted(result, key=lambda item: item.value))
    if len(canonical) != len(set(canonical)):
        raise MessageExactContractError("message roles must be unique")
    return canonical


def _enum(enum_type: type[StrEnum], value: object, *, label: str) -> StrEnum:
    if type(value) is not str or len(value) > 80:
        raise MessageExactContractError(f"{label} is outside the closed enum")
    try:
        return enum_type(value)
    except ValueError:
        raise MessageExactContractError(f"{label} is outside the closed enum") from None


def _keyed_handle(domain: bytes, payload: Mapping[str, Any]) -> str:
    material = domain + b"\0" + _canonical_json(payload).encode("ascii")
    return hmac.new(_PROCESS_KEY, material, hashlib.sha256).hexdigest()


def _finite_metadata_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise MessageExactContractError("stored message metadata is invalid")
    return parsed


def _metadata_json(value: object) -> str:
    metadata = _valid_utf8(
        value,
        label="stored message metadata",
        maximum_bytes=_MAX_STORED_METADATA_BYTES,
        allow_empty=False,
    )
    try:
        decoded = json.loads(
            metadata,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                MessageExactContractError("stored message metadata is invalid")
            ),
            parse_float=_finite_metadata_float,
            object_pairs_hook=_closed_object,
        )
    except MessageExactContractError:
        raise
    except (UnicodeError, OverflowError, RecursionError, ValueError):
        raise MessageExactContractError("stored message metadata is invalid") from None
    if type(decoded) is not dict:
        raise MessageExactContractError("stored message metadata must be one object")
    return metadata


def _message_exact_row_revision_sha256(
    *,
    message_id: object,
    conversation_id: object,
    principal_id: object,
    role: object,
    content: object,
    metadata_json: object,
    reply_to_message_id: object,
    reply_revision_sha256: object,
    created_at: object,
) -> str:
    message = _message_id(message_id, label="stored message identity")
    conversation = _conversation_id(conversation_id)
    principal = _scope(principal_id, label="stored principal identity")
    if type(role) is not MessageRole or role not in {MessageRole.USER, MessageRole.ASSISTANT}:
        raise MessageExactContractError("stored message role is outside the closed contract")
    body = _valid_utf8(
        content,
        label="stored message body",
        maximum_bytes=_MAX_STORED_BODY_BYTES,
        allow_empty=True,
    )
    metadata = _metadata_json(metadata_json)
    reply = _message_id(
        reply_to_message_id,
        label="stored reply identity",
        optional=True,
    )
    if (reply is None) != (reply_revision_sha256 is None):
        raise MessageExactContractError("stored reply identity and parent revision must be supplied together")
    reply_revision = (
        None
        if reply_revision_sha256 is None
        else _sha256(reply_revision_sha256, label="stored reply parent revision")
    )
    created = _instant(created_at, label="stored message timestamp")
    material = {
        "content": body,
        "conversation_id": conversation,
        "created_at": created,
        "message_id": message,
        "metadata_json": metadata,
        "principal_id": principal,
        "reply_to_message_id": reply,
        "reply_revision_sha256": reply_revision,
        "role": role.value,
        "schema": "friday.message-exact-row.private.v1",
    }
    return hashlib.sha256(_canonical_json(material).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class MessageExactContinuation:
    """An opaque signed token; its signature and payload belong to storage."""

    token: str

    def __post_init__(self) -> None:
        if type(self.token) is not str:
            raise MessageExactContractError("message continuation must be opaque text")
        try:
            encoded = self.token.encode("ascii", errors="strict")
        except UnicodeEncodeError:
            raise MessageExactContractError("message continuation must use canonical ASCII") from None
        if (
            not _MIN_CONTINUATION_BYTES <= len(encoded) <= _MAX_CONTINUATION_BYTES
            or _OPAQUE_TOKEN.fullmatch(self.token) is None
        ):
            raise MessageExactContractError("message continuation is outside the closed envelope")

    def __repr__(self) -> str:
        return "MessageExactContinuation(private=True)"

    @classmethod
    def create(cls, token: str) -> MessageExactContinuation:
        return cls(token)


_REQUEST_KEYS = frozenset(
    {
        "accepted_boundary_user_message_id",
        "content_mode",
        "continuation",
        "conversation_id",
        "page_size",
        "roles",
        "schema",
        "since",
        "until",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class MessageExactRequest:
    """One queryless current-conversation selection intent.

    The activation caller must derive ``accepted_boundary_user_message_id``
    from this turn's durable ingress.  This pure foundation can validate and
    bind that row after it is supplied, but cannot infer its turn provenance.
    """

    conversation_id: str
    accepted_boundary_user_message_id: str
    since: str | None
    until: str | None
    roles: tuple[MessageRole, ...]
    page_size: int
    content_mode: MessageExactContentMode
    continuation: MessageExactContinuation | None

    def __post_init__(self) -> None:
        _conversation_id(self.conversation_id)
        _message_id(
            self.accepted_boundary_user_message_id,
            label="accepted boundary user message identity",
        )
        if (self.since is None) != (self.until is None):
            raise MessageExactContractError("message UTC boundaries must be supplied together")
        if self.since is not None and self.until is not None:
            _instant(self.since, label="message window start")
            _instant(self.until, label="message window end")
            if self.since >= self.until:
                raise MessageExactContractError("message UTC window must be non-empty")
        if type(self.roles) is not tuple or self.roles != _roles(self.roles):
            raise MessageExactContractError("message roles must be canonical")
        _count(self.page_size, label="message page size", low=1, high=MESSAGE_EXACT_MAX_PAGE_SIZE)
        if type(self.content_mode) is not MessageExactContentMode:
            raise MessageExactContractError("message content mode is outside the closed contract")
        if self.continuation is not None and type(self.continuation) is not MessageExactContinuation:
            raise MessageExactContractError("message continuation must use the opaque wrapper")
        if len(self.to_private_json().encode("ascii")) > _MAX_REQUEST_JSON_BYTES:
            raise MessageExactContractError("message request exceeds the closed byte limit")

    def __repr__(self) -> str:
        return (
            "MessageExactRequest(queryless=True, current_conversation=True, "
            f"page_size={self.page_size}, content_mode={self.content_mode.value!r})"
        )

    @property
    def boundary_user_message_id(self) -> str:
        """Compatibility spelling for storage selectors; still private."""

        return self.accepted_boundary_user_message_id

    @classmethod
    def create(
        cls,
        *,
        conversation_id: str,
        accepted_boundary_user_message_id: str,
        since: datetime | str | None = None,
        until: datetime | str | None = None,
        roles: Iterable[MessageRole] = (MessageRole.ASSISTANT, MessageRole.USER),
        page_size: int = MESSAGE_EXACT_DEFAULT_PAGE_SIZE,
        content_mode: MessageExactContentMode = MessageExactContentMode.EXCERPT,
        continuation: MessageExactContinuation | None = None,
    ) -> MessageExactRequest:
        return cls(
            _conversation_id(conversation_id),
            cast(
                str,
                _message_id(
                    accepted_boundary_user_message_id,
                    label="accepted boundary user message identity",
                ),
            ),
            _instant_input(since, label="message window start"),
            _instant_input(until, label="message window end"),
            _roles(roles),
            _count(
                page_size,
                label="message page size",
                low=1,
                high=MESSAGE_EXACT_MAX_PAGE_SIZE,
            ),
            content_mode,
            continuation,
        )

    def to_private_payload(self) -> dict[str, object]:
        return {
            "accepted_boundary_user_message_id": self.accepted_boundary_user_message_id,
            "content_mode": self.content_mode.value,
            "continuation": None if self.continuation is None else self.continuation.token,
            "conversation_id": self.conversation_id,
            "page_size": self.page_size,
            "roles": [item.value for item in self.roles],
            "schema": MESSAGE_EXACT_REQUEST_SCHEMA,
            "since": self.since,
            "until": self.until,
        }

    def to_private_json(self) -> str:
        return _canonical_json(self.to_private_payload())

    def to_identity_payload(self) -> dict[str, object]:
        payload = self.to_private_payload()
        del payload["continuation"]
        payload["schema"] = MESSAGE_EXACT_REQUEST_IDENTITY_SCHEMA
        return payload

    def to_identity_json(self) -> str:
        return _canonical_json(self.to_identity_payload())

    def identity_digest_material(self) -> bytes:
        return b"friday/message-exact-request-identity/v1\0" + self.to_identity_json().encode("ascii")

    @classmethod
    def from_private_payload(cls, value: object) -> MessageExactRequest:
        payload = _exact_object(value, _REQUEST_KEYS, label="exact-message request")
        if payload["schema"] != MESSAGE_EXACT_REQUEST_SCHEMA:
            raise MessageExactContractError("exact-message request schema is unsupported")
        roles = payload["roles"]
        if type(roles) is not list:
            raise MessageExactContractError("message roles must be one closed array")
        continuation = payload["continuation"]
        since = payload["since"]
        until = payload["until"]
        if since is not None and type(since) is not str:
            raise MessageExactContractError("message window start must be private text or null")
        if until is not None and type(until) is not str:
            raise MessageExactContractError("message window end must be private text or null")
        if continuation is not None and type(continuation) is not str:
            raise MessageExactContractError("message continuation must be private text or null")
        conversation = payload["conversation_id"]
        boundary = payload["accepted_boundary_user_message_id"]
        if type(conversation) is not str or type(boundary) is not str:
            raise MessageExactContractError("message request identities must be private text")
        return cls.create(
            conversation_id=conversation,
            accepted_boundary_user_message_id=boundary,
            since=cast(str | None, since),
            until=cast(str | None, until),
            roles=(
                cast(
                    MessageRole,
                    _enum(MessageRole, item, label="message role"),
                )
                for item in roles
            ),
            page_size=_count(
                payload["page_size"],
                label="message page size",
                low=1,
                high=MESSAGE_EXACT_MAX_PAGE_SIZE,
            ),
            content_mode=cast(
                MessageExactContentMode,
                _enum(
                    MessageExactContentMode,
                    payload["content_mode"],
                    label="message content mode",
                ),
            ),
            continuation=(None if continuation is None else MessageExactContinuation.create(continuation)),
        )

    @classmethod
    def parse_private(cls, value: str) -> MessageExactRequest:
        result = cls.from_private_payload(_parse_canonical_object(value, label="exact-message request"))
        if result.to_private_json() != value:
            raise MessageExactContractError("exact-message request is not semantically canonical")
        return result


class _ProcessPrivate:
    __slots__ = ()

    def __copy__(self) -> NoReturn:
        raise TypeError("exact-message carrier is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("exact-message carrier is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("exact-message carrier is process-private")


class MessageExactRow(_ProcessPrivate):
    """One exact stored row.  Its body never has a serialization method."""

    __slots__ = (
        "_seal",
        "content",
        "conversation_id",
        "created_at",
        "message_id",
        "metadata_json",
        "principal_id",
        "reply_to_message_id",
        "reply_revision_sha256",
        "revision_sha256",
        "role",
        "storage_sequence",
    )

    def __init__(
        self,
        *,
        message_id: str,
        conversation_id: str,
        principal_id: str,
        role: MessageRole,
        content: str,
        metadata_json: str,
        reply_to_message_id: str | None,
        reply_revision_sha256: str | None,
        created_at: str,
        storage_sequence: int,
        _factory: object = None,
    ) -> None:
        if _factory is not _CARRIER_FACTORY:
            raise MessageExactContractError("message row requires the private carrier factory")
        message = cast(str, _message_id(message_id, label="stored message identity"))
        conversation = _conversation_id(conversation_id)
        principal = _scope(principal_id, label="stored principal identity")
        if type(role) is not MessageRole or role not in {MessageRole.USER, MessageRole.ASSISTANT}:
            raise MessageExactContractError("stored message role is outside the closed contract")
        body = _valid_utf8(
            content,
            label="stored message body",
            maximum_bytes=_MAX_STORED_BODY_BYTES,
            allow_empty=True,
        )
        metadata = _metadata_json(metadata_json)
        reply = _message_id(
            reply_to_message_id,
            label="stored reply identity",
            optional=True,
        )
        if (reply is None) != (reply_revision_sha256 is None):
            raise MessageExactContractError(
                "stored reply identity and parent revision must be supplied together"
            )
        reply_revision = (
            None
            if reply_revision_sha256 is None
            else _sha256(reply_revision_sha256, label="stored reply parent revision")
        )
        created = _instant(created_at, label="stored message timestamp")
        sequence = _count(storage_sequence, label="stored message sequence", low=1)
        revision = _message_exact_row_revision_sha256(
            message_id=message,
            conversation_id=conversation,
            principal_id=principal,
            role=role,
            content=body,
            metadata_json=metadata,
            reply_to_message_id=reply,
            reply_revision_sha256=reply_revision,
            created_at=created,
        )
        for name, value in (
            ("message_id", message),
            ("conversation_id", conversation),
            ("principal_id", principal),
            ("role", role),
            ("content", body),
            ("metadata_json", metadata),
            ("reply_to_message_id", reply),
            ("reply_revision_sha256", reply_revision),
            ("created_at", created),
            ("storage_sequence", sequence),
            ("revision_sha256", revision),
            (
                "_seal",
                _keyed_handle(
                    b"friday/message-exact-row-seal/v1",
                    {"revision": revision, "storage_sequence": sequence},
                ),
            ),
        ):
            object.__setattr__(self, name, value)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("exact-message row is immutable")

    def __repr__(self) -> str:
        if not self._is_process_owned():
            return "MessageExactRow(invalid=True, private_body=True)"
        return f"MessageExactRow(role={self.role.value!r}, private_body=True)"

    def _is_process_owned(self) -> bool:
        try:
            current_revision = _message_exact_row_revision_sha256(
                message_id=self.message_id,
                conversation_id=self.conversation_id,
                principal_id=self.principal_id,
                role=self.role,
                content=self.content,
                metadata_json=self.metadata_json,
                reply_to_message_id=self.reply_to_message_id,
                reply_revision_sha256=self.reply_revision_sha256,
                created_at=self.created_at,
            )
            _count(self.storage_sequence, label="stored message sequence", low=1)
            expected_seal = _keyed_handle(
                b"friday/message-exact-row-seal/v1",
                {
                    "revision": current_revision,
                    "storage_sequence": self.storage_sequence,
                },
            )
            return hmac.compare_digest(
                self.revision_sha256,
                current_revision,
            ) and hmac.compare_digest(self._seal, expected_seal)
        except (AttributeError, MessageExactContractError, TypeError, UnicodeError):
            return False


def _create_message_exact_row(
    *,
    message_id: str,
    conversation_id: str,
    principal_id: str,
    role: MessageRole,
    content: str,
    metadata_json: str,
    reply_to_message_id: str | None,
    reply_revision_sha256: str | None,
    created_at: str,
    storage_sequence: int,
) -> MessageExactRow:
    """Private storage/adapter seam; not part of the exported API."""

    return MessageExactRow(
        message_id=message_id,
        conversation_id=conversation_id,
        principal_id=principal_id,
        role=role,
        content=content,
        metadata_json=metadata_json,
        reply_to_message_id=reply_to_message_id,
        reply_revision_sha256=reply_revision_sha256,
        created_at=created_at,
        storage_sequence=storage_sequence,
        _factory=_CARRIER_FACTORY,
    )


def _page_selection_handle(
    *,
    request: object,
    principal_id: object,
    authority_handle: object,
    snapshot_handle: object,
    boundary: object,
    rows: object,
    offset: object,
    total_rows: object,
    next_continuation: object,
) -> str:
    if type(request) is not MessageExactRequest:
        raise MessageExactContractError("message page requires its canonical request")
    principal = _scope(principal_id, label="page principal identity")
    authority = _sha256(authority_handle, label="message authority handle")
    snapshot = _sha256(snapshot_handle, label="message snapshot handle")
    if type(boundary) is not MessageExactRow or not boundary._is_process_owned():
        raise MessageExactContractError("message page requires an exact accepted boundary")
    if type(rows) is not tuple or any(
        type(item) is not MessageExactRow or not item._is_process_owned() for item in rows
    ):
        raise MessageExactContractError("message page rows require storage authority")
    page_offset = _count(offset, label="message page offset")
    total = _count(total_rows, label="authorized message row total")
    if next_continuation is not None and type(next_continuation) is not MessageExactContinuation:
        raise MessageExactContractError("outbound message continuation is invalid")
    return _keyed_handle(
        b"friday/message-exact-page-selection/v1",
        {
            "authority_handle": authority,
            "boundary_revision": boundary.revision_sha256,
            "next_continuation": (None if next_continuation is None else next_continuation.token),
            "offset": page_offset,
            "principal_id": principal,
            "request": request.to_private_payload(),
            "row_revisions": [item.revision_sha256 for item in rows],
            "schema": "friday.message-exact-page-selection.private.v1",
            "snapshot_handle": snapshot,
            "total_rows": total,
        },
    )


class MessageExactPage(_ProcessPrivate):
    """One authorized, one-snapshot page retaining exact adjacent rows."""

    __slots__ = (
        "_seal",
        "authority_handle",
        "boundary",
        "next_continuation",
        "offset",
        "principal_id",
        "request",
        "rows",
        "selection_handle",
        "snapshot_handle",
        "total_rows",
    )

    def __init__(
        self,
        *,
        request: MessageExactRequest,
        principal_id: str,
        authority_handle: str,
        snapshot_handle: str,
        boundary: MessageExactRow,
        rows: tuple[MessageExactRow, ...],
        offset: int,
        total_rows: int,
        next_continuation: MessageExactContinuation | None,
        _factory: object = None,
    ) -> None:
        if _factory is not _CARRIER_FACTORY:
            raise MessageExactContractError("message page requires the private carrier factory")
        if type(request) is not MessageExactRequest:
            raise MessageExactContractError("message page requires its canonical request")
        if MessageExactRequest.parse_private(request.to_private_json()) != request:
            raise MessageExactContractError("message page request is not canonical")
        principal = _scope(principal_id, label="page principal identity")
        authority = _sha256(authority_handle, label="message authority handle")
        snapshot = _sha256(snapshot_handle, label="message snapshot handle")
        if type(boundary) is not MessageExactRow or not boundary._is_process_owned():
            raise MessageExactContractError("message page requires an exact accepted boundary")
        if (
            boundary.message_id != request.accepted_boundary_user_message_id
            or boundary.conversation_id != request.conversation_id
            or boundary.principal_id != principal
            or boundary.role is not MessageRole.USER
        ):
            raise MessageExactContractError("accepted message boundary escaped its authority scope")
        if type(rows) is not tuple or len(rows) > request.page_size:
            raise MessageExactContractError("message page rows are outside the closed page size")
        if any(type(item) is not MessageExactRow or not item._is_process_owned() for item in rows):
            raise MessageExactContractError("message page rows require storage authority")
        if any(
            item.conversation_id != request.conversation_id
            or item.principal_id != principal
            or item.role not in request.roles
            or item.storage_sequence >= boundary.storage_sequence
            for item in rows
        ):
            raise MessageExactContractError("message page row escaped its exact authority boundary")
        if (
            request.since is not None
            and request.until is not None
            and any(not request.since <= item.created_at < request.until for item in rows)
        ):
            raise MessageExactContractError("message page row escaped its UTC boundary")
        if len({item.message_id for item in rows}) != len(rows):
            raise MessageExactContractError("message page must retain each exact row once")
        order = tuple((item.created_at, item.storage_sequence) for item in rows)
        if order != tuple(sorted(order)) or len(order) != len(set(order)):
            raise MessageExactContractError("message page rows are not in stable chronological order")
        page_offset = _count(offset, label="message page offset")
        total = _count(total_rows, label="authorized message row total")
        covered_through = page_offset + len(rows)
        if page_offset > total or covered_through > total:
            raise MessageExactContractError("message page row coverage is inconsistent")
        if next_continuation is not None and type(next_continuation) is not MessageExactContinuation:
            raise MessageExactContractError("outbound message continuation is invalid")
        if next_continuation is not None and not rows:
            raise MessageExactContractError("an empty message page cannot continue")
        if (request.continuation is None) != (page_offset == 0):
            raise MessageExactContractError("message page offset is not bound to its continuation")
        if (next_continuation is None) != (covered_through == total):
            raise MessageExactContractError("message page continuation and coverage disagree")
        if (
            request.continuation is not None
            and next_continuation is not None
            and request.continuation.token == next_continuation.token
        ):
            raise MessageExactContractError("outbound continuation must advance the message page")
        body_bytes = (
            len(boundary.content.encode("utf-8"))
            + len(boundary.metadata_json.encode("utf-8"))
            + sum(
                len(item.content.encode("utf-8")) + len(item.metadata_json.encode("utf-8")) for item in rows
            )
        )
        if body_bytes > _MAX_STORED_PAGE_BYTES:
            raise MessageExactContractError("message page bodies exceed the closed byte limit")
        selection = _page_selection_handle(
            request=request,
            principal_id=principal,
            authority_handle=authority,
            snapshot_handle=snapshot,
            boundary=boundary,
            rows=rows,
            offset=page_offset,
            total_rows=total,
            next_continuation=next_continuation,
        )
        seal = _keyed_handle(
            b"friday/message-exact-page-seal/v1",
            {"selection_handle": selection},
        )
        for name, value in (
            ("request", request),
            ("principal_id", principal),
            ("authority_handle", authority),
            ("snapshot_handle", snapshot),
            ("boundary", boundary),
            ("rows", rows),
            ("offset", page_offset),
            ("total_rows", total),
            ("next_continuation", next_continuation),
            ("selection_handle", selection),
            ("_seal", seal),
        ):
            object.__setattr__(self, name, value)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("exact-message page is immutable")

    def __repr__(self) -> str:
        if not self._is_process_owned():
            return "MessageExactPage(invalid=True, private_bodies=True)"
        return f"MessageExactPage(row_count={len(self.rows)}, private_bodies=True)"

    @property
    def has_more(self) -> bool:
        return self.next_continuation is not None

    def _is_process_owned(self) -> bool:
        try:
            rebuilt = MessageExactPage(
                request=self.request,
                principal_id=self.principal_id,
                authority_handle=self.authority_handle,
                snapshot_handle=self.snapshot_handle,
                boundary=self.boundary,
                rows=self.rows,
                offset=self.offset,
                total_rows=self.total_rows,
                next_continuation=self.next_continuation,
                _factory=_CARRIER_FACTORY,
            )
            return hmac.compare_digest(
                self.selection_handle,
                rebuilt.selection_handle,
            ) and hmac.compare_digest(self._seal, rebuilt._seal)
        except (AttributeError, MessageExactContractError, TypeError, UnicodeError):
            return False


def _create_message_exact_page(
    *,
    request: MessageExactRequest,
    principal_id: str,
    authority_handle: str,
    snapshot_handle: str,
    boundary: MessageExactRow,
    rows: tuple[MessageExactRow, ...],
    offset: int,
    total_rows: int,
    next_continuation: MessageExactContinuation | None,
) -> MessageExactPage:
    """Private storage/adapter seam; counts and rows must already be authorized."""

    return MessageExactPage(
        request=request,
        principal_id=principal_id,
        authority_handle=authority_handle,
        snapshot_handle=snapshot_handle,
        boundary=boundary,
        rows=rows,
        offset=offset,
        total_rows=total_rows,
        next_continuation=next_continuation,
        _factory=_CARRIER_FACTORY,
    )


@dataclass(frozen=True, slots=True, repr=False)
class MessageExactProjectionRow:
    """One model-safe row: no message, reply, principal, or conversation ID."""

    ordinal: int
    role: MessageRole
    at: str
    text: str
    truncated: bool
    content_chars: int

    def __post_init__(self) -> None:
        _count(
            self.ordinal,
            label="message projection ordinal",
            low=1,
            high=MESSAGE_EXACT_MAX_PAGE_SIZE,
        )
        if type(self.role) is not MessageRole or self.role not in {
            MessageRole.USER,
            MessageRole.ASSISTANT,
        }:
            raise MessageExactContractError("message projection role is invalid")
        _instant(self.at, label="message projection timestamp")
        _valid_utf8(
            self.text,
            label="message projection text",
            maximum_bytes=MESSAGE_EXACT_MAX_FULL_ROW_CHARS * 4 + 4,
            allow_empty=True,
        )
        if type(self.truncated) is not bool:
            raise MessageExactContractError("message projection truncation flag is invalid")
        _count(self.content_chars, label="message projection source character count")
        if self.truncated:
            if not self.text.endswith("…") or self.content_chars <= len(self.text[:-1]):
                raise MessageExactContractError("truncated message projection is inconsistent")
        elif self.content_chars != len(self.text):
            raise MessageExactContractError("complete message projection is inconsistent")

    def __repr__(self) -> str:
        return (
            f"MessageExactProjectionRow(ordinal={self.ordinal}, role={self.role.value!r}, "
            f"truncated={self.truncated}, private_text=True)"
        )

    def to_model_payload(self) -> dict[str, object]:
        return {
            "at": self.at,
            "content_chars": self.content_chars,
            "ordinal": self.ordinal,
            "role": self.role.value,
            "text": self.text,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True, repr=False)
class MessageExactProjection:
    """Bounded initial-read projection suitable for synthesis/model input."""

    rows: tuple[MessageExactProjectionRow, ...]
    content_mode: MessageExactContentMode
    row_coverage: MessageExactRowCoverage
    content_coverage: MessageExactContentCoverage
    offset: int
    shown_rows: int
    total_rows: int
    truncated_rows: int

    def __post_init__(self) -> None:
        if type(self.rows) is not tuple or any(
            type(item) is not MessageExactProjectionRow for item in self.rows
        ):
            raise MessageExactContractError("message projection rows must be immutable typed values")
        if tuple(item.ordinal for item in self.rows) != tuple(range(1, len(self.rows) + 1)):
            raise MessageExactContractError("message projection ordinals must be consecutive")
        if type(self.content_mode) is not MessageExactContentMode:
            raise MessageExactContractError("message projection content mode is invalid")
        if type(self.row_coverage) is not MessageExactRowCoverage:
            raise MessageExactContractError("message row coverage is invalid")
        if type(self.content_coverage) is not MessageExactContentCoverage:
            raise MessageExactContractError("message content coverage is invalid")
        offset = _count(self.offset, label="message projection offset")
        shown = _count(self.shown_rows, label="shown message rows")
        total = _count(self.total_rows, label="authorized message row total")
        truncated = _count(self.truncated_rows, label="truncated message rows")
        if shown != len(self.rows) or offset + shown > total or truncated > shown:
            raise MessageExactContractError("message projection row counts are inconsistent")
        if (self.row_coverage is MessageExactRowCoverage.COMPLETE) != (offset + shown == total):
            raise MessageExactContractError("message row coverage is inconsistent")
        actual_truncated = sum(item.truncated for item in self.rows)
        if truncated != actual_truncated or (
            self.content_coverage is MessageExactContentCoverage.COMPLETE
        ) != (actual_truncated == 0):
            raise MessageExactContractError("message content coverage is inconsistent")
        maximum = (
            MESSAGE_EXACT_MAX_EXCERPT_CHARS
            if self.content_mode is MessageExactContentMode.EXCERPT
            else MESSAGE_EXACT_MAX_FULL_ROW_CHARS
        )
        if any(len(item.text) > maximum for item in self.rows):
            raise MessageExactContractError("message projection exceeded its per-row content budget")
        if (
            self.content_mode is MessageExactContentMode.FULL_CONTENT
            and sum(len(item.text) for item in self.rows) > MESSAGE_EXACT_MAX_FULL_PAGE_CHARS
        ):
            raise MessageExactContractError("message projection exceeded its full-page content budget")
        encoded = json.dumps(
            self.to_model_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
        if len(encoded) > _MAX_MODEL_JSON_BYTES:
            raise MessageExactContractError("message model projection exceeds the closed byte limit")

    def __repr__(self) -> str:
        return (
            f"MessageExactProjection(row_count={len(self.rows)}, "
            f"row_coverage={self.row_coverage.value!r}, "
            f"content_coverage={self.content_coverage.value!r}, private_text=True)"
        )

    def to_model_payload(self) -> dict[str, object]:
        """Return only synthesis-safe values; all IDs and cursors stay private."""

        return {
            "content_coverage": self.content_coverage.value,
            "content_mode": self.content_mode.value,
            "offset": self.offset,
            "results": [item.to_model_payload() for item in self.rows],
            "row_coverage": self.row_coverage.value,
            "schema": MESSAGE_EXACT_MODEL_PROJECTION_SCHEMA,
            "shown_rows": self.shown_rows,
            "total_rows": self.total_rows,
            "truncated_rows": self.truncated_rows,
        }

    def to_model_json(self) -> str:
        return json.dumps(
            self.to_model_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


def project_message_exact_page(page: MessageExactPage) -> MessageExactProjection:
    """Produce the sole model-facing projection from an authorized private page."""

    if type(page) is not MessageExactPage or not page._is_process_owned():
        raise MessageExactContractError("message projection requires a process-owned page")
    mode = page.request.content_mode
    visible_rows: list[MessageExactProjectionRow] = []
    used_chars = 0
    excerpt_allowance = max(
        24,
        min(MESSAGE_EXACT_MAX_EXCERPT_CHARS, 3_600 // max(1, len(page.rows))),
    )
    source_rows = tuple(row.content for row in page.rows)
    for ordinal, (row, source) in enumerate(zip(page.rows, source_rows, strict=True), 1):
        if mode is MessageExactContentMode.EXCERPT:
            visible_budget = excerpt_allowance
        else:
            remaining_nonempty = sum(bool(item) for item in source_rows[ordinal:])
            visible_budget = min(
                MESSAGE_EXACT_MAX_FULL_ROW_CHARS,
                max(
                    1 if source else 0,
                    MESSAGE_EXACT_MAX_FULL_PAGE_CHARS - used_chars - remaining_nonempty,
                ),
            )
        truncated = len(source) > visible_budget
        if not truncated:
            text = source
        elif visible_budget <= 1:
            text = "…"
        else:
            text = source[: visible_budget - 1] + "…"
        used_chars += len(text)
        visible_rows.append(
            MessageExactProjectionRow(
                ordinal=ordinal,
                role=row.role,
                at=row.created_at,
                text=text,
                truncated=truncated,
                content_chars=len(source),
            )
        )
    rows = tuple(visible_rows)
    truncated_rows = sum(item.truncated for item in rows)
    complete_rows = page.next_continuation is None and page.offset + len(rows) == page.total_rows
    return MessageExactProjection(
        rows=rows,
        content_mode=mode,
        row_coverage=(MessageExactRowCoverage.COMPLETE if complete_rows else MessageExactRowCoverage.PARTIAL),
        content_coverage=(
            MessageExactContentCoverage.COMPLETE
            if truncated_rows == 0
            else MessageExactContentCoverage.TRUNCATED
        ),
        offset=page.offset,
        shown_rows=len(rows),
        total_rows=page.total_rows,
        truncated_rows=truncated_rows,
    )


class MessageExactPublicationDecision(_ProcessPrivate):
    """Body-free late reauthorization receipt bound to one selected page."""

    __slots__ = ("_authority_handle", "_seal", "_selection_handle", "status")

    def __init__(
        self,
        *,
        status: MessageExactPublicationStatus,
        selection_handle: str,
        authority_handle: str,
        _factory: object = None,
    ) -> None:
        if _factory is not _DECISION_FACTORY:
            raise MessageExactContractError("publication decision requires late reauthorization")
        if type(status) is not MessageExactPublicationStatus:
            raise MessageExactContractError("message publication status is invalid")
        selection = _sha256(selection_handle, label="message selection handle")
        authority = _sha256(authority_handle, label="message authority handle")
        seal = _keyed_handle(
            b"friday/message-exact-publication-decision/v1",
            {
                "authority_handle": authority,
                "selection_handle": selection,
                "status": status.value,
            },
        )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "_selection_handle", selection)
        object.__setattr__(self, "_authority_handle", authority)
        object.__setattr__(self, "_seal", seal)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("message publication decision is immutable")

    def __repr__(self) -> str:
        if not self._is_process_owned():
            return "MessageExactPublicationDecision(invalid=True, body_free=True)"
        return f"MessageExactPublicationDecision(status={self.status.value!r}, body_free=True)"

    def _is_process_owned(self) -> bool:
        try:
            if type(self.status) is not MessageExactPublicationStatus:
                return False
            selection = _sha256(self._selection_handle, label="message selection handle")
            authority = _sha256(self._authority_handle, label="message authority handle")
            expected = _keyed_handle(
                b"friday/message-exact-publication-decision/v1",
                {
                    "authority_handle": authority,
                    "selection_handle": selection,
                    "status": self.status.value,
                },
            )
            return hmac.compare_digest(self._seal, expected)
        except (AttributeError, MessageExactContractError, TypeError, UnicodeError):
            return False

    @property
    def authorized(self) -> bool:
        return self._is_process_owned() and self.status is MessageExactPublicationStatus.AUTHORIZED

    def authorizes(self, page: MessageExactPage) -> bool:
        if not self._is_process_owned() or type(page) is not MessageExactPage or not page._is_process_owned():
            return False
        return (
            self.status is MessageExactPublicationStatus.AUTHORIZED
            and hmac.compare_digest(self._selection_handle, page.selection_handle)
            and hmac.compare_digest(self._authority_handle, page.authority_handle)
        )

    def to_public_payload(self) -> dict[str, object]:
        """The final receipt is deliberately body-, scope-, and identity-free."""

        if not self._is_process_owned():
            raise MessageExactContractError("message publication decision integrity failed")
        return {
            "authorized": self.authorized,
            "schema": MESSAGE_EXACT_PUBLICATION_DECISION_SCHEMA,
            "status": self.status.value,
        }


def _create_message_exact_publication_decision(
    *,
    page: MessageExactPage,
    status: MessageExactPublicationStatus,
) -> MessageExactPublicationDecision:
    """Private late-authorization seam; the decision itself contains no bodies."""

    if type(page) is not MessageExactPage or not page._is_process_owned():
        raise MessageExactContractError("publication decision requires a process-owned page")
    return MessageExactPublicationDecision(
        status=status,
        selection_handle=page.selection_handle,
        authority_handle=page.authority_handle,
        _factory=_DECISION_FACTORY,
    )


__all__ = [
    "MESSAGE_EXACT_DEFAULT_PAGE_SIZE",
    "MESSAGE_EXACT_MAX_EXCERPT_CHARS",
    "MESSAGE_EXACT_MAX_FULL_PAGE_CHARS",
    "MESSAGE_EXACT_MAX_FULL_ROW_CHARS",
    "MESSAGE_EXACT_MAX_PAGE_SIZE",
    "MESSAGE_EXACT_MODEL_PROJECTION_SCHEMA",
    "MESSAGE_EXACT_PUBLICATION_DECISION_SCHEMA",
    "MESSAGE_EXACT_REQUEST_IDENTITY_SCHEMA",
    "MESSAGE_EXACT_REQUEST_SCHEMA",
    "MessageExactContentCoverage",
    "MessageExactContentMode",
    "MessageExactContinuation",
    "MessageExactContractError",
    "MessageExactPage",
    "MessageExactProjection",
    "MessageExactProjectionRow",
    "MessageExactPublicationDecision",
    "MessageExactPublicationStatus",
    "MessageExactRequest",
    "MessageExactRow",
    "MessageExactRowCoverage",
    "project_message_exact_page",
]
