"""One-snapshot, principal-authorized lexical recall over private messages.

The caller owns the SQLite transaction.  This module neither starts nor ends a
transaction.  Its only write-shaped statement is FTS5's diagnostic
``integrity-check`` command, which changes no durable rows; cancellation can
therefore only abort verification/read work.
Returned values contain message bodies and storage identities; they are
process-private inputs to the archive authority layer, never public payloads.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import InitVar, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn, SupportsIndex, TypeAlias

from friday.retrieval.archive_search_contract import ConversationScope
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    LifecycleRef,
    LifecycleState,
    MessageRole,
    MessageWindowLocator,
    RepresentationKind,
    ResolvedSource,
    RevalidationTarget,
    RevisionKind,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
)
from friday.storage._base import SCHEMA_VERSION
from friday.storage._knowledge import _fts_term_groups

_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{16}\Z")
_CONVERSATION_ID = re.compile(r"conv_[0-9a-f]{16}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FACTORY = object()
_PROCESS_KEY = secrets.token_bytes(32)
_MAX_QUERY_BYTES = 4_000
_MAX_QUERY_CHARS = 1_000
_MAX_RESULTS = 20
_MAX_NEIGHBORS = 3


class ArchiveMessageStorageError(ValueError):
    """A body-free failure at the private archive-message storage boundary."""


ArchiveMessageScope: TypeAlias = ConversationScope

_MESSAGE_LIFECYCLE_STATES = frozenset(
    {
        LifecycleState.ACTIVE,
        LifecycleState.ARCHIVED,
        LifecycleState.DELETED,
    }
)


class _ProcessPrivate:
    __slots__ = ()

    def __copy__(self) -> NoReturn:
        raise TypeError("archive message storage value is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("archive message storage value is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("archive message storage value is process-private")


def _private_scope(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ArchiveMessageStorageError(f"{label} is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ArchiveMessageStorageError(f"{label} is invalid") from None
    if len(encoded) > 200 or any(ord(character) < 32 for character in value):
        raise ArchiveMessageStorageError(f"{label} is invalid")
    return value


def _private_content(value: object) -> str:
    if not isinstance(value, str):
        raise ArchiveMessageStorageError("stored message content is invalid")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ArchiveMessageStorageError("stored message content is invalid") from None
    return value


def _private_title(value: object) -> str:
    if not isinstance(value, str) or len(value) > 1_000:
        raise ArchiveMessageStorageError("stored conversation title is invalid")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ArchiveMessageStorageError("stored conversation title is invalid") from None
    normalized = " ".join(value.split())[:200]
    if any(ord(character) < 32 for character in normalized):
        raise ArchiveMessageStorageError("stored conversation title is invalid")
    return normalized


def _message_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _MESSAGE_ID.fullmatch(value) is None:
        raise ArchiveMessageStorageError(f"{label} is invalid")
    return value


def _conversation_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _CONVERSATION_ID.fullmatch(value) is None:
        raise ArchiveMessageStorageError(f"{label} is invalid")
    return value


def _utc(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 64:
        raise ArchiveMessageStorageError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ArchiveMessageStorageError(f"{label} is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArchiveMessageStorageError(f"{label} is invalid")
    normalized = parsed.astimezone(UTC).isoformat()
    if value != normalized:
        raise ArchiveMessageStorageError(f"{label} is invalid")
    return normalized


def _optional_utc(value: object, *, label: str) -> str | None:
    return None if value is None else _utc(value, label=label)


def _canonical_sha256(payload: dict[str, object]) -> str:
    material = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _row_identity(
    *,
    message_id: str,
    conversation_id: str,
    principal_id: str,
    role: MessageRole,
    content: str,
    created_at: str,
) -> str:
    # Kept byte-for-byte compatible with the accepted exact-window ledger.
    return _canonical_sha256(
        {
            "schema": "friday.private-message-window-row.v1",
            "id": message_id,
            "conversation_id": conversation_id,
            "person_id": principal_id,
            "role": role.value,
            "content": content,
            "created_at": created_at,
        }
    )


def _boundary_identity(values: dict[str, Any]) -> str:
    return _canonical_sha256(
        {
            "schema": "friday.private-message-window-boundary.v1",
            "id": _message_id(values["boundary_id"], label="stored boundary"),
            "conversation_id": _conversation_id(
                values["boundary_conversation_id"],
                label="stored boundary conversation",
            ),
            "person_id": _private_scope(values["boundary_principal_id"], label="stored principal"),
            "role": MessageRole.USER.value,
            "content": _private_content(values["boundary_content"]),
            "created_at": _utc(values["boundary_created_at"], label="stored boundary timestamp"),
        }
    )


def _roles(values: Iterable[MessageRole]) -> tuple[MessageRole, ...]:
    items = tuple(values)
    if not items or any(type(item) is not MessageRole for item in items):
        raise ArchiveMessageStorageError("message roles are invalid")
    if not set(items) <= {MessageRole.USER, MessageRole.ASSISTANT}:
        raise ArchiveMessageStorageError("message roles are invalid")
    result = tuple(sorted(items, key=lambda item: item.value))
    if len(result) != len(set(result)):
        raise ArchiveMessageStorageError("message roles are invalid")
    return result


def _query(value: object) -> str:
    if not isinstance(value, str):
        raise ArchiveMessageStorageError("message query is invalid")
    normalized = " ".join(value.split())
    try:
        encoded = normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ArchiveMessageStorageError("message query is invalid") from None
    if (
        not normalized
        or len(normalized) > _MAX_QUERY_CHARS
        or len(encoded) > _MAX_QUERY_BYTES
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ArchiveMessageStorageError("message query is invalid")
    return normalized


def _match_queries(value: str) -> tuple[str, ...]:
    groups = _fts_term_groups(value)
    if not groups:
        raise ArchiveMessageStorageError("message query has no searchable terms")

    def atom(term: str) -> str:
        lexical = term[:-1] if term.endswith("*") else term
        escaped = lexical.replace('"', '""')
        return f'"{escaped}"*'

    return tuple(dict.fromkeys(" OR ".join(atom(term) for term in group) for group in groups))


def _lifecycle_states(values: Iterable[LifecycleState]) -> tuple[LifecycleState, ...]:
    items = tuple(values)
    if (
        not items
        or any(type(item) is not LifecycleState for item in items)
        or not set(items) <= _MESSAGE_LIFECYCLE_STATES
    ):
        raise ArchiveMessageStorageError("message lifecycle states are invalid")
    result = tuple(sorted(items, key=lambda item: item.value))
    if len(result) != len(set(result)):
        raise ArchiveMessageStorageError("message lifecycle states are invalid")
    return result


def _bounded_integer(value: object, *, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ArchiveMessageStorageError(f"{label} is invalid")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveMessageRow(_ProcessPrivate):
    message_id: str
    conversation_id: str
    principal_id: str
    role: MessageRole
    content: str
    created_at: str
    conversation_archived: bool
    _factory: InitVar[object] = None

    def __post_init__(self, _factory: object) -> None:
        if _factory is not _FACTORY:
            raise ArchiveMessageStorageError("message rows require storage authority")

    def __repr__(self) -> str:
        return f"ArchiveMessageRow(role={self.role.value!r}, private=True)"


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveMessageContextRow(_ProcessPrivate):
    row: ArchiveMessageRow
    relative_position: int
    _factory: InitVar[object] = None

    def __post_init__(self, _factory: object) -> None:
        if (
            _factory is not _FACTORY
            or type(self.row) is not ArchiveMessageRow
            or not -_MAX_NEIGHBORS <= self.relative_position <= _MAX_NEIGHBORS
        ):
            raise ArchiveMessageStorageError("message context is invalid")

    def __repr__(self) -> str:
        return f"ArchiveMessageContextRow(relative_position={self.relative_position}, private=True)"


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveMessageLedgerEvidence(_ProcessPrivate):
    """Exact accepted-window row ledger for one conversation revision."""

    conversation_id: str
    row_ledger_sha256: str
    boundary_identity_sha256: str | None
    row_count: int
    first_message_id: str
    last_message_id: str
    conversation_title: str
    conversation_archived: bool
    _factory: InitVar[object] = None

    def __post_init__(self, _factory: object) -> None:
        if (
            _factory is not _FACTORY
            or _CONVERSATION_ID.fullmatch(self.conversation_id) is None
            or _SHA256.fullmatch(self.row_ledger_sha256) is None
            or (
                self.boundary_identity_sha256 is not None
                and _SHA256.fullmatch(self.boundary_identity_sha256) is None
            )
            or isinstance(self.row_count, bool)
            or self.row_count <= 0
            or _MESSAGE_ID.fullmatch(self.first_message_id) is None
            or _MESSAGE_ID.fullmatch(self.last_message_id) is None
            or self.conversation_title != _private_title(self.conversation_title)
            or type(self.conversation_archived) is not bool
        ):
            raise ArchiveMessageStorageError("message ledger evidence is invalid")

    def __repr__(self) -> str:
        return f"ArchiveMessageLedgerEvidence(row_count={self.row_count}, private=True)"


class ArchiveMessageReplayWindow(_ProcessPrivate):
    """Exact rows addressed by one already-selected message locator."""

    __slots__ = ("rows",)

    rows: tuple[ArchiveMessageRow, ...]

    def __init__(
        self,
        rows: tuple[ArchiveMessageRow, ...],
        *,
        _factory: object = None,
    ) -> None:
        if (
            _factory is not _FACTORY
            or type(rows) is not tuple
            or not rows
            or any(type(item) is not ArchiveMessageRow for item in rows)
            or len({item.message_id for item in rows}) != len(rows)
            or len({item.conversation_id for item in rows}) != 1
            or len({item.principal_id for item in rows}) != 1
            or len({item.conversation_archived for item in rows}) != 1
        ):
            raise ArchiveMessageStorageError("message replay window is invalid")
        object.__setattr__(self, "rows", rows)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive message replay window is immutable")

    def __repr__(self) -> str:
        return f"ArchiveMessageReplayWindow(row_count={len(self.rows)}, private=True)"


class ArchiveMessageReplaySource(_ProcessPrivate):
    """Fresh conversation revision plus exact selected windows."""

    __slots__ = ("resolved_source", "windows")

    resolved_source: ResolvedSource
    windows: tuple[ArchiveMessageReplayWindow, ...]

    def __init__(
        self,
        resolved_source: ResolvedSource,
        windows: tuple[ArchiveMessageReplayWindow, ...],
        *,
        _factory: object = None,
    ) -> None:
        if (
            _factory is not _FACTORY
            or type(resolved_source) is not ResolvedSource
            or type(windows) is not tuple
            or not windows
            or any(type(item) is not ArchiveMessageReplayWindow for item in windows)
            or any(
                item.rows[0].conversation_id != resolved_source.source_ref.canonical_object_id
                for item in windows
            )
        ):
            raise ArchiveMessageStorageError("message replay source is invalid")
        object.__setattr__(self, "resolved_source", resolved_source)
        object.__setattr__(self, "windows", windows)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive message replay source is immutable")

    def __repr__(self) -> str:
        return f"ArchiveMessageReplaySource(window_count={len(self.windows)}, private=True)"


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveMessageHit(_ProcessPrivate):
    match_rank: int
    lexical_score: float
    message: ArchiveMessageRow
    context: tuple[ArchiveMessageContextRow, ...]
    ledger: ArchiveMessageLedgerEvidence
    _factory: InitVar[object] = None

    def __post_init__(self, _factory: object) -> None:
        if (
            _factory is not _FACTORY
            or not 1 <= self.match_rank <= _MAX_RESULTS
            or not math.isfinite(self.lexical_score)
            or type(self.message) is not ArchiveMessageRow
            or type(self.context) is not tuple
            or not self.context
            or any(type(item) is not ArchiveMessageContextRow for item in self.context)
            or sum(item.relative_position == 0 for item in self.context) != 1
            or type(self.ledger) is not ArchiveMessageLedgerEvidence
            or self.message.conversation_id != self.ledger.conversation_id
        ):
            raise ArchiveMessageStorageError("message hit evidence is invalid")

    def __repr__(self) -> str:
        return (
            f"ArchiveMessageHit(match_rank={self.match_rank}, "
            f"context_count={len(self.context)}, private=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveMessageSearchPage(_ProcessPrivate):
    """Private page; ``examined`` is eligible corpus size and ``total`` matched size."""

    principal_id: str
    query: str
    scope: ArchiveMessageScope
    conversation_id: str | None
    boundary_user_message_id: str | None
    hits: tuple[ArchiveMessageHit, ...]
    ledgers: tuple[ArchiveMessageLedgerEvidence, ...]
    roles: tuple[MessageRole, ...]
    lifecycle_states: tuple[LifecycleState, ...]
    since: str | None
    until: str | None
    limit: int
    context_before: int
    context_after: int
    lexical_index_build: str
    total: int
    examined: int
    has_more: bool
    boundary_identity_sha256: str | None
    _seal: bytes = field(default=b"", init=False, repr=False, compare=False)
    _factory: InitVar[object] = None

    def __post_init__(self, _factory: object) -> None:
        if (
            _factory is not _FACTORY
            or self.principal_id != _private_scope(self.principal_id, label="stored principal")
            or self.query != _query(self.query)
            or type(self.scope) is not ArchiveMessageScope
            or _CONVERSATION_ID.fullmatch(self.conversation_id or "") is None
            or _MESSAGE_ID.fullmatch(self.boundary_user_message_id or "") is None
            or self.boundary_identity_sha256 is None
            or type(self.roles) is not tuple
            or self.roles != _roles(self.roles)
            or type(self.lifecycle_states) is not tuple
            or self.lifecycle_states != _lifecycle_states(self.lifecycle_states)
            or self.context_before
            != _bounded_integer(
                self.context_before,
                label="message context radius",
                low=0,
                high=_MAX_NEIGHBORS,
            )
            or self.context_after
            != _bounded_integer(
                self.context_after,
                label="message context radius",
                low=0,
                high=_MAX_NEIGHBORS,
            )
            or type(self.hits) is not tuple
            or any(type(item) is not ArchiveMessageHit for item in self.hits)
            or tuple(item.match_rank for item in self.hits) != tuple(range(1, len(self.hits) + 1))
            or type(self.ledgers) is not tuple
            or any(type(item) is not ArchiveMessageLedgerEvidence for item in self.ledgers)
            or tuple(item.conversation_id for item in self.ledgers)
            != tuple(sorted({item.conversation_id for item in self.ledgers}))
            or any(item.ledger not in self.ledgers for item in self.hits)
            or {item.conversation_id for item in self.ledgers}
            != {item.message.conversation_id for item in self.hits}
            or any(item.boundary_identity_sha256 != self.boundary_identity_sha256 for item in self.ledgers)
            or any(
                item.message.principal_id != self.principal_id
                or item.message.role not in self.roles
                or (
                    item.message.conversation_archived
                    and LifecycleState.ARCHIVED not in self.lifecycle_states
                )
                or (
                    not item.message.conversation_archived
                    and LifecycleState.ACTIVE not in self.lifecycle_states
                )
                or (
                    self.scope is ArchiveMessageScope.CURRENT
                    and item.message.conversation_id != self.conversation_id
                )
                or tuple(context.relative_position for context in item.context)
                != tuple(sorted({context.relative_position for context in item.context}))
                or any(
                    context.row.principal_id != self.principal_id
                    or context.row.conversation_id != item.message.conversation_id
                    or context.row.conversation_archived is not item.message.conversation_archived
                    or context.relative_position < -self.context_before
                    or context.relative_position > self.context_after
                    for context in item.context
                )
                or next(
                    (context.row for context in item.context if context.relative_position == 0),
                    None,
                )
                != item.message
                for item in self.hits
            )
            or self.since != _optional_utc(self.since, label="message time boundary")
            or self.until != _optional_utc(self.until, label="message time boundary")
            or (self.since is not None and self.until is not None and self.since >= self.until)
            or self.limit
            != _bounded_integer(
                self.limit,
                label="message result limit",
                low=1,
                high=_MAX_RESULTS,
            )
            or self.lexical_index_build != str(SCHEMA_VERSION)
            or isinstance(self.total, bool)
            or isinstance(self.examined, bool)
            or self.total < len(self.hits)
            or self.examined < self.total
            or len(self.hits) > self.limit
            or type(self.has_more) is not bool
            or self.has_more is not (self.total > len(self.hits))
            or (
                self.boundary_identity_sha256 is not None
                and _SHA256.fullmatch(self.boundary_identity_sha256) is None
            )
        ):
            raise ArchiveMessageStorageError("message page evidence is invalid")
        object.__setattr__(self, "_seal", _page_seal(self))

    @property
    def returned(self) -> int:
        return len(self.hits)

    def __repr__(self) -> str:
        return (
            "ArchiveMessageSearchPage("
            f"scope={self.scope.value!r}, returned={self.returned}, total={self.total}, "
            f"examined={self.examined}, has_more={self.has_more}, private=True)"
        )

    def is_valid(self) -> bool:
        """Return whether this exact process-private selection remains intact."""

        try:
            return bool(
                type(self) is ArchiveMessageSearchPage
                and type(self._seal) is bytes
                and len(self._seal) == 32
                and hmac.compare_digest(self._seal, _page_seal(self))
            )
        except Exception:
            return False

    @property
    def selection_handle(self) -> str:
        """Opaque identity of every sealed storage control and selected byte."""

        if not self.is_valid():
            raise ArchiveMessageStorageError("message page evidence is invalid")
        return self._seal.hex()


def _page_seal(page: ArchiveMessageSearchPage) -> bytes:
    def row(item: ArchiveMessageRow) -> dict[str, object]:
        return {
            "archived": item.conversation_archived,
            "content": item.content,
            "conversation_id": item.conversation_id,
            "created_at": item.created_at,
            "message_id": item.message_id,
            "principal_id": item.principal_id,
            "role": item.role.value,
        }

    def ledger(item: ArchiveMessageLedgerEvidence) -> dict[str, object]:
        return {
            "archived": item.conversation_archived,
            "boundary_identity_sha256": item.boundary_identity_sha256,
            "conversation_id": item.conversation_id,
            "conversation_title": item.conversation_title,
            "first_message_id": item.first_message_id,
            "last_message_id": item.last_message_id,
            "row_count": item.row_count,
            "row_ledger_sha256": item.row_ledger_sha256,
        }

    material = {
        "boundary_identity_sha256": page.boundary_identity_sha256,
        "boundary_user_message_id": page.boundary_user_message_id,
        "context_after": page.context_after,
        "context_before": page.context_before,
        "conversation_id": page.conversation_id,
        "examined": page.examined,
        "has_more": page.has_more,
        "hits": [
            {
                "context": [
                    {"relative_position": context.relative_position, "row": row(context.row)}
                    for context in hit.context
                ],
                "ledger": ledger(hit.ledger),
                "lexical_score": hit.lexical_score,
                "match_rank": hit.match_rank,
                "message": row(hit.message),
            }
            for hit in page.hits
        ],
        "ledgers": [ledger(item) for item in page.ledgers],
        "lifecycle_states": [item.value for item in page.lifecycle_states],
        "lexical_index_build": page.lexical_index_build,
        "limit": page.limit,
        "principal_id": page.principal_id,
        "query": page.query,
        "roles": [item.value for item in page.roles],
        "scope": page.scope.value,
        "since": page.since,
        "total": page.total,
        "until": page.until,
    }
    try:
        encoded = json.dumps(
            material,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise ArchiveMessageStorageError("message page evidence is invalid") from None
    return hmac.new(
        _PROCESS_KEY,
        b"friday/archive-message-storage-page/v1\0" + encoded,
        hashlib.sha256,
    ).digest()


def _records(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = tuple(item[0] for item in (cursor.description or ()))
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _require_active_principal_and_current_lexical_index(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
) -> str:
    authority = conn.execute(
        "SELECT 1 FROM users WHERE id=? AND status='active'",
        (principal_id,),
    ).fetchone()
    if authority is None:
        raise ArchiveMessageStorageError("message principal authority is unavailable")
    marker = conn.execute(
        "SELECT value FROM schema_meta WHERE key='fts_build'",
    ).fetchone()
    expected = str(SCHEMA_VERSION)
    if marker is None or type(marker[0]) is not str or marker[0] != expected:
        raise ArchiveMessageStorageError("message lexical index is unavailable")
    missing = conn.execute(
        """SELECT 1
             FROM messages m
             JOIN conversations c
               ON c.id=m.conversation_id AND c.user_id=m.user_id
             LEFT JOIN messages_fts_docsize fts_row ON fts_row.id=m.rowid
            WHERE m.user_id=? AND fts_row.id IS NULL
            LIMIT 1""",
        (principal_id,),
    ).fetchone()
    if missing is not None:
        raise ArchiveMessageStorageError("message lexical index is unavailable")
    try:
        # ``rank=1`` compares an external-content index with its source table,
        # rather than checking only the internal FTS b-tree structure.
        integrity_cursor = conn.execute(
            "INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)"
        )
        # Python 3.14 may keep the write-shaped FTS statement active until the
        # temporary cursor is collected.  A following archive corpus then
        # cannot register its deterministic SQLite fold function, making two
        # identical federations disagree.  Finalize this read-attestation
        # statement at the exact boundary instead of relying on GC timing.
        integrity_cursor.close()
    except sqlite3.DatabaseError:
        raise ArchiveMessageStorageError("message lexical index is unavailable") from None
    return expected


def _validate_authorized_scope_timestamps(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    scope: ArchiveMessageScope,
    conversation_id: str | None,
    boundary_user_message_id: str | None,
    include_active: int,
    include_archived: int,
) -> None:
    """Decode every authorized timestamp before any temporal row can disappear."""

    cursor = conn.execute(
        """WITH owned_conversations AS MATERIALIZED (
                   SELECT c.id, c.user_id, c.is_archived
                     FROM conversations c
                    WHERE c.user_id=? AND (?='all' OR c.id=?)
               ),
               eligible_conversations AS MATERIALIZED (
                   SELECT *
                     FROM owned_conversations
                    WHERE (is_archived=0 AND ?=1) OR (is_archived=1 AND ?=1)
               ),
               scope_gate AS MATERIALIZED (
                   SELECT b.conversation_id AS boundary_conversation_id,
                          b.rowid AS boundary_rowid
                     FROM messages b
                     JOIN owned_conversations c
                       ON c.id=b.conversation_id AND c.user_id=b.user_id
                    WHERE b.id=? AND b.user_id=? AND b.role='user'
                      AND b.conversation_id=?
               )
               SELECT m.created_at
                 FROM messages m
                 JOIN eligible_conversations c
                   ON c.id=m.conversation_id AND c.user_id=m.user_id
                 CROSS JOIN scope_gate gate
                WHERE m.user_id=?
                  AND m.role IN ('user', 'assistant')
                  AND m.rowid<gate.boundary_rowid
                  AND (?='all' OR m.conversation_id=gate.boundary_conversation_id)""",
        (
            principal_id,
            scope.value,
            conversation_id,
            include_active,
            include_archived,
            boundary_user_message_id,
            principal_id,
            conversation_id,
            principal_id,
            scope.value,
        ),
    )
    while True:
        batch = cursor.fetchmany(256)
        if not batch:
            return
        for row in batch:
            _utc(row[0], label="stored message timestamp")


def _row(values: dict[str, Any], *, prefix: str) -> ArchiveMessageRow:
    role_value = values[f"{prefix}_role"]
    try:
        role = MessageRole(role_value)
    except (TypeError, ValueError):
        raise ArchiveMessageStorageError("stored message role is invalid") from None
    if role not in {MessageRole.USER, MessageRole.ASSISTANT}:
        raise ArchiveMessageStorageError("stored message role is invalid")
    archived = values[f"{prefix}_conversation_archived"]
    if archived not in {0, 1}:
        raise ArchiveMessageStorageError("stored conversation lifecycle is invalid")
    return ArchiveMessageRow(
        message_id=_message_id(values[f"{prefix}_id"], label="stored message identity"),
        conversation_id=_conversation_id(
            values[f"{prefix}_conversation_id"],
            label="stored conversation identity",
        ),
        principal_id=_private_scope(values[f"{prefix}_principal_id"], label="stored principal"),
        role=role,
        content=_private_content(values[f"{prefix}_content"]),
        created_at=_utc(values[f"{prefix}_created_at"], label="stored message timestamp"),
        conversation_archived=bool(archived),
        _factory=_FACTORY,
    )


class _LedgerAccumulator:
    __slots__ = ("archived", "count", "first", "hasher", "last", "title")

    def __init__(self) -> None:
        self.hasher = hashlib.sha256()
        self.hasher.update(b'{"row_identity_sha256s":[')
        self.count = 0
        self.first = ""
        self.last = ""
        self.archived: bool | None = None
        self.title: str | None = None

    def add(self, row: ArchiveMessageRow, *, conversation_title: str) -> None:
        identity = _row_identity(
            message_id=row.message_id,
            conversation_id=row.conversation_id,
            principal_id=row.principal_id,
            role=row.role,
            content=row.content,
            created_at=row.created_at,
        )
        if self.count:
            self.hasher.update(b",")
        self.hasher.update(json.dumps(identity).encode("ascii"))
        self.count += 1
        self.first = self.first or row.message_id
        self.last = row.message_id
        if self.archived is None:
            self.archived = row.conversation_archived
        elif self.archived is not row.conversation_archived:
            raise ArchiveMessageStorageError("stored conversation lifecycle changed within snapshot")
        title = _private_title(conversation_title)
        if self.title is None:
            self.title = title
        elif self.title != title:
            raise ArchiveMessageStorageError("stored conversation title changed within snapshot")

    def finish(self) -> str:
        self.hasher.update(b'],"schema":"friday.private-message-window-row-ledger.v1"}')
        return self.hasher.hexdigest()


def _ledger_evidence(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    conversation_ids: tuple[str, ...],
    scope: ArchiveMessageScope,
    boundary_conversation_id: str | None,
    boundary_rowid: int | None,
    boundary_identity_sha256: str | None,
) -> tuple[ArchiveMessageLedgerEvidence, ...]:
    # Search-time since/until bounds select candidate windows, not source
    # identity.  Attest the complete accepted pre-boundary conversation so the
    # same SourceRevision can be recomputed later without persisting the query.
    placeholders = ",".join("?" for _item in conversation_ids)
    statement = f"""WITH selected_owned AS MATERIALIZED (
                         SELECT c.id, c.user_id, c.is_archived, c.title
                           FROM conversations c
                          WHERE c.user_id=? AND c.id IN ({placeholders})
                     )
                     SELECT m.id AS ledger_id,
                            m.conversation_id AS ledger_conversation_id,
                            m.user_id AS ledger_principal_id,
                            m.role AS ledger_role,
                            m.content AS ledger_content,
                            m.created_at AS ledger_created_at,
                            c.title AS ledger_conversation_title,
                            c.is_archived AS ledger_conversation_archived
                       FROM messages m
                       JOIN selected_owned c
                         ON c.id=m.conversation_id AND c.user_id=m.user_id
                      WHERE m.user_id=?
                        AND m.role IN ('user', 'assistant')
                        AND m.rowid<?
                        AND (?='all' OR m.conversation_id=?)
                      ORDER BY m.conversation_id ASC,
                               julianday(m.created_at) ASC, m.rowid ASC"""  # nosec B608
    cursor = conn.execute(
        statement,
        (
            principal_id,
            *conversation_ids,
            principal_id,
            boundary_rowid,
            scope.value,
            boundary_conversation_id,
        ),
    )
    columns = tuple(item[0] for item in (cursor.description or ()))
    accumulators = {conversation_id: _LedgerAccumulator() for conversation_id in conversation_ids}
    while True:
        batch = cursor.fetchmany(256)
        if not batch:
            break
        for raw in batch:
            values = dict(zip(columns, raw, strict=True))
            row = _row(values, prefix="ledger")
            accumulator = accumulators.get(row.conversation_id)
            if accumulator is None:
                raise ArchiveMessageStorageError("message ledger escaped its authorized scope")
            accumulator.add(
                row,
                conversation_title=_private_title(values["ledger_conversation_title"]),
            )

    evidence: list[ArchiveMessageLedgerEvidence] = []
    for conversation_id in conversation_ids:
        accumulator = accumulators[conversation_id]
        if accumulator.count == 0 or accumulator.archived is None or accumulator.title is None:
            raise ArchiveMessageStorageError("message ledger could not be reselected")
        evidence.append(
            ArchiveMessageLedgerEvidence(
                conversation_id=conversation_id,
                row_ledger_sha256=accumulator.finish(),
                boundary_identity_sha256=boundary_identity_sha256,
                row_count=accumulator.count,
                first_message_id=accumulator.first,
                last_message_id=accumulator.last,
                conversation_title=accumulator.title,
                conversation_archived=accumulator.archived,
                _factory=_FACTORY,
            )
        )
    return tuple(evidence)


def _select_authorized_archive_message_replay_source_in_transaction(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    origin_boundary_user_message_id: str,
    source_ref: SourceRef,
    locators: tuple[MessageWindowLocator, ...],
) -> ArchiveMessageReplaySource | None:
    """Reselect one conversation ledger and exact windows without FTS/search.

    The ledger is recomputed over all user/assistant rows whose SQLite rowid is
    strictly before the immutable accepted-turn boundary.  Rows inserted after
    that boundary therefore cannot change the replayed source revision.
    """

    if type(conn) is not sqlite3.Connection or not conn.in_transaction:
        raise RuntimeError("archive message replay requires a caller-owned transaction")
    principal = _private_scope(principal_id, label="principal identity")
    boundary_id = _message_id(
        origin_boundary_user_message_id,
        label="archive replay boundary",
    )
    if (
        type(source_ref) is not SourceRef
        or source_ref.source_kind is not SourceKind.CONVERSATION
        or source_ref.authority_scope is not AuthorityScope.PRINCIPAL
        or source_ref.tenant_id is not None
        or source_ref.principal_id != principal
        or source_ref.canonical_object_kind is not CanonicalObjectKind.CONVERSATION
    ):
        raise ArchiveMessageStorageError("message replay source is invalid")
    if (
        type(locators) is not tuple
        or not 1 <= len(locators) <= 8
        or any(type(item) is not MessageWindowLocator for item in locators)
        or len(locators) != len(set(locators))
    ):
        raise ArchiveMessageStorageError("message replay locators are invalid")
    if any(item.context_before > _MAX_NEIGHBORS or item.context_after > _MAX_NEIGHBORS for item in locators):
        return None

    try:
        cursor = conn.execute(
            """WITH replay_boundary AS MATERIALIZED (
                       SELECT b.rowid AS boundary_rowid
                         FROM messages b
                         JOIN conversations boundary_conversation
                           ON boundary_conversation.id=b.conversation_id
                          AND boundary_conversation.user_id=b.user_id
                         JOIN users principal_authority
                           ON principal_authority.id=b.user_id
                          AND principal_authority.status='active'
                        WHERE b.id=? AND b.user_id=? AND b.role='user'
                   ),
                   selected_owned AS MATERIALIZED (
                       SELECT c.id, c.user_id, c.title, c.is_archived
                         FROM conversations c
                         JOIN users principal_authority
                           ON principal_authority.id=c.user_id
                          AND principal_authority.status='active'
                        WHERE c.id=? AND c.user_id=?
                   )
                   SELECT c.id AS selected_conversation_id,
                          c.user_id AS selected_conversation_principal_id,
                          c.title AS selected_conversation_title,
                          c.is_archived AS selected_conversation_archived,
                          m.rowid AS replay_rowid,
                          m.id AS replay_id,
                          m.conversation_id AS replay_conversation_id,
                          m.user_id AS replay_principal_id,
                          m.role AS replay_role,
                          m.content AS replay_content,
                          m.created_at AS replay_created_at,
                          c.is_archived AS replay_conversation_archived
                     FROM replay_boundary boundary
                     CROSS JOIN selected_owned c
                     LEFT JOIN messages m
                       ON m.conversation_id=c.id AND m.user_id=c.user_id
                      AND m.role IN ('user', 'assistant')
                      AND m.rowid<boundary.boundary_rowid
                    ORDER BY julianday(m.created_at) ASC, m.rowid ASC""",
            (
                boundary_id,
                principal,
                source_ref.canonical_object_id,
                principal,
            ),
        )
        columns = tuple(str(item[0]) for item in (cursor.description or ()))
        first_raw = cursor.fetchone()
    except sqlite3.Error:
        raise ArchiveMessageStorageError("message replay selection is unavailable") from None
    if first_raw is None:
        return None

    first = dict(zip(columns, tuple(first_raw), strict=True))
    if first["replay_rowid"] is None:
        return None
    archived_value = first["selected_conversation_archived"]
    if archived_value not in {0, 1}:
        raise ArchiveMessageStorageError("stored conversation lifecycle is invalid")
    title = _private_title(first["selected_conversation_title"])
    accumulator = _LedgerAccumulator()
    captured: list[list[ArchiveMessageRow]] = [[] for _item in locators]
    started = [False for _item in locators]
    completed = [False for _item in locators]

    def accept(values: dict[str, Any]) -> bool:
        if (
            values["selected_conversation_id"] != source_ref.canonical_object_id
            or values["selected_conversation_principal_id"] != principal
            or values["selected_conversation_title"] != first["selected_conversation_title"]
            or values["selected_conversation_archived"] != archived_value
        ):
            raise ArchiveMessageStorageError("stored conversation changed within replay snapshot")
        row = _row(values, prefix="replay")
        accumulator.add(row, conversation_title=title)
        for index, locator in enumerate(locators):
            if completed[index]:
                continue
            if not started[index] and row.message_id == locator.first_message_id:
                started[index] = True
            if started[index]:
                captured[index].append(row)
                if len(captured[index]) > locator.context_before + locator.context_after + 1:
                    return False
                if row.message_id == locator.last_message_id:
                    completed[index] = True
        return True

    if not accept(first):
        return None
    while True:
        try:
            batch = cursor.fetchmany(256)
        except sqlite3.Error:
            raise ArchiveMessageStorageError("message replay selection is unavailable") from None
        if not batch:
            break
        for raw in batch:
            values = dict(zip(columns, tuple(raw), strict=True))
            if not accept(values):
                return None
    if accumulator.count <= 0 or accumulator.archived is None:
        raise ArchiveMessageStorageError("message replay ledger is unavailable")

    representation = SourceRepresentation(
        RepresentationKind.CONVERSATION,
        source_ref.canonical_object_id,
    )
    revision = SourceRevision(
        representation,
        RevisionKind.MESSAGE_LEDGER_SHA256,
        accumulator.finish(),
    )
    resolved_source = ResolvedSource.create(
        source_ref=SourceRef(
            SourceKind.CONVERSATION,
            AuthorityScope.PRINCIPAL,
            None,
            principal,
            CanonicalObjectKind.CONVERSATION,
            source_ref.canonical_object_id,
        ),
        representations=(representation,),
        lifecycle=(
            LifecycleRef(
                representation,
                LifecycleState.ARCHIVED if bool(archived_value) else LifecycleState.ACTIVE,
            ),
        ),
        revisions=(revision,),
        revalidation_targets=(RevalidationTarget(representation, AuthorityScope.PRINCIPAL),),
    )

    windows: list[ArchiveMessageReplayWindow] = []
    for index, locator in enumerate(locators):
        selected = tuple(captured[index])
        if (
            not started[index]
            or not completed[index]
            or len(selected) != locator.context_before + locator.context_after + 1
            or selected[0].message_id != locator.first_message_id
            or selected[-1].message_id != locator.last_message_id
        ):
            return None
        matched = selected[locator.context_before]
        if locator.matched_role is not None and matched.role is not locator.matched_role:
            return None
        first_instant = datetime.fromisoformat(selected[0].created_at)
        last_instant = datetime.fromisoformat(selected[-1].created_at)
        try:
            exclusive_end = last_instant + timedelta(microseconds=1)
            if exclusive_end <= first_instant:
                exclusive_end = first_instant + timedelta(microseconds=1)
        except OverflowError:
            raise ArchiveMessageStorageError("message replay window is unavailable") from None
        if (
            locator.start_at != first_instant.astimezone(UTC).isoformat()
            or locator.end_at != exclusive_end.astimezone(UTC).isoformat()
        ):
            return None
        windows.append(ArchiveMessageReplayWindow(tuple(selected), _factory=_FACTORY))
    return ArchiveMessageReplaySource(
        resolved_source,
        tuple(windows),
        _factory=_FACTORY,
    )


def select_authorized_archive_message_replay_source_in_transaction(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    origin_boundary_user_message_id: str,
    source_ref: SourceRef,
    locators: tuple[MessageWindowLocator, ...],
) -> ArchiveMessageReplaySource | None:
    """Public body-free wrapper for one exact message replay SELECT."""

    try:
        return _select_authorized_archive_message_replay_source_in_transaction(
            conn,
            principal_id=principal_id,
            origin_boundary_user_message_id=origin_boundary_user_message_id,
            source_ref=source_ref,
            locators=locators,
        )
    except ArchiveMessageStorageError:
        raise
    except Exception:
        raise ArchiveMessageStorageError("message replay selection is unavailable") from None


def _select_authorized_archive_message_page_in_transaction(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    query: str,
    scope: ArchiveMessageScope = ArchiveMessageScope.ALL,
    conversation_id: str | None = None,
    boundary_user_message_id: str | None = None,
    roles: Iterable[MessageRole] = (MessageRole.ASSISTANT, MessageRole.USER),
    lifecycle_states: Iterable[LifecycleState] = (
        LifecycleState.ACTIVE,
        LifecycleState.ARCHIVED,
    ),
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
    context_before: int = 0,
    context_after: int = 0,
) -> ArchiveMessageSearchPage | None:
    """Select one authorized lexical page and exact context in the caller's transaction.

    ``None`` is reserved for a missing/foreign accepted-turn boundary.
    An authorized search with zero matches returns an empty typed page.
    """

    principal = _private_scope(principal_id, label="principal identity")
    lexical_index_build = _require_active_principal_and_current_lexical_index(
        conn,
        principal_id=principal,
    )
    if type(scope) is not ArchiveMessageScope:
        raise ArchiveMessageStorageError("message scope is invalid")
    normalized_query = _query(query)
    selected_roles = _roles(roles)
    selected_lifecycles = _lifecycle_states(lifecycle_states)
    start = _optional_utc(since, label="message time boundary")
    end = _optional_utc(until, label="message time boundary")
    if start is not None and end is not None and start >= end:
        raise ArchiveMessageStorageError("message time window is invalid")
    page_size = _bounded_integer(limit, label="message result limit", low=1, high=_MAX_RESULTS)
    before = _bounded_integer(
        context_before,
        label="message context radius",
        low=0,
        high=_MAX_NEIGHBORS,
    )
    after = _bounded_integer(
        context_after,
        label="message context radius",
        low=0,
        high=_MAX_NEIGHBORS,
    )
    current_conversation = _conversation_id(
        conversation_id,
        label="current conversation identity",
    )
    boundary_id = _message_id(boundary_user_message_id, label="current message boundary")
    include_user = int(MessageRole.USER in selected_roles)
    include_assistant = int(MessageRole.ASSISTANT in selected_roles)
    include_active = int(LifecycleState.ACTIVE in selected_lifecycles)
    include_archived = int(LifecycleState.ARCHIVED in selected_lifecycles)
    _validate_authorized_scope_timestamps(
        conn,
        principal_id=principal,
        scope=scope,
        conversation_id=current_conversation,
        boundary_user_message_id=boundary_id,
        include_active=include_active,
        include_archived=include_archived,
    )

    match_queries = _match_queries(normalized_query)
    local_score = " + ".join(
        f"""CASE WHEN EXISTS (
                         SELECT 1
                           FROM messages_fts AS authorized_fts_{index}
                          WHERE authorized_fts_{index}.rowid=eligible.message_rowid
                            AND authorized_fts_{index}.content MATCH ?
                     ) THEN 1 ELSE 0 END"""
        for index, _query_value in enumerate(match_queries)
    )

    cursor = conn.execute(
        f"""WITH owned_conversations AS MATERIALIZED (
                   SELECT c.id, c.user_id, c.is_archived
                     FROM conversations c
                    WHERE c.user_id=?
                      AND (?='all' OR c.id=?)
               ),
               eligible_conversations AS MATERIALIZED (
                   SELECT *
                     FROM owned_conversations
                    WHERE (is_archived=0 AND ?=1) OR (is_archived=1 AND ?=1)
               ),
               scope_gate AS MATERIALIZED (
                   SELECT b.id AS boundary_id,
                          b.conversation_id AS boundary_conversation_id,
                          b.user_id AS boundary_principal_id,
                          b.content AS boundary_content,
                          b.created_at AS boundary_created_at,
                          b.rowid AS boundary_rowid
                     FROM messages b
                     JOIN owned_conversations c
                       ON c.id=b.conversation_id AND c.user_id=b.user_id
                    WHERE b.id=? AND b.user_id=? AND b.role='user'
                      AND b.conversation_id=?
               ),
               authorized_scope_rows AS MATERIALIZED (
                   SELECT m.id, m.conversation_id, m.user_id, m.role, m.content,
                          m.created_at, m.rowid AS message_rowid,
                          c.is_archived AS conversation_archived
                     FROM messages m
                     JOIN eligible_conversations c
                       ON c.id=m.conversation_id AND c.user_id=m.user_id
                     CROSS JOIN scope_gate gate
                    WHERE m.user_id=?
                      AND m.role IN ('user', 'assistant')
                      AND m.rowid<gate.boundary_rowid
                      AND (?='all' OR m.conversation_id=gate.boundary_conversation_id)
               ),
               authorized_context AS MATERIALIZED (
                   SELECT scoped.*,
                          ROW_NUMBER() OVER (
                              PARTITION BY scoped.conversation_id
                              ORDER BY julianday(scoped.created_at) ASC,
                                       scoped.message_rowid ASC
                          ) AS conversation_sequence
                     FROM authorized_scope_rows scoped
                    WHERE (? IS NULL OR julianday(scoped.created_at) >= julianday(?))
                      AND (? IS NULL OR julianday(scoped.created_at) < julianday(?))
               ),
               eligible AS MATERIALIZED (
                   SELECT * FROM authorized_context
                    WHERE (role='user' AND ?=1) OR (role='assistant' AND ?=1)
               ),
               scored AS MATERIALIZED (
                   SELECT eligible.*, -CAST(({local_score}) AS REAL) AS lexical_score
                     FROM eligible
               ),
               lexical AS MATERIALIZED (
                   SELECT * FROM scored WHERE lexical_score<0
               ),
               ranked AS MATERIALIZED (
                   SELECT lexical.*,
                          ROW_NUMBER() OVER (
                              ORDER BY lexical_score ASC,
                                       julianday(created_at) DESC, message_rowid DESC
                          ) AS match_rank
                     FROM lexical
               ),
               page AS MATERIALIZED (
                   SELECT * FROM ranked WHERE match_rank<=?
               ),
               statistics AS MATERIALIZED (
                   SELECT (SELECT COUNT(*) FROM eligible) AS examined,
                          (SELECT COUNT(*) FROM ranked) AS total,
                          (SELECT COUNT(*)
                             FROM owned_conversations
                            WHERE typeof(is_archived)!='integer'
                               OR is_archived NOT IN (0, 1)) AS invalid_lifecycle,
                          (SELECT COUNT(*)
                             FROM authorized_scope_rows
                            WHERE julianday(created_at) IS NULL) AS invalid_timestamp
               ),
               expanded AS MATERIALIZED (
                   SELECT page.match_rank,
                          page.lexical_score,
                          page.message_rowid AS hit_rowid,
                          page.id AS hit_id,
                          context.id AS window_id,
                          context.conversation_id AS window_conversation_id,
                          context.user_id AS window_principal_id,
                          context.role AS window_role,
                          context.content AS window_content,
                          context.created_at AS window_created_at,
                          context.conversation_archived AS window_conversation_archived,
                          context.message_rowid AS window_rowid,
                          context.conversation_sequence-page.conversation_sequence
                              AS relative_position
                     FROM page
                     JOIN authorized_context context
                       ON context.conversation_id=page.conversation_id
                      AND context.conversation_sequence BETWEEN
                          page.conversation_sequence-? AND page.conversation_sequence+?
               )
               SELECT gate.boundary_id,
                      gate.boundary_conversation_id,
                      gate.boundary_principal_id,
                      gate.boundary_content,
                      gate.boundary_created_at,
                      gate.boundary_rowid,
                      statistics.examined,
                      statistics.total,
                      statistics.invalid_lifecycle,
                      statistics.invalid_timestamp,
                      expanded.*
                 FROM scope_gate gate
                 CROSS JOIN statistics
                 LEFT JOIN expanded ON 1=1
                ORDER BY expanded.match_rank ASC,
                         julianday(expanded.window_created_at) ASC,
                         expanded.window_rowid ASC""",  # nosec B608
        (
            principal,
            scope.value,
            current_conversation,
            include_active,
            include_archived,
            boundary_id,
            principal,
            current_conversation,
            principal,
            scope.value,
            start,
            start,
            end,
            end,
            include_user,
            include_assistant,
            *match_queries,
            page_size,
            before,
            after,
        ),
    )
    records = _records(cursor)
    if not records:
        return None
    first = records[0]
    if int(first["invalid_lifecycle"]):
        raise ArchiveMessageStorageError("stored conversation lifecycle is invalid")
    if int(first["invalid_timestamp"]):
        raise ArchiveMessageStorageError("stored message timestamp is invalid")
    examined = int(first["examined"])
    total = int(first["total"])
    boundary_digest = _boundary_identity(first)
    boundary_rowid = int(first["boundary_rowid"])
    boundary_conversation = _conversation_id(
        first["boundary_conversation_id"],
        label="stored boundary conversation",
    )

    grouped: dict[int, list[dict[str, Any]]] = {}
    for values in records:
        if values["match_rank"] is not None:
            grouped.setdefault(int(values["match_rank"]), []).append(values)
    conversation_ids = tuple(
        sorted(
            {
                _conversation_id(rows[0]["window_conversation_id"], label="stored conversation identity")
                for rows in grouped.values()
            }
        )
    )
    ledgers = (
        _ledger_evidence(
            conn,
            principal_id=principal,
            conversation_ids=conversation_ids,
            scope=scope,
            boundary_conversation_id=boundary_conversation,
            boundary_rowid=boundary_rowid,
            boundary_identity_sha256=boundary_digest,
        )
        if conversation_ids
        else ()
    )
    ledger_by_conversation = {item.conversation_id: item for item in ledgers}
    hits: list[ArchiveMessageHit] = []
    for rank in sorted(grouped):
        rows = grouped[rank]
        contexts = tuple(
            ArchiveMessageContextRow(
                row=_row(values, prefix="window"),
                relative_position=int(values["relative_position"]),
                _factory=_FACTORY,
            )
            for values in rows
        )
        hit_context = next((item for item in contexts if item.relative_position == 0), None)
        if hit_context is None:
            raise ArchiveMessageStorageError("message hit lost its exact context")
        ledger = ledger_by_conversation.get(hit_context.row.conversation_id)
        if ledger is None:
            raise ArchiveMessageStorageError("message hit lost its exact ledger")
        score = float(rows[0]["lexical_score"])
        hits.append(
            ArchiveMessageHit(
                match_rank=rank,
                lexical_score=score,
                message=hit_context.row,
                context=contexts,
                ledger=ledger,
                _factory=_FACTORY,
            )
        )
    return ArchiveMessageSearchPage(
        principal_id=principal,
        query=normalized_query,
        scope=scope,
        conversation_id=current_conversation,
        boundary_user_message_id=boundary_id,
        hits=tuple(hits),
        ledgers=ledgers,
        roles=selected_roles,
        lifecycle_states=selected_lifecycles,
        since=start,
        until=end,
        limit=page_size,
        context_before=before,
        context_after=after,
        lexical_index_build=lexical_index_build,
        total=total,
        examined=examined,
        has_more=total > len(hits),
        boundary_identity_sha256=boundary_digest,
        _factory=_FACTORY,
    )


def select_authorized_archive_message_page_in_transaction(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    query: str,
    scope: ArchiveMessageScope = ArchiveMessageScope.ALL,
    conversation_id: str | None = None,
    boundary_user_message_id: str | None = None,
    roles: Iterable[MessageRole] = (MessageRole.ASSISTANT, MessageRole.USER),
    lifecycle_states: Iterable[LifecycleState] = (
        LifecycleState.ACTIVE,
        LifecycleState.ARCHIVED,
    ),
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
    context_before: int = 0,
    context_after: int = 0,
) -> ArchiveMessageSearchPage | None:
    """Select one authorized lexical page in the caller's transaction.

    ``None`` is reserved for a missing/foreign accepted-turn boundary.  Both
    scopes require that owned current user row: ``current`` admits only older
    rows from its conversation, while ``all`` admits every owned conversation
    only up to the same global SQLite rowid snapshot.
    An authorized search with zero matches returns an empty typed page.  The
    archive authority callback must rerun this exact selector, with the same
    normalized controls, inside its publication ``BEGIN IMMEDIATE`` and compare
    the resulting private candidate and coverage evidence.  Selection requires
    an active principal, the durable FTS build marker and principal-local index
    row parity in the same snapshot.  A returned page remains non-transferable;
    ``has_more`` without a frozen tail is capped coverage, not a cursor.

    The caller must provide a writable SQLite transaction: FTS5 exposes its
    external-content ``integrity-check`` only as a write-shaped diagnostic
    command.  The command changes no durable rows and this selector never
    commits, but a ``PRAGMA query_only`` connection cannot attest the index and
    therefore fails unavailable.
    """

    if type(conn) is not sqlite3.Connection or not conn.in_transaction:
        raise RuntimeError("archive message selector requires a caller-owned transaction")
    try:
        return _select_authorized_archive_message_page_in_transaction(
            conn,
            principal_id=principal_id,
            query=query,
            scope=scope,
            conversation_id=conversation_id,
            boundary_user_message_id=boundary_user_message_id,
            roles=roles,
            lifecycle_states=lifecycle_states,
            since=since,
            until=until,
            limit=limit,
            context_before=context_before,
            context_after=context_after,
        )
    except ArchiveMessageStorageError:
        raise
    except Exception:
        raise ArchiveMessageStorageError("archive message selection is unavailable") from None


__all__ = [
    "ArchiveMessageContextRow",
    "ArchiveMessageHit",
    "ArchiveMessageLedgerEvidence",
    "ArchiveMessageReplaySource",
    "ArchiveMessageReplayWindow",
    "ArchiveMessageRow",
    "ArchiveMessageScope",
    "ArchiveMessageSearchPage",
    "ArchiveMessageStorageError",
    "select_authorized_archive_message_replay_source_in_transaction",
    "select_authorized_archive_message_page_in_transaction",
]
